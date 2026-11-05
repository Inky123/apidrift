"""The release pass end to end: the worker polls a new Stripe release and answers each repo from its index.

Every test starts from a real full check (fake GitHub, the fixture repo), so the index under test is the one
the checker wrote. ``own_child`` makes that check run this checkout's scan code (see indexing.py)."""

import json
import logging
import re

import httpx
import pytest
from conftest import NEW_VERSION, SHA, THIRD_VERSION, push_payload, third_doc
from indexing import FIFTH_VERSION, FOURTH_VERSION, checked, clean_tree, latest, own_child, poll, release  # noqa: F401

from apidrift.app import checker, db, inventory, releasepass
from apidrift.app.web import create_app
from apidrift.app.webhooks import handle_event
from apidrift.app.worker import sweep

STAMPS = {"last_checked_at", "updated_at"}


@pytest.fixture
def indexed(installed, conn, gh, settings, own_child):  # noqa: F811
    """The fixture repo after its first full check: issue #1 open with three findings, and an index."""
    assert checked(conn, gh, settings) == 1
    assert inventory.get(conn, 501)["sha"] == SHA and installed.issues[1]["state"] == "open"
    db.update_repo(conn, 501, last_checked_at="2026-02-15T00:00:00Z")  # before any release the tests poll
    return installed


def row(conn, repo_id=501) -> dict:
    return {k: v for k, v in dict(db.get_repo(conn, repo_id)).items() if k not in STAMPS}


def last_pass(conn) -> dict:
    return json.loads(db.get_meta(conn, "last_release_pass"))


def queued(conn) -> list[tuple[int, str]]:
    return [(j["repo_id"], j["reason"]) for j in db.queued_jobs(conn)]


# --- the three outcomes -------------------------------------------------------------------------


def test_a_release_that_changes_nothing_for_the_repo_makes_no_github_call(indexed, conn, settings, caplog):
    before, requests = row(conn), len(indexed.requests)
    with caplog.at_level(logging.DEBUG):
        path = poll(settings, conn, release("unrelated"))
    assert path is not None and queued(conn) == [] and len(indexed.requests) == requests
    assert row(conn) == before  # the issue, its fingerprint, the baseline: all as the full check left them
    assert db.get_repo(conn, 501)["last_checked_at"] > "2026-02-15T00:00:00Z"
    header = inventory.get(conn, 501)
    assert (header["checked"], header["checked_by"], header["sha"]) == (path.name, "index", SHA)
    line = next(r.message for r in caplog.records if r.message.startswith("release "))
    assert re.fullmatch(rf"release {re.escape(THIRD_VERSION)}: 1 repos checked from the index in \d+ ms, 0 need a full check, 0 had no index", line)
    assert {k: last_pass(conn)[k] for k in ("release", "same", "differ", "none", "queued")} == {
        "release": THIRD_VERSION, "same": 1, "differ": 0, "none": 0, "queued": 0}
    assert sweep(settings, conn) == 0  # the repo counts as checked for this release
    ours = [r.getMessage() for r in caplog.records if r.name in ("apidrift.release", "apidrift.index")]
    assert ours and not any("octo/shop" in m or "server." in m for m in ours)  # logs carry repo ids only


def test_a_release_with_a_new_finding_queues_the_full_check_which_updates_the_issue(indexed, conn, gh, settings):
    writes = len(indexed.writes)
    path = poll(settings, conn, third_doc())
    assert queued(conn) == [(501, "release")] and len(indexed.writes) == writes  # the pass itself wrote nothing
    assert last_pass(conn)["differ"] == 1 and inventory.get(conn, 501)["checked"] != path.name
    assert checked(conn, gh, settings) == 1
    repo = db.get_repo(conn, 501)
    assert repo["issue_version"] == THIRD_VERSION and len(json.loads(repo["issue_items"])) == 2
    assert indexed.comments == [(1, f"Stripe {THIRD_VERSION} adds 1 new finding(s); the issue body is updated.")]
    header = inventory.get(conn, 501)
    assert (header["checked"], header["checked_by"]) == (path.name, "full")


def test_findings_that_are_gone_queue_the_full_check_which_closes_the_issue(indexed, conn, gh, settings):
    path = poll(settings, conn, release("restored"))
    assert queued(conn) == [(501, "release")]
    checked(conn, gh, settings)
    repo = db.get_repo(conn, 501)
    assert (repo["issue_state"], repo["last_result"], repo["baseline"]) == ("closed_by_us", "clean", path.name)


