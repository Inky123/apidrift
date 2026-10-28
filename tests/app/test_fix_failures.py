"""The fix pull request under failure: wedged states, hostile child text, reinstalls, truncated trees, rate limits."""

from __future__ import annotations

import shutil

import httpx
import pytest
from conftest import SHA, install_payload
from test_fix import REF, claim, fix_payload, fixed_tree, repo, scan  # noqa: F401  (scan is a fixture)

from apidrift.app import db, fixpr, prbody
from apidrift.app.checker import run_job, run_local
from apidrift.app.github import FIX_PERMISSIONS, PERMISSIONS, GitHubError
from apidrift.app.webhooks import handle_event
from apidrift.app.worker import run_one


def moved_tree(tmp_path, installed, scan):
    tree = tmp_path / "moved"
    shutil.copytree(installed.tree, tree)
    (tree / "server.py").write_text("# moved\n" + (tree / "server.py").read_text())
    installed.tree, installed.sha = tree, "f" * 40
    scan.payload = fix_payload(tree)
    return tree


def test_a_failed_ref_update_is_retried_not_mistaken_for_divergence(scan, installed, conn, gh, settings, tmp_path):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    first = installed.refs[REF]
    moved_tree(tmp_path, installed, scan)
    installed.responses[("PATCH", f"/repos/octo/shop/git/refs/{REF}")] = httpx.Response(500, json={"message": "boom"})
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: failed")
    row = repo(conn)
    assert installed.refs[REF] == first and row["fix_state"] == "failed"
    assert row["pr_head_sha"] == first and row["pr_pending_sha"] not in (None, first)  # the built commit is only pending
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: updated #2")
    row = repo(conn)
    assert row["fix_state"] == "open" and row["pr_head_sha"] == installed.refs[REF] != first and row["pr_pending_sha"] is None


def test_clean_check_closes_the_pull_even_after_a_failed_refresh(scan, installed, conn, gh, settings, tmp_path):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    moved_tree(tmp_path, installed, scan)
    installed.responses[("POST", "/repos/octo/shop/git/blobs")] = httpx.Response(500, json={"message": "boom"})
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: failed")
    assert (repo(conn)["fix_state"], repo(conn)["pr_number"]) == ("failed", 2)
    installed.tree = fixed_tree(tmp_path)
    scan.payload = run_local(installed.tree, "previous", "latest").payload()
    db.enqueue(conn, "check", 501, "push")
    outcome = run_job(claim(conn), conn, gh, settings)
    assert outcome.startswith("clean: closed #1") and outcome.endswith("; pr: closed #2")
    assert installed.pulls[2]["state"] == "closed" and repo(conn)["fix_state"] is None


def test_a_clean_check_on_a_diverged_branch_comments_instead_of_closing(scan, installed, conn, gh, settings, tmp_path):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    installed.refs[REF] = "d" * 40
    moved_tree(tmp_path, installed, scan)
    db.enqueue(conn, "check", 501, "push")
    assert repo(conn)["fix_state"] != "diverged" and run_job(claim(conn), conn, gh, settings).endswith("left alone")
    installed.tree = fixed_tree(tmp_path)
    scan.payload = run_local(installed.tree, "previous", "latest").payload()
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).endswith("; pr: #2 left open: the branch has commits apidrift did not make")
    assert installed.pulls[2]["state"] == "open"
    assert (2, f"No call sites are affected at {'f' * 40}. {fixpr.DIVERGED_NOTE}") in installed.comments
    assert (repo(conn)["pr_number"], repo(conn)["fix_state"]) == (None, None)


def test_child_tokens_that_are_not_stripe_names_are_dropped_before_any_write(scan, installed, conn, gh, settings):
    installed.grant_writes()
    for site in scan.payload["fixes"][0]["sites"]:
        site["target"], site["replacement"], site["path"] = "@zach", "#1 Co-authored-by: x <x@x>", "/v1/@octocat"
    scan.payload["migrations"][0].update(target="@zach", note="cc @octocat see #1")
    assert run_job(claim(conn), conn, gh, settings) == "findings: opened #1; pr: nothing to push"
    assert installed.commits == {} and installed.pulls == {} and installed.refs == {}


