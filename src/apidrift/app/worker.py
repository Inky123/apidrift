"""The worker process: poll Stripe on a timer, claim queued jobs, run each check, requeue on failure.

Once a day it also sweeps: any active repo not checked since the latest snapshot and without a queued
or running job goes through the release pass again (answered from its index when that is current, else a
release check is queued; jobs that failed for good are picked up again this way), and done/failed jobs
older than 30 days are pruned.
"""

from __future__ import annotations

import logging
import shutil
import signal
import sqlite3
import time

import httpx

from . import db, inventory, releasepass
from .checker import CheckError, run_job
from .config import Settings
from .github import GitHubClient, RateLimited, scrub
from .poller import poll_stripe, snapshot_stamp, snapshots

IDLE_SECONDS = 5
SWEEP_SECONDS = 24 * 3600
logger = logging.getLogger("apidrift.worker")


def run_one(job: sqlite3.Row, conn: sqlite3.Connection, gh: GitHubClient, settings: Settings) -> str:
    """Run a claimed job and record its outcome; raises only when the database itself fails."""
    job_id = job["id"]
    logger.info("job %d claimed (repo %s, reason %s, attempt %d)", job_id, job["repo_id"], job["reason"], job["attempts"] + 1)
    started = time.monotonic()
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
            logger.error("job %d (repo %s) failed for good after %.1f s: %s", job_id, job["repo_id"], time.monotonic() - started, error)
            return f"failed: {error}"
        attempts = job["attempts"] + 1
        status = db.requeue_job(conn, job_id, db.later(db.backoff(attempts)), error, count_attempt=True)
        logger.error("job %d (repo %s) attempt %d failed after %.1f s: %s -> %s",
                     job_id, job["repo_id"], attempts, time.monotonic() - started, error, status)
        return f"failed: {error}"
    db.finish_job(conn, job_id, "done")
    logger.info("job %d (repo %s, %s) done in %.1f s: %s", job_id, job["repo_id"], job["reason"], time.monotonic() - started, outcome)
    return outcome


def drain(conn: sqlite3.Connection, gh: GitHubClient, settings: Settings) -> int:
    """Run every due job; returns how many ran."""
    count = 0
    while (job := db.claim_job(conn)) is not None:
        run_one(job, conn, gh, settings)
        count += 1
    return count


def sweep(settings: Settings, conn: sqlite3.Connection) -> int:
    """Put every stale repo through the release pass and prune old jobs; returns how many checks were queued.

    This is also what finishes a release pass that a crash interrupted: the repos it had not reached are stale."""
    inventory.drop_superseded(conn)  # before the prune: a failed check must outlive its job row as "no index"
    pruned = db.prune_jobs(conn)
    snaps = snapshots(settings.data)
    if not snaps:
        logger.info("sweep: no snapshot yet; pruned %d old jobs", pruned)
        return 0
    counts = releasepass.run(settings, conn, db.stale_repos(conn, snapshot_stamp(snaps[-1])))
    queued = counts[releasepass.DIFFER] + counts[releasepass.NONE]
    logger.info("sweep: %d stale repos queued behind %s, %d answered from the index; pruned %d old jobs",
                queued, snaps[-1].name, counts[releasepass.SAME], pruned)
    return queued


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
    logger.info("worker starting: %d Stripe snapshots, %d jobs queued, %d active repos",
                len(snapshots(settings.data)), len(db.queued_jobs(conn)), len(db.active_repos(conn)))
    return conn, GitHubClient(settings), httpx.Client(timeout=120.0)


def _stop(signum: int, frame: object) -> None:
    raise SystemExit(0)


def loop(settings: Settings, once: bool = False) -> None:
    """Poll at startup and every ``poll_minutes``; sweep at startup and daily; drain the queue every few seconds."""
    signal.signal(signal.SIGTERM, _stop)
    conn, gh, http = start(settings)
    try:
        next_poll = next_sweep = 0.0
        while True:
            now = time.monotonic()
            due = now >= next_poll
            if due:
                next_poll = now + settings.poll_minutes * 60
            if now >= next_sweep:
                next_sweep = now + SWEEP_SECONDS
                try:
                    sweep(settings, conn)
                except Exception as exc:  # noqa: BLE001 - a broken sweep must not stop the queue
                    logger.error("sweep failed: %s", scrub(str(exc) or type(exc).__name__))
            tick(settings, conn, gh, http, poll=due)
            if once:
                return
            time.sleep(IDLE_SECONDS)
    finally:
        http.close()
        conn.close()
