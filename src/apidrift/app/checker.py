"""Run one check: fetch the default branch, scan it in a subprocess, reconcile the repo's issue.

``run_local`` is the pure pipeline (snapshots -> diff -> scan -> impacts) and the entry point of the
sandboxed child process. ``run_job`` does the GitHub side around it and applies the issue transition
table in ``decide``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import snapshot as snap
from .. import migrations
from ..diff import BREAKING, diff_chain
from ..impact import Impact, assess, render_impact_json
from ..openapi import Spec, load_spec
from ..report import filter_changes
from ..scanner import ScanResult, scan_repo
from ..sdkmap import SdkIndex
from . import db, fixpr, issue
from .checkout import ExtractError, extract_tarball
from .config import Settings
from .github import GitHubClient, GitHubError, RateLimited, TarballTooLarge, scrub
from .poller import baseline_for, snapshots, version_of

MAX_TARBALL_BYTES = 200 * 1024 * 1024
SCAN_TIMEOUT = 600
DEFAULT_MAX_MEM_MB = 1024
# Retrying cannot help: revoked, forbidden (a 403 with rate-limit headers is RateLimited instead), gone, not in
# the installation. A 401 from the token endpoint gets one retry first (clock skew), see run_job.
TERMINAL_STATUSES = frozenset({401, 403, 404, 410, 422})
GONE_STATUSES = frozenset({404, 422})  # the installation no longer covers this repository
CHILD_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
_PATH_RE = re.compile(r"\S*/\S*")
logger = logging.getLogger("apidrift.checker")


class CheckError(Exception):
    """A check failed; the message is already scrubbed and free of customer paths.

    ``terminal`` marks failures a retry cannot fix (the job is failed at once instead of backed off).
    ``result`` names a terminal failure the customer can act on (``too_large``, ``timeout``, ``bad_archive``);
    run_job stores it as the repo's ``last_result`` so a page can surface it.
    """

    def __init__(self, message: str, terminal: bool = False, result: str | None = None) -> None:
        super().__init__(message)
        self.terminal = terminal
        self.result = result


# --- the pipeline (also the subprocess entry) ------------------------------------


def limit_memory(max_mb: int) -> bool:
    """Cap this process's address space (Linux); False where unsupported or when ``max_mb`` is 0."""
    if max_mb <= 0 or not sys.platform.startswith("linux"):
        return False
    try:
        import resource
    except ImportError:
        return False
    limit = max_mb * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        return False
    return True


@dataclass
class LocalCheck:
    result: ScanResult
    impacts: list[Impact]
    old_path: Path
    new_path: Path
    old_spec: Spec
    new_spec: Spec
    versions: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # the fix plan (``--fix``): fixes, unfixable, pins, migrations, leftovers

    @property
    def old_label(self) -> str:
        return f"{self.old_path.name} (version {self.old_spec.version})"

    @property
    def new_label(self) -> str:
        return f"{self.new_path.name} (version {self.new_spec.version})"

    @property
    def breaking(self) -> bool:
        return any(i.severity == BREAKING for i in self.impacts)

    def payload(self) -> dict:
        data = json.loads(render_impact_json(self.result, self.impacts, self.old_label, self.new_label))
        data["old_version"], data["new_version"] = self.old_spec.version, self.new_spec.version
        data["versions"] = self.versions
        data.update(self.extra)
        return data


def run_local(repo_dir: Path, old: str, new: str, fix: bool = False) -> LocalCheck:
    """Diff two Stripe snapshots and cross the breaking changes with the call sites in ``repo_dir``.

    With ``fix`` the documented renames are planned too (``fixer.plan_fixes``) and land in the payload.
    """
    old_path, new_path = snap.resolve("stripe", old), snap.resolve("stripe", new)
    old_spec, new_spec = load_spec(snap.load(old_path)), load_spec(snap.load(new_path))
    index = SdkIndex.build(old_spec, new_spec)
    if index.empty:
        raise CheckError("the stored Stripe spec has no SDK method metadata")
    middle = [load_spec(snap.load(p)) for p in snap.between("stripe", old_path, new_path)]
    changes, versions = diff_chain([old_spec, *middle, new_spec])
    changes = filter_changes(changes, min_severity=BREAKING)
    result = scan_repo(Path(repo_dir), index)
    check = LocalCheck(result, assess(result.usages, changes), old_path, new_path, old_spec, new_spec, versions)
    if fix:
        from ..fixer import plan_fixes  # the child process only; imported here so the parent never loads it

        check.extra = dict(plan_fixes(check.result, check.impacts, index, Path(repo_dir)).to_dict())
    return check


