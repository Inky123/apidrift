import re
import shutil
import subprocess
from datetime import datetime, timezone

import httpx
import pytest
from conftest import NEW_VERSION, OLD_VERSION, REPO, SHA, THIRD_VERSION, save_snapshot, third_doc

from apidrift.app import checker, db, issue
from apidrift.app.checker import CheckError, decide, run_job, run_local, scan_subprocess
from apidrift.app.poller import snapshots
from apidrift.app.worker import run_one

TABLE = [
    (None, True, {}, "create"),
    ("open", True, {"body_changed": True}, "update"),
    ("open", True, {"body_changed": False}, "keep"),
    ("closed_by_us", True, {}, "reopen"),
    ("closed_by_us", True, {"body_changed": False}, "reopen"),
    ("closed_by_user", True, {"version_changed": False, "gained": True}, "silence"),
    ("closed_by_user", True, {"version_changed": True, "gained": False}, "silence"),
    ("closed_by_user", True, {"version_changed": True, "gained": True}, "fresh"),
    ("open", False, {}, "close"),
    (None, False, {}, "none"),
    ("closed_by_us", False, {}, "none"),
    ("closed_by_user", False, {}, "none"),
]


@pytest.mark.parametrize("state,findings,flags,expected", TABLE)
def test_transition_table(state, findings, flags, expected):
    kwargs = {"body_changed": False, "version_changed": False, "gained": False, **flags}
    assert decide(state, findings, **kwargs) == expected


def claim(conn):
    job = db.claim_job(conn)
    assert job is not None
    return job


def fixed_tree(tmp_path):
    """A copy of the fixture repo that no longer sends ``description``."""
    tree = tmp_path / "fixed"
    shutil.copytree(REPO, tree)
    for path in tree.iterdir():
        path.write_text(re.sub(r",\s*description[:=][^,)\n]*", "", path.read_text()))
    return tree


# --- the pipeline -----------------------------------------------------------------


def test_run_local_payload_shape(data_dir):
    check = run_local(REPO, "previous", "latest")
    payload = check.payload()
    assert check.breaking and payload["call_sites"] == 6 and payload["summary"]["breaking"] == 3
    assert (payload["old_version"], payload["new_version"]) == (OLD_VERSION, NEW_VERSION)
    assert not run_local(REPO, "latest", "latest").breaking


def test_scan_subprocess_never_imports_from_the_tree(data_dir, tmp_path):
    tree = tmp_path / "planted"
    shutil.copytree(REPO, tree)
    (tree / "json.py").write_text("raise SystemExit('planted module imported')\n")
    (tree / "apidrift.py").write_text("raise SystemExit('planted module imported')\n")
    old, new = (p.name for p in snapshots(data_dir))
    payload = scan_subprocess(tree, old, new, data_dir, max_mem_mb=0)
    assert len(issue.breaking(payload)) == 3 and payload["new_version"] == NEW_VERSION


def test_scan_subprocess_failures_are_reported_without_paths(data_dir):
    with pytest.raises(CheckError, match=r"scan failed \(exit 1\)") as caught:
        scan_subprocess(REPO, "nope", "latest", data_dir, max_mem_mb=0)
    assert "/" not in str(caught.value).split(":", 2)[-1]


# --- run_job end to end -------------------------------------------------------------


def test_first_run_creates_an_issue_with_permalinks(installed, conn, gh, settings):
    assert run_job(claim(conn), conn, gh, settings) == "findings: opened #1"
    body = installed.issues[1]["body"]
    for name in ("server.py", "server.js", "server.rb"):
        assert f"blob/{SHA}/{name}#L" in body
    assert installed.issues[1]["title"] == f"Stripe {NEW_VERSION} breaks 3 Stripe call sites"
    assert issue.marker_in(body) is not None
    repo = db.get_repo(conn, 501)
    assert (repo["issue_number"], repo["issue_state"], repo["last_result"], repo["last_sha"]) == (1, "open", "findings", SHA)
    assert repo["issue_version"] == NEW_VERSION and repo["baseline"].startswith("2026-01-01T")  # broken: baseline stays
    assert installed.token_requests[0]["repository_ids"] == [501]
    assert not settings.work_dir.exists() or not any(settings.work_dir.iterdir())


def test_identical_second_run_makes_zero_write_requests(installed, conn, gh, settings):
    run_job(claim(conn), conn, gh, settings)
    writes = len(installed.writes)
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings) == "findings: #1 unchanged"
    assert len(installed.writes) == writes and len(installed.issues) == 1


