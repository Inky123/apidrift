"""The parent's checks on updated tests: it rebuilds each file from the base blob and refuses any edit that is
not the declared action on the declared key, written the way the fixer writes it."""

from __future__ import annotations

import copy
import hashlib
import shutil

import pytest
from conftest import REPO, Disk, blob_sha
from test_fix import charges_migration, claim, scan  # noqa: F401  (fixtures)
from test_fix_test_edits import JS_APP, JS_TEST, PHP_APP, PHP_TEST, PY_APP, PY_TEST, RUBY_APP, RUBY_SPEC
from test_fixer import plan_for
from test_fixer_v2 import plan_v2

from apidrift.app import editshape, fixcheck, fixpr, fixtestcheck, prbody
from apidrift.app.checker import run_job
from apidrift.app.github import GitHubError

SHA = "a" * 40


def git_sha(raw: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()


class Repo(Disk):
    """:class:`conftest.Disk`, whose blob endpoint can also fail (``lie``) for the files in ``failing``."""

    def __init__(self, root, swap: dict[str, bytes] | None = None, lie: bool = False, failing: tuple = ()) -> None:
        super().__init__(root, swap)
        self.lie, self.failing = lie, failing

    def get_blob(self, token, full_name, sha):
        if self.lie and (not self.failing or any(git_sha((self.root / f).read_bytes()) == sha for f in self.failing)):
            raise GitHubError("boom", 500)
        return super().get_blob(token, full_name, sha)


def checked(root, payload, gh=None):
    gh = gh or Repo(root)
    fixes = fixcheck.validate(gh, "t", "o/r", SHA, payload["fixes"])
    return fixes, fixtestcheck.validate(gh, "t", "o/r", SHA, payload, fixes)


@pytest.fixture
def real(tmp_path):
    files = {
        "app/pay.rb": RUBY_APP, "spec/pay_spec.rb": RUBY_SPEC, "src/pay.js": JS_APP, "src/pay.test.js": JS_TEST,
        "app/pay.py": PY_APP, "tests/test_pay.py": PY_TEST, "app/Pay.php": PHP_APP, "tests/PayTest.php": PHP_TEST,
    }
    plan = plan_for(tmp_path, files)
    return tmp_path, plan, plan.to_dict()


def test_every_real_test_edit_passes_and_the_parent_rebuilds_the_same_content(real):
    root, plan, payload = real
    fixes, tests = checked(root, payload)
    assert len(fixes) == 4 and {t["file"] for t in tests} == {f.file for f in plan.test_fixes} and len(tests) == 4
    for t in tests:
        child = next(f for f in plan.test_fixes if f.file == t["file"])
        assert t["content"] == child.content and t["blob_sha"] == child.blob_sha
        assert t["edits"] == [{"line": e.line, "path": e.path, "target": e.target, "replacement": e.replacement,
                               "action": e.action} for e in child.edits]
    title, body = prbody.render_pr(payload, fixes, full_name="o/r", sha=SHA, version="2026-09-30.endive",
                                   issue_number=3, fingerprint="f" * 64, tests=tests)
    assert "**Tests updated**" in body and f"blob/{SHA}/spec/pay_spec.rb#L4" in body
    assert "Other tests that name the old field" in body
    left = body.split("**Left for you**")[1]
    assert f"spec/pay_spec.rb#L4," not in left and f"spec/pay_spec.rb#L9" in left  # updated lines are not left
    assert fixpr.fingerprint(fixes, "v", 3, tests) != fixpr.fingerprint(fixes, "v", 3)
    assert fixpr.fingerprint(fixes, "v", 3, []) == fixpr.fingerprint(fixes, "v", 3)  # no tests: the old fingerprint


def test_every_real_non_rename_edit_passes(tmp_path):
    from test_fix_test_edits import test_every_other_action_is_the_same_edit_the_code_got as build

    build(tmp_path)  # writes the files and checks the child's edits
    plan = plan_v2(tmp_path)
    fixes, tests = checked(tmp_path, plan.to_dict())
    assert sorted(e["action"] for t in tests for e in t["edits"]) == ["delete", "rename_first", "wrap_type"]


def tamper(payload, which, **changes):
    out = copy.deepcopy(payload)
    item = next(t for t in out["test_fixes"] if t["file"] == which)
    for key, value in changes.items():
        if key == "file":
            item["file"] = value
        else:
            item["edits"][0][key] = value
    return out


def only(file):
    return lambda tests: [t["file"] for t in tests if t["file"] == file]


@pytest.mark.parametrize("file, changes", [
    ("spec/pay_spec.rb", {"text": "allowed_payment_method_types: 1, evil"}),  # more than the name
    ("spec/pay_spec.rb", {"text": "allowed_payment_method_types; system('id')"}),
    ("spec/pay_spec.rb", {"replacement": "evil"}),  # not the table's replacement
    ("spec/pay_spec.rb", {"action": "delete"}),  # not the table's action
    ("spec/pay_spec.rb", {"path": "/v1/charges"}),  # no migration there
    ("spec/pay_spec.rb", {"line": 9}),  # the line does not hold the edit
    ("spec/pay_spec.rb", {"line": True}),
    ("spec/pay_spec.rb", {"start": -1}),
    ("spec/pay_spec.rb", {"end": 10**9}),
    ("spec/pay_spec.rb", {"file": "app/pay.rb"}),  # the code fix's own file
    ("spec/pay_spec.rb", {"file": "lib/pay.rb"}),  # not a test location
    ("spec/pay_spec.rb", {"file": "../spec/pay_spec.rb"}),
    ("spec/pay_spec.rb", {"file": ".github/spec/x_spec.rb"}),
    ("tests/test_pay.py", {"text": "allowed_payment_method_types=os.system('x') or ", "end": None}),
])
def test_a_tampered_edit_rejects_the_whole_file(real, file, changes):
    root, plan, payload = real
    if changes.get("end", 0) is None:
        changes = {k: v for k, v in changes.items() if k != "end"}
        item = next(t for t in payload["test_fixes"] if t["file"] == file)
        changes["end"] = item["edits"][0]["end"] + 1  # swallows the "=" too
    bad = tamper(payload, file, **changes)
    _, tests = checked(root, bad)
    assert only(changes.get("file", file))(tests) == [] and only(file)(tests) == []
    assert len(tests) == 3  # the other files still go out


def test_edits_on_text_that_is_not_the_key_are_refused(tmp_path):
    spec = ("RSpec.describe Pay do\n  it do\n"
            "    expect(Stripe::PaymentIntent).to have_received(:create).with(hash_including(payment_method_types: ['card']))\n"
            "    expect(intent.payment_method_types).to eq(['card'])\n    payment_method_types = 1\n"
            "    x = 'payment_method_types: 1'\n  end\nend\n")
    plan = plan_for(tmp_path, {"app/pay.rb": RUBY_APP, "spec/pay_spec.rb": spec})
    payload = plan.to_dict()
    good = payload["test_fixes"][0]["edits"][0]
    assert checked(tmp_path, payload)[1]
    for needle in ("intent.payment_method_types", "\n    payment_method_types =", "'payment_method_types: 1'"):
        at = spec.index(needle) + needle.index("payment_method_types")
        line = spec.count("\n", 0, at) + 1
        bad = copy.deepcopy(payload)
        bad["test_fixes"][0]["edits"] = [{**good, "start": at, "end": at + len("payment_method_types"), "line": line}]
        bad["tests"].append({"file": "spec/pay_spec.rb", "line": line})
        assert checked(tmp_path, bad)[1] == [], needle


def test_the_parent_reads_the_base_blob_itself(real):
    root, plan, payload = real
    fixes, tests = checked(root, payload, Repo(root, swap={"spec/pay_spec.rb": b"evil"}))
    assert "spec/pay_spec.rb" not in [t["file"] for t in tests]  # bytes that do not hash to the sha
    with pytest.raises(GitHubError):  # a test blob GitHub fails to serve: no reduced pull request goes out
        checked(root, payload, Repo(root, lie=True, failing=("spec/pay_spec.rb",)))
    bad = copy.deepcopy(payload)
    bad["test_fixes"][0]["blob_sha"] = git_sha(b"other")
    assert bad["test_fixes"][0]["file"] not in [t["file"] for t in checked(root, bad)[1]]
    bad = copy.deepcopy(payload)
    bad["tests"] = []  # the child did not list the lines
    assert checked(root, bad)[1] == []
    bad = copy.deepcopy(payload)
    bad["fixes"] = [f for f in bad["fixes"] if f["file"] != "app/pay.rb"]
    assert "spec/pay_spec.rb" in [t["file"] for t in checked(root, bad)[1]]  # other code fixes still change it
    bad["fixes"] = []
    assert checked(root, bad) == ([], [])  # no code change: no test is updated


def test_overlapping_duplicate_and_too_many_edits_are_refused(real):
    root, plan, payload = real
    item = next(t for t in payload["test_fixes"] if t["file"] == "spec/pay_spec.rb")
    for edits in (item["edits"] + item["edits"][:1], item["edits"][:1] * 51, [], "x", [None]):
        bad = copy.deepcopy(payload)
        next(t for t in bad["test_fixes"] if t["file"] == "spec/pay_spec.rb")["edits"] = edits
        assert "spec/pay_spec.rb" not in [t["file"] for t in checked(root, bad)[1]]
    bad = copy.deepcopy(payload)
    bad["test_fixes"].append(copy.deepcopy(item))  # the same file twice
    assert [t["file"] for t in checked(root, bad)[1]].count("spec/pay_spec.rb") == 1


@pytest.mark.parametrize("language, text, start_after, old, new, ok", [
    ("ruby", "f({ payto: { a: [1, ')'] }, b: 2 })", "{ ", "payto: { a: [1, ')'] }, ", "", True),
    ("ruby", "f({ payto: { a: 1 }, b: 2, c: 3 })", "{ ", "payto: { a: 1 }, b: 2, ", "", False),  # two entries
    ("ruby", "f({ payto: (a + b), c: 3 })", "{ ", "payto: (a + ", "", False),  # not the whole value
    ("ruby", "f({ payto: [1, 2 })", "{ ", "payto: [1, 2 ", "", False),  # unbalanced
    ("ruby", "f({ b: 2, payto: 1 })", "2", ", payto: 1", "", True),
    ("ruby", "f({ other: 1, b: 2 })", "{ ", "other: 1, ", "", False),  # another key
    ("javascript", "f({ payto: `${x}`, b: 2 })", "{ ", "payto: `${x}`, ", "", True),
])
def test_delete_shapes(language, text, start_after, old, new, ok):
    from apidrift import migrations

    m = migrations.for_site("/v1/payment_methods/{payment_method}", "payto")
    start = text.index(start_after) + len(start_after)
    assert text[start : start + len(old)] == old
    e = {"start": start, "end": start + len(old), "text": new}
    assert (editshape.shape(text, language, e, m) is not None) is ok


@pytest.mark.parametrize("language, text, old, new, ok", [
    ("ruby", "f(billing_cycle_anchor: 'now')", "'now'", "{ type: 'now' }", True),
    ("ruby", "f(billing_cycle_anchor: 'now')", "'now'", "{ type: 'now', x: `id` }", False),
    ("ruby", "f(billing_cycle_anchor: 'later')", "'later'", "{ type: 'later' }", False),  # not a documented value
    ("ruby", "f(billing_cycle_anchor: run!)", "run!", "{ type: run! }", False),
    ("python", "f(billing_cycle_anchor='now')", "'now'", "{'type': 'now'}", True),
    ("php", "f(['billing_cycle_anchor' => $a])", "$a", "['type' => $a]", True),
    ("javascript", "f({ other: 'now' })", "'now'", "{ type: 'now' }", False),  # another key
])
def test_wrap_shapes(language, text, old, new, ok):
    from apidrift import migrations

    m = migrations.for_site("/v1/subscriptions/{subscription_exposed_id}", "billing_cycle_anchor")
    start = text.index(old)
    e = {"start": start, "end": start + len(old), "text": new}
    assert (editshape.shape(text, language, e, m) is not None) is ok


@pytest.mark.parametrize("text, old, new, ok", [
    ("f(filters: { countries: ['US', 'CA'] })", "countries: ['US', 'CA']", "country: 'US'", True),
    ("f(filters: { countries: ['US', 'CA'] })", "countries: ['US', 'CA']", "country: 'CA'", False),
    ("f(filters: { countries: list })", "countries: list", "country: list[0]", True),
    ("f(filters: { countries: list })", "countries: list", "country: list.evil", False),
])
def test_rename_first_shapes(text, old, new, ok):
    from apidrift import migrations

    m = migrations.for_site("/v1/financial_connections/sessions", "filters.countries")
    start = text.index(old)
    e = {"start": start, "end": start + len(old), "text": new}
    assert (editshape.shape(text, "ruby", e, m) is not None) is ok


# --- through a real job ----------------------------------------------------------------------------

TEST = "@patch(\"stripe.Charge.create\")\ndef test_charge(create):\n    create.assert_called_once_with(amount=1, description=\"x\")\n"


def test_a_job_pushes_the_updated_test_in_the_same_commit(scan, installed, conn, gh, settings, tmp_path):
    tree = tmp_path / "tree"
    shutil.copytree(REPO, tree)
    (tree / "tests").mkdir()
    (tree / "tests/test_server.py").write_text(TEST)
    installed.tree = tree
    at = TEST.index("description")
    scan.payload["tests"] = [{"file": "tests/test_server.py", "line": 3}]
    scan.payload["unfixable"] = []  # every description site is fixed, so its tests may be updated
    scan.payload["test_fixes"] = [{"file": "tests/test_server.py", "blob_sha": blob_sha(TEST.encode()), "edits": [
        {"line": 3, "path": "/v1/charges", "target": "description", "replacement": "allowed_description",
         "action": "rename", "start": at, "end": at + len("description"), "text": "allowed_description"}]}]
    installed.grant_writes()
    assert run_job(claim(conn), conn, gh, settings) == "findings: opened #1; pr: opened #2"
    (commit_sha,) = installed.refs.values()
    commit = installed.commits[commit_sha]
    entries = {e["path"]: installed.blobs[e["sha"]].decode() for e in installed.trees[commit["tree"]["sha"]]["tree"]}
    assert entries["tests/test_server.py"] == TEST.replace('description="x"', 'allowed_description="x"')
    assert "server.py" in entries
    body = installed.pulls[2]["body"]
    assert "**Tests updated**" in body and "tests/test_server.py#L3" in body
    assert "Tests that name the old field" not in body  # its only listed line was updated
    blobs = [r for r in installed.requests if r.method == "GET" and "/git/blobs/" in r.url.path]
    assert len(blobs) == 2  # the parent read both base blobs itself, the code and the test
