"""The worker process: poll Stripe on a timer, claim queued jobs, run each check, requeue on failure."""

from __future__ import annotations

import logging
import shutil
import signal
import sqlite3
import time

import httpx

from . import db
from .checker import CheckError, run_job
from .config import Settings
from .github import GitHubClient, RateLimited, scrub
from .poller import poll_stripe

IDLE_SECONDS = 5
logger = logging.getLogger("apidrift.worker")


def run_one(job: sqlite3.Row, conn: sqlite3.Connection, gh: GitHubClient, settings: Settings) -> str:
    """Run a claimed job and record its outcome; raises only when the database itself fails."""
    job_id = job["id"]
    try:
        outcome = run_job(job, conn, gh, settings)
    except RateLimited as exc:
        status = db.requeue_job(conn, job_id, db.from_epoch(exc.reset_at), "rate limited", count_attempt=False)
        logger.warning("job %d rate limited; %s until %s", job_id, status, db.from_epoch(exc.reset_at))
        return "rate limited"
    except Exception as exc:  # the check already scrubbed its own text; scrub again for anything else
        error = scrub(str(exc) or type(exc).__name__)[:1000]
        if isinstance(exc, CheckError) and exc.terminal:
            db.finish_job(conn, job_id, "failed", error)
            logger.error("job %d (repo %s) failed for good: %s", job_id, job["repo_id"], error)
            return f"failed: {error}"
        attempts = job["attempts"] + 1
        status = db.requeue_job(conn, job_id, db.later(60 * 2**attempts), error, count_attempt=True)
        logger.error("job %d (repo %s) attempt %d failed: %s -> %s", job_id, job["repo_id"], attempts, error, status)
        return f"failed: {error}"
    db.finish_job(conn, job_id, "done")
    logger.info("job %d (repo %s, %s): %s", job_id, job["repo_id"], job["reason"], outcome)
    return outcome


def drain(conn: sqlite3.Connection, gh: GitHubClient, settings: Settings) -> int:
    """Run every due job; returns how many ran."""
    count = 0
    while (job := db.claim_job(conn)) is not None:
        run_one(job, conn, gh, settings)
        count += 1
    return count


def tick(settings: Settings, conn: sqlite3.Connection, gh: GitHubClient, http: httpx.Client, poll: bool = True) -> int:
    """One worker iteration: optionally poll Stripe, then drain the queue."""
    if poll:
        try:
            poll_stripe(settings, conn, http)
        except Exception as exc:
            logger.error("Stripe poll failed: %s", scrub(str(exc) or type(exc).__name__))
    return drain(conn, gh, settings)


def start(settings: Settings) -> tuple[sqlite3.Connection, GitHubClient, httpx.Client]:
    """Open the database, requeue jobs a dead worker left running, clear stale work dirs."""
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    requeued = db.requeue_running(conn)
    if requeued:
        logger.info("re-queued %d jobs left running by a previous worker", requeued)
    shutil.rmtree(settings.work_dir, ignore_errors=True)
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    return conn, GitHubClient(settings), httpx.Client(timeout=120.0)


def _stop(signum: int, frame: object) -> None:
    raise SystemExit(0)


def loop(settings: Settings, once: bool = False) -> None:
    """Poll at startup and every ``poll_minutes``; drain the queue every few seconds."""
    signal.signal(signal.SIGTERM, _stop)
    conn, gh, http = start(settings)
    try:
        next_poll = 0.0
        while True:
            due = time.monotonic() >= next_poll
            if due:
                next_poll = time.monotonic() + settings.poll_minutes * 60
            tick(settings, conn, gh, http, poll=due)
            if once:
                return
            time.sleep(IDLE_SECONDS)
    finally:
        http.close()
        conn.close()