def test_fixed_fixture_closes_the_issue_and_advances_the_baseline(installed, conn, gh, settings, tmp_path):
    run_job(claim(conn), conn, gh, settings)
    installed.tree = fixed_tree(tmp_path)
    db.enqueue(conn, "check", 501, "push")
    outcome = run_job(claim(conn), conn, gh, settings)
    assert outcome.startswith("clean: closed #1; baseline advanced to 2026-02-01T")
    assert installed.issues[1]["state"] == "closed" and installed.comments == [(1, f"Clean at {SHA}")]
    repo = db.get_repo(conn, 501)
    assert repo["baseline"] == snapshots(settings.data)[-1].name
    assert (repo["issue_state"], repo["last_result"]) == ("closed_by_us", "clean")


def test_reopens_after_our_own_close(installed, conn, gh, settings, tmp_path):
    run_job(claim(conn), conn, gh, settings)
    installed.tree = fixed_tree(tmp_path)
    db.enqueue(conn, "check", 501, "push")
    run_job(claim(conn), conn, gh, settings)
    installed.tree = REPO
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).startswith("clean")  # baseline moved: nothing new until a release
    save_snapshot(third_doc(), datetime(2026, 3, 1, tzinfo=timezone.utc))
    db.enqueue(conn, "check", 501, "release")
    assert run_job(claim(conn), conn, gh, settings) == "findings: reopened #1"
    assert installed.issues[1]["state"] == "open" and len(installed.issues) == 1
    assert installed.issues[1]["title"] == f"Stripe {THIRD_VERSION} breaks 3 Stripe call sites"
    assert db.get_repo(conn, 501)["issue_state"] == "open"


def test_a_human_close_is_respected(installed, conn, gh, settings):
    run_job(claim(conn), conn, gh, settings)
    installed.close_by_human(1)
    writes = len(installed.writes)
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings) == "findings: #1 was closed by a person; staying silent"
    assert len(installed.writes) == writes and len(installed.issues) == 1
    assert db.get_repo(conn, 501)["issue_state"] == "closed_by_user"


def test_fresh_issue_when_a_later_release_adds_findings(installed, conn, gh, settings):
    run_job(claim(conn), conn, gh, settings)
    installed.close_by_human(1)
    save_snapshot(third_doc(), datetime(2026, 3, 1, tzinfo=timezone.utc))
    db.enqueue(conn, "check", 501, "release")
    assert run_job(claim(conn), conn, gh, settings) == "findings: opened #2 superseding #1"
    assert installed.issues[2]["body"].startswith("Supersedes #1,")
    assert installed.issues[2]["title"] == f"Stripe {THIRD_VERSION} breaks 6 Stripe call sites"
    repo = db.get_repo(conn, 501)
    assert (repo["issue_number"], repo["issue_state"], repo["issue_version"]) == (2, "open", THIRD_VERSION)


