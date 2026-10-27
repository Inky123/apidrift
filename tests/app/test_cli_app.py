import json

import pytest
from conftest import NEW_VERSION, REPO, SECRET, install_payload

from apidrift.app import db
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
        "https://apidrift.test/github/webhook", "Webhook secret:", "Contents: Read-only", "Issues: Read and write",
        "Metadata: Read-only", "Push only", "Any account", "APIDRIFT_GITHUB_APP_ID: MISSING", "private key: file not found",
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
