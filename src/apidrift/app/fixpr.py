"""The fix pull request: push the files the sandboxed child rewrote and open (or refresh) one PR per repository.

The child computes the fix and returns bytes; this module never parses repository text. Everything it pushes
goes through :mod:`fixcheck` first. Writes go to one branch, ``apidrift/stripe-<version>``, with a write-scoped
token that is revoked as soon as the step is done; the default branch is never written. Nothing in here fails
the check: GitHub errors become ``fix_state="failed"`` and the next check tries again.

``fix_state``: NULL | needs_permissions | open | merged | declined | diverged | failed. ``pr_head_sha`` is the
last commit confirmed on the branch, ``pr_pending_sha`` one built but not yet confirmed there; a branch whose
tip is neither has commits apidrift did not make and is left alone (``diverged``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3

from . import db, fixcheck, prbody
from .config import Settings
from .github import FIX_PERMISSIONS, GitHubClient, GitHubError, RateLimited, scrub

CLOSABLE = ("open", "failed", "diverged", None)  # states in which a recorded PR is still ours to close
_BRANCH_RE = re.compile(r"[^A-Za-z0-9._-]+")
_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_reject = fixcheck.reject  # validation by its older name
logger = logging.getLogger("apidrift.fixpr")


def has_write(permissions: dict) -> bool:
    return permissions.get("contents") == "write" and permissions.get("pull_requests") == "write"


def needs_permissions_comment(url: str) -> str:
    return (
        "apidrift can open a pull request with this fix once the installation grants Contents: write and "
        f"Pull requests: write: {url}"
    )


def settings_url(installation: dict, installation_id: int) -> str:
    """Where the installation's permissions are accepted: GitHub's ``html_url``, else derived from the account."""
    url = installation.get("html_url")
    if isinstance(url, str) and url.startswith("https://github.com/") and " " not in url:
        return url
    account = installation.get("account") if isinstance(installation.get("account"), dict) else {}
    login = str(account.get("login") or "")
    if account.get("type") == "Organization" and _LOGIN_RE.fullmatch(login):
        return f"https://github.com/organizations/{login}/settings/installations/{installation_id}"
    return f"https://github.com/settings/installations/{installation_id}"


def clean_comment(sha: str) -> str:
    return f"No call sites are affected at {sha}; closing."


def gone_comment(sha: str) -> str:
    return f"The call sites this pull request renamed are gone at {sha}; closing."


DIVERGED_NOTE = "This branch has commits apidrift did not make, so the pull request is left open for you to close."


def fingerprint(fixes: list[dict], version: str, issue_number: int) -> str:
    """sha256 over the sorted (file, blob_sha, target, replacement) tuples, the Stripe version and the issue."""
    keys = {
        (f["file"], f["blob_sha"], s["target"], s["replacement"]) for f in fixes for s in f["sites"]
    }
    text = "\n".join(repr(k) for k in sorted(keys)) + f"\n{version}\n#{issue_number}"
    return hashlib.sha256(text.encode()).hexdigest()


def site_keys(fixes: list[dict]) -> list[str]:
    """Sorted ``file\\tpath\\ttarget`` keys: what counts as a new site after a person declined the last PR."""
    return sorted({"\t".join((f["file"], s["path"], s["target"])) for f in fixes for s in f["sites"]})


def branch_name(version: str) -> str:
    name = _BRANCH_RE.sub("-", version)
    while ".." in name:
        name = name.replace("..", "-")
    name = name.strip(".-")
    if name.endswith(".lock"):
        name = name[: -len(".lock")] + "-lock"
    return f"apidrift/stripe-{name or 'latest'}"


def commit_message(version: str, fixes: list[dict]) -> str:
    """Built from validated tokens only (``fixcheck`` admits ``[a-z0-9_]`` fields and version-like versions)."""
    renames = sorted({(s["target"], s["replacement"]) for f in fixes for s in f["sites"]})
    old, new = renames[0]
    return f"Stripe {version}: rename {old} to {new}"


# --- entry points (called by the checker after the issue is reconciled) -----------------


def run_fix(
    conn: sqlite3.Connection, gh: GitHubClient, settings: Settings, repo: sqlite3.Row, full_name: str,
    default_branch: str, base_sha: str, payload: dict, issue_number: int, version: str, issue_token: str,
) -> str | None:
    """Open or refresh the fix PR for a repo whose issue is open; returns a short note for the check outcome."""
    try:
        installation = gh.get_installation(int(repo["installation_id"]))
        if not has_write(installation.get("permissions") or {}):
            if repo["fix_state"] != "needs_permissions":  # one comment per transition into this state
                url = settings_url(installation, int(repo["installation_id"]))
                gh.comment(issue_token, full_name, issue_number, needs_permissions_comment(url))
                db.update_repo(conn, repo["id"], fix_state="needs_permissions")
            return "needs permissions"
        return _with_write_token(gh, repo, lambda token: _push(
            conn, gh, settings, token, repo, full_name, default_branch, base_sha, payload, issue_number, version
        ))
    except RateLimited:
        raise
    except Exception as exc:  # noqa: BLE001 - the fix step never fails the check
        return _failed(conn, repo, exc, issue_token)


def run_clean(conn: sqlite3.Connection, gh: GitHubClient, repo: sqlite3.Row, full_name: str, comment: str) -> str | None:
    """No fix is wanted any more (clean check, or the renamed sites are gone): close the recorded PR, if any."""
    if repo["pr_number"] is None or repo["fix_state"] not in CLOSABLE:
        return None
    try:
        if not has_write(gh.installation_permissions(int(repo["installation_id"]))):
            return None
        return _with_write_token(gh, repo, lambda token: _close(conn, gh, token, repo, full_name, comment))
    except RateLimited:
        raise
    except Exception as exc:  # noqa: BLE001
        return _failed(conn, repo, exc, "")


def _with_write_token(gh: GitHubClient, repo: sqlite3.Row, work) -> str:
    token, granted = gh.mint_token(int(repo["installation_id"]), int(repo["id"]), FIX_PERMISSIONS)
    try:
        if not has_write(granted):
            raise GitHubError("the write token was not granted contents and pull_requests write")
        return work(token)
    except RateLimited:
        raise
    except GitHubError as exc:
        raise GitHubError(scrub(str(exc), (token,)), exc.status) from None
    finally:
        try:
            gh.revoke_token(token)
        except Exception as exc:  # noqa: BLE001 - best effort; it expires within the hour
            logger.debug("could not revoke the write token: %s", scrub(str(exc), (token,)))


def _failed(conn: sqlite3.Connection, repo: sqlite3.Row, exc: Exception, token: str) -> str:
    db.update_repo(conn, repo["id"], fix_state="failed")
    logger.warning("repo %s: fix step failed: %s", repo["id"], scrub(str(exc) or type(exc).__name__, (token,))[:300])
    return "failed"


# --- the steps -----------------------------------------------------------------------------


def _settle_pull(conn: sqlite3.Connection, gh: GitHubClient, token: str, repo: sqlite3.Row, full_name: str) -> None:
    """Read what became of the recorded PR: merged or closed by a person changes ``fix_state``; gone clears it."""
    number = repo["pr_number"]
    if number is None:
        return
    try:
        pull = gh.get_pull(token, full_name, int(number))
    except GitHubError as exc:
        if exc.status in (404, 410):
            db.update_repo(conn, repo["id"], pr_number=None, fix_state=None)
            return
        raise
    if pull.get("merged_at") or pull.get("merged"):
        state = "merged"
    elif pull.get("state") == "closed":
        state = "declined"
    elif repo["fix_state"] is None:
        state = "open"  # e.g. permissions were revoked and granted again: the PR never went away
    else:
        return  # still open: diverged/failed/open keep their meaning
    if state != repo["fix_state"]:
        db.update_repo(conn, repo["id"], fix_state=state)


def _ours(pull: dict | None, settings: Settings) -> dict | None:
    """A pull request apidrift may adopt: opened by this App, or carrying its marker."""
    if pull is None:
        return None
    login = (pull.get("user") or {}).get("login")
    return pull if login == settings.bot_login or prbody.pr_marker_in(pull.get("body")) else None


def _push(
    conn: sqlite3.Connection, gh: GitHubClient, settings: Settings, token: str, repo: sqlite3.Row, full_name: str,
    default_branch: str, base_sha: str, payload: dict, issue_number: int, version: str,
) -> str:
    _settle_pull(conn, gh, token, repo, full_name)
    repo = db.get_repo(conn, repo["id"]) or repo
    accepted = fixcheck.validate(gh, token, full_name, base_sha, payload.get("fixes"))
    number, state = repo["pr_number"], repo["fix_state"]
    if not accepted:
        if number is not None and state in CLOSABLE:
            return _close(conn, gh, token, repo, full_name, gone_comment(base_sha))
        return "nothing to push"
    fp, keys = fingerprint(accepted, version, issue_number), site_keys(accepted)
    if number is not None and state == "open" and fp == repo["pr_fingerprint"]:
        return f"#{number} unchanged"
    if state in ("declined", "merged"):
        if fp == repo["pr_fingerprint"]:
            return f"#{number} was {state}; staying silent"
        previous = set(json.loads(repo["pr_items"])) if repo["pr_items"] else set()
        if not set(keys) - previous:
            return f"#{number} was {state} and no new call site appeared; staying silent"
        number = None  # new sites: a new pull request on the same branch
    branch = branch_name(version)
    ref, head = f"heads/{branch}", f"{full_name.split('/', 1)[0]}:{branch}"
    existing = gh.get_ref(token, full_name, ref)
    existing_sha = str(((existing or {}).get("object") or {}).get("sha") or "")
    ours = {sha for sha in (repo["pr_head_sha"], repo["pr_pending_sha"]) if sha}
    if existing is not None and existing_sha not in ours:
        adopted = _ours(gh.find_pull(token, full_name, head), settings) if not ours else None  # reinstall: no memory
        if adopted is None:
            db.update_repo(conn, repo["id"], fix_state="diverged", pr_branch=branch)
            return f"branch {branch} has commits apidrift did not make; left alone"
        number = int(adopted["number"])
    title, body = prbody.render_pr(
        payload, accepted, full_name=full_name, sha=base_sha, version=version, issue_number=issue_number, fingerprint=fp
    )
    commit = fixcheck.commit(gh, token, full_name, base_sha, accepted, commit_message(version, accepted))
    db.update_repo(conn, repo["id"], pr_branch=branch, pr_pending_sha=commit, pr_fingerprint=fp, pr_items=json.dumps(keys))
    if existing is None:
        gh.create_ref(token, full_name, f"refs/{ref}", commit)
    elif existing_sha != commit:
        gh.update_ref(token, full_name, ref, commit, force=True)
    db.update_repo(conn, repo["id"], pr_head_sha=commit, pr_pending_sha=None)
    if number is None:
        found = _ours(gh.find_pull(token, full_name, head), settings)
        number = int(found["number"]) if found else None
    if number is None:
        number = int(gh.create_pull(token, full_name, title, body, head, default_branch)["number"])
        what = "opened"
    else:
        gh.update_pull(token, full_name, int(number), title=title, body=body)
        what = "updated"
    db.update_repo(conn, repo["id"], pr_number=int(number), fix_state="open")
    return f"{what} #{number}"


def _close(conn: sqlite3.Connection, gh: GitHubClient, token: str, repo: sqlite3.Row, full_name: str, comment: str) -> str:
    number = int(repo["pr_number"])
    _settle_pull(conn, gh, token, repo, full_name)
    current = db.get_repo(conn, repo["id"]) or repo
    if current["pr_number"] is None or current["fix_state"] not in CLOSABLE:
        return f"#{number} was {current['fix_state'] or 'deleted'}"
    forget = dict(pr_number=None, fix_state=None, pr_fingerprint=None, pr_items=None)
    if current["fix_state"] == "diverged":  # not ours alone any more: say so, do not close over someone's commits
        base = comment[: -len("; closing.")] if comment.endswith("; closing.") else comment.rstrip(".")
        gh.comment(token, full_name, number, f"{base}. {DIVERGED_NOTE}")
        db.update_repo(conn, repo["id"], **forget)
        return f"#{number} left open: the branch has commits apidrift did not make"
    gh.comment(token, full_name, number, comment)
    gh.update_pull(token, full_name, number, state="closed")
    db.update_repo(conn, repo["id"], **forget)
    return f"closed #{number}"
