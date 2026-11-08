"""The independent review's repros for the parent's checks, each kept as a test."""

from __future__ import annotations

import copy
import hashlib

from test_fix import charges_migration, claim, scan  # noqa: F401  (fixtures)
from test_fix_test_check import checked
from test_fixer import plan_for
from test_fixer_v2 import plan_v2

from apidrift import migrations
from apidrift.app import db, editshape
from apidrift.app.checker import run_job

SHA = "a" * 40


def git_sha(raw: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()


PM_APP = "def detach(id)\n  Stripe::PaymentMethod.update(id, { payto: { pay_id: 'x' }, metadata: { a: '1' } })\nend\n"


def test_r2_a_delete_keeps_only_the_rest_of_the_comma_line(tmp_path):
    test = 'def test_x(update):\n    update.assert_called_once_with(\n        "pm_1",\n        "a",\n        "b",\n' \
           '        payto={"pay_id": "x"}\n    )\n'
    plan = plan_v2(tmp_path, {"app/pm.rb": PM_APP, "tests/test_pm.py": test})
    payload = plan.to_dict()
    site = payload["fixes"][0]["sites"][0]
    start = test.index('"a",') + 3  # the comma after "a": not the one before payto
    out = test[start + 1 : test.index("payto")]
    out = out[: out.rindex("\n") + 1]
    payload["tests"] = [{"file": "tests/test_pm.py", "line": 6}]
    payload["test_fixes"] = [{"file": "tests/test_pm.py", "blob_sha": git_sha(test.encode()), "edits": [{
        "line": 6, "start": start, "end": test.index('"x"}') + 4, "text": out,
        **{k: site[k] for k in ("path", "target", "replacement", "action")}}]}]
    assert checked(tmp_path, payload)[1] == []
    m = migrations.for_site(site["path"], site["target"])
    good = "f(\n    a=1,  # keep\n    payto=2\n)"
    at = good.index(",  # keep")
    e = {"start": at, "end": good.index("payto=2") + len("payto=2") + 1, "text": "  # keep\n"}
    assert editshape.shape(good, "python", e, m) is not None  # the fixer's own form still passes


def test_r7_crlf_test_files_are_edited_and_checked_byte_for_byte(tmp_path):
    for n, eol in enumerate(("\n", "\r\n")):
        root = tmp_path / str(n)
        spec = ("it do\n  expect(Stripe::PaymentMethod).to have_received(:update).with('pm_1', hash_including(\n"
                "    metadata: { a: '1' },\n    payto: { pay_id: 'x' }\n  ))\nend\n").replace("\n", eol)
        plan = plan_v2(root, {"app/pm.rb": PM_APP, "spec/pm_spec.rb": spec.encode()})
        fixes, tests = checked(root, plan.to_dict())
        assert [t["content"] for t in tests] == [f.content for f in plan.test_fixes] and len(tests) == 1, eol
        assert tests[0]["content"] == spec.replace(f",{eol}    payto: {{ pay_id: 'x' }}", ""), eol


def pay_class(i: int) -> str:
    return f"class Pay{i:02}\n  def call\n    Stripe::PaymentIntent.create({{ payment_method_types: ['card'], amount: {i} }})\n  end\nend\n"


def test_r3_a_code_file_the_parent_drops_stops_tests_of_that_field(tmp_path):
    files = {f"app/pay{i:02}.rb": pay_class(i) for i in range(2)}
    files["spec/pay01_spec.rb"] = ("RSpec.describe Pay01 do\n  it 'creates' do\n"
                                   "    expect(Stripe::PaymentIntent).to receive(:create).with({ payment_method_types: ['card'], amount: 1 })\n"
                                   "  end\nend\n")
    plan = plan_for(tmp_path, files)
    payload = plan.to_dict()
    assert [t["file"] for t in checked(tmp_path, payload)[1]] == ["spec/pay01_spec.rb"]
    bad = copy.deepcopy(payload)
    bad["fixes"][0]["edits"][0]["text"] = "evil"  # the parent drops app/pay00.rb: payment_intents is not all fixed
    fixes, tests = checked(tmp_path, bad)
    assert [f["file"] for f in fixes] == ["app/pay01.rb"] and tests == []
    bad = copy.deepcopy(payload)
    bad["not_attempted"] = [{"file": "x.rb", "line": 1, "path": "/v1/payment_intents", "target": "payment_method_types"}]
    assert checked(tmp_path, bad)[1] == []  # a site the child reports left: no test edit for that field
    many = {f"app/pay{i:02}.rb": pay_class(i) for i in range(26)}
    plan = plan_for(tmp_path / "many", {**many, "spec/pay01_spec.rb": files["spec/pay01_spec.rb"]})
    fixes, tests = checked(tmp_path / "many", plan.to_dict())
    assert len(fixes) == 25 and tests == []  # the cap dropped a file (unfixable "cap"): the test stays listed


def test_item6_an_unchanged_plan_is_not_fetched_again(scan, installed, conn, gh, settings):
    installed.grant_writes()
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: opened #2")
    before = len(installed.requests)
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: #2 unchanged")
    fetched = [r.url.path for r in installed.requests[before:] if "/git/blobs/" in r.url.path or "/contents/" in r.url.path]
    assert fetched == []


def test_item6_a_body_github_garbles_pushes_nothing(scan, installed, conn, gh, settings):
    import httpx

    installed.grant_writes()
    sha = scan.payload["fixes"][0]["blob_sha"]
    for broken in (httpx.Response(200, content=b"<html>not json"), httpx.Response(200, json={"encoding": "base64", "content": "@@@"})):
        installed.responses[("GET", f"/repos/octo/shop/git/blobs/{sha}")] = broken
        db.enqueue(conn, "check", 501, "push")
        assert run_job(claim(conn), conn, gh, settings).endswith("pr: failed")
        assert installed.refs == {} and installed.pulls == {}
    installed.responses[("GET", "/repos/octo/shop/contents/server.py")] = httpx.Response(200, content=b"nope")
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: failed")


def test_r8_an_edit_outside_any_expectation_is_refused(tmp_path):
    spec = ("it do\n  allow(Stripe::PaymentIntent).to receive(:retrieve).and_return(double(payment_method_types: ['card']))\n"
            "  expect(log).to eq('create(payment_method_types: 1)')\nend\n")
    plan = plan_for(tmp_path, {"app/pay.rb": "Stripe::PaymentIntent.create({ payment_method_types: ['card'], amount: 1 })\n",
                               "spec/x_spec.rb": spec})
    payload = plan.to_dict()
    site = payload["fixes"][0]["sites"][0]
    for line, needle in ((2, "payment_method_types: ['card']"), (3, "payment_method_types: 1")):
        s = spec.index(needle)
        payload["tests"] = [{"file": "spec/x_spec.rb", "line": line}]
        payload["test_fixes"] = [{"file": "spec/x_spec.rb", "blob_sha": git_sha(spec.encode()), "edits": [{
            "line": line, "start": s, "end": s + len("payment_method_types"), "text": "allowed_payment_method_types",
            **{k: site[k] for k in ("path", "target", "replacement", "action")}}]}]
        assert checked(tmp_path, payload)[1] == [], needle  # a mock's return value, a string: no expectation found
