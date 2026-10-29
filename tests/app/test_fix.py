"""The fix pull request: permissions, opening, idempotence, rebuilds, divergence, declines, merges, validation."""

from __future__ import annotations

import copy
import re
import shutil
import subprocess
import sys
import types

import httpx
import pytest
from conftest import NEW_VERSION, REPO, SHA, TOKEN, blob_sha, install_payload

from apidrift import migrations
from apidrift.app import checker, db, fixcheck, fixpr, issue, prbody
from apidrift.app.checker import run_job, run_local, scan_subprocess
from apidrift.app.commands import render_status
from apidrift.app.github import FIX_PERMISSIONS, PERMISSIONS, GitHubError
from apidrift.app.webhooks import handle_event
from apidrift.app.worker import run_one

BRANCH = f"apidrift/stripe-{NEW_VERSION}"
REF = f"heads/{BRANCH}"
HEAD = f"octo:{BRANCH}"
DOC = "https://docs.stripe.com/changelog/test/removes-description"
CHARGES = migrations.Migration(
    method="post", path="/v1/charges", target="description", replacement="allowed_description", introduced=NEW_VERSION,
    doc=DOC, note="allowed_description keeps your text.",
)


@pytest.fixture(autouse=True)
def charges_migration(monkeypatch):
    """The test spec removes ``description`` from POST /v1/charges; the parent's own table documents its rename."""
    monkeypatch.setitem(migrations._BY_SITE, (CHARGES.path, CHARGES.target), CHARGES)


def claim(conn):
    job = db.claim_job(conn)
    assert job is not None
    return job


def fixed_tree(tmp_path):
    tree = tmp_path / "fixed"
    shutil.copytree(REPO, tree)
    for path in tree.iterdir():
        path.write_text(re.sub(r",\s*description[:=][^,)\n]*", "", path.read_text()))
    return tree


def fix_payload(tree=REPO, files=("server.py",)) -> dict:
    """The real check payload for ``tree`` plus a fix plan renaming ``description`` in ``files``."""
    payload = run_local(tree, "previous", "latest").payload()
    fixes = []
    for name in files:
        text = (tree / name).read_text()
        lines = sorted({i["usage"]["line"] for i in issue.breaking(payload) if i["usage"]["file"] == name})
        fixes.append({
            "file": name, "blob_sha": blob_sha((tree / name).read_bytes()),
            "content": text.replace("description", "allowed_description"),
            "sites": [
                {"line": ln, "path": "/v1/charges", "target": "description", "replacement": "allowed_description", "action": "rename"}
                for ln in lines
            ],
        })
    payload.update({
        "fixes": fixes,
        "migrations": [{"method": "post", "path": "/v1/charges", "target": "description", "replacement": "allowed_description",
                        "introduced": NEW_VERSION, "doc": DOC, "note": "allowed_description keeps your text.", "action": "rename"}],
        "pins": [{"file": "server.py", "line": 2, "version": "2023-10-16"}],
        "unfixable": [{"file": "server.rb", "line": 7, "path": "/v1/charges", "target": "description", "why": "incomplete_binding"}],
        "leftovers": [{"file": "server.js", "line": 30}],
    })
    return payload