def test_a_clean_repo_stays_clean_and_its_baseline_advances_as_after_a_full_check(installed, conn, gh, settings, own_child, tmp_path):  # noqa: F811
    installed.tree = clean_tree(tmp_path)
    checked(conn, gh, settings)
    assert db.get_repo(conn, 501)["last_result"] == "clean" and installed.issues == {}
    requests = len(installed.requests)
    path = poll(settings, conn, release("unrelated"))
    assert queued(conn) == [] and len(installed.requests) == requests
    after_pass = row(conn)
    assert (after_pass["baseline"], after_pass["last_result"], after_pass["issue_number"]) == (path.name, "clean", None)
    db.enqueue(conn, "check", 501, "manual")  # the full check of the same commit agrees with every column
    checked(conn, gh, settings)
    assert db.get_job(conn, 2)["status"] == "done" and row(conn) == after_pass and installed.issues == {}


def test_a_clean_repo_that_a_release_breaks_gets_the_full_check_and_its_issue(installed, conn, gh, settings, own_child, tmp_path):  # noqa: F811
    installed.tree = clean_tree(tmp_path)
    checked(conn, gh, settings)
    poll(settings, conn, third_doc())  # currency goes: the clean tree still sends it
    assert queued(conn) == [(501, "release")] and last_pass(conn)["differ"] == 1
    checked(conn, gh, settings)
    assert installed.issues[1]["state"] == "open" and db.get_repo(conn, 501)["last_result"] == "findings"


def test_an_issue_a_person_closed_stays_silent_until_a_release_gains_a_finding(indexed, conn, gh, settings):
    indexed.close_by_human(1)
    db.enqueue(conn, "check", 501, "push")
    checked(conn, gh, settings)  # a full check notices the close
    assert db.get_repo(conn, 501)["issue_state"] == "closed_by_user"
    requests = len(indexed.requests)
    first = release("unrelated")
    poll(settings, conn, first)
    assert queued(conn) == [] and len(indexed.requests) == requests and last_pass(conn)["same"] == 1
    poll(settings, conn, release("currency", version=FOURTH_VERSION, base=first))
    assert queued(conn) == [(501, "release")]
    checked(conn, gh, settings)
    assert db.get_repo(conn, 501)["issue_number"] == 2 and indexed.issues[2]["state"] == "open"


def test_an_issue_body_that_no_longer_matches_the_index_is_not_called_the_same(indexed, conn, settings):
    db.update_repo(conn, 501, issue_body_sha="0" * 64)
    poll(settings, conn, release("unrelated"))
    assert queued(conn) == [(501, "release")] and last_pass(conn)["differ"] == 1


# --- failure modes ------------------------------------------------------------------------------


def test_a_release_arriving_while_a_push_check_is_queued_leaves_that_one_job_to_cover_it(indexed, conn, gh, settings):
    handle_event(conn, "push", "p-1", push_payload(indexed), settings.data)
    path = poll(settings, conn, third_doc())
    assert queued(conn) == [(501, "push")] and last_pass(conn)["queued"] == 1
    assert checked(conn, gh, settings) == 1  # it reads the snapshots when it runs: the new release is covered
    assert db.get_repo(conn, 501)["issue_version"] == THIRD_VERSION and inventory.get(conn, 501)["checked"] == path.name


def test_a_release_arriving_while_a_check_is_running_queues_a_full_check_behind_it(indexed, conn, settings):
    db.enqueue(conn, "check", 501, "push")
    assert db.claim_job(conn)["status"] == "running"  # it may have read the snapshot list before the release
    poll(settings, conn, release("unrelated"))
    assert queued(conn) == [(501, "release")] and last_pass(conn)["none"] == 1


def test_an_index_for_another_commit_than_the_last_checked_one_is_not_used(indexed, conn, settings, caplog):
    db.update_repo(conn, 501, last_sha="b" * 40)
    with caplog.at_level(logging.INFO, logger="apidrift.release"):
        poll(settings, conn, release("unrelated"))
    assert queued(conn) == [(501, "release")] and last_pass(conn)["none"] == 1
    assert "repo 501: none (the index is for another commit)" in [r.message for r in caplog.records]


