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
from dataclasses import dataclass
from pathlib import Path

from .. import snapshot as snap
from ..diff import BREAKING, diff_specs
from ..impact import Impact, assess, render_impact_json
from ..openapi import Spec, load_spec
from ..report import filter_changes
from ..scanner import ScanResult, scan_repo
from ..sdkmap import SdkIndex
from . import db, issue
from .checkout import extract_tarball
from .config import Settings
from .github import GitHubClient, GitHubError, RateLimited, scrub
from .poller import snapshots, version_of

MAX_TARBALL_BYTES = 200 * 1024 * 1024
SCAN_TIMEOUT = 600
DEFAULT_MAX_MEM_MB = 1024
TERMINAL_STATUSES = frozenset({401, 404, 410, 422})  # retrying cannot help: revoked, gone, not in the installation
CHILD_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
_PATH_RE = re.compile(r"\S*/\S*")
logger = logging.getLogger("apidrift.checker")


class CheckError(Exception):
    """A check failed; the message is already scrubbed and free of customer paths.

    ``terminal`` marks failures a retry cannot fix (the job is failed at once instead of backed off).
    """

    def __init__(self, message: str, terminal: bool = False) -> None:
        super().__init__(message)
        self.terminal = terminal


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
        return data


def run_local(repo_dir: Path, old: str, new: str) -> LocalCheck:
    """Diff two Stripe snapshots and cross the breaking changes with the call sites in ``repo_dir``."""
    old_path, new_path = snap.resolve("stripe", old), snap.resolve("stripe", new)
    old_spec, new_spec = load_spec(snap.load(old_path)), load_spec(snap.load(new_path))
    index = SdkIndex.build(old_spec, new_spec)
    if index.empty:
        raise CheckError("the stored Stripe spec has no SDK method metadata")
    changes = filter_changes(diff_specs(old_spec, new_spec), min_severity=BREAKING)
    result = scan_repo(Path(repo_dir), index)
    return LocalCheck(result, assess(result.usages, changes), old_path, new_path, old_spec, new_spec)


def _last_line(stderr: bytes) -> str:
    lines = [ln.strip() for ln in stderr.decode("utf-8", "replace").splitlines() if ln.strip()]
    return _PATH_RE.sub("<path>", lines[-1])[:200] if lines else "no output"


def scan_subprocess(
    tree: Path, old: str, new: str, data_dir: Path, max_mem_mb: int = DEFAULT_MAX_MEM_MB, timeout: int = SCAN_TIMEOUT
) -> dict:
    """Run ``apidrift check --local`` on ``tree`` in an isolated child and return its JSON payload."""
    cmd = [
        sys.executable, "-I", "-m", "apidrift.cli", "check", "--local", str(tree), "--old", old, "--new", new,
        "--json", "--max-mem-mb", str(max_mem_mb),
    ]
    # A minimal environment: no webhook secret, app id or key path reaches the process that parses untrusted bytes.
    env = {key: os.environ[key] for key in CHILD_ENV_KEYS if key in os.environ}
    env["APIDRIFT_DATA"] = str(data_dir)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env, cwd=str(data_dir))
    except subprocess.TimeoutExpired:
        raise CheckError(f"scan exceeded {timeout} s") from None
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
    try:
        token = gh.installation_token(installation["id"], repo["id"])
    except RateLimited:
        raise
    except GitHubError as exc:
        raise CheckError(scrub(str(exc)), terminal=exc.status in TERMINAL_STATUSES) from None
    except Exception as exc:
        raise CheckError(scrub(str(exc) or type(exc).__name__)) from None
    try:
        return _check(job, conn, gh, settings, repo, token)
    except RateLimited:
        raise
    except GitHubError as exc:
        raise CheckError(scrub(str(exc), (token,)), terminal=exc.status in TERMINAL_STATUSES) from None
    except Exception as exc:
        raise CheckError(scrub(str(exc) or type(exc).__name__, (token,))) from None


def _check(job: sqlite3.Row, conn: sqlite3.Connection, gh: GitHubClient, settings: Settings, repo: sqlite3.Row, token: str) -> str:
    meta = gh.get_repo(token, repo["full_name"])
    full_name = str(meta.get("full_name") or repo["full_name"])
    if full_name != repo["full_name"]:
        db.update_repo(conn, repo["id"], full_name=full_name)
    if meta.get("archived") or not meta.get("has_issues", True):
        db.update_repo(conn, repo["id"], last_result="skipped", last_checked_at=db.now())
        return "skipped: issues are disabled or the repository is archived"
    sha = gh.branch_sha(token, full_name, str(meta.get("default_branch") or "main"))
    snaps = snapshots(settings.data)
    if not snaps:
        raise CheckError("no Stripe snapshot is stored yet")
    latest = snaps[-1]
    baseline = _baseline(repo["baseline"], snaps)
    work = settings.work_dir / str(job["id"])
    try:
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        gh.download_tarball(token, full_name, sha, work / "src.tgz", MAX_TARBALL_BYTES)
        tree = extract_tarball(work / "src.tgz", work / "tree")
        payload = scan_subprocess(tree, baseline.name, latest.name, settings.data)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return _reconcile(conn, gh, settings, token, repo, full_name, sha, payload, latest)


def _baseline(name: str | None, snaps: list[Path]) -> Path:
    """The stored baseline when it still exists, else the previous snapshot (or the only one)."""
    for path in snaps:
        if path.name == name:
            return path
    return snaps[-2] if len(snaps) >= 2 else snaps[-1]


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
                  call_sites=int(payload.get("call_sites", 0)), fingerprint=fp)
    title, body = issue.render_issue(items, **render)
    action = decide(
        state, bool(items),
        body_changed=_sha(body) != repo["issue_body_sha"],
        version_changed=repo["issue_version"] is not None and repo["issue_version"] != new_version,
        gained=previous is not None and bool(set(hashes) - set(previous)),
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
        gh.comment(token, full_name, number, f"Clean at {sha}")
        gh.update_issue(token, full_name, number, state="closed")
        db.update_repo(conn, repo["id"], issue_state="closed_by_us", last_result="clean", baseline=latest.name, **stamp)
        return f"clean: closed #{number}; baseline advanced to {latest.name}"
    db.update_repo(conn, repo["id"], last_result="clean", baseline=latest.name, **stamp)
    return f"clean; baseline advanced to {latest.name}"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
