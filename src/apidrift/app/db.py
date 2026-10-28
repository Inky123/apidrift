"""SQLite persistence: installations, repos, the job queue and webhook delivery ids.

Writes run inside ``BEGIN IMMEDIATE`` through :func:`write`; helpers may be
nested inside an outer ``write`` so a webhook can record its delivery id,
upsert rows and enqueue jobs atomically. Timestamps are UTC ISO-8601 text,
which compares correctly as strings.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

Row = sqlite3.Row
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MAX_ATTEMPTS = 8
MAX_BACKOFF_SECONDS = 6 * 3600
JOB_KINDS = ("check", "poll")
JOB_REASONS = ("install", "push", "release", "manual")
ISSUE_STATES = ("open", "closed_by_us", "closed_by_user")
REPO_FIELDS = frozenset({
    "installation_id", "full_name", "baseline", "last_sha", "last_result", "last_checked_at",
    "issue_number", "issue_state", "issue_fingerprint", "issue_items", "issue_body_sha", "issue_version", "gone",
})
META_KEYS = ("last_poll_at", "last_poll_status", "latest_snapshot")

MIGRATIONS: list[str] = [
    """
    CREATE TABLE installations (
        id INTEGER PRIMARY KEY,
        account TEXT NOT NULL,
        suspended INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE repos (
        id INTEGER PRIMARY KEY,
        installation_id INTEGER NOT NULL REFERENCES installations(id) ON DELETE CASCADE,
        full_name TEXT NOT NULL,
        baseline TEXT,
        last_sha TEXT,
        last_result TEXT,
        last_checked_at TEXT,
        issue_number INTEGER,
        issue_state TEXT CHECK (issue_state IN ('open', 'closed_by_us', 'closed_by_user')),
        issue_fingerprint TEXT,
        issue_items TEXT,
        issue_body_sha TEXT,
        issue_version TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX repos_installation ON repos(installation_id);
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('check', 'poll')),
        repo_id INTEGER REFERENCES repos(id) ON DELETE CASCADE,
        reason TEXT NOT NULL CHECK (reason IN ('install', 'push', 'release', 'manual')),
        status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        not_before TEXT NOT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        error TEXT
    );
    CREATE UNIQUE INDEX jobs_one_queued ON jobs(kind, repo_id) WHERE status = 'queued';
    CREATE INDEX jobs_claim ON jobs(status, not_before);
    CREATE TABLE deliveries (
        id TEXT PRIMARY KEY,
        event TEXT NOT NULL,
        received_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    ALTER TABLE repos ADD COLUMN gone INTEGER NOT NULL DEFAULT 0;
    """,
]


def now() -> str:
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


def later(seconds: float, base: str | None = None) -> str:
    """``base`` (default now) plus ``seconds``, as a timestamp string."""
    start = datetime.strptime(base, TS_FORMAT).replace(tzinfo=timezone.utc) if base else datetime.now(timezone.utc)
    return (start + timedelta(seconds=seconds)).strftime(TS_FORMAT)


def backoff(attempts: int) -> float:
    """Seconds to wait before retry number ``attempts``: 60 * 2**n, capped at six hours."""
    return float(min(60 * 2**attempts, MAX_BACKOFF_SECONDS))


def from_epoch(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime(TS_FORMAT)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the database in autocommit mode with WAL and a 5 s busy timeout; the caller runs migrate()."""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")  # before the WAL switch, which itself needs the lock
    _enable_wal(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _enable_wal(conn: sqlite3.Connection, attempts: int = 40) -> None:
    """Switch to WAL. On a brand-new file two processes opening together can each see ``database is
    locked`` here (the busy handler does not cover this lock); WAL is persistent, so retry briefly."""
    for attempt in range(attempts):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == attempts - 1:
                raise
            time.sleep(0.05)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the resulting ``PRAGMA user_version``.

    The version is read inside the same ``BEGIN IMMEDIATE`` as the DDL, so two processes starting
    on an empty database cannot both run the first migration.
    """
    with write(conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS, start=1):
            if number <= version:
                continue
            _run_script(conn, script)
            conn.execute(f"PRAGMA user_version = {number}")
            version = number
    return version


def _run_script(conn: sqlite3.Connection, script: str) -> None:
    for statement in script.split(";"):
        if statement.strip():
            conn.execute(statement)


@contextmanager
def write(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A ``BEGIN IMMEDIATE`` transaction; a no-op when one is already open on this connection."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --- installations -----------------------------------------------------------


def upsert_installation(conn: sqlite3.Connection, installation_id: int, account: str, suspended: bool = False) -> None:
    ts = now()
    with write(conn):
        conn.execute(
            "INSERT INTO installations (id, account, suspended, created_at, updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET account = excluded.account, suspended = excluded.suspended,"
            " updated_at = excluded.updated_at",
            (installation_id, account, int(suspended), ts, ts),
        )


def set_suspended(conn: sqlite3.Connection, installation_id: int, suspended: bool) -> None:
    with write(conn):
        conn.execute(
            "UPDATE installations SET suspended = ?, updated_at = ? WHERE id = ?", (int(suspended), now(), installation_id)
        )


def get_installation(conn: sqlite3.Connection, installation_id: int) -> Row | None:
    return conn.execute("SELECT * FROM installations WHERE id = ?", (installation_id,)).fetchone()


def delete_installation(conn: sqlite3.Connection, installation_id: int) -> list[int]:
    """Purge an installation, its repos, deliveries aside; returns the ids of the jobs that went with it."""
    with write(conn):
        rows = conn.execute(
            "SELECT j.id FROM jobs j JOIN repos r ON r.id = j.repo_id WHERE r.installation_id = ?", (installation_id,)
        ).fetchall()
        conn.execute("DELETE FROM installations WHERE id = ?", (installation_id,))
    return [row["id"] for row in rows]


# --- repos --------------------------------------------------------------------


def upsert_repo(
    conn: sqlite3.Connection, repo_id: int, installation_id: int, full_name: str, baseline: str | None
) -> None:
    """Insert a repo; an existing row keeps its baseline and issue state but follows the installation and name.

    GitHub mentioning the repo again (install, push) also clears ``gone``, set when its token was refused.
    """
    ts = now()
    with write(conn):
        conn.execute(
            "INSERT INTO repos (id, installation_id, full_name, baseline, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET installation_id = excluded.installation_id,"
            " full_name = excluded.full_name, gone = 0, updated_at = excluded.updated_at",
            (repo_id, installation_id, full_name, baseline, ts, ts),
        )


def delete_repos(conn: sqlite3.Connection, repo_ids: list[int], installation_id: int) -> list[int]:
    """Delete repos that belong to ``installation_id`` (their jobs cascade); returns the removed job ids."""
    if not repo_ids:
        return []
    marks = ",".join("?" * len(repo_ids))
    params = (*repo_ids, installation_id)
    with write(conn):
        rows = conn.execute(
            f"SELECT j.id FROM jobs j JOIN repos r ON r.id = j.repo_id"
            f" WHERE r.id IN ({marks}) AND r.installation_id = ?", params,
        ).fetchall()
        conn.execute(f"DELETE FROM repos WHERE id IN ({marks}) AND installation_id = ?", params)
    return [row["id"] for row in rows]


def get_repo(conn: sqlite3.Connection, repo_id: int) -> Row | None:
    return conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()


def find_repo(conn: sqlite3.Connection, full_name: str) -> Row | None:
    return conn.execute("SELECT * FROM repos WHERE full_name = ? COLLATE NOCASE", (full_name,)).fetchone()


def active_repos(conn: sqlite3.Connection) -> list[Row]:
    """Repos whose installation is not suspended and that GitHub has not refused a token for, oldest first."""
    return conn.execute(
        "SELECT r.* FROM repos r JOIN installations i ON i.id = r.installation_id"
        " WHERE i.suspended = 0 AND r.gone = 0 ORDER BY r.id"
    ).fetchall()


def stale_repos(conn: sqlite3.Connection, stamp: str) -> list[Row]:
    """Active repos not checked since ``stamp`` (a timestamp string) that have no queued or running check."""
    return conn.execute(
        "SELECT r.* FROM repos r JOIN installations i ON i.id = r.installation_id"
        " WHERE i.suspended = 0 AND r.gone = 0 AND (r.last_checked_at IS NULL OR r.last_checked_at < ?)"
        " AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.repo_id = r.id AND j.kind = 'check'"
        " AND j.status IN ('queued', 'running')) ORDER BY r.id",
        (stamp,),
    ).fetchall()


def update_repo(conn: sqlite3.Connection, repo_id: int, **fields: object) -> None:
    unknown = set(fields) - REPO_FIELDS
    if unknown:
        raise ValueError(f"unknown repo fields: {sorted(unknown)}")
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with write(conn):
        conn.execute(
            f"UPDATE repos SET {assignments}, updated_at = ? WHERE id = ?", (*fields.values(), now(), repo_id)
        )


# --- jobs ---------------------------------------------------------------------


def enqueue(
    conn: sqlite3.Connection, kind: str, repo_id: int | None, reason: str, not_before: str | None = None
) -> int | None:
    """Queue a job; returns its id, or None when one is already queued for this kind and repo."""
    if kind not in JOB_KINDS or reason not in JOB_REASONS:
        raise ValueError(f"bad job kind/reason: {kind}/{reason}")
    ts = now()
    with write(conn):
        row = conn.execute(
            "INSERT INTO jobs (kind, repo_id, reason, not_before, created_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(kind, repo_id) WHERE status = 'queued' DO NOTHING RETURNING id",
            (kind, repo_id, reason, not_before or ts, ts),
        ).fetchone()
    return row["id"] if row else None


def claim_job(conn: sqlite3.Connection, at: str | None = None, job_id: int | None = None) -> Row | None:
    """Atomically take the oldest due job (or exactly ``job_id``, due or not), marking it running."""
    ts = at or now()
    with write(conn):
        if job_id is not None:
            return conn.execute(
                "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued' RETURNING *",
                (ts, job_id),
            ).fetchone()
        return conn.execute(
            "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ("
            " SELECT id FROM jobs WHERE status = 'queued' AND not_before <= ? ORDER BY id LIMIT 1) RETURNING *",
            (ts, ts),
        ).fetchone()


def queued_for(conn: sqlite3.Connection, repo_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM jobs WHERE kind = 'check' AND repo_id = ? AND status = 'queued'", (repo_id,)
    ).fetchone()
    return row["id"] if row else None


def finish_job(conn: sqlite3.Connection, job_id: int, status: str, error: str | None = None) -> None:
    if status not in ("done", "failed"):
        raise ValueError(f"bad final status {status!r}")
    with write(conn):
        conn.execute(
            "UPDATE jobs SET status = ?, finished_at = ?, error = ? WHERE id = ?", (status, now(), error, job_id)
        )


def requeue_job(conn: sqlite3.Connection, job_id: int, not_before: str, error: str | None, count_attempt: bool) -> str:
    """Put a job back in the queue; returns the resulting status.

    A failure counts an attempt and the job is marked ``failed`` after MAX_ATTEMPTS. If another job for
    the same repo was queued meanwhile, this one is retired instead (the queued one will run anyway).
    """
    with write(conn):
        job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            return "gone"
        attempts = job["attempts"] + (1 if count_attempt else 0)
        if count_attempt and attempts >= MAX_ATTEMPTS:
            conn.execute(
                "UPDATE jobs SET status = 'failed', attempts = ?, finished_at = ?, error = ? WHERE id = ?",
                (attempts, now(), error, job_id),
            )
            return "failed"
        try:
            conn.execute(
                "UPDATE jobs SET status = 'queued', attempts = ?, not_before = ?, started_at = NULL, error = ?"
                " WHERE id = ?",
                (attempts, not_before, error, job_id),
            )
            return "queued"
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE jobs SET status = 'failed', attempts = ?, finished_at = ?, error = ? WHERE id = ?",
                (attempts, now(), (error or "") + " (superseded by a newer queued job)", job_id),
            )
            return "failed"


def requeue_running(conn: sqlite3.Connection) -> int:
    """Re-queue jobs a previous worker left running (it died mid-check). Returns how many.

    Each restart counts as an attempt, so a repo that keeps killing the worker ends up ``failed``.
    """
    rows = conn.execute("SELECT id FROM jobs WHERE status = 'running' ORDER BY id").fetchall()
    for row in rows:
        requeue_job(conn, row["id"], now(), "worker restarted", count_attempt=True)
    return len(rows)


def get_job(conn: sqlite3.Connection, job_id: int) -> Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def queued_jobs(conn: sqlite3.Connection) -> list[Row]:
    return conn.execute("SELECT * FROM jobs WHERE status = 'queued' ORDER BY id").fetchall()


def recent_jobs(conn: sqlite3.Connection, hours: int = 24) -> list[Row]:
    """Jobs created in the last ``hours`` plus anything still queued or running, oldest first."""
    return conn.execute(
        "SELECT * FROM jobs WHERE created_at >= ? OR status IN ('queued', 'running') ORDER BY id",
        (later(-hours * 3600),),
    ).fetchall()


def prune_jobs(conn: sqlite3.Connection, keep_days: int = 30) -> int:
    """Delete done and failed jobs older than ``keep_days``; returns how many went."""
    with write(conn):
        cursor = conn.execute(
            "DELETE FROM jobs WHERE status IN ('done', 'failed') AND finished_at < ?", (later(-keep_days * 86400),)
        )
    return cursor.rowcount


# --- deliveries ---------------------------------------------------------------


def record_delivery(conn: sqlite3.Connection, delivery_id: str, event: str, keep_days: int = 30) -> bool:
    """Remember a webhook delivery id; False when it was seen before."""
    with write(conn):
        conn.execute("DELETE FROM deliveries WHERE received_at < ?", (later(-keep_days * 86400),))
        row = conn.execute(
            "INSERT INTO deliveries (id, event, received_at) VALUES (?, ?, ?) ON CONFLICT(id) DO NOTHING RETURNING id",
            (delivery_id, event, now()),
        ).fetchone()
    return row is not None


# --- meta and status ----------------------------------------------------------


def set_meta(conn: sqlite3.Connection, **values: str | None) -> None:
    """Upsert operator-facing key/value pairs (poll outcome, latest snapshot)."""
    with write(conn):
        for key, value in values.items():
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def all_meta(conn: sqlite3.Connection) -> dict[str, str | None]:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta ORDER BY key")}


def status_summary(conn: sqlite3.Connection, hours: int = 24) -> dict:
    """Counts an operator wants at a glance; plain types only, so it can be printed or served as JSON."""
    installations = conn.execute("SELECT count(*) AS n FROM installations WHERE suspended = 0").fetchone()["n"]
    suspended = conn.execute("SELECT count(*) AS n FROM installations WHERE suspended = 1").fetchone()["n"]
    repos: dict[str, int] = {}
    for row in conn.execute(
        "SELECT CASE WHEN gone = 1 THEN 'gone' ELSE coalesce(last_result, 'unchecked') END AS state, count(*) AS n"
        " FROM repos GROUP BY state ORDER BY state"
    ):
        repos[row["state"]] = row["n"]
    open_issues = conn.execute("SELECT count(*) AS n FROM repos WHERE issue_state = 'open'").fetchone()["n"]
    jobs = {"queued": 0, "running": 0, "done": 0, "failed": 0}
    errors: list[dict] = []
    for job in recent_jobs(conn, hours):
        jobs[job["status"]] = jobs.get(job["status"], 0) + 1
        if job["status"] == "failed" or job["error"]:
            errors.append({"id": job["id"], "repo_id": job["repo_id"], "status": job["status"], "error": job["error"]})
    return {
        "installations": installations, "suspended_installations": suspended, "repos": repos,
        "open_issues": open_issues, "jobs": jobs, "job_errors": errors, **all_meta(conn),
    }
