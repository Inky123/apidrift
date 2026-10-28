import json
import logging

import pytest
from conftest import NEW_VERSION, REPO, SECRET, install_payload

from apidrift.app import commands, db
from apidrift.app.checker import run_local
from apidrift.app.config import REQUIRED
from apidrift.app.webhooks import handle_event
from apidrift.cli import main


@pytest.fixture
def no_config(monkeypatch, tmp_path):
    for name in REQUIRED:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here


@pytest.fixture
def configured(monkeypatch, settings):
    monkeypatch.setenv("APIDRIFT_GITHUB_APP_ID", settings.github_app_id)
    monkeypatch.setenv("APIDRIFT_GITHUB_APP_SLUG", settings.github_app_slug)
    monkeypatch.setenv("APIDRIFT_GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("APIDRIFT_GITHUB_PRIVATE_KEY_PATH", str(settings.github_private_key_path))
    monkeypatch.setenv("APIDRIFT_HOST", settings.host)
    monkeypatch.chdir(settings.data.parent)
    return settings


def test_app_setup_lists_the_permissions_and_urls(no_config, monkeypatch, capsys):
    monkeypatch.setenv("APIDRIFT_HOST", "apidrift.test")
    assert main(["app-setup"]) == 1  # nothing configured yet
    out = capsys.readouterr().out
    for needle in (
        "https://apidrift.test/", "https://apidrift.test/installed", "Redirect on update",
        "https://apidrift.test/github/webhook", "Webhook secret:", "Contents: Read and write", "Issues: Read and write",
        "Pull requests: Read and write", "Metadata: Read-only", "Push only", "Any account", "APIDRIFT_GITHUB_APP_ID: MISSING", "private key: file not found",
    ):
        assert needle in out


def test_app_setup_accepts_a_complete_configuration(configured, capsys):
    assert main(["app-setup"]) == 0
    out = capsys.readouterr().out
    assert "private key: OK" in out and "configuration OK" in out and "MISSING" not in out


def test_check_local_json_matches_the_pipeline(data_dir, capsys):
    assert main(["check", "--local", str(REPO), "--json", "--max-mem-mb", "0"]) == 1
    payload = json.loads(capsys.readouterr().out)
    expected = run_local(REPO, "previous", "latest").payload()
    assert payload["impacts"] == expected["impacts"] and payload["summary"] == expected["summary"]
    assert payload["call_sites"] == 6 and payload["new_version"] == NEW_VERSION


def test_check_local_text_output_and_clean_exit(data_dir, capsys):
    assert main(["check", "--local", str(REPO), "--max-mem-mb", "0"]) == 1
    out = capsys.readouterr().out
    assert "apidrift impact:" in out and "server.py:11" in out
    assert main(["check", "--local", str(REPO), "--old", "latest", "--new", "latest", "--max-mem-mb", "0"]) == 0


@pytest.mark.parametrize("argv", [["serve"], ["worker", "--once"], ["check", "--all"]])
def test_serve_and_worker_refuse_to_start_without_configuration(no_config, argv):
    with pytest.raises(SystemExit) as caught:
        main(argv)
    assert "missing configuration" in str(caught.value) and "APIDRIFT_GITHUB_APP_ID" in str(caught.value)


def test_settings_repr_never_shows_the_secret(settings):
    assert SECRET not in repr(settings) and SECRET not in str(settings) and "apidrift-test" in repr(settings)


def test_unreadable_private_key_is_refused_with_a_one_line_error(no_config, monkeypatch, tmp_path):
    for name in REQUIRED:
        monkeypatch.setenv(name, "x")
    pem = tmp_path / "locked.pem"
    pem.write_bytes(b"secret")
    pem.chmod(0)
    monkeypatch.setenv("APIDRIFT_GITHUB_PRIVATE_KEY_PATH", str(pem))
    with pytest.raises(SystemExit, match="cannot read the private key"):
        main(["worker", "--once"])


def test_missing_private_key_file_is_refused(no_config, monkeypatch, tmp_path):
    for name in REQUIRED:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("APIDRIFT_GITHUB_PRIVATE_KEY_PATH", str(tmp_path / "missing.pem"))
    with pytest.raises(SystemExit, match="does not point at a file"):
        main(["worker", "--once"])


def test_check_repo_and_all_queue_manual_jobs(configured, fake, capsys):
    conn = db.connect(configured.db_path)
    db.migrate(conn)
    handle_event(conn, "installation", "d-1", install_payload(fake), configured.data)
    while db.claim_job(conn) is not None:
        pass
    assert main(["check", "--repo", "octo/shop"]) == 0
    assert "queued check 2 for octo/shop" in capsys.readouterr().out
    assert main(["check", "--all"]) == 0
    assert "queued 0 checks" in capsys.readouterr().out  # already queued
    assert [(j["id"], j["reason"]) for j in db.queued_jobs(conn)] == [(2, "manual")]
    with pytest.raises(SystemExit, match="not an installed repository"):
        main(["check", "--repo", "octo/nope"])
    conn.close()


def test_check_job_runs_the_claimed_job_in_process(configured, fake, capsys):
    conn = db.connect(configured.db_path)
    db.migrate(conn)
    handle_event(conn, "installation", "d-1", install_payload(fake), configured.data)
    job_id = db.queued_jobs(conn)[0]["id"]
    assert main(["check", "--job", str(job_id)]) == 0
    assert "failed: network access is disabled in tests" in capsys.readouterr().out
    row = db.get_job(conn, job_id)
    assert (row["status"], row["attempts"]) == ("queued", 1)
    with pytest.raises(SystemExit, match="is not queued"):
        main(["check", "--job", "999"])
    conn.close()


def test_check_local_never_reads_a_dotenv(data_dir, monkeypatch, tmp_path, capsys):
    """The sandboxed child gets APIDRIFT_DATA from its parent; a .env in the cwd must stay unread."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("APIDRIFT_GITHUB_WEBHOOK_SECRET=hunter2\nAPIDRIFT_DATA=/nonexistent\n")
    monkeypatch.delenv("APIDRIFT_GITHUB_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(commands, "load_dotenv", lambda *a, **k: pytest.fail("_check_local called load_dotenv"))
    assert main(["check", "--local", str(REPO), "--json", "--max-mem-mb", "0"]) == 1
    assert json.loads(capsys.readouterr().out)["new_version"] == NEW_VERSION
    import os
    assert "APIDRIFT_GITHUB_WEBHOOK_SECRET" not in os.environ


def test_serve_and_worker_still_load_dotenv(no_config, monkeypatch, tmp_path, settings):
    lines = [
        f"APIDRIFT_GITHUB_APP_ID={settings.github_app_id}", f"APIDRIFT_GITHUB_APP_SLUG={settings.github_app_slug}",
        f"APIDRIFT_GITHUB_WEBHOOK_SECRET={SECRET}", f"APIDRIFT_GITHUB_PRIVATE_KEY_PATH={settings.github_private_key_path}",
        f"APIDRIFT_DATA={settings.data}",
    ]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n")
    seen = {}
    monkeypatch.setattr("apidrift.app.worker.loop", lambda s, once=False: seen.update(data=s.data, once=once))
    assert main(["worker", "--once"]) == 0 and seen == {"data": settings.data, "once": True}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: seen.update(port=kw["port"]))
    assert main(["serve", "--port", "8123"]) == 0 and seen["port"] == 8123


def test_healthz_probes_are_filtered_from_the_access_log():
    keep = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("1.2.3.4:1", "GET", "/", "1.1", 200), None)
    drop = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("10.0.0.1:2", "GET", "/healthz", "1.1", 200), None)
    assert commands.HealthzFilter().filter(keep) and not commands.HealthzFilter().filter(drop)


def test_check_stale_queues_repos_behind_the_latest_snapshot(configured, fake, capsys):
    conn = db.connect(configured.db_path)
    db.migrate(conn)
    handle_event(conn, "installation", "d-1", install_payload(fake), configured.data)
    db.finish_job(conn, db.claim_job(conn)["id"], "failed", "gave up")
    db.upsert_repo(conn, 502, 77, "octo/fresh", None)
    db.update_repo(conn, 502, last_checked_at=db.now())
    assert main(["check", "--stale"]) == 0
    assert "queued 1 checks for stale repositories" in capsys.readouterr().out
    assert [(j["repo_id"], j["reason"]) for j in db.queued_jobs(conn)] == [(501, "release")]
    conn.close()


def test_status_prints_counts_jobs_and_the_last_poll(configured, fake, capsys):
    conn = db.connect(configured.db_path)
    db.migrate(conn)
    handle_event(conn, "installation", "d-1", install_payload(fake), configured.data)
    db.upsert_repo(conn, 502, 77, "octo/other", None)
    db.update_repo(conn, 501, issue_state="open", last_result="findings")
    db.update_repo(conn, 502, last_result="too_large")
    db.finish_job(conn, db.claim_job(conn)["id"], "failed", "tarball exceeds the cap")
    db.enqueue(conn, "check", 502, "manual")
    db.set_meta(conn, last_poll_at="2026-10-27T12:00:00Z", last_poll_status="304", latest_snapshot="snap.json.gz")
    summary = db.status_summary(conn)
    assert summary["installations"] == 1 and summary["repos"] == {"findings": 1, "too_large": 1}
    assert summary["open_issues"] == 1 and summary["jobs"] == {"queued": 1, "running": 0, "done": 0, "failed": 1}
    assert summary["job_errors"] == [{"id": 1, "repo_id": 501, "status": "failed", "error": "tarball exceeds the cap"}]
    assert summary["last_poll_status"] == "304" and summary["latest_snapshot"] == "snap.json.gz"
    conn.close()
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    for needle in (
        "installations: 1 active, 0 suspended", "repos: 1 findings, 1 too_large", "open issues: 1",
        "jobs (last 24 h): 1 queued, 0 running, 0 done, 1 failed", "job 1 repo 501 failed: tarball exceeds the cap",
        "last poll: 2026-10-27T12:00:00Z (304)", "latest snapshot: snap.json.gz",
    ):
        assert needle in out, needle


def test_status_on_an_empty_database(configured, capsys):
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "repos: none" in out and "last poll: never (no status)" in out and "latest snapshot: none" in out
