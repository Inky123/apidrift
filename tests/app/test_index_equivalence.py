"""The claim the release pass rests on: assessing a release from a repo's index gives exactly what a full check
of the same commit reports. Every test here runs both paths on the same fixture repo and compares them."""

import pytest
from conftest import NEW_VERSION, OLD_VERSION, SHA, THIRD_VERSION
from indexing import MUTATIONS, TREES, child_is_this_checkout, release, save_release

from apidrift.app import db, indexcheck, inventory, issue
from apidrift.app.checker import issue_view, run_local, scan_subprocess
from apidrift.app.poller import snapshots

# The releases after which a fixture's calls resolve to other operations than its index recorded. The index
# must then refuse to answer (the full check runs); everywhere else it must answer, and answer identically.
RESOLVES_DIFFERENTLY = {("plain", "repick"), ("possible", "repick"), ("docs", "repick"), ("possible", "refunds")}


@pytest.fixture
def repo_row(conn):
    db.upsert_installation(conn, 77, "octo")
    db.upsert_repo(conn, 501, 77, "octo/shop", None)
    return 501


def full_check(tree, baseline, latest) -> dict:
    """What the sandboxed child of a full check prints for ``tree``: ``run_local`` is its whole pipeline."""
    return run_local(tree, baseline.name, latest.name).payload()


def from_index(conn, repo_id, baseline, latest) -> dict | None:
    """What the release pass computes for the stored index; None when the index declines to answer."""
    stored = inventory.load(conn, inventory.get(conn, repo_id))
    pr = indexcheck.pair(snapshots(baseline.parent.parent.parent), baseline, latest)
    if not indexcheck.same_answers(pr.index, stored.answers):
        return None
    return indexcheck.payload_of(stored.usages, stored.unread, pr)


def view(payload):
    items, fp, hashes, render = issue_view(payload, "octo/shop", SHA, str(payload["new_version"]))
    return items, fp, hashes, issue.render_issue(items, **render)


COMPARED = ("impacts", "call_sites", "documentation_matches", "unread", "old_version", "new_version", "versions")


@pytest.mark.parametrize("mutation", sorted(MUTATIONS))
@pytest.mark.parametrize("tree", sorted(TREES))
@pytest.mark.parametrize("advanced", [False, True], ids=["baseline-kept", "baseline-advanced"])
def test_a_release_assessed_from_the_index_equals_a_full_check_of_the_same_commit(data_dir, conn, repo_row, tree, mutation, advanced):
    old, new = snapshots(data_dir)
    first = full_check(TREES[tree], old, new)
    assert indexcheck.record(conn, [old, new], repo_row, 1, SHA, first, old, new) is None  # stored, and verified
    third = save_release(release(mutation))
    baseline = new if advanced else old  # a repo that was clean moves its baseline to the release it was clean at
    expected = full_check(TREES[tree], baseline, third)
    got = from_index(conn, repo_row, baseline, third)
    if (tree, mutation) in RESOLVES_DIFFERENTLY:
        assert got is None
        return
    assert got is not None
    for key in COMPARED:
        assert got[key] == expected[key], key
    assert view(got) == view(expected)  # the findings, fingerprint, title and body of the issue, byte for byte


def test_the_fixtures_cover_certain_possible_and_documentation_usages(data_dir):
    old, new = snapshots(data_dir)
    possible = full_check(TREES["possible"], old, new)
    assert {i["certainty"] for i in issue.breaking(possible)} == {"affected", "possible"}
    docs = full_check(TREES["docs"], old, new)
    languages = {i["usage"]["language"] for i in docs["impacts"]}
    assert {"doc", "http", "python"} <= languages and docs["documentation_matches"] == 7 and docs["unread"]
    assert {i["usage"]["language"] for i in issue.breaking(docs)} == {"http", "python"}  # doc matches are not findings


