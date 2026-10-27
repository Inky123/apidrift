import json
import threading

import httpx
from conftest import SHA, install_payload, third_doc

from apidrift.app import db
from apidrift.app.webhooks import handle_event
from apidrift.app.worker import start, tick


def stripe_http(responder):
    return httpx.Client(transport=httpx.MockTransport(responder))


def test_tick_polls_stripe_then_drains_the_queue(installed, conn, gh, settings):
    polls = []

    def respond(request):
        polls.append(request.url.host)
        return httpx.Response(304)

    assert tick(settings, conn, gh, stripe_http(respond)) == 1
    assert polls == ["raw.githubusercontent.com"]
    assert installed.issues[1]["title"].endswith("breaks 3 Stripe call sites")
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
        assert (job["status"], job["attempts"], job["error"]) == ("queued", 0, "worker restarted")
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