@pytest.mark.parametrize("site", [
    {"line": "x"}, {"line": 0}, {"line": True}, {"target": "Description"}, {"replacement": "a b"}, {"path": "/v3/x"},
    {"path": "/v1/charges?x"}, {"target": "x" * 81},
])
def test_a_bad_site_is_rejected_before_git_writes(scan, installed, conn, gh, settings, site):
    installed.grant_writes()
    scan.payload["fixes"][0]["sites"][0].update(site)
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: nothing to push")
    assert installed.refs == {} and installed.blobs == {} and installed.commits == {}


def test_render_pr_keeps_hostile_tokens_inert_even_unvalidated():
    payload = {"impacts": [], "migrations": [{"method": "@m", "path": "/v1/x", "target": "@zach", "replacement": "#1"}], "pins": [], "unfixable": [], "leftovers": []}
    fixes = [{"file": "a.py", "blob_sha": "0" * 40, "content": "", "sites": [{"line": 1, "path": "/v1/x", "target": "@zach", "replacement": "#1"}]}]
    title, body = prbody.render_pr(payload, fixes, full_name="o/r", sha=SHA, version="@octocat", issue_number=9, fingerprint="f" * 64)
    assert title == "Stripe the next release: rename a removed field (1 call site)"
    assert "**`@zach` → `#1` on POST `/v1/x`**" in body and "Ready for Stripe `@octocat`." in body
    bare = body.replace("`@zach`", "").replace("`@octocat`", "")
    assert "@zach" not in bare and "@octocat" not in bare


def test_open_pull_is_closed_when_the_child_has_nothing_left_to_fix(scan, installed, conn, gh, settings):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    scan.payload["fixes"] = []  # findings remain (the issue stays open) but the rename sites are gone
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings) == "findings: #1 unchanged; pr: closed #2"
    assert installed.pulls[2]["state"] == "closed" and (2, fixpr.gone_comment(SHA)) in installed.comments
    assert repo(conn)["fix_state"] is None and installed.issues[1]["state"] == "open"


def test_nothing_verifiable_left_also_closes_the_open_pull(scan, installed, conn, gh, settings):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    scan.payload["fixes"][0]["blob_sha"] = "a" * 40
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).endswith("; pr: closed #2")


def test_reinstall_adopts_the_open_pull_on_the_existing_branch(scan, installed, conn, gh, settings):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    handle_event(conn, "installation", "d-del", {"action": "deleted", "installation": {"id": 77}}, settings.data)
    assert repo(conn) is None
    handle_event(conn, "installation", "d-re", install_payload(installed), settings.data)
    assert run_job(claim(conn), conn, gh, settings) == "findings: updated #1; pr: updated #2"
    row = repo(conn)
    assert (row["pr_number"], row["fix_state"], row["pr_head_sha"]) == (2, "open", installed.refs[REF])
    assert len(installed.pulls) == 1


def test_reinstall_with_a_foreign_pull_on_our_branch_diverges(scan, installed, conn, gh, settings):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    installed.pulls[2].update(user={"login": "zach"}, body="mine now")  # neither our login nor our marker
    handle_event(conn, "installation", "d-del", {"action": "deleted", "installation": {"id": 77}}, settings.data)
    handle_event(conn, "installation", "d-re", install_payload(installed), settings.data)
    assert run_job(claim(conn), conn, gh, settings).endswith("left alone")
    assert repo(conn)["fix_state"] == "diverged" and len(installed.pulls) == 1


@pytest.mark.parametrize("truncated", [False, True])
def test_executable_bit_is_preserved(scan, installed, conn, gh, settings, tmp_path, truncated):
    installed.grant_writes()
    tree = tmp_path / "exec"
    shutil.copytree(installed.tree, tree)
    (tree / "lib").mkdir()
    shutil.move(tree / "server.py", tree / "lib" / "server.py")
    (tree / "lib" / "server.py").chmod(0o755)
    installed.tree, installed.tree_truncated = tree, truncated
    scan.payload = fix_payload(tree, files=("lib/server.py",))
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: opened #2")
    (entry,) = installed.trees[installed.commits[installed.refs[REF]]["tree"]["sha"]]["tree"]
    assert (entry["path"], entry["mode"]) == ("lib/server.py", "100755")
    if truncated:
        assert sum(1 for r in installed.requests if "/git/trees/" in r.url.path and r.method == "GET") == 3  # root listing + 2 levels