def _last_line(stderr: bytes) -> str:
    lines = [ln.strip() for ln in stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    return _PATH_RE.sub("<path>", lines[-1])[:200] if lines else "no output"


def scan_subprocess(
    tree: Path, old: str, new: str, data_dir: Path, max_mem_mb: int = DEFAULT_MAX_MEM_MB, timeout: int = SCAN_TIMEOUT,
    fix: bool = False,
) -> dict:
    """Run ``apidrift check --local`` on ``tree`` in an isolated child and return its JSON payload."""
    cmd = [
        sys.executable, "-I", "-m", "apidrift.cli", "check", "--local", str(tree), "--old", old, "--new", new,
        "--json", "--max-mem-mb", str(max_mem_mb),
    ]
    if fix:
        cmd.append("--fix")
    # A minimal environment: no webhook secret, app id or key path reaches the process that parses untrusted bytes.
    env = {key: os.environ[key] for key in CHILD_ENV_KEYS if key in os.environ}
    env["APIDRIFT_DATA"] = str(data_dir)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env, cwd=str(data_dir))
    except subprocess.TimeoutExpired:
        raise CheckError(f"scan exceeded {timeout} s", terminal=True, result="timeout") from None
    if proc.returncode not in (0, 1) or not proc.stdout.strip():
        raise CheckError(f"scan failed (exit {proc.returncode}): {_last_line(proc.stderr)}")
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        raise CheckError("scan produced unreadable output") from None
    if not isinstance(payload, dict) or "impacts" not in payload:
        raise CheckError("scan produced an unexpected payload")
    return payload


# --- the transition table -----------------------------------------------------------


def decide(state: str | None, has_findings: bool, *, body_changed: bool, version_changed: bool, gained: bool) -> str:
    """What to do with the repo's issue, given its recorded state and this check's findings."""
    if not has_findings:
        return "close" if state == "open" else "none"
    if state is None:
        return "create"
    if state == "open":
        return "update" if body_changed else "keep"
    if state == "closed_by_us":
        return "reopen"
    return "fresh" if version_changed and gained else "silence"


# --- one job ------------------------------------------------------------------------


def run_job(job: sqlite3.Row, conn: sqlite3.Connection, gh: GitHubClient, settings: Settings) -> str:
    """Check one repo end to end; returns a short outcome. Raises CheckError or RateLimited."""
    repo = db.get_repo(conn, job["repo_id"])
    if repo is None:
        return "skipped: repository is no longer installed"
    installation = db.get_installation(conn, repo["installation_id"])
    if installation is None or installation["suspended"]:
        return "skipped: installation suspended"
    token = _token(job, conn, gh, installation["id"], repo)
    try:
        return _check(job, conn, gh, settings, repo, token)
    except RateLimited:
        raise
    except CheckError as exc:
        raise _noted(conn, repo, exc)
    except TarballTooLarge as exc:
        raise _noted(conn, repo, CheckError(scrub(str(exc), (token,)), terminal=True, result="too_large")) from None
    except ExtractError as exc:
        raise _noted(conn, repo, CheckError(str(exc), terminal=True, result="bad_archive")) from None
    except GitHubError as exc:
        raise CheckError(scrub(str(exc), (token,)), terminal=exc.status in TERMINAL_STATUSES) from None
    except Exception as exc:
        raise CheckError(scrub(str(exc) or type(exc).__name__, (token,))) from None
    finally:
        try:
            gh.revoke_token(token)
        except Exception as exc:  # noqa: BLE001 - best effort; the token expires within the hour anyway
            logger.debug("could not revoke the installation token: %s", scrub(str(exc), (token,)))