def test_response_field_findings_survive_the_hashed_reads(data_dir, conn, repo_row):
    """Affected (the file reads the removed field) versus possible (it does not) is decided from ``reads``,
    which the index keeps only as hashes."""
    old, new = snapshots(data_dir)
    indexcheck.record(conn, [old, new], repo_row, 1, SHA, full_check(TREES["possible"], old, new), old, new)
    third = save_release(release("response"))
    got = from_index(conn, repo_row, old, third)
    by_reason = {(i["usage"]["file"], i["change"]["target"]): i["certainty"] for i in got["impacts"] if i["change"]["kind"] == "response_field_removed"}
    assert by_reason[("pay.py", "receipt_url")] == "affected" and by_reason[("billing.js", "receipt_url")] == "affected"
    assert got["impacts"] == full_check(TREES["possible"], old, third)["impacts"]
    stored = inventory.load(conn, inventory.get(conn, repo_row))
    reads = next(u.reads for u in stored.usages if u.file == "pay.py")
    assert "receipt_url" in reads and "email" in reads and "nonexistent" not in reads
    assert all(len(h) == inventory.HASH_CHARS for h in reads) and "receipt_url" not in set(reads)  # hashes, not names


def test_an_endpoint_removed_by_the_release_is_still_reported_from_the_index(data_dir, conn, repo_row):
    """The index stores resolved endpoints. A removed endpoint still resolves (the baseline has it), so the
    index is still right, and impact still reports every call to it."""
    old, new = snapshots(data_dir)
    indexcheck.record(conn, [old, new], repo_row, 1, SHA, full_check(TREES["plain"], old, new), old, new)
    third = save_release(release("endpoint_removed"))
    got = from_index(conn, repo_row, old, third)
    removed = [i for i in got["impacts"] if i["change"]["kind"] == "endpoint_removed"]
    assert sorted(i["usage"]["file"] for i in removed) == ["server.js", "server.py", "server.rb"]
    assert {i["change"]["introduced"] for i in removed} == {THIRD_VERSION}
    assert got["impacts"] == full_check(TREES["plain"], old, third)["impacts"]


def test_a_fresh_scan_really_differs_when_the_index_declines(data_dir, conn, repo_row):
    """The refusal is not over-caution: after ``repick`` a scan of the same code finds other endpoints."""
    old, new = snapshots(data_dir)
    first = full_check(TREES["plain"], old, new)
    indexcheck.record(conn, [old, new], repo_row, 1, SHA, first, old, new)
    third = save_release(release("repick"))
    assert from_index(conn, repo_row, old, third) is None
    fresh = full_check(TREES["plain"], old, third)
    assert fresh["index"]["sites"] != first["index"]["sites"]
    assert {s["path"] for s in fresh["index"]["sites"] if s["language"] == "python"} == {"/v1/billing", "/v1/charges/{charge}"}


def test_three_releases_on_one_index_keep_matching_the_full_check(data_dir, conn, repo_row):
    """The index is written once; two later releases are each assessed from it."""
    old, new = snapshots(data_dir)
    indexcheck.record(conn, [old, new], repo_row, 1, SHA, full_check(TREES["possible"], old, new), old, new)
    third_doc = release("required")
    third = save_release(third_doc)
    assert from_index(conn, repo_row, old, third)["impacts"] == full_check(TREES["possible"], old, third)["impacts"]
    fourth = save_release(release("currency", "response", version="2026-04-01.test", base=third_doc), month=4)
    got, expected = from_index(conn, repo_row, old, fourth), full_check(TREES["possible"], old, fourth)
    assert got["impacts"] == expected["impacts"] and got["versions"] == [OLD_VERSION, NEW_VERSION, THIRD_VERSION, "2026-04-01.test"]
    assert {i["change"]["introduced"] for i in got["impacts"]} == {NEW_VERSION, THIRD_VERSION, "2026-04-01.test"}


def test_the_real_child_process_exports_an_index_the_parent_admits(data_dir, conn, repo_row):
    """End to end over the wire: the isolated subprocess, its JSON, the parent's gate, and the comparison."""
    if not child_is_this_checkout():
        pytest.skip("the isolated child imports the installed apidrift, which is another checkout than this one")
    old, new = snapshots(data_dir)
    payload = scan_subprocess(TREES["docs"], old.name, new.name, data_dir, max_mem_mb=0, fix=True)
    assert indexcheck.record(conn, [old, new], repo_row, 1, SHA, payload, old, new) is None
    third = save_release(release("currency"))
    expected = scan_subprocess(TREES["docs"], old.name, third.name, data_dir, max_mem_mb=0, fix=True)
    got = from_index(conn, repo_row, old, third)
    assert got["impacts"] == expected["impacts"] and view(got) == view(expected)
