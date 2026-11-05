"""A Stripe release, checked against every repo's index instead of every repo's code.

For each repo the pass reaches one verdict:

* ``same``: the index is current, GitHub confirms the default branch is still at the indexed commit, and what
  the index yields against the new release is what the repo's issue was last written from (or the repo is
  still clean). The repo is stamped as checked; a clean repo's baseline advances to the new snapshot exactly
  as after a clean full check. The only GitHub calls are that lookup (a read-only token, the repository, its
  branch, the token's revocation): no tarball is downloaded and no issue or pull request is touched.
* ``differ``: the index is current but findings are new, changed or gone: the full check is queued, and it
  alone touches the issue and the fix pull request, from real code.
* ``none``: no usable index (never written, another commit, another scanner, a later check that failed, calls
  that resolve differently against this release, a branch that moved or could not be looked up): the full
  check is queued, as before the index existed.
* ``queued``: a check is already queued; it reads the newest snapshot when it runs.

Each repo is settled in one transaction, so the pass can stop anywhere and be run again: the worker's sweep
does that for every repo still not checked since the latest snapshot.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from . import db, indexcheck, inventory, issue
from .checker import _baseline
from .config import Settings
from .github import GitHubClient, RateLimited
from .poller import STAGGER_SECONDS, snapshots, version_of

SAME, DIFFER, NONE, QUEUED = "same", "differ", "none", "queued"
CLEAN = "still clean"
LOOKUP_PERMISSIONS = {"contents": "read", "metadata": "read"}  # enough to read a branch head; nothing can be written
# What a verdict was computed from; if any of it moved before the verdict is applied, the full check runs instead.
WATCHED = (
    "installation_id", "full_name", "baseline", "last_sha", "last_result", "last_checked_at", "issue_number", "issue_state",
    "issue_fingerprint", "issue_items", "issue_body_sha", "issue_version", "gone",
)
logger = logging.getLogger("apidrift.release")


def assess_repo(conn: sqlite3.Connection, repo: sqlite3.Row, snaps: list[Path]) -> tuple[str, str]:
    """(verdict, why) for one repo against the latest snapshot, from the database alone."""
    if db.queued_for(conn, repo["id"]) is not None:
        return QUEUED, "a check is already queued"
    header = inventory.get(conn, repo["id"])
    if header is None:
        return NONE, "no index"
    if header["format"] != inventory.FORMAT or not header["scanner"] or header["scanner"] != inventory.scanner_digest():
        return NONE, "the index was written by another version of the scanner"
    if not repo["last_sha"] or header["sha"] != repo["last_sha"]:
        return NONE, "the index is for another commit"
    if repo["last_result"] not in ("clean", "findings"):
        return NONE, "the last check did not finish"
    if inventory.later_job(conn, repo["id"], header["job_id"]):
        return NONE, "a later check did not rewrite the index"
    stored = inventory.load(conn, header)
    if stored is None:
        return NONE, "the index does not read back"
    pr = indexcheck.pair(snaps, _baseline(repo["baseline"], snaps), snaps[-1])
    if pr.index.empty or not indexcheck.same_answers(pr.index, stored.answers):
        return NONE, "calls resolve differently against this release"
    return _decide(repo, indexcheck.payload_of(stored.usages, stored.unread, pr), stored.impacts)


def _decide(repo: sqlite3.Row, payload: dict, written_from: str) -> tuple[str, str]:
    """Compare what the index yields now with what the last full check left on the repo row.

    An open issue was last written by the check that wrote the index (every finished check rewrites both), from
    that check's impacts. If the whole impact list is the same now, a full check of the same commit would get
    the same code, the same impacts and the same SDK answers, so the same findings and the same fix plan: the
    issue already says what it would say, except for the version in its footer. Nothing is rendered here, so
    the comparison does not depend on how the issue is worded or on the fix plan, which the index does not keep.
    An issue a person closed stays closed unless findings were gained, the rule ``checker.decide`` applies.
    """
    found = issue.breaking(payload)
    state = repo["issue_state"]
    if not found:
        if repo["last_result"] == "clean" and state != "open":
            return SAME, CLEAN
        return DIFFER, "the findings are gone"
    if repo["last_result"] != "findings":
        return DIFFER, "new findings"
    if state == "open":
        if inventory.impacts_digest(payload["impacts"]) == written_from and issue.fingerprint(found) == repo["issue_fingerprint"]:
            return SAME, "same findings"
    elif state == "closed_by_user" and repo["issue_version"] and repo["issue_items"]:
        if set(issue.finding_hashes(found)) <= set(json.loads(repo["issue_items"])):
            return SAME, "no findings gained on an issue a person closed"
    return DIFFER, "the findings changed"


def head_moved(gh: GitHubClient, repo: sqlite3.Row, sha: str) -> str | None:
    """Ask GitHub whether the default branch is still at ``sha`` (a push webhook can be lost). Returns None when
    it is, else why the index cannot answer. Raises what the client raises; the token is revoked either way."""
    token = gh.installation_token(repo["installation_id"], repo["id"], LOOKUP_PERMISSIONS)
    try:
        meta = gh.get_repo(token, repo["full_name"])
        if meta.get("archived") or not meta.get("has_issues", True):
            return "the repository is archived or has issues disabled"
        if str(meta.get("full_name") or repo["full_name"]) != repo["full_name"]:
            return "the repository was renamed"
        head = gh.branch_sha(token, repo["full_name"], str(meta.get("default_branch") or "main"))
        return None if head == sha else "the default branch moved past the indexed commit"
    finally:
        try:
            gh.revoke_token(token)
        except Exception as exc:  # noqa: BLE001 - best effort, as in run_job; the token expires within the hour
            logger.debug("could not revoke a lookup token: %s", type(exc).__name__)


def _watched(repo: sqlite3.Row) -> tuple:
    return tuple(repo[name] for name in WATCHED)


def run(
    settings: Settings, conn: sqlite3.Connection, gh: GitHubClient | None = None, repos: list[sqlite3.Row] | None = None
) -> dict[str, int]:
    """Check ``repos`` (default: every active repo) against the latest snapshot; returns the count per verdict.

    Without ``gh`` no branch head can be confirmed, so no repo is answered from its index. A rate limit ends
    the lookups for this pass: the repos still to come get the full check, which waits for the reset itself.
    Logs one line per pass and records it in ``meta`` for ``apidrift status``.
    """
    counts = {SAME: 0, DIFFER: 0, NONE: 0, QUEUED: 0}
    snaps = snapshots(settings.data)
    if not snaps:
        return counts
    latest, started, base, enqueued, lookups = snaps[-1], time.monotonic(), db.now(), 0, gh is not None
    inventory.drop_superseded(conn)
    todo = db.active_repos(conn) if repos is None else repos
    if repos is not None and not todo:
        return counts
    for repo in sorted(todo, key=lambda r: (r["baseline"] or "", r["id"])):  # one spec pair per baseline, built once
        try:
            verdict, why = assess_repo(conn, repo, snaps)
        except Exception as exc:  # noqa: BLE001 - a repo the index cannot answer for gets the full check
            verdict, why = NONE, f"the index check failed ({type(exc).__name__})"
        if verdict == SAME:
            moved: str | None = "the branch head was not looked up"
            if lookups:
                try:
                    moved = head_moved(gh, repo, repo["last_sha"])
                except RateLimited:
                    lookups, moved = False, "GitHub rate limit; no more lookups in this pass"
                except Exception as exc:  # noqa: BLE001 - token refused, GitHub down: the full check deals with it
                    moved = f"the branch head lookup failed ({type(exc).__name__})"
            if moved is not None:
                verdict, why = NONE, moved
        with db.write(conn):
            current = db.get_repo(conn, repo["id"])
            if current is None:
                continue
            if db.queued_for(conn, repo["id"]) is not None:
                verdict, why = QUEUED, "a check is already queued"
            elif verdict == SAME and _watched(current) != _watched(repo):
                verdict, why = NONE, "the repository changed during the pass"
            if verdict == SAME:
                stamp: dict = {"last_checked_at": db.now()}
                if why == CLEAN:  # what a clean full check records
                    stamp.update(last_result="clean", baseline=latest.name)
                db.update_repo(conn, repo["id"], **stamp)
                inventory.mark_checked(conn, repo["id"], latest.name)
            elif verdict != QUEUED:
                db.enqueue(conn, "check", repo["id"], "release", not_before=db.later(enqueued * STAGGER_SECONDS, base=base))
                enqueued += 1
        counts[verdict] += 1
        logger.log(logging.DEBUG if verdict == SAME else logging.INFO, "repo %s: %s (%s)", repo["id"], verdict, why)
    ms = int((time.monotonic() - started) * 1000)
    release = version_of(latest)
    logger.info(
        "release %s: %d repos checked from the index in %d ms, %d need a full check, %d had no usable index%s",
        release, counts[SAME] + counts[DIFFER], ms, counts[DIFFER], counts[NONE],
        f", {counts[QUEUED]} already had a check queued" if counts[QUEUED] else "",
    )
    if repos is None:  # the pass over every repo; a sweep over the leftovers does not replace its record
        db.set_meta(conn, last_release_pass=json.dumps({"release": release, "at": db.now(), "ms": ms, **counts}))
    return counts
