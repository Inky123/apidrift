"""The parent never pushes the child's bytes: it rebuilds every code fix from the base blob and the child's
edits, and accepts an edit only as the documented action of one of the fix's sites on its key."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
from conftest import SHA, Disk, blob_sha
from test_fixer import FIXTURES, plan_for
from test_fixer_v2 import V2, plan_v2

from apidrift.app import fixcheck

V2_FILES = sorted(p.name for p in V2.iterdir() if p.is_file())
V1_FILES = sorted(p.name for p in FIXTURES.iterdir() if p.is_file())


def through_parent(root: Path, payload: dict, gh=None) -> list[dict]:
    return fixcheck.validate(gh or Disk(root), "t", "o/r", SHA, payload["fixes"])


@pytest.mark.parametrize("name", V2_FILES)
def test_every_v2_fix_is_rebuilt_byte_for_byte(tmp_path, name):
    shutil.copy(V2 / name, tmp_path / name)
    plan = plan_v2(tmp_path)
    payload = plan.to_dict()
    assert payload["fixes"], name
    accepted = through_parent(tmp_path, payload)
    assert [(f["file"], f["content"]) for f in accepted] == [(f.file, f.content) for f in plan.fixes]


@pytest.mark.parametrize("name", V1_FILES)
def test_every_rename_fixture_is_rebuilt_byte_for_byte(tmp_path, name):
    shutil.copy(FIXTURES / name, tmp_path / name)
    plan = plan_for(tmp_path)
    accepted = through_parent(tmp_path, plan.to_dict())
    assert [(f["file"], f["content"]) for f in accepted] == [(f.file, f.content) for f in plan.fixes] and accepted


APP = "Stripe::PaymentIntent.create({ payment_method_types: ['card'], amount: 1 })\n"


@pytest.fixture
def real(tmp_path):
    plan = plan_for(tmp_path, {"app/pay.rb": APP})
    payload = plan.to_dict()
    assert through_parent(tmp_path, payload)[0]["content"] == plan.fixes[0].content
    return tmp_path, payload


def with_edit(payload, **change):
    bad = copy.deepcopy(payload)
    bad["fixes"][0]["edits"][0].update(change)
    bad["fixes"][0].pop("content")
    return bad


@pytest.mark.parametrize("change", [
    {"text": "allowed_payment_method_types: system('id'), x"},  # code after the name
    {"text": "allowed_payment_method_types; `id`"},
    {"text": "payment_method_types_v2"},  # not the table's name
    {"target": "amount"},  # a key no site declares
    {"start": -1}, {"end": 10**6}, {"start": True}, {"text": None},
])
def test_a_tampered_code_edit_rejects_the_file(real, change):
    root, payload = real
    assert through_parent(root, with_edit(payload, **change)) == []


def test_r6_the_childs_bytes_are_never_pushed(real):
    root, payload = real
    bad = copy.deepcopy(payload)
    bad["fixes"][0]["content"] = "CHILD BYTES, NOT DERIVED\n"
    assert through_parent(root, bad) == []  # the claimed content is not what its edits give
    bad["fixes"][0].pop("content")
    (fix,) = through_parent(root, bad)
    assert fix["content"] == APP.replace("{ payment_method_types", "{ allowed_payment_method_types")
    spec = "RSpec.describe Pay do\n  it { expect(Stripe::PaymentIntent).to have_received(:create).with(payment_method_types: ['card']) }\nend\n"
    (root / "spec").mkdir()
    (root / "spec/pay_spec.rb").write_text(spec)
    bad["fixes"].append({"file": "spec/pay_spec.rb", "blob_sha": blob_sha(spec.encode()), "content": "CHILD BYTES\n",
                         "sites": bad["fixes"][0]["sites"], "edits": []})  # a test sent as a code fix
    assert [f["file"] for f in through_parent(root, bad)] == ["app/pay.rb"]


def test_edits_that_are_not_the_key_are_refused(real):
    root, payload = real
    other = APP + "puts intent.payment_method_types\nlog('payment_method_types')\n"  # a response read, a string
    (root / "app/other.rb").write_text(other)
    for needle, text in (("intent.payment_method_types", "allowed_payment_method_types"),
                         ("'payment_method_types'", "allowed_payment_method_types"), ("amount: 1", "")):
        start = other.index(needle) + (len("intent.") if needle.startswith("intent") else 1 if needle[0] == "'" else 0)
        end = start + (len("payment_method_types") if text else len(needle))
        bad = copy.deepcopy(payload)
        child = bad["fixes"][0]
        child.pop("content")
        bad["fixes"] = [{**child, "file": "app/other.rb", "blob_sha": blob_sha(other.encode()),
                         "edits": [*child["edits"], {"start": start, "end": end, "text": text, "target": "payment_method_types"}]}]
        assert through_parent(root, bad) == [], needle


def test_a_blob_github_cannot_serve_pushes_nothing(real):
    from apidrift.app.github import GitHubError

    root, payload = real

    class Failing(Disk):
        def get_blob(self, token, full_name, sha):
            raise GitHubError("boom", 502)

    with pytest.raises(GitHubError):
        through_parent(root, payload, Failing(root))
    swapped = Disk(root, swap={"app/pay.rb": b"evil"})
    assert through_parent(root, payload, swapped) == []  # bytes that do not hash to the sha


@pytest.mark.parametrize("text, start_at, old, new, ok", [
    ("p := &stripe.PaymentIntentParams{\n\tAmount:             stripe.Int64(1),\n\tPaymentMethodTypes: x,\n}",
     "Amount:", " " * 13, " " * 20, True),  # gofmt realignment of a sibling field
    ("p := &stripe.PaymentIntentParams{\n\tAmount:   stripe.Int64(1),\n}", "Amount:", "   ", "  \n", False),
    ("p := &stripe.PaymentIntentParams{\n\tAmount:   stripe.Int64(1),\n}", "Amount:", "   ", " x ", False),
    ("p.PaymentMethodTypes = stripe.StringSlice(x)\n", "p.", "PaymentMethodTypes", "AllowedPaymentMethodTypes", True),
])
def test_go_shapes(text, start_at, old, new, ok):
    from apidrift import migrations
    from apidrift.app.editshape import shape

    m = migrations.for_site("/v1/payment_intents", "payment_method_types")
    start = text.index(start_at) + len(start_at)
    assert text[start : start + len(old)] == old
    e = {"start": start, "end": start + len(old), "text": new}
    assert (shape(text, "go", e, m, code=True) is not None) is ok
    assert shape(text, "go", e, m) is None  # a test edit is never Go


@pytest.mark.parametrize("language, text, needle, ok", [
    ("php", "<?php $data['payment_method_types'] = ['card'];", "payment_method_types", True),
    ("php", "<?php if ($data['payment_method_types'] == 1) {}", "payment_method_types", False),  # a read
    ("ruby", "params[:payment_method_types] = ['card']", "payment_method_types", True),
    ("ruby", "params[:payment_method_types] == ['card']", "payment_method_types", False),
    ("javascript", "params.payment_method_types = ['card'];", "payment_method_types", True),
    ("javascript", "use(params.payment_method_types);", "payment_method_types", False),
    ("python", "params.setdefault('payment_method_types', ['card'])", "payment_method_types", True),
    ("python", "params.get('payment_method_types', ['card'])", "payment_method_types", False),
])
def test_written_keys(language, text, needle, ok):
    from apidrift import migrations
    from apidrift.app.editshape import shape

    m = migrations.for_site("/v1/payment_intents", "payment_method_types")
    start = text.index(needle)
    e = {"start": start, "end": start + len(needle), "text": "allowed_payment_method_types"}
    assert (shape(text, language, e, m, code=True) is not None) is ok


def test_wrap_over_a_multi_line_ternary_is_copied_whole(tmp_path):
    from apidrift import migrations
    from apidrift.app.editshape import shape

    m = migrations.for_site("/v1/subscriptions/{subscription_exposed_id}", "billing_cycle_anchor")
    text = "stripe.subscriptions.update(id, {\n  billing_cycle_anchor: now\n    ? 'now'\n    : 'unchanged',\n})"
    value = "now\n    ? 'now'\n    : 'unchanged'"
    start = text.index(value)
    ok = {"start": start, "end": start + len(value), "text": "{ type: " + value + " }"}
    assert shape(text, "javascript", ok, m, code=True) is not None
    assert shape(text, "javascript", ok, m) is None  # a test edit wraps only a plain value
    half = {"start": start, "end": start + 3, "text": "{ type: now }"}  # not the whole value
    assert shape(text, "javascript", half, m, code=True) is None
