import json
import logging
import re
import shutil
import subprocess
from datetime import datetime, timezone

import httpx
import pytest
from conftest import NEW_VERSION, OLD_VERSION, REPO, SHA, THIRD_VERSION, TOKEN, new_doc, push_payload, save_snapshot, third_doc

from apidrift.app import checker, db, issue
from apidrift.app.checker import CheckError, decide, run_job, run_local, scan_subprocess
from apidrift.app.poller import snapshots
from apidrift.app.webhooks import handle_event
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
    assert installed.issues[1]["title"] == f"Upgrading to Stripe {NEW_VERSION} breaks 3 call sites"
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
    assert installed.issues[1]["state"] == "closed" and installed.comments == [(1, f"No call sites are affected at {SHA}; closing. Baseline is now {NEW_VERSION}.")]
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
    assert installed.issues[1]["title"] == f"Upgrading to Stripe {THIRD_VERSION} breaks 3 call sites"
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
    assert installed.issues[2]["title"] == f"Upgrading to Stripe {THIRD_VERSION} breaks 3 call sites"  # sites, not findings
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


def test_missing_baseline_skips_same_version_snapshots(installed, conn, gh, settings):
    """Reviewer scenario 6: a docs-only commit at the current version must not become the baseline."""
    doc = new_doc()
    doc["info"]["description"] = "docs-only"
    save_snapshot(doc, datetime(2026, 2, 2, tzinfo=timezone.utc))
    db.update_repo(conn, 501, baseline="2000-01-01T000000Z_gone_000000000000.json.gz")
    assert run_job(claim(conn), conn, gh, settings) == "findings: opened #1"
    assert f"against Stripe {OLD_VERSION} -> {NEW_VERSION}" in installed.issues[1]["body"]


def test_release_that_adds_findings_updates_the_issue_and_comments(installed, conn, gh, settings):
    """Reviewer scenario 1: a body edit notifies nobody; the new findings need a comment."""
    assert run_job(claim(conn), conn, gh, settings) == "findings: opened #1"
    save_snapshot(third_doc(), datetime(2026, 3, 1, tzinfo=timezone.utc))
    db.enqueue(conn, "check", 501, "release")
    assert run_job(claim(conn), conn, gh, settings) == "findings: updated #1"
    assert installed.issues[1]["title"] == f"Upgrading to Stripe {THIRD_VERSION} breaks 3 call sites"
    assert installed.comments == [(1, f"Stripe {THIRD_VERSION} adds 1 new finding(s); the issue body is updated.")]
    assert [r.method for r in installed.writes] == ["POST", "PATCH", "POST"]
    # the same findings again at the same version: keep, and nothing is posted
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings) == "findings: #1 unchanged"
    assert len(installed.comments) == 1 and len(installed.writes) == 3


def test_body_change_without_new_findings_or_version_updates_silently(installed, conn, gh, settings):
    run_job(claim(conn), conn, gh, settings)
    installed.sha = "f" * 40  # a push moves the permalinks: the body changes, the findings do not
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings) == "findings: updated #1"
    assert installed.comments == []


def test_suspended_installation_is_skipped_without_a_token(installed, conn, gh, settings):
    db.set_suspended(conn, 77, True)
    assert run_job(claim(conn), conn, gh, settings) == "skipped: installation suspended"
    assert installed.requests == []


# --- failure handling through the worker -----------------------------------------------


def test_tarball_over_the_cap_is_terminal_and_recorded_on_the_repo(installed, conn, gh, settings, monkeypatch):
    """Reviewer scenario 9: an oversize tarball used to be downloaded five times per release."""
    monkeypatch.setattr(checker, "MAX_TARBALL_BYTES", 64)
    job = claim(conn)
    outcome = run_one(job, conn, gh, settings)
    assert outcome.startswith("failed:") and "cap" in outcome
    row = db.get_job(conn, job["id"])
    assert (row["status"], row["attempts"]) == ("failed", 0)
    assert "cap" in row["error"] and not (settings.work_dir / str(job["id"])).exists()
    assert sum(1 for r in installed.requests if r.url.host == "codeload.github.com") == 1
    repo = db.get_repo(conn, 501)
    assert repo["last_result"] == "too_large" and repo["last_checked_at"] is not None
    assert installed.issues == {} and installed.comments == []


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


@pytest.mark.parametrize("status", [403, 404, 410, 422])
def test_terminal_github_errors_fail_the_job_at_once(installed, conn, gh, settings, status):
    installed.responses[("POST", "/app/installations/77/access_tokens")] = httpx.Response(status, json={"message": "no"})
    job = claim(conn)
    assert run_one(job, conn, gh, settings).startswith("failed: GitHub returned")
    row = db.get_job(conn, job["id"])
    assert row["status"] == "failed" and row["attempts"] == 0 and f"returned {status}" in row["error"]