def _token(job: sqlite3.Row, conn: sqlite3.Connection, gh: GitHubClient, installation_id: int, repo: sqlite3.Row) -> str:
    """Mint the job's token, mapping token-endpoint failures: 404/422 mark the repo gone, 401 retries once."""
    try:
        return gh.installation_token(installation_id, repo["id"])
    except RateLimited:
        raise
    except GitHubError as exc:
        if exc.status in GONE_STATUSES:
            if not repo["gone"]:
                logger.warning("repo %s (%s): GitHub refuses a token (%s); marking it gone until GitHub mentions it again",
                               repo["id"], repo["full_name"], exc.status)
            db.update_repo(conn, repo["id"], gone=1)
        skew_retry = exc.status == 401 and job["attempts"] == 0
        raise CheckError(scrub(str(exc)), terminal=exc.status in TERMINAL_STATUSES and not skew_retry) from None
    except Exception as exc:
        raise CheckError(scrub(str(exc) or type(exc).__name__)) from None


def _noted(conn: sqlite3.Connection, repo: sqlite3.Row, exc: CheckError) -> CheckError:
    """Store a customer-actionable terminal failure on the repo row; the check counts as done for this release."""
    if exc.terminal and exc.result:
        db.update_repo(conn, repo["id"], last_result=exc.result, last_checked_at=db.now())
    return exc


