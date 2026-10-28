"""GitHub webhook handling: HMAC verification and the event handlers (rows and jobs only, no GitHub calls)."""

from __future__ import annotations

import hashlib
import hmac
import re
import shutil
import sqlite3
from pathlib import Path

from .. import snapshot as snap
from . import db
from .poller import initial_baseline

MAX_BODY = 25 * 1024 * 1024
MAX_ID = 2**63 - 1  # SQLite INTEGER
HANDLED_EVENTS = frozenset({"installation", "installation_repositories", "push"})
INSTALL_ACTIONS = frozenset({"created", "new_permissions_accepted"})
FULL_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class BadPayload(ValueError):
    """The JSON lacked a field we rely on."""


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of ``X-Hub-Signature-256`` against HMAC-SHA256 of the raw body."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode("ascii"), header[7:].strip().lower().encode("utf-8", "replace"))


def handle_event(
    conn: sqlite3.Connection, event: str, delivery_id: str, payload: dict, data_dir: Path | None = None
) -> list[str]:
    """Apply one delivery atomically; returns what was done (empty for events we do not handle)."""
    if event not in HANDLED_EVENTS:
        return []
    if not isinstance(payload, dict):
        raise BadPayload("payload is not an object")
    data_dir = data_dir or snap.data_dir()
    with db.write(conn):
        if not db.record_delivery(conn, delivery_id, event):
            return ["duplicate delivery ignored"]
        if event == "installation":
            return _installation(conn, payload, data_dir)
        if event == "installation_repositories":
            return _installation_repositories(conn, payload, data_dir)
        return _push(conn, payload, data_dir)


def _id(obj: object, what: str) -> int:
    value = obj.get("id") if isinstance(obj, dict) else None
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= MAX_ID:
        raise BadPayload(f"{what} id missing or out of range")
    return value


def _full_name(obj: dict) -> str:
    name = obj.get("full_name")
    if not isinstance(name, str) or not FULL_NAME_RE.fullmatch(name):
        raise BadPayload("repository full_name missing or malformed")
    return name


def _add_repos(conn: sqlite3.Connection, installation_id: int, repos: object, data_dir: Path) -> list[str]:
    actions: list[str] = []
    baseline = initial_baseline(data_dir)
    for item in repos if isinstance(repos, list) else []:
        if not isinstance(item, dict):
            continue
        repo_id, full_name = _id(item, "repository"), _full_name(item)
        db.upsert_repo(conn, repo_id, installation_id, full_name, baseline)
        job = db.enqueue(conn, "check", repo_id, "install")
        actions.append(f"repo {repo_id} added" + (f", check {job} queued" if job else ""))
    return actions


def _purge_work(data_dir: Path, job_ids: list[int]) -> None:
    for job_id in job_ids:
        shutil.rmtree(data_dir / "work" / str(job_id), ignore_errors=True)


def _installation(conn: sqlite3.Connection, payload: dict, data_dir: Path) -> list[str]:
    action = str(payload.get("action", ""))
    installation = payload.get("installation") or {}
    installation_id = _id(installation, "installation")
    if action == "deleted":
        _purge_work(data_dir, db.delete_installation(conn, installation_id))
        return [f"installation {installation_id} deleted"]
    if action == "suspend":
        db.set_suspended(conn, installation_id, True)
        return [f"installation {installation_id} suspended"]
    if action == "unsuspend":
        db.set_suspended(conn, installation_id, False)
        queued = [db.enqueue(conn, "check", r["id"], "install") for r in db.active_repos(conn) if r["installation_id"] == installation_id]
        return [f"installation {installation_id} unsuspended, {sum(1 for j in queued if j)} checks queued"]
    if action not in INSTALL_ACTIONS:
        return [f"installation {action} ignored"]
    account = str((installation.get("account") or {}).get("login") or "")
    db.upsert_installation(conn, installation_id, account, suspended=False)
    actions = [f"installation {installation_id} {action}"]
    actions += _add_repos(conn, installation_id, payload.get("repositories"), data_dir)
    if action == "new_permissions_accepted":
        # the re-queued checks decide again whether a fix pull request can be opened
        cleared = db.clear_fix_state(conn, installation_id, "needs_permissions")
        if cleared:
            actions.append(f"{cleared} repos may get a fix pull request now")
        for repo in db.active_repos(conn):
            if repo["installation_id"] == installation_id:
                db.enqueue(conn, "check", repo["id"], "install")
    return actions


def _installation_repositories(conn: sqlite3.Connection, payload: dict, data_dir: Path) -> list[str]:
    installation_id = _id(payload.get("installation"), "installation")
    if db.get_installation(conn, installation_id) is None:
        account = str(((payload.get("installation") or {}).get("account") or {}).get("login") or "")
        db.upsert_installation(conn, installation_id, account)
    actions = _add_repos(conn, installation_id, payload.get("repositories_added"), data_dir)
    removed = [
        _id(item, "repository") for item in payload.get("repositories_removed") or [] if isinstance(item, dict)
    ]
    if removed:  # scoped to this installation: a repo that moved to another one is not ours to delete
        _purge_work(data_dir, db.delete_repos(conn, removed, installation_id))
        actions.append(f"{len(removed)} repos removed")
    return actions


def _push(conn: sqlite3.Connection, payload: dict, data_dir: Path) -> list[str]:
    if payload.get("deleted"):
        return ["push: branch deleted, ignored"]
    repo = payload.get("repository") or {}
    default_branch = repo.get("default_branch") if isinstance(repo, dict) else None
    if not default_branch or payload.get("ref") != f"refs/heads/{default_branch}":
        return ["push: not the default branch, ignored"]
    installation_id = _id(payload.get("installation"), "installation")
    if db.get_installation(conn, installation_id) is None:
        return ["push: unknown installation, ignored"]
    repo_id, full_name = _id(repo, "repository"), _full_name(repo)
    db.upsert_repo(conn, repo_id, installation_id, full_name, initial_baseline(data_dir))
    job = db.enqueue(conn, "check", repo_id, "push")
    return [f"push: check {job} queued" if job else "push: a check is already queued"]
