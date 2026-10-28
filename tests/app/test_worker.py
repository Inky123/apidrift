import json
import logging
import threading

import httpx
from conftest import SHA, install_payload, third_doc

from apidrift.app import db
from apidrift.app.poller import snapshot_stamp, snapshots
from apidrift.app.webhooks import handle_event
from apidrift.app.worker import start, sweep, tick


def stripe_http(responder):
    return httpx.Client(transport=httpx.MockTransport(responder))


def test_tick_polls_stripe_then_drains_the_queue(installed, conn, gh, settings):
    polls = []

    def respond(request):
        polls.append(request.url.host)
        return httpx.Response(304)

    assert tick(settings, conn, gh, stripe_http(respond)) == 1
    assert polls == ["raw.githubusercontent.com"]
    assert installed.issues[1]["title"].endswith("breaks 3 call sites")
    assert db.get_job(conn, 1)["status"] == "done" and db.queued_jobs(conn) == []
    assert tick(settings, conn, gh, stripe_http(respond), poll=False) == 0 and len(polls) == 1


def test_tick_survives_a_failed_poll_and_a_new_release_queues_a_check(installed, conn, gh, settings):
    while db.claim_job(conn) is not None:
        pass
    assert tick(settings, conn, gh, stripe_http(lambda r: httpx.Response(500))) == 0
    body = json.dumps(third_doc()).encode()
    assert tick(settings, conn, gh, stripe_http(lambda r: httpx.Response(200, content=body))) == 1
    job = db.get_job(conn, 2)
    assert (job["reason"], job["status"]) == ("release", "done") and f"blob/{SHA}/" in installed.issues[1]["body"]


def test_start_uses_a_wal_file_requeues_running_jobs_and_clears_work(settings, fake, monkeypatch):
    first = db.connect(settings.db_path)
    db.migrate(first)
    assert first.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert first.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    handle_event(first, "installation", "d-1", install_payload(fake), settings.data)
    running = db.claim_job(first)
    stale = settings.work_dir / str(running["id"])
    stale.mkdir(parents=True)
    (stale / "src.tgz").write_bytes(b"x")
    first.close()
    conn, gh, http = start(settings)
    try:
        job = db.get_job(conn, running["id"])
        assert (job["status"], job["attempts"], job["error"]) == ("queued", 1, "worker restarted")
        assert settings.work_dir.is_dir() and not stale.exists()
        assert db.migrate(conn) == len(db.MIGRATIONS)  # idempotent
    finally:
        http.close()
        conn.close()


def test_web_and_worker_can_migrate_an_empty_database_at_the_same_time(tmp_path):
    """Both processes start on a fresh volume; neither may die on 'table already exists'."""
    path = tmp_path / "race.db"
    errors = []
    barrier = threading.Barrier(2)

    def boot():
        try:
            barrier.wait(timeout=10)
            conn = db.connect(path)  # the open itself (the WAL switch) is part of the race
            try:
                db.migrate(conn)
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - the point is that nothing is raised
            errors.append(exc)

    threads = [threading.Thread(target=boot) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    check = db.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    check.close()


def test_requeue_running_retires_a_job_when_a_newer_one_is_queued(conn, settings, fake):
    handle_event(conn, "installation", "d-1", install_payload(fake), settings.data)
    running = db.claim_job(conn)
    db.enqueue(conn, "check", 501, "push")  # queued while the first was running
    assert db.requeue_running(conn) == 1
    assert db.get_job(conn, running["id"])["status"] == "failed"
    assert "superseded" in db.get_job(conn, running["id"])["error"]
    assert [j["reason"] for j in db.queued_jobs(conn)] == ["push"]


def test_a_job_that_keeps_killing_the_worker_fails_after_max_attempts(installed, conn):
    """Reviewer scenario 5: a restart used to count no attempt, so a poison repo looped forever."""
    job = db.claim_job(conn)
    for restart in range(1, db.MAX_ATTEMPTS):
        assert db.requeue_running(conn) == 1
        row = db.get_job(conn, job["id"])
        assert (row["status"], row["attempts"]) == ("queued", restart)
        assert db.claim_job(conn)["id"] == job["id"]
    assert db.requeue_running(conn) == 1
    row = db.get_job(conn, job["id"])
    assert (row["status"], row["attempts"]) == ("failed", db.MAX_ATTEMPTS) and db.claim_job(conn) is None


def test_sweep_queues_repos_not_checked_since_the_latest_snapshot(installed, conn, settings, caplog):
    db.upsert_repo(conn, 502, 77, "octo/other", None)  # never checked, no job
    db.upsert_repo(conn, 503, 77, "octo/fresh", None)
    db.upsert_repo(conn, 504, 77, "octo/old", None)
    db.upsert_repo(conn, 505, 77, "octo/gone", None)
    db.update_repo(conn, 503, last_checked_at=db.now())  # checked after the latest snapshot
    db.update_repo(conn, 504, last_checked_at="2026-01-15T00:00:00Z")  # checked before the latest (2026-02-01) snapshot
    db.update_repo(conn, 505, gone=1)
    assert snapshot_stamp(snapshots(settings.data)[-1]) == "2026-02-01T00:00:00Z"
    with caplog.at_level(logging.INFO, logger="apidrift.worker"):
        assert sweep(settings, conn) == 2  # 501 has its install job queued; 503 is fresh; 505 is gone
    queued = {(j["repo_id"], j["reason"]) for j in db.queued_jobs(conn)}
    assert queued == {(501, "install"), (502, "release"), (504, "release")}
    assert any("2 stale repos queued" in r.message for r in caplog.records)
    assert sweep(settings, conn) == 0  # idempotent while those jobs are queued


def test_sweep_prunes_jobs_older_than_thirty_days(installed, conn, settings):
    job = db.claim_job(conn)
    db.finish_job(conn, job["id"], "done")
    conn.execute("UPDATE jobs SET finished_at = ? WHERE id = ?", (db.later(-31 * 86400), job["id"]))
    recent = db.enqueue(conn, "check", 501, "push")
    db.finish_job(conn, db.claim_job(conn)["id"], "failed", "x")
    sweep(settings, conn)
    assert db.get_job(conn, job["id"]) is None and db.get_job(conn, recent)["status"] == "failed"


def test_start_logs_snapshot_count_and_queue_depth(settings, fake, caplog):
    first = db.connect(settings.db_path)
    db.migrate(first)
    handle_event(first, "installation", "d-1", install_payload(fake), settings.data)
    first.close()
    with caplog.at_level(logging.INFO, logger="apidrift.worker"):
        conn, gh, http = start(settings)
    http.close()
    conn.close()
    assert any("worker starting: 2 Stripe snapshots, 1 jobs queued, 1 active repos" in r.message for r in caplog.records)


def test_run_one_logs_claim_and_finish_with_elapsed_time(installed, conn, gh, settings, caplog):
    with caplog.at_level(logging.INFO):
        tick(settings, conn, gh, stripe_http(lambda r: httpx.Response(304)))
    messages = [r.message for r in caplog.records]
    assert any(m.startswith("job 1 claimed (repo 501, reason install, attempt 1)") for m in messages)
    assert any(m.startswith("job 1 (repo 501): tarball ") and " scanned in " in m for m in messages)
    assert any(m.startswith("job 1 (repo 501, install) done in ") and "findings: opened #1" in m for m in messages)
    assert "Stripe poll: 304 unchanged" in messages