class Scan:
    """Stands in for the sandboxed child: returns a copy of ``payload`` and records how it was called."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def __call__(self, tree, old, new, data_dir, **kwargs):
        self.calls.append(kwargs)
        return copy.deepcopy(self.payload)


@pytest.fixture
def scan(installed, monkeypatch) -> Scan:
    stub = Scan(fix_payload())
    monkeypatch.setattr(checker, "scan_subprocess", stub)
    return stub


def repo(conn):
    return db.get_repo(conn, 501)


def paths(fake, method):
    return [r.url.path for r in fake.writes if r.method == method]


# --- permissions ------------------------------------------------------------------------


def test_needs_permissions_comment_is_posted_once(scan, installed, conn, gh, settings):
    assert run_job(claim(conn), conn, gh, settings) == "findings: opened #1; pr: needs permissions"
    assert installed.comments == [(1, fixpr.needs_permissions_comment(installed.installation_url))]
    assert "https://github.com/organizations/octo/settings/installations/77" in installed.comments[0][1]
    assert repo(conn)["fix_state"] == "needs_permissions" and installed.pulls == {}
    assert [t["permissions"] for t in installed.token_requests] == [PERMISSIONS]  # no write token was asked for
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings) == "findings: #1 unchanged; pr: needs permissions"
    assert len(installed.comments) == 1
    assert scan.calls and all(call.get("fix") is True for call in scan.calls)


def test_accepting_the_permissions_clears_the_state_and_the_next_check_opens_the_pr(scan, installed, conn, gh, settings):
    run_job(claim(conn), conn, gh, settings)
    assert repo(conn)["fix_state"] == "needs_permissions"
    actions = handle_event(conn, "installation", "d-perms", install_payload(installed, "new_permissions_accepted"), settings.data)
    assert "1 repos may get a fix pull request now" in actions and repo(conn)["fix_state"] is None
    installed.grant_writes()
    assert run_job(claim(conn), conn, gh, settings) == "findings: #1 unchanged; pr: opened #2"
    assert len(installed.comments) == 1


def test_write_token_is_a_422_until_the_installation_grants_it(gh, fake):
    assert gh.installation_permissions(77) == PERMISSIONS
    with pytest.raises(GitHubError) as caught:
        gh.mint_token(77, 501, FIX_PERMISSIONS)
    assert caught.value.status == 422
    fake.grant_writes()
    token, granted = gh.mint_token(77, 501, FIX_PERMISSIONS)
    assert token == TOKEN and granted == FIX_PERMISSIONS and fixpr.has_write(granted)
    assert FIX_PERMISSIONS == {"contents": "write", "pull_requests": "write", "metadata": "read"}
    assert PERMISSIONS == {"contents": "read", "issues": "write", "metadata": "read"}  # checks stay read-only


# --- opening ----------------------------------------------------------------------------


def test_opens_a_pull_request_from_a_branch_on_the_base_commit(scan, installed, conn, gh, settings):
    installed.grant_writes()
    assert run_job(claim(conn), conn, gh, settings) == "findings: opened #1; pr: opened #2"
    pull = installed.pulls[2]
    assert pull["title"] == f"Stripe {NEW_VERSION}: rename description (1 call site)"
    assert (pull["head"]["label"], pull["base"]["ref"]) == (HEAD, "main")
    body = pull["body"]
    line = scan.payload["fixes"][0]["sites"][0]["line"]
    for needle in (
        f"Ready for Stripe {NEW_VERSION}.", "**`description` → `allowed_description` on POST `/v1/charges`**",
        f"allowed_description keeps your text. [Changelog]({DOC})", f"blob/{SHA}/server.py#L{line} via `stripe.Charge.create`",
        "**Merge together with your upgrade**", f"blob/{SHA}/server.py#L2 pins `2023-10-16`",
        f"Verified: apidrift re-scanned the changed files; 1 call site now sends what Stripe {NEW_VERSION} expects; "
        "every other Stripe call is unchanged.",
        "on your current pinned version Stripe rejects the new shape.",
        f"blob/{SHA}/server.py#L2 pins `2023-10-16`", "These files pin an API version that predates this change;",
        "**Left for you**", prbody.UNFIXABLE_WHY["incomplete_binding"], f"blob/{SHA}/server.rb#L7",
        f"Still mentions the old name at https://github.com/octo/shop/blob/{SHA}/server.js#L30", "Addresses #1",
    ):
        assert needle in body, needle
    assert "Closes #" not in body and "Fixes #" not in body
    fp = prbody.pr_marker_in(body)
    row = repo(conn)
    assert (row["pr_number"], row["fix_state"], row["pr_branch"], row["pr_fingerprint"]) == (2, "open", BRANCH, fp)
    assert fp == fixpr.fingerprint(scan.payload["fixes"], NEW_VERSION, 1)
    commit_sha = installed.refs[REF]
    assert commit_sha == row["pr_head_sha"]
    commit = installed.commits[commit_sha]
    assert commit["message"] == f"Stripe {NEW_VERSION}: rename description"
    assert commit["parents"] == [{"sha": SHA}]
    tree = installed.trees[commit["tree"]["sha"]]
    assert tree["base_tree"] == installed.tree_sha()
    (entry,) = tree["tree"]
    assert (entry["path"], entry["mode"], entry["type"]) == ("server.py", "100644", "blob")
    assert installed.blobs[entry["sha"]].decode() == scan.payload["fixes"][0]["content"]
    assert [t["permissions"] for t in installed.token_requests] == [PERMISSIONS, FIX_PERMISSIONS]
    assert installed.revoked == 2  # both tokens given back
    assert installed.issues[1]["state"] == "open"


def test_identical_rerun_makes_zero_writes(scan, installed, conn, gh, settings):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    writes = len(installed.writes)
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings) == "findings: #1 unchanged; pr: #2 unchanged"
    assert len(installed.writes) == writes and len(installed.pulls) == 1 and installed.revoked == 4


def test_new_push_rebuilds_the_branch_and_patches_the_pull(scan, installed, conn, gh, settings, tmp_path):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    first = installed.refs[REF]
    tree = tmp_path / "moved"
    shutil.copytree(REPO, tree)
    (tree / "server.py").write_text("# moved\n" + (tree / "server.py").read_text())
    installed.tree, installed.sha = tree, "f" * 40
    scan.payload = fix_payload(tree)
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings) == "findings: updated #1; pr: updated #2"
    second = installed.refs[REF]
    assert second != first and repo(conn)["pr_head_sha"] == second
    assert installed.commits[second]["parents"] == [{"sha": "f" * 40}]
    assert paths(installed, "PATCH") == ["/repos/octo/shop/issues/1", f"/repos/octo/shop/git/refs/{REF}", "/repos/octo/shop/pulls/2"]
    assert len(installed.pulls) == 1 and f"blob/{'f' * 40}/server.py" in installed.pulls[2]["body"]
    assert repo(conn)["fix_state"] == "open"


def test_a_branch_someone_else_pushed_to_is_left_alone(scan, installed, conn, gh, settings, tmp_path):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    installed.refs[REF] = "d" * 40  # a person committed on our branch
    body = installed.pulls[2]["body"]
    tree = tmp_path / "moved"
    shutil.copytree(REPO, tree)
    (tree / "server.py").write_text("# moved\n" + (tree / "server.py").read_text())
    installed.tree, installed.sha = tree, "f" * 40
    scan.payload = fix_payload(tree)
    writes = len(installed.writes)
    db.enqueue(conn, "check", 501, "push")
    outcome = run_job(claim(conn), conn, gh, settings)
    assert outcome == f"findings: updated #1; pr: branch {BRANCH} has commits apidrift did not make; left alone"
    assert installed.refs[REF] == "d" * 40 and installed.pulls[2]["body"] == body
    assert repo(conn)["fix_state"] == "diverged" and repo(conn)["pr_number"] == 2
    assert [r.url.path for r in installed.writes[writes:]] == ["/repos/octo/shop/issues/1"]  # no git or pull writes
    db.enqueue(conn, "check", 501, "push")
    run_job(claim(conn), conn, gh, settings)  # still diverged: nothing more is written
    assert repo(conn)["fix_state"] == "diverged" and len(installed.writes) == writes + 1


def test_a_crash_between_branch_and_pull_resumes_by_adopting_the_open_pull(scan, installed, conn, gh, settings):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    db.update_repo(conn, 501, pr_number=None, fix_state=None, pr_fingerprint=None)
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings) == "findings: #1 unchanged; pr: updated #2"
    assert len(installed.pulls) == 1 and repo(conn)["pr_number"] == 2


# --- what people do with the pull -------------------------------------------------------


def test_a_declined_pull_stays_silent_on_the_same_fingerprint(scan, installed, conn, gh, settings):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    installed.close_pull_by_human(2)
    writes = len(installed.writes)
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings) == "findings: #1 unchanged; pr: #2 was declined; staying silent"
    assert repo(conn)["fix_state"] == "declined" and len(installed.writes) == writes and len(installed.pulls) == 1


def test_a_declined_pull_gets_a_successor_only_when_new_sites_appear(scan, installed, conn, gh, settings, tmp_path):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    installed.close_pull_by_human(2)
    tree = tmp_path / "moved"
    shutil.copytree(REPO, tree)
    (tree / "server.py").write_text("# moved\n" + (tree / "server.py").read_text())
    installed.tree, installed.sha = tree, "f" * 40
    scan.payload = fix_payload(tree)  # new fingerprint (new blob), same sites
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: #2 was declined and no new call site appeared; staying silent")
    assert len(installed.pulls) == 1
    scan.payload = fix_payload(tree, files=("server.py", "server.js"))  # a new site in a second file
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: opened #3")
    row = repo(conn)
    assert (row["pr_number"], row["fix_state"]) == (3, "open") and installed.pulls[3]["head"]["label"] == HEAD
    assert installed.pulls[2]["state"] == "closed" and "2 call sites" in installed.pulls[3]["title"]


def test_a_merged_pull_then_a_clean_check_closes_the_issue_and_keeps_merged(scan, installed, conn, gh, settings, tmp_path):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    installed.merge_pull(2)
    installed.tree = fixed_tree(tmp_path)
    scan.payload = run_local(installed.tree, "previous", "latest").payload()
    assert "fixes" not in scan.payload
    db.enqueue(conn, "check", 501, "push")
    outcome = run_job(claim(conn), conn, gh, settings)
    assert outcome.startswith("clean: closed #1; baseline advanced to") and outcome.endswith("; pr: #2 was merged")
    row = repo(conn)
    assert (row["issue_state"], row["fix_state"], row["pr_number"]) == ("closed_by_us", "merged", 2)
    assert "/repos/octo/shop/pulls/2" not in paths(installed, "PATCH")
    assert [n for n, _ in installed.comments] == [1]


def test_a_clean_check_closes_an_open_pull_with_a_comment(scan, installed, conn, gh, settings, tmp_path):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    installed.tree = fixed_tree(tmp_path)
    scan.payload = run_local(installed.tree, "previous", "latest").payload()
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).endswith("; pr: closed #2")
    assert installed.pulls[2]["state"] == "closed" and not installed.pulls[2]["merged"]
    assert (2, f"No call sites are affected at {SHA}; closing.") in installed.comments
    row = repo(conn)
    assert (row["fix_state"], row["pr_number"], row["pr_fingerprint"], row["pr_pending_sha"]) == (None, None, None, None)
    assert row["pr_branch"] == BRANCH and row["pr_head_sha"] == installed.refs[REF]  # the branch stays; a later fix reuses it
    assert db.status_summary(conn)["open_prs"] == 0


# --- the parent trusts nothing from the child ---------------------------------------------


@pytest.mark.parametrize("bad", [
    {"blob_sha": "a" * 40},                       # not the bytes that were scanned
    {"file": "../server.py"},                      # outside the tree
    {"file": "/server.py"},
    {"file": ".github/workflows/deploy.py"},       # never workflow files
    {"file": "notes.md"},                          # not a scanned language
    {"file": "nope.py"},                           # not in the repo at all
    {"file": "server.py\x00"},
    {"content": 42},
    {"blob_sha": "not-a-sha"},
    {"file": "./server.py"},                       # not normalised
    {"file": "a//server.py"},
    {"file": ".GitHub/x.py"},
    {"file": "server.py/"},
    {"content": "\udc80 lone surrogate"},           # cannot be encoded
])
def test_parent_rejects_fixes_it_cannot_verify(scan, installed, conn, gh, settings, bad):
    installed.grant_writes()
    scan.payload["fixes"][0].update(bad)
    assert run_job(claim(conn), conn, gh, settings) == "findings: opened #1; pr: nothing to push"
    assert installed.pulls == {} and installed.blobs == {} and installed.refs == {}
    assert paths(installed, "POST") == ["/repos/octo/shop/issues"]
    assert repo(conn)["fix_state"] is None


def test_parent_keeps_the_verifiable_files_and_drops_the_rest(scan, installed, conn, gh, settings):
    installed.grant_writes()
    good = scan.payload["fixes"][0]
    scan.payload["fixes"] = [{**good, "file": "server.js"}, good, {**good, "file": ".github/x.py"}]
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: opened #2")
    (entry,) = installed.trees[installed.commits[installed.refs[REF]]["tree"]["sha"]]["tree"]
    assert entry["path"] == "server.py" and len(installed.blobs) == 1


def test_size_caps(scan, installed, conn, gh, settings, monkeypatch):
    installed.grant_writes()
    monkeypatch.setattr(fixcheck, "MAX_FILE_BYTES", 10)
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: nothing to push")


def test_a_github_error_in_the_fix_step_does_not_fail_the_job(scan, installed, conn, gh, settings):
    installed.grant_writes()
    installed.responses[("POST", "/repos/octo/shop/git/blobs")] = httpx.Response(500, json={"message": "boom"})
    job = claim(conn)
    assert run_one(job, conn, gh, settings) == "findings: opened #1; pr: failed"
    assert db.get_job(conn, job["id"])["status"] == "done"
    assert repo(conn)["fix_state"] == "failed" and installed.pulls == {} and installed.revoked == 2
    db.enqueue(conn, "check", 501, "push")  # the next check tries again
    assert run_job(claim(conn), conn, gh, settings) == "findings: #1 unchanged; pr: opened #2"


def test_fix_step_only_runs_while_the_issue_is_open(scan, installed, conn, gh, settings):
    installed.grant_writes()
    installed.add_issue("old " + issue.marker("1" * 64), state="closed", closed_by="zach")
    assert "staying silent" in run_job(claim(conn), conn, gh, settings)
    assert installed.pulls == {} and len(installed.token_requests) == 1


# --- status, the child contract -------------------------------------------------------------


def test_status_counts_open_pull_requests(installed, conn):
    db.update_repo(conn, 501, fix_state="open", pr_number=4)
    summary = db.status_summary(conn)
    assert summary["open_prs"] == 1 and "open pull requests: 1" in render_status(summary)


def test_run_local_with_fix_merges_the_plan_into_the_payload(data_dir, monkeypatch):
    seen = {}

    def plan_fixes(result, impacts, index, root):
        seen.update(root=root, usages=len(result.usages), impacts=len(impacts))
        return types.SimpleNamespace(to_dict=lambda: {"fixes": [{"file": "x.py"}], "pins": [], "unfixable": [], "migrations": [], "leftovers": []})

    monkeypatch.setitem(sys.modules, "apidrift.fixer", types.SimpleNamespace(plan_fixes=plan_fixes))
    payload = run_local(REPO, "previous", "latest", fix=True).payload()
    assert payload["fixes"] == [{"file": "x.py"}] and seen["root"] == REPO and seen["impacts"] > 0
    assert "fixes" not in run_local(REPO, "previous", "latest").payload()


def test_scan_subprocess_passes_the_fix_flag(data_dir, monkeypatch):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b'{"impacts": [], "new_version": "x"}', stderr=b"")

    monkeypatch.setattr(checker.subprocess, "run", fake_run)
    scan_subprocess(REPO, "a", "b", data_dir, fix=True)
    scan_subprocess(REPO, "a", "b", data_dir)
    assert seen[0][-1] == "--fix" and "--fix" not in seen[1]


def test_render_pr_keeps_repo_text_inert():
    payload = {
        "impacts": [{"usage": {"file": "pay @zach.py", "line": 3, "path": "/v1/charges", "call": "@zach `#1`"}}],
        "migrations": [{"method": "post", "path": "/v1/charges", "target": "description", "replacement": "d2", "doc": "javascript:alert(1)", "note": "[x](http://evil)"}],
        "pins": [{"file": "a.py", "line": 1, "version": "`2023`"}],
        "unfixable": [{"file": "b.py", "line": 2, "why": "mystery <script>"}],
        "leftovers": [],
        "unread": {"Go": 2},
    }
    fixes = [{"file": "pay @zach.py", "blob_sha": "0" * 40, "content": "", "sites": [{"line": 3, "path": "/v1/charges", "target": "description", "replacement": "d2"}]}]
    title, body = prbody.render_pr(payload, fixes, full_name="octo/shop", sha=SHA, version="v", issue_number=9, fingerprint="f" * 64)
    assert title == "Stripe v: rename description (1 call site)"
    assert "javascript:" not in body and "evil" not in body and "`` @zach `#1` ``" in body  # the child's note is ignored
    assert "**`description` → `d2` on POST `/v1/charges`**" in body and "Ready for Stripe v." in body
    assert "pay%20%40zach.py#L3" in body and "pins `` `2023` ``" in body and "apidrift does not read Go call sites yet" in body
    assert "mystery" not in body and "<script>" not in body and prbody.UNKNOWN_UNFIXABLE in body  # never the child's why
    assert "Addresses #9" in body and prbody.pr_marker_in(body) == "f" * 64
    assert "No API-version pin" not in body


def test_not_attempted_sites_are_listed_under_left_for_you():
    payload = {
        "impacts": [], "migrations": [], "pins": [], "unfixable": [{"file": "a.py", "line": 1, "why": "unsupported_key_form"}],
        "not_attempted": [{"file": "b.py", "line": 2, "why": "arguments_not_visible"}, {"file": "c.py", "line": 3, "why": "odd <x>"}],
        "leftovers": [],
    }
    fixes = [{"file": "a.py", "blob_sha": "0" * 40, "content": "", "sites": [{"line": 1, "path": "/v1/x", "target": "t", "replacement": "u"}]}]
    _, body = prbody.render_pr(payload, fixes, full_name="o/r", sha=SHA, version="v", issue_number=1, fingerprint="f" * 64)
    assert f"- Not attempted (arguments are not fully visible): https://github.com/o/r/blob/{SHA}/b.py#L2" in body
    assert f"- Not attempted (apidrift did not try this site): https://github.com/o/r/blob/{SHA}/c.py#L3" in body and "odd" not in body
    assert "the key is written in a form apidrift does not rewrite" in body