def test_401_from_the_token_endpoint_gets_one_retry_for_clock_skew(installed, conn, gh, settings):
    """Reviewer scenario 7: a skewed clock used to fail every job for good with nothing retrying it."""
    skew = {"message": "'Expiration time' claim ('exp') is too far in the future"}
    installed.responses[("POST", "/app/installations/77/access_tokens")] = httpx.Response(401, json=skew)
    job = claim(conn)
    assert run_one(job, conn, gh, settings).startswith("failed: GitHub returned 401")
    row = db.get_job(conn, job["id"])
    assert (row["status"], row["attempts"]) == ("queued", 1) and row["not_before"] > db.now()
    installed.responses[("POST", "/app/installations/77/access_tokens")] = httpx.Response(401, json=skew)
    run_one(db.claim_job(conn, job_id=job["id"]), conn, gh, settings)
    assert db.get_job(conn, job["id"])["status"] == "failed"
    assert db.get_repo(conn, 501)["gone"] == 0  # a 401 says nothing about the repository


@pytest.mark.parametrize("status", [404, 422])
def test_token_refused_for_the_repo_marks_it_gone_until_github_mentions_it_again(installed, conn, gh, settings, status, caplog):
    installed.responses[("POST", "/app/installations/77/access_tokens")] = httpx.Response(status, json={"message": "no"})
    job = claim(conn)
    with caplog.at_level(logging.WARNING, logger="apidrift.checker"):
        run_one(job, conn, gh, settings)
    assert db.get_repo(conn, 501)["gone"] == 1 and db.active_repos(conn) == []
    assert sum("marking it gone" in r.message for r in caplog.records) == 1
    # a release poll skips it: nothing queued
    save_snapshot(third_doc(), datetime(2026, 3, 1, tzinfo=timezone.utc))
    from apidrift.app.poller import poll_stripe
    body = json.dumps(third_doc()).encode()
    poll_stripe(settings, conn, httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))))
    assert db.queued_jobs(conn) == []
    # a push or an installation event clears the flag
    handle_event(conn, "push", "p-back", push_payload(installed), settings.data)
    assert db.get_repo(conn, 501)["gone"] == 0 and len(db.queued_jobs(conn)) == 1


def test_403_without_rate_limit_headers_is_terminal(installed, conn, gh, settings):
    """Reviewer scenario 7: 'Resource not accessible by integration' is a permission problem, not transient."""
    installed.responses[("GET", "/repos/octo/shop")] = httpx.Response(403, json={"message": "Resource not accessible by integration"})
    job = claim(conn)
    assert run_one(job, conn, gh, settings).startswith("failed: GitHub returned 403")
    row = db.get_job(conn, job["id"])
    assert (row["status"], row["attempts"]) == ("failed", 0)


def test_scan_timeout_is_terminal_and_recorded(installed, conn, gh, settings, monkeypatch):
    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

    monkeypatch.setattr(checker.subprocess, "run", slow)
    job = claim(conn)
    assert run_one(job, conn, gh, settings).startswith("failed: scan exceeded")
    assert db.get_job(conn, job["id"])["status"] == "failed"
    repo = db.get_repo(conn, 501)
    assert repo["last_result"] == "timeout" and repo["last_checked_at"] is not None and installed.issues == {}


def test_unreadable_archive_is_terminal_and_recorded(installed, conn, gh, settings):
    installed.tarball = b"not a tarball"
    job = claim(conn)
    assert run_one(job, conn, gh, settings).startswith("failed: archive could not be read")
    assert db.get_job(conn, job["id"])["status"] == "failed"
    assert db.get_repo(conn, 501)["last_result"] == "bad_archive"


def test_installation_token_is_revoked_after_every_job(installed, conn, gh, settings):
    run_job(claim(conn), conn, gh, settings)
    revokes = [r for r in installed.requests if r.method == "DELETE" and r.url.path == "/installation/token"]
    assert len(revokes) == 1 and revokes[0].headers["authorization"] == f"Bearer {TOKEN}"
    installed.responses[("GET", "/repos/octo/shop")] = httpx.Response(502, json={})
    db.enqueue(conn, "check", 501, "push")
    run_one(claim(conn), conn, gh, settings)  # revoked on the failure path too, and a failed revoke is ignored
    assert sum(1 for r in installed.requests if r.method == "DELETE" and r.url.path == "/installation/token") == 2


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


def test_jobs_fail_for_good_after_eight_attempts(installed, conn, gh, settings):
    """Reviewer scenario 5: five attempts totalled ~30 minutes; now eight, 2 min .. 6 h apart."""
    assert db.MAX_ATTEMPTS == 8
    job_id = db.queued_jobs(conn)[0]["id"]
    for attempt in range(1, 9):
        installed.responses[("GET", "/repos/octo/shop")] = httpx.Response(502, json={})
        run_one(db.claim_job(conn, job_id=job_id), conn, gh, settings)
        row = db.get_job(conn, job_id)
        assert row["attempts"] == attempt and row["status"] == ("failed" if attempt == 8 else "queued")
    assert db.claim_job(conn, job_id=job_id) is None
    waits = [db.backoff(n) for n in range(1, db.MAX_ATTEMPTS)]
    assert waits == [120, 240, 480, 960, 1920, 3840, 7680] and db.backoff(12) == 6 * 3600