def test_a_check_that_failed_after_the_index_was_written_sends_the_release_to_the_full_check(indexed, conn, gh, settings):
    """The push moved the branch, its check failed for good: last_sha and the index still agree, on old code."""
    indexed.sha = "b" * 40
    indexed.responses[("GET", "/repos/octo/shop")] = httpx.Response(403, json={"message": "Resource not accessible by integration"})
    db.enqueue(conn, "check", 501, "push")
    checked(conn, gh, settings)
    assert db.get_job(conn, 2)["status"] == "failed"
    assert db.get_repo(conn, 501)["last_sha"] == inventory.get(conn, 501)["sha"] == SHA
    poll(settings, conn, release("unrelated"))
    assert queued(conn) == [(501, "release")] and last_pass(conn)["none"] == 1
    assert inventory.get(conn, 501) is None  # dropped, so the verdict does not depend on the job row surviving


def test_the_sweep_drops_such_an_index_before_it_prunes_the_failed_job(indexed, conn, gh, settings):
    indexed.responses[("GET", "/repos/octo/shop")] = httpx.Response(403, json={"message": "no"})
    db.enqueue(conn, "check", 501, "push")
    checked(conn, gh, settings)
    conn.execute("UPDATE jobs SET finished_at = ?", (db.later(-31 * 86400),))
    db.update_repo(conn, 501, last_checked_at=db.now())
    sweep(settings, conn)
    assert db.get_job(conn, 2) is None and inventory.get(conn, 501) is None
    assert conn.execute("SELECT count(*) FROM index_sites").fetchone()[0] == 0


def test_a_repo_whose_last_check_ended_in_a_recorded_failure_takes_the_full_check(indexed, conn, gh, settings, monkeypatch):
    monkeypatch.setattr(checker, "MAX_TARBALL_BYTES", 64)
    db.enqueue(conn, "check", 501, "push")
    checked(conn, gh, settings)
    assert db.get_repo(conn, 501)["last_result"] == "too_large"
    poll(settings, conn, release("unrelated"))
    assert queued(conn) == [(501, "release")] and last_pass(conn)["none"] == 1


def test_calls_that_resolve_differently_against_the_new_spec_get_the_full_check(indexed, conn, gh, settings, caplog):
    """The index stores resolved endpoints; when the release's SDK metadata sends a call elsewhere, it is stale."""
    with caplog.at_level(logging.INFO, logger="apidrift.release"):
        poll(settings, conn, release("repick"))
    assert queued(conn) == [(501, "release")] and last_pass(conn)["none"] == 1
    assert "repo 501: none (calls resolve differently against this release)" in [r.message for r in caplog.records]
    checked(conn, gh, settings)  # the full check re-scans and writes an index that holds for this release
    assert {s["path"] for s in inventory.sites(conn, 501) if s["language"] == "python"} == {"/v1/billing", "/v1/charges/{charge}"}
    assert poll(settings, conn, release("unrelated", "repick", version=FOURTH_VERSION)) is not None
    assert queued(conn) == [] and last_pass(conn)["same"] == 1


def test_an_endpoint_the_release_removes_is_found_from_the_index_and_reported_by_the_full_check(indexed, conn, gh, settings):
    poll(settings, conn, release("endpoint_removed"))
    assert queued(conn) == [(501, "release")] and last_pass(conn)["differ"] == 1
    before = set(json.loads(db.get_repo(conn, 501)["issue_items"]))
    checked(conn, gh, settings)
    assert len(set(json.loads(db.get_repo(conn, 501)["issue_items"])) - before) == 1  # GET /v1/charges/{charge} removed


def test_two_releases_in_quick_succession(indexed, conn, gh, settings):
    first = release("unrelated")
    poll(settings, conn, first)  # nothing for this repo
    assert queued(conn) == []
    second = release("currency", version=FOURTH_VERSION, base=first)
    poll(settings, conn, second)  # a new finding: the full check is queued
    assert queued(conn) == [(501, "release")]
    path = poll(settings, conn, release("required", version=FIFTH_VERSION, base=second))  # arrives before it ran
    assert queued(conn) == [(501, "release")] and last_pass(conn)["queued"] == 1
    assert checked(conn, gh, settings) == 1  # one check, against the newest release
    assert db.get_repo(conn, 501)["issue_version"] == FIFTH_VERSION and inventory.get(conn, 501)["checked"] == path.name
    assert sweep(settings, conn) == 0 and queued(conn) == []


def test_two_quiet_releases_in_a_row_are_both_answered_from_the_same_index(indexed, conn, settings):
    first = release("unrelated")
    poll(settings, conn, first)
    before, requests = row(conn), len(indexed.requests)
    path = poll(settings, conn, release("required", version=FOURTH_VERSION, base=first))  # the fixture sends no shipping
    assert queued(conn) == [] and row(conn) == before and len(indexed.requests) == requests
    assert inventory.get(conn, 501)["checked"] == path.name and db.get_repo(conn, 501)["issue_version"] == NEW_VERSION