def _check(job: sqlite3.Row, conn: sqlite3.Connection, gh: GitHubClient, settings: Settings, repo: sqlite3.Row, token: str) -> str:
    meta = gh.get_repo(token, repo["full_name"])
    full_name = str(meta.get("full_name") or repo["full_name"])
    if full_name != repo["full_name"]:
        db.update_repo(conn, repo["id"], full_name=full_name)
    if meta.get("archived") or not meta.get("has_issues", True):
        db.update_repo(conn, repo["id"], last_result="skipped", last_checked_at=db.now())
        return "skipped: issues are disabled or the repository is archived"
    default_branch = str(meta.get("default_branch") or "main")
    sha = gh.branch_sha(token, full_name, default_branch)
    snaps = snapshots(settings.data)
    if not snaps:
        raise CheckError("no Stripe snapshot is stored yet")
    latest = snaps[-1]
    baseline = _baseline(repo["baseline"], snaps)
    work = settings.work_dir / str(job["id"])
    started = time.monotonic()
    try:
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        size = gh.download_tarball(token, full_name, sha, work / "src.tgz", MAX_TARBALL_BYTES)
        tree = extract_tarball(work / "src.tgz", work / "tree")
        payload = scan_subprocess(tree, baseline.name, latest.name, settings.data, fix=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    logger.info("job %s (repo %s): tarball %d bytes, %s -> %s scanned in %.1f s",
                job["id"], repo["id"], size, baseline.name, latest.name, time.monotonic() - started)
    outcome = _reconcile(conn, gh, settings, token, repo, full_name, sha, payload, latest)
    return outcome + _fix_step(conn, gh, settings, token, repo, full_name, default_branch, sha, payload, latest)


def _fix_step(
    conn: sqlite3.Connection, gh: GitHubClient, settings: Settings, token: str, repo: sqlite3.Row,
    full_name: str, default_branch: str, sha: str, payload: dict, latest: Path,
) -> str:
    """After the issue is settled: open or refresh the fix PR while the issue is open and the child planned fixes;
    close the PR on a clean check. Never raises except RateLimited (fixpr records its own failures)."""
    repo = db.get_repo(conn, repo["id"]) or repo
    if not issue.breaking(payload):
        note = fixpr.run_clean(conn, gh, repo, full_name, fixpr.clean_comment(sha))
    elif repo["issue_state"] != "open" or repo["issue_number"] is None:
        return ""
    elif not payload.get("fixes"):  # findings remain but none the fixer can rename: an open PR has nothing left to do
        note = fixpr.run_clean(conn, gh, repo, full_name, fixpr.gone_comment(sha))
    else:  # the version comes from our migration table or snapshot, never the child's text: it names the branch
        version = fix_version(payload, latest)
        note = fixpr.run_fix(conn, gh, settings, repo, full_name, default_branch, sha, payload, int(repo["issue_number"]), version, token)
    return f"; pr: {note}" if note else ""


def fix_version(payload: dict, latest: Path) -> str:
    """The release a fix pull request is named after: the newest release that introduced one of the renamed
    changes, looked up in the trusted migration table by (path, target); the latest snapshot when none match.
    This matches the issue, whose title names the release that introduced the change."""
    found: set[str] = set()
    for fix in payload.get("fixes") or []:
        sites = fix.get("sites") if isinstance(fix, dict) else None
        for site in sites if isinstance(sites, list) else []:
            if isinstance(site, dict):
                release = migrations.introduced_for(str(site.get("path")), str(site.get("target")))
                if release:
                    found.add(release)
    return max(found) if found else version_of(latest)


def _baseline(name: str | None, snaps: list[Path]) -> Path:
    """The stored baseline when it still exists, else the newest snapshot of a different version than the latest."""
    for path in snaps:
        if path.name == name:
            return path
    fallback = baseline_for(snaps)
    assert fallback is not None  # snaps is non-empty here
    return fallback


def _settle_issue(
    conn: sqlite3.Connection, gh: GitHubClient, settings: Settings, token: str, repo: sqlite3.Row, full_name: str, adopt: bool
) -> tuple[int | None, str | None]:
    """Current (issue number, state) after adopting an older bot issue and reading GitHub's state."""
    number = repo["issue_number"]
    if number is None and adopt:
        candidates = [i for i in gh.list_bot_issues(token, full_name) if issue.marker_in(i.get("body"))]
        if candidates:
            number = int(candidates[0]["number"])
            db.update_repo(
                conn, repo["id"], issue_number=number, issue_fingerprint=issue.marker_in(candidates[0].get("body")),
                issue_items=None, issue_body_sha=None, issue_version=None,
            )
    if number is None:
        return None, None
    try:
        data = gh.get_issue(token, full_name, int(number))
    except GitHubError as exc:
        if exc.status in (404, 410):
            db.update_repo(conn, repo["id"], issue_number=None, issue_state=None)
            return None, None
        raise
    if data.get("state") == "open":
        state = "open"
    else:
        closed_by = (data.get("closed_by") or {}).get("login")
        state = "closed_by_us" if closed_by == settings.bot_login else "closed_by_user"
    if state != repo["issue_state"]:
        db.update_repo(conn, repo["id"], issue_state=state)
    return int(number), state


def _reconcile(
    conn: sqlite3.Connection, gh: GitHubClient, settings: Settings, token: str, repo: sqlite3.Row,
    full_name: str, sha: str, payload: dict, latest: Path,
) -> str:
    impacts = issue.breaking(payload)
    items = issue.findings(payload, full_name, sha)
    old_version, new_version = str(payload.get("old_version", "?")), str(payload.get("new_version", version_of(latest)))
    number, state = _settle_issue(conn, gh, settings, token, repo, full_name, adopt=bool(items))
    repo = db.get_repo(conn, repo["id"]) or repo
    fp, hashes = issue.fingerprint(impacts), issue.finding_hashes(impacts)
    previous = json.loads(repo["issue_items"]) if repo["issue_items"] else None
    render = dict(full_name=full_name, sha=sha, old_version=old_version, new_version=new_version,
                  call_sites=int(payload.get("call_sites", 0)), fingerprint=fp, unread=issue.unread_note(payload))
    title, body = issue.render_issue(items, **render)
    gained = set(hashes) - set(previous) if previous is not None else set()
    version_changed = repo["issue_version"] is not None and repo["issue_version"] != new_version
    action = decide(
        state, bool(items), body_changed=_sha(body) != repo["issue_body_sha"], version_changed=version_changed, gained=bool(gained)
    )
    record = dict(issue_fingerprint=fp, issue_items=json.dumps(hashes), issue_body_sha=_sha(body), issue_version=new_version)
    stamp = dict(last_sha=sha, last_checked_at=db.now())
    if action == "create":
        created = gh.create_issue(token, full_name, title, body)
        db.update_repo(conn, repo["id"], issue_number=int(created["number"]), issue_state="open", last_result="findings", **record, **stamp)
        return f"findings: opened #{created['number']}"
    if action == "fresh":
        title, body = issue.render_issue(items, supersedes=number, **render)
        created = gh.create_issue(token, full_name, title, body)
        record["issue_body_sha"] = _sha(body)
        db.update_repo(conn, repo["id"], issue_number=int(created["number"]), issue_state="open", last_result="findings", **record, **stamp)
        return f"findings: opened #{created['number']} superseding #{number}"
    if action == "update":
        gh.update_issue(token, full_name, number, title=title, body=body)
        if gained or version_changed:  # editing a body notifies nobody; a comment does
            gh.comment(token, full_name, number, f"Stripe {new_version} adds {len(gained)} new finding(s); the issue body is updated.")
        db.update_repo(conn, repo["id"], last_result="findings", **record, **stamp)
        return f"findings: updated #{number}"
    if action == "reopen":
        gh.update_issue(token, full_name, number, state="open", title=title, body=body)
        db.update_repo(conn, repo["id"], issue_state="open", last_result="findings", **record, **stamp)
        return f"findings: reopened #{number}"
    if action == "keep":
        db.update_repo(conn, repo["id"], last_result="findings", **stamp)
        return f"findings: #{number} unchanged"
    if action == "silence":
        if repo["issue_version"] is None:  # adopted issue: this check becomes the reference the next release is judged against
            stamp.update(issue_version=new_version, issue_items=json.dumps(hashes))
        db.update_repo(conn, repo["id"], last_result="findings", **stamp)
        return f"findings: #{number} was closed by a person; staying silent"
    if action == "close":
        gh.comment(token, full_name, number, f"No call sites are affected at {sha}; closing. Baseline is now {new_version}.")
        gh.update_issue(token, full_name, number, state="closed")
        db.update_repo(conn, repo["id"], issue_state="closed_by_us", last_result="clean", baseline=latest.name, **stamp)
        _unread_notice(conn, gh, token, repo, full_name, sha, payload)
        return f"clean: closed #{number}; baseline advanced to {latest.name}"
    db.update_repo(conn, repo["id"], last_result="clean", baseline=latest.name, **stamp)
    _unread_notice(conn, gh, token, repo, full_name, sha, payload)
    return f"clean; baseline advanced to {latest.name}"


def _unread_notice(
    conn: sqlite3.Connection, gh: GitHubClient, token: str, repo: sqlite3.Row, full_name: str, sha: str, payload: dict
) -> None:
    """A clean verdict is only honest if every file was read. When files in a language apidrift cannot parse yet
    mention Stripe, open one notice issue (once per repository); close it when that is no longer the case.
    A findings issue carries the same sentence in its footer, so no notice is opened while one is open."""
    unread = payload.get("unread") or {}
    existing = repo["unread_issue"]
    if unread and existing is None:
        title, body = issue.render_unread_issue(full_name, sha, payload)
        created = gh.create_issue(token, full_name, title, body)
        db.update_repo(conn, repo["id"], unread_issue=int(created["number"]))
        logger.info("repo %s: opened unread-language notice #%s (%s)", repo["id"], created["number"], ", ".join(unread))
    elif not unread and existing is not None:
        try:
            gh.comment(token, full_name, int(existing), f"Every file that mentions Stripe is now read at {sha}; closing.")
            gh.update_issue(token, full_name, int(existing), state="closed")
        except GitHubError as exc:  # already deleted or locked: forget it either way
            logger.info("repo %s: could not close notice #%s: %s", repo["id"], existing, exc)
        db.update_repo(conn, repo["id"], unread_issue=None)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
