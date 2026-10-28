"""The fix pull request is named after the release that introduced the change, and moving it to a new branch
supersedes the old pull request instead of leaving two open."""

from __future__ import annotations

import pytest
from conftest import NEW_VERSION
from test_fix import Scan, claim, fix_payload, repo

from apidrift.app import checker
from apidrift.app.checker import run_job
from apidrift.app.poller import snapshots

PMT = {"path": "/v1/payment_intents", "target": "payment_method_types", "replacement": "allowed_payment_method_types"}


def test_fix_version_comes_from_the_migration_table(settings, data_dir):
    latest = snapshots(data_dir)[-1]
    payload = {
        "fixes": [{"sites": [dict(PMT, line=3)]}, {"sites": ["junk", {"path": None}]}, "junk"],
        "migrations": [dict(PMT, introduced="2099-01-01.child")],  # child text never names the branch
    }
    assert checker.fix_version(payload, latest) == "2026-09-30.endive"
    assert checker.fix_version({"fixes": [{"sites": [{"path": "/v1/charges", "target": "description"}]}]}, latest) == NEW_VERSION
    assert checker.fix_version({}, latest) == NEW_VERSION


@pytest.fixture
def scan(installed, monkeypatch) -> Scan:
    stub = Scan(fix_payload())
    monkeypatch.setattr(checker, "scan_subprocess", stub)
    return stub


def renamed(monkeypatch, version):
    monkeypatch.setattr(checker, "fix_version", lambda payload, latest: version)


def test_a_new_branch_name_supersedes_the_open_pull(scan, installed, conn, gh, settings, monkeypatch):
    installed.grant_writes()
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: opened #2")
    old_ref = f"heads/apidrift/stripe-{NEW_VERSION}"
    assert old_ref in installed.refs
    renamed(monkeypatch, "2026-01-15.test")
    checker.db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: opened #3, superseding #2")
    assert installed.pulls[2]["state"] == "closed" and old_ref not in installed.refs
    assert installed.pulls[3]["state"] == "open" and installed.pulls[3]["title"].startswith("Stripe 2026-01-15.test: ")
    assert installed.pulls[3]["head"]["label"] == "octo:apidrift/stripe-2026-01-15.test"
    assert "Ready for Stripe 2026-01-15.test." in installed.pulls[3]["body"]
    assert installed.comments[-1] == (2, "Superseded by #3, which names Stripe 2026-01-15.test, the release that introduced this change.")
    row = repo(conn)
    assert (row["pr_number"], row["fix_state"], row["pr_branch"]) == (3, "open", "apidrift/stripe-2026-01-15.test")
    writes = len(installed.writes)
    checker.db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: #3 unchanged")
    assert len(installed.writes) == writes  # settled: zero writes on the next run


def test_a_superseded_branch_someone_pushed_to_is_kept(scan, installed, conn, gh, settings, monkeypatch):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    old_ref = f"heads/apidrift/stripe-{NEW_VERSION}"
    installed.refs[old_ref] = "f" * 40  # a person pushed to the branch
    renamed(monkeypatch, "2026-01-15.test")
    checker.db.enqueue(conn, "check", 501, "push")
    outcome = run_job(claim(conn), conn, gh, settings)
    assert installed.pulls[2]["state"] == "closed"
    assert installed.refs[old_ref] == "f" * 40  # not ours any more: never deleted
    assert outcome.endswith("superseding #2")