def test_a_suspended_installation_is_left_alone_and_keeps_its_index(indexed, conn, settings):
    db.set_suspended(conn, 77, True)
    before = row(conn)
    poll(settings, conn, third_doc())
    counts = last_pass(conn)
    assert queued(conn) == [] and row(conn) == before and (counts["same"], counts["differ"], counts["none"]) == (0, 0, 0)
    assert inventory.get(conn, 501)["checked_by"] == "full"
    handle_event(conn, "installation", "d-un", {"action": "unsuspend", "installation": {"id": 77}}, settings.data)
    assert queued(conn) == [(501, "install")]  # the full check, which sees the release that passed meanwhile


def test_a_pass_interrupted_midway_is_finished_by_the_sweep_and_nothing_is_done_twice(indexed, conn, settings, monkeypatch):
    for repo_id in (502, 503):
        db.upsert_repo(conn, repo_id, 77, f"octo/r{repo_id}", None)
    real = releasepass.assess_repo

    def dying(conn_, repo, snaps):
        if repo["id"] == 503:
            raise KeyboardInterrupt  # the worker is killed here: 502 is settled, 503 and 501 are not reached
        return real(conn_, repo, snaps)

    monkeypatch.setattr(releasepass, "assess_repo", dying)
    with pytest.raises(KeyboardInterrupt):
        poll(settings, conn, release("unrelated"))
    monkeypatch.setattr(releasepass, "assess_repo", real)
    assert queued(conn) == [(502, "release")] and inventory.get(conn, 501)["checked_by"] == "full"
    assert poll(settings, conn, release("unrelated")) is None  # after the restart the snapshot is already stored
    assert sweep(settings, conn) == 1  # what the worker runs at startup: 503 gets its job, 501 its answer
    assert queued(conn) == [(502, "release"), (503, "release")] and inventory.get(conn, 501)["checked_by"] == "index"
    settled = row(conn)
    counts = releasepass.run(settings, conn)  # and a whole second pass changes nothing
    assert (counts["same"], counts["queued"], counts["differ"], counts["none"]) == (1, 2, 0, 0)
    assert sweep(settings, conn) == 0 and queued(conn) == [(502, "release"), (503, "release")] and row(conn) == settled


def test_an_index_written_by_another_scanner_version_is_not_used(indexed, conn, settings, monkeypatch):
    monkeypatch.setattr(inventory, "_digest", "0" * 16)  # as after a deploy that changed a parser
    poll(settings, conn, release("unrelated"))
    assert queued(conn) == [(501, "release")] and last_pass(conn)["none"] == 1


def test_an_older_index_format_is_not_used(indexed, conn, settings):
    conn.execute("UPDATE repo_index SET format = 0")
    poll(settings, conn, release("unrelated"))
    assert queued(conn) == [(501, "release")] and last_pass(conn)["none"] == 1


def test_a_stored_index_that_does_not_read_back_is_not_used(indexed, conn, settings):
    conn.execute("UPDATE repo_index SET extras = '{not json'")
    poll(settings, conn, release("unrelated"))
    assert queued(conn) == [(501, "release")] and last_pass(conn)["none"] == 1


def test_a_repo_that_changes_between_the_verdict_and_its_stamp_gets_the_full_check(indexed, conn, settings, monkeypatch):
    real = releasepass.assess_repo

    def racing(conn_, repo, snaps):
        verdict = real(conn_, repo, snaps)
        db.update_repo(conn_, 501, last_sha="c" * 40)  # another process finished a check in between
        return verdict

    monkeypatch.setattr(releasepass, "assess_repo", racing)
    poll(settings, conn, release("unrelated"))
    assert queued(conn) == [(501, "release")] and last_pass(conn)["none"] == 1


def test_repos_without_an_index_take_the_full_check_as_before(installed, conn, settings):
    while db.claim_job(conn) is not None:  # installed but never checked: the state of every row before this migration
        pass
    db.finish_job(conn, 1, "done")
    poll(settings, conn, release("unrelated"))
    assert queued(conn) == [(501, "release")] and last_pass(conn)["none"] == 1


def test_the_web_process_serves_nothing_about_an_index(settings, conn):
    paths = {getattr(route, "path", "") for route in create_app(settings, conn).routes}
    assert "/healthz" in paths and not any("index" in p or "repo" in p for p in paths)