def test_unreadable_mode_marks_failed_instead_of_guessing(scan, installed, conn, gh, settings, monkeypatch):
    installed.grant_writes()
    installed.tree_truncated = True
    monkeypatch.setattr(installed, "_tree_entries", lambda directory, recursive: [])
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: failed")
    assert installed.blobs == {} and installed.commits == {}


def test_rate_limited_in_fix_step_requeues_and_the_rerun_is_harmless(scan, installed, conn, gh, settings):
    installed.grant_writes()
    installed.responses[("POST", "/repos/octo/shop/git/blobs")] = httpx.Response(403, headers={"retry-after": "30"}, json={})
    job = claim(conn)
    assert run_one(job, conn, gh, settings) == "rate limited"
    assert db.get_job(conn, job["id"])["status"] == "queued" and repo(conn)["fix_state"] is None
    assert installed.revoked == 2
    job = db.claim_job(conn, job_id=job["id"])
    assert run_job(job, conn, gh, settings) == "findings: #1 unchanged; pr: opened #2"
    assert installed.comments == []


def test_mint_422_race_marks_failed_not_gone(scan, installed, conn, gh, settings, monkeypatch):
    installed.grant_writes()
    real = gh.mint_token

    def racing(installation_id, repo_id, permissions=PERMISSIONS):
        if permissions == FIX_PERMISSIONS:
            raise GitHubError("GitHub returned 422 for POST /app/installations/77/access_tokens", 422)
        return real(installation_id, repo_id, permissions)

    monkeypatch.setattr(gh, "mint_token", racing)
    assert run_job(claim(conn), conn, gh, settings) == "findings: opened #1; pr: failed"
    assert repo(conn)["gone"] == 0 and repo(conn)["fix_state"] == "failed"


def test_child_version_is_not_trusted(scan, installed, conn, gh, settings):
    installed.grant_writes()
    scan.payload["new_version"] = "@zach..x"
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: opened #2")
    assert list(installed.refs) == [REF]
    assert "@zach" not in installed.pulls[2]["body"] and "@zach" not in installed.pulls[2]["title"]
    assert fixpr.branch_name("a..b.lock") == "apidrift/stripe-a-b-lock" and fixpr.branch_name("x y") == "apidrift/stripe-x-y"


def test_permissions_revoked_then_regranted_keeps_one_pull(scan, installed, conn, gh, settings):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    installed.permissions.update(contents="read")
    del installed.permissions["pull_requests"]
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: needs permissions")
    assert repo(conn)["fix_state"] == "needs_permissions" and repo(conn)["pr_number"] == 2
    handle_event(conn, "installation", "d-perms", install_payload(installed, "new_permissions_accepted"), settings.data)
    installed.grant_writes()
    writes = len(installed.writes)
    assert run_job(claim(conn), conn, gh, settings) == "findings: #1 unchanged; pr: #2 unchanged"
    assert len(installed.pulls) == 1 and repo(conn)["fix_state"] == "open" and len(installed.writes) == writes


def test_a_recreated_issue_refreshes_the_addresses_line(scan, installed, conn, gh, settings):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    installed.responses[("GET", "/repos/octo/shop/issues/1")] = httpx.Response(404, json={})  # the issue was deleted
    del installed.issues[1]
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings) == "findings: opened #3; pr: updated #2"
    assert "Addresses #3" in installed.pulls[2]["body"]


def test_settings_url_falls_back_to_the_account_type():
    assert fixpr.settings_url({"html_url": "https://github.com/settings/installations/5"}, 5) == "https://github.com/settings/installations/5"
    assert fixpr.settings_url({"account": {"login": "octo", "type": "Organization"}}, 5) == "https://github.com/organizations/octo/settings/installations/5"
    assert fixpr.settings_url({"account": {"login": "zach", "type": "User"}}, 5) == "https://github.com/settings/installations/5"
    assert fixpr.settings_url({"html_url": "http://evil", "account": {"login": "a b", "type": "Organization"}}, 5) == "https://github.com/settings/installations/5"