def test_human_close_stays_silent_when_a_release_adds_nothing(installed, conn, gh, settings):
    run_job(claim(conn), conn, gh, settings)
    installed.close_by_human(1)
    bumped = third_doc()
    bumped["paths"]["/v1/charges"]["post"]["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]["properties"]["currency"] = {"type": "string"}
    save_snapshot(bumped, datetime(2026, 3, 1, tzinfo=timezone.utc))  # same findings, new version
    db.enqueue(conn, "check", 501, "release")
    assert "staying silent" in run_job(claim(conn), conn, gh, settings)
    assert len(installed.issues) == 1


@pytest.mark.parametrize("field", ["has_issues", "archived"])
def test_repos_without_issues_or_archived_are_skipped(installed, conn, gh, settings, field):
    installed.repo[field] = field == "archived"
    assert run_job(claim(conn), conn, gh, settings).startswith("skipped")
    assert db.get_repo(conn, 501)["last_result"] == "skipped"
    assert installed.writes == [] and not any(r.url.host == "codeload.github.com" for r in installed.requests)


def test_adopts_an_existing_bot_issue_instead_of_creating(installed, conn, gh, settings):
    installed.add_issue("by a person " + issue.marker("0" * 64), login="zach")
    installed.add_issue("stale bot issue\n" + issue.marker("1" * 64))
    assert run_job(claim(conn), conn, gh, settings) == "findings: updated #2"
    assert [(r.method, r.url.path) for r in installed.writes] == [("PATCH", "/repos/octo/shop/issues/2")]
    assert db.get_repo(conn, 501)["issue_number"] == 2 and f"blob/{SHA}/" in installed.issues[2]["body"]


def test_adopted_issue_closed_by_a_person_is_left_alone(installed, conn, gh, settings):
    installed.add_issue("old " + issue.marker("1" * 64), state="closed", closed_by="zach")
    assert "staying silent" in run_job(claim(conn), conn, gh, settings)
    assert installed.writes == []


def test_missing_baseline_falls_back_to_the_previous_snapshot(installed, conn, gh, settings):
    db.update_repo(conn, 501, baseline="2000-01-01T000000Z_gone_000000000000.json.gz")
    assert run_job(claim(conn), conn, gh, settings) == "findings: opened #1"


def test_suspended_installation_is_skipped_without_a_token(installed, conn, gh, settings):
    db.set_suspended(conn, 77, True)
    assert run_job(claim(conn), conn, gh, settings) == "skipped: installation suspended"
    assert installed.requests == []


# --- failure handling through the worker -----------------------------------------------


def test_tarball_over_the_cap_fails_the_job_and_leaves_no_work_dir(installed, conn, gh, settings, monkeypatch):
    monkeypatch.setattr(checker, "MAX_TARBALL_BYTES", 64)
    job = claim(conn)
    outcome = run_one(job, conn, gh, settings)
    assert outcome.startswith("failed:") and "cap" in outcome
    row = db.get_job(conn, job["id"])
    assert (row["status"], row["attempts"]) == ("queued", 1) and row["not_before"] > db.now()
    assert "cap" in row["error"] and not (settings.work_dir / str(job["id"])).exists()
    assert installed.issues == {}


def test_error_text_is_scrubbed_before_it_is_stored(installed, conn, gh, settings):
    installed.codeload_error = httpx.ConnectError("could not reach https://codeload.github.com/x?token=CODELOADSECRET")
    job = claim(conn)
    run_one(job, conn, gh, settings)
    error = db.get_job(conn, job["id"])["error"]
    assert "CODELOADSECRET" not in error and "ghs_testtoken" not in error and "token=***" in error


def test_rate_limit_requeues_at_the_reset_without_counting_an_attempt(installed, conn, gh, settings):
    installed.responses[("GET", "/repos/octo/shop")] = httpx.Response(403, headers={"retry-after": "120"}, json={})
    job = claim(conn)
    assert run_one(job, conn, gh, settings) == "rate limited"
    row = db.get_job(conn, job["id"])
    assert (row["status"], row["attempts"]) == ("queued", 0) and row["not_before"] >= db.later(100)


@pytest.mark.parametrize("status", [401, 404, 410, 422])
def test_terminal_github_errors_fail_the_job_at_once(installed, conn, gh, settings, status):
    installed.responses[("POST", "/app/installations/77/access_tokens")] = httpx.Response(status, json={"message": "no"})
    job = claim(conn)
    assert run_one(job, conn, gh, settings).startswith("failed: GitHub returned")
    row = db.get_job(conn, job["id"])
    assert row["status"] == "failed" and row["attempts"] == 0 and f"returned {status}" in row["error"]


def test_server_errors_still_back_off(installed, conn, gh, settings):
    installed.responses[("GET", "/repos/octo/shop")] = httpx.Response(502, json={})
    job = claim(conn)
    run_one(job, conn, gh, settings)
    assert (db.get_job(conn, job["id"])["status"], db.get_job(conn, job["id"])["attempts"]) == ("queued", 1)


def test_scan_child_gets_a_minimal_environment(data_dir, monkeypatch):
    seen = {}
    monkeypatch.setenv("APIDRIFT_GITHUB_WEBHOOK_SECRET", "hunter2")
    monkeypatch.setenv("APIDRIFT_GITHUB_APP_ID", "1")

    def fake_run(cmd, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(cmd, 0, stdout=b'{"impacts": [], "new_version": "x"}', stderr=b"")

    monkeypatch.setattr(checker.subprocess, "run", fake_run)
    scan_subprocess(REPO, "a", "b", data_dir)
    assert seen["APIDRIFT_DATA"] == str(data_dir) and "PATH" in seen
    assert not any(key.startswith("APIDRIFT_GITHUB_") for key in seen)


def test_jobs_fail_for_good_after_five_attempts(installed, conn, gh, settings, monkeypatch):
    monkeypatch.setattr(checker, "MAX_TARBALL_BYTES", 64)
    job_id = db.queued_jobs(conn)[0]["id"]
    for attempt in range(1, 6):
        run_one(db.claim_job(conn, job_id=job_id), conn, gh, settings)
        row = db.get_job(conn, job_id)
        assert row["attempts"] == attempt and row["status"] == ("failed" if attempt == 5 else "queued")
    assert db.claim_job(conn, job_id=job_id) is None
