"""Spreads and ``Object.assign`` of a same-file helper's object, and helpers that send a removed key or its
documented replacement depending on the API version (jsswitch).

The shapes come from medusajs/medusa's Stripe provider: ``...additionalParameters`` (a local bound to a builder
that ``Object.assign``s a version-switched helper into ``res``), and ``...this.getPaymentMethodTypesParams(types)``
next to ``...setupIntentData``, the rest of a destructuring that takes ``payment_method_types`` out. Every hostile
variant must be read exactly or left hidden, and a switch must never hide a key the release still receives.
"""

import json

import pytest
from test_fixer import docs, plan_for
from test_fixer_v2 import plan_v2
from test_helpers import scan

from apidrift.app import db, inventory
from apidrift.diff import BREAKING, diff_chain
from apidrift.impact import AFFECTED, POSSIBLE, assess
from apidrift.openapi import load_spec
from apidrift.report import filter_changes
from apidrift.scanner import ScanResult, scan_repo
from apidrift.sdkmap import SdkIndex

JS = "const stripe = require('stripe')('sk', { apiVersion: config.apiVersion ?? '2024-04-10' });\nconst SINCE = '2026-07-29';\n"
HELPER = """
class Pay {
  params(types) {
    if (!types) {
      return {}
    }
    const apiVersion = config.apiVersion ?? '2024-04-10'
    return apiVersion >= SINCE
      ? { allowed_payment_method_types: types }
      : { payment_method_types: types }
  }

  charge(types) {
    return stripe.paymentIntents.create({ amount: 1, ...this.params(types) })
  }
}
"""
NOT_VISIBLE = "arguments are not fully visible at this call"


def site(tmp_path, text, name="pay.js"):
    """(fields, hidden, switched) of the one payment-intent call in ``text``."""
    found = [u for u in scan(tmp_path, {name: text}) if u.path == "/v1/payment_intents"]
    assert len(found) == 1, found
    u = found[0]
    return set(u.fields), set(u.hidden or ()), set(u.switched)


def outcome(tmp_path, text, name="pay.js"):
    """(certainty, reason) of the payment_method_types removal at the call, or None; and the fix plan."""
    plan = plan_for(tmp_path, {name: text})
    old, new = (load_spec(d) for d in docs())
    changes = filter_changes(diff_chain([old, new])[0], min_severity=BREAKING)
    result = scan_repo(tmp_path, SdkIndex.build(old, new))
    hits = [
        (i.certainty, i.reason) for i in assess(result.usages, changes)
        if i.change.target == "payment_method_types" and i.usage.path == "/v1/payment_intents"
    ]
    assert len(hits) <= 1, hits
    return (hits[0] if hits else None), plan


def untouched(plan):
    """No fix edits anything (the version-switched helper above all)."""
    assert plan.fixes == [], [f.diff() for f in plan.fixes]


# --- the medusa shapes ----------------------------------------------------------------------


def test_a_spread_of_a_version_switched_helper_is_read_and_needs_no_change(tmp_path):
    fields, hidden, switched = site(tmp_path, JS + HELPER)
    assert fields == {"amount", "allowed_payment_method_types", "payment_method_types"} and hidden == set()
    assert switched == {("payment_method_types", "allowed_payment_method_types", ">=2026-07-29", False)}
    hit, plan = outcome(tmp_path, JS + HELPER)
    assert hit is None
    untouched(plan)
    assert plan.not_attempted == [] and plan.unfixable == []


def test_object_assign_of_the_helper_into_a_builder_spread_through_a_local(tmp_path):
    text = JS + HELPER.replace(
        "    return stripe.paymentIntents.create({ amount: 1, ...this.params(types) })\n",
        "    const extra = this.build(types)\n"
        "    const request = { amount: 1, metadata: { ...(meta ?? {}), id: 1 }, ...extra }\n"
        "    request.customer = who\n"
        "    return stripe.paymentIntents.create(request)\n",
    ).replace(
        "  charge(types) {",
        "  build(types) {\n"
        "    const res = {}\n"
        "    res.description = types.description\n"
        "    Object.assign(res, this.params(types))\n"
        "    if (!types.length) {\n"
        "      res.payment_method_configuration = types.config\n"
        "    }\n"
        "    return res\n"
        "  }\n\n"
        "  charge(types) {",
    )
    fields, hidden, switched = site(tmp_path, text)
    assert fields == {
        "amount", "metadata", "metadata.id", "customer", "description", "payment_method_configuration",
        "allowed_payment_method_types", "payment_method_types",
    }
    assert hidden == {"metadata.", "customer."}  # the spread under metadata, and what customer holds
    assert switched == {("payment_method_types", "allowed_payment_method_types", ">=2026-07-29", False)}
    hit, plan = outcome(tmp_path, text)
    assert hit is None
    untouched(plan)


def rest_call(taken: str) -> str:
    return JS + HELPER.replace(
        "    return stripe.paymentIntents.create({ amount: 1, ...this.params(types) })\n",
        f"    const {{ {taken}, ...rest }} = data ?? {{}}\n"
        "    return stripe.paymentIntents.create({ amount: 1, ...rest, ...this.params(types) })\n",
    )


def test_the_rest_of_a_destructuring_that_takes_the_key_out_cannot_send_it(tmp_path):
    fields, hidden, switched = site(tmp_path, rest_call("payment_method_types"))
    assert hidden == {""}  # the rest is still hidden for every other key
    assert switched == {("payment_method_types", "allowed_payment_method_types", ">=2026-07-29", False)}
    hit, plan = outcome(tmp_path, rest_call("payment_method_types"))
    assert hit is None
    untouched(plan)


@pytest.mark.parametrize("taken", ["other", "[key]: k", "payment_method_types: { a }"])
def test_a_rest_that_does_not_name_the_key_may_still_send_it(tmp_path, taken):
    _, hidden, switched = site(tmp_path, rest_call(taken))
    assert hidden == {""}
    assert switched in (set(), {("payment_method_types", "allowed_payment_method_types", ">=2026-07-29", True)})
    hit, plan = outcome(tmp_path, rest_call(taken))
    assert hit == (POSSIBLE, NOT_VISIBLE)
    untouched(plan)
    assert [u.why for u in plan.not_attempted] == ["arguments_not_visible"]


def test_a_rest_used_before_the_spread_may_have_gained_the_key(tmp_path):
    text = rest_call("payment_method_types").replace("    return stripe", "    fill(rest)\n    return stripe")
    hit, _ = outcome(tmp_path, text)
    assert hit == (POSSIBLE, NOT_VISIBLE)


def test_a_version_switched_helper_passed_as_the_parameters_needs_no_change(tmp_path):
    text = JS + HELPER.replace("{ amount: 1, ...this.params(types) }", "this.params(types)")
    assert site(tmp_path, text)[2] == {("payment_method_types", "allowed_payment_method_types", ">=2026-07-29", False)}
    hit, plan = outcome(tmp_path, text)
    assert hit is None
    untouched(plan)


def test_a_returned_choice_on_anything_else_is_fixed_in_its_branch_through_the_helper(tmp_path):
    text = JS + HELPER.replace("{ amount: 1, ...this.params(types) }", "this.params(types)").replace(
        "apiVersion >= SINCE\n      ? { allowed_payment_method_types: types }", "cards\n      ? { amount: 1 }")
    hit, plan = outcome(tmp_path, text)
    assert hit == (AFFECTED, "payment_method_types is sent here")
    assert plan.unfixable == [] and len(plan.fixes) == 1, plan.to_dict()
    assert ": { allowed_payment_method_types: types }" in plan.fixes[0].content


def test_a_returned_choice_that_already_sends_the_replacement_is_refused_by_the_oracle(tmp_path):
    text = JS + HELPER.replace("{ amount: 1, ...this.params(types) }", "this.params(types)").replace(
        "apiVersion >= SINCE", "cards")
    hit, plan = outcome(tmp_path, text)
    assert hit == (AFFECTED, "payment_method_types is sent here")
    untouched(plan)
    assert [u.why for u in plan.unfixable] == ["oracle_changed_other_calls"]


# --- hostile helpers: read exactly or left hidden ----------------------------------------------


@pytest.mark.parametrize("helper", [
    # defined twice: which one is called is not visible
    "  params(types) { return { a: 1 } }\n  params(types) { return { payment_method_types: types } }\n",
    # returns its parameter
    "  params(types) { return types }\n",
    # nested ternary
    "  params(types) {\n    return apiVersion >= SINCE ? (x ? { a: 1 } : { b: 1 }) : { payment_method_types: types }\n  }\n",
    # the else-branch returns a variable
    "  params(types) {\n    return apiVersion >= SINCE ? { allowed_payment_method_types: types } : other\n  }\n",
    # Object.assign with something that is not a helper's object
    "  params(types) {\n    const res = {}\n    Object.assign(res, extra)\n    return res\n  }\n",
    # Object.assign with two sources
    "  params(types) {\n    const res = {}\n    Object.assign(res, this.more(), extra)\n    return res\n  }\n"
    "  more() { return { a: 1 } }\n",
])
def test_a_helper_that_cannot_be_read_leaves_the_spread_hidden(tmp_path, helper):
    text = JS + "class Pay {\n" + helper + (
        "  charge(types) {\n    return stripe.paymentIntents.create({ amount: 1, ...this.params(types) })\n  }\n}\n"
    )
    fields, hidden, switched = site(tmp_path, text)
    assert "" in hidden and switched == set()
    assert "allowed_payment_method_types" not in fields
    hit, plan = outcome(tmp_path, text)
    assert hit == (POSSIBLE, NOT_VISIBLE)
    untouched(plan)


def test_helpers_spreading_each_other_terminate_hidden(tmp_path):
    text = JS + (
        "class Pay {\n"
        "  a() { return { x: 1, ...this.b() } }\n"
        "  b() { return { y: 1, ...this.a() } }\n"
        "  charge() { return stripe.paymentIntents.create({ amount: 1, ...this.a() }) }\n"
        "}\n"
    )
    fields, hidden, _ = site(tmp_path, text)
    assert {"amount", "x", "y"} <= fields and "" in hidden


def test_a_helper_spread_under_a_key_adds_its_keys_there(tmp_path):
    text = JS + (
        "function meta(id) { return { order: id, ...(id ? { ref: id } : {}) } }\n"
        "stripe.paymentIntents.create({ amount: 1, metadata: { ...meta(id), note: 'x' } })\n"
    )
    fields, hidden, _ = site(tmp_path, text)
    assert fields == {"amount", "metadata", "metadata.order", "metadata.ref", "metadata.note"} and hidden == set()


# --- what counts as a version switch ----------------------------------------------------------


def switched_helper(test: str, then: str, other: str, extra: str = "") -> str:
    body = HELPER.replace("apiVersion >= SINCE", test).replace("{ allowed_payment_method_types: types }", "THEN")
    return JS + extra + body.replace("{ payment_method_types: types }", "OTHER").replace("THEN", then).replace("OTHER", other)


ALLOWED, OLD = "{ allowed_payment_method_types: types }", "{ payment_method_types: types }"


@pytest.mark.parametrize("test, then, other, since", [
    ("apiVersion >= SINCE", ALLOWED, OLD, ">=2026-07-29"),
    ("apiVersion > SINCE", ALLOWED, OLD, ">2026-07-29"),
    ("apiVersion < SINCE", OLD, ALLOWED, ">=2026-07-29"),
    ("SINCE <= apiVersion", ALLOWED, OLD, ">=2026-07-29"),
    ("apiVersion >= '2026-09-30.endive'", ALLOWED, OLD, ">=2026-09-30.endive"),
])
def test_a_choice_by_api_version_that_picks_the_replacement_later_is_a_switch(tmp_path, test, then, other, since):
    text = switched_helper(test, then, other)
    assert site(tmp_path, text)[2] == {("payment_method_types", "allowed_payment_method_types", since, False)}
    hit, plan = outcome(tmp_path, text)
    assert hit is None
    untouched(plan)


@pytest.mark.parametrize("test, then, other, extra", [
    ("useNew", ALLOWED, OLD, ""),  # a switch on something other than a version
    ("createdAt >= SINCE", ALLOWED, OLD, ""),  # a date, but not the API version
    ("apiVersion >= SINCE", OLD, ALLOWED, ""),  # the replacement on the older side
    ("apiVersion >= SINCE", "{ other_key: types }", OLD, ""),  # the replacement in neither branch
    ("apiVersion >= SINCE", "{ allowed_payment_method_types: types, payment_method_types: types }", OLD, ""),
    ("apiVersion >= LATER", ALLOWED, OLD, "let LATER = '2026-07-29';\n"),  # not a const
    ("apiVersion >= TAG", ALLOWED, OLD, "const TAG = 'v2';\n"),  # not a release date
])
def test_any_other_choice_is_read_as_sending_both_keys(tmp_path, test, then, other, extra):
    text = switched_helper(test, then, other, extra)
    fields, _, switched = site(tmp_path, text)
    assert "payment_method_types" in fields
    assert not any(s[:2] == ("payment_method_types", "allowed_payment_method_types") for s in switched)
    hit, plan = outcome(tmp_path, text)
    assert hit == (AFFECTED, "payment_method_types is sent here")
    untouched(plan)  # the fixer does not read through the spread: it finds nothing it may edit


def test_a_condition_that_is_more_than_a_comparison_leaves_the_helper_unread(tmp_path):
    text = switched_helper("apiVersion >= SINCE && x", ALLOWED, OLD)
    assert site(tmp_path, text)[1:] == ({""}, set())
    assert outcome(tmp_path, text)[0] == (POSSIBLE, NOT_VISIBLE)


def test_a_threshold_later_than_the_removal_still_sends_the_old_key_there(tmp_path):
    text = switched_helper("apiVersion >= '2026-12-01'", ALLOWED, OLD)
    assert site(tmp_path, text)[2] == {("payment_method_types", "allowed_payment_method_types", ">=2026-12-01", False)}
    hit, plan = outcome(tmp_path, text)
    assert hit == (AFFECTED, "payment_method_types is sent here on API versions before 2026-12-01, 2026-09-30.endive among them")
    untouched(plan)


def test_the_old_key_sent_anywhere_else_drops_the_switch(tmp_path):
    text = JS + HELPER.replace("{ amount: 1, ...this.params(types) }", "{ payment_method_types: t, ...this.params(types) }")
    assert site(tmp_path, text)[2] == set()
    assert outcome(tmp_path, text)[0] == (AFFECTED, "payment_method_types is sent here")


def test_two_helpers_with_different_thresholds_are_settled_one_by_one(tmp_path):
    late = HELPER.replace("params(types)", "late(types)").replace("apiVersion >= SINCE", "apiVersion >= '2026-12-01'")
    late = late.replace("class Pay {", "").rsplit("charge(types)", 1)[0]
    text = JS + HELPER.replace("class Pay {", "class Pay {" + late).replace(
        "...this.params(types) }", "...this.params(types), ...this.late(types) }")
    _, _, switched = site(tmp_path, text)
    assert switched == set()  # each one's old key is also sent by the other
    assert outcome(tmp_path, text)[0] == (AFFECTED, "payment_method_types is sent here")


def test_an_if_else_switch_is_not_recognised_and_stays_conservative(tmp_path):
    text = JS + HELPER.replace(
        "    return apiVersion >= SINCE\n      ? { allowed_payment_method_types: types }\n      : { payment_method_types: types }\n",
        "    if (apiVersion >= SINCE) {\n      return { allowed_payment_method_types: types }\n    }\n"
        "    return { payment_method_types: types }\n",
    )
    assert site(tmp_path, text)[2] == set()
    assert outcome(tmp_path, text)[0] == (AFFECTED, "payment_method_types is sent here")


# --- writes into a bound literal ---------------------------------------------------------------


@pytest.mark.parametrize("write, fields, hidden", [
    ("params.customer = who", {"amount", "customer"}, {"customer."}),
    ("params['customer'] = who", {"amount", "customer"}, {"customer."}),
    ("if (who) params.customer = who", {"amount", "customer"}, {"customer."}),
    ("params.customer.name = who", {"amount", "customer"}, {""}),  # a deeper write
    ("params[key] = who", {"amount"}, {""}),  # a computed key
    ("Object.assign(params, extra)", {"amount"}, {""}),
])
def test_a_plain_one_key_write_adds_its_key_and_hides_only_what_is_under_it(tmp_path, write, fields, hidden):
    text = JS + f"const params = {{ amount: 1 }};\n{write};\nstripe.paymentIntents.create(params);\n"
    got_fields, got_hidden, _ = site(tmp_path, text)
    assert (got_fields, got_hidden) == (fields, hidden)


def test_writes_still_hide_every_key_when_another_binding_may_reach_the_call(tmp_path):
    text = JS + (
        "let params = {};\n"
        "if (a) {\n  params = { payment_method_types: ['card'] };\n} else {\n  params = { amount: 1 };\n}\n"
        "params.customer = who;\n"
        "stripe.paymentIntents.create(params);\n"
    )
    fields, hidden, _ = site(tmp_path, text)
    assert "" in hidden


def test_a_write_of_the_documented_key_is_renamed_with_the_literal(tmp_path):
    text = JS + (
        "const params = { amount: 1, payment_method_types: ['card'] };\n"
        "if (link) params.payment_method_types = ['card', 'link'];\n"
        "stripe.paymentIntents.create(params);\n"
    )
    hit, plan = outcome(tmp_path, text)
    assert hit == (AFFECTED, "payment_method_types is sent here")
    assert plan.unfixable == [] and len(plan.fixes) == 1
    content = plan.fixes[0].content
    assert "payment_method_types" not in content.replace("allowed_payment_method_types", "")
    assert content.count("allowed_payment_method_types") == 2


def test_a_write_alone_is_renamed_where_it_is_written(tmp_path):
    text = JS + "const params = { amount: 1 };\nparams.payment_method_types = ['card'];\nstripe.paymentIntents.create(params);\n"
    hit, plan = outcome(tmp_path, text)
    assert hit == (AFFECTED, "payment_method_types is sent here")
    assert len(plan.fixes) == 1
    assert "params.allowed_payment_method_types = ['card'];" in plan.fixes[0].content


def test_a_deletion_written_by_a_statement_is_refused(tmp_path):
    text = (
        "const stripe = require('stripe')('sk');\n"
        "const params = { billing_details: { name: 'n' } };\n"
        "params.payto = { pay_id: 'x' };\n"
        "stripe.paymentMethods.update('pm_1', params);\n"
    )
    plan = plan_v2(tmp_path, {"pm.js": text})
    assert plan.fixes == []
    assert [(u.target, u.why) for u in plan.unfixable] == [("payto", "incomplete_binding")]


def test_ruby_writes_are_read_as_before(tmp_path):
    text = "client = Stripe::StripeClient.new('sk')\nparams = { amount: 1 }\nparams[:customer] = who\nclient.v1.payment_intents.create(params)\n"
    found = [u for u in scan(tmp_path, {"pay.rb": text}) if u.path == "/v1/payment_intents"]
    assert [(set(u.fields), set(u.hidden)) for u in found] == [({"amount", "customer"}, {""})]


# --- every binding that reaches the call -------------------------------------------------------


def branches(first: str, second: str) -> str:
    return JS + (
        "let p;\n"
        f"if (a) {{\n  p = {first};\n}} else {{\n  p = {second};\n}}\n"
        "stripe.paymentIntents.create(p);\n"
    )


def test_a_key_bound_in_one_branch_only_is_sent_and_renamed_there(tmp_path):
    text = branches("{ payment_method_types: ['card'] }", "{ amount: 1 }")
    assert site(tmp_path, text)[:2] == ({"amount", "payment_method_types"}, set())  # main read {amount}: a missed break
    hit, plan = outcome(tmp_path, text)
    assert hit == (AFFECTED, "payment_method_types is sent here")
    assert plan.unfixable == [] and len(plan.fixes) == 1
    assert "p = { allowed_payment_method_types: ['card'] };" in plan.fixes[0].content


def test_a_key_bound_in_both_branches_is_renamed_in_both(tmp_path):
    text = branches("{ payment_method_types: ['card'] }", "{ amount: 1, payment_method_types: ['link'] }")
    hit, plan = outcome(tmp_path, text)
    assert hit == (AFFECTED, "payment_method_types is sent here")
    assert len(plan.fixes) == 1
    content = plan.fixes[0].content
    assert content.count("allowed_payment_method_types") == 2 and "{ payment_method_types" not in content


def test_a_branch_bound_to_a_call_hides_every_key_and_the_site_is_never_edited(tmp_path):
    text = branches("build()", "{ payment_method_types: ['card'] }")  # the literal is the nearest binding
    fields, hidden, _ = site(tmp_path, text)
    assert "payment_method_types" in fields and "" in hidden
    hit, plan = outcome(tmp_path, text)
    assert hit == (AFFECTED, "payment_method_types is sent here")
    untouched(plan)
    assert [u.why for u in plan.unfixable] == ["incomplete_binding"]
    text = branches("{ payment_method_types: ['card'] }", "build()")  # the call is the nearest: read as before
    assert site(tmp_path, text)[1] == {""}
    hit, plan = outcome(tmp_path, text)
    assert hit == (POSSIBLE, NOT_VISIBLE)
    untouched(plan)
    assert [u.why for u in plan.not_attempted] == ["arguments_not_visible"]


def test_ruby_reads_every_binding_that_reaches_the_call_too(tmp_path):
    text = (
        "class Pay\n  def charge(a)\n    if a\n      params = { payment_method_types: ['card'] }\n    else\n"
        "      params = { amount: 1 }\n    end\n    Stripe::PaymentIntent.create(params)\n  end\nend\n"
    )
    found = [u for u in scan(tmp_path, {"pay.rb": text}) if u.path == "/v1/payment_intents"]
    assert [(set(u.fields), set(u.hidden)) for u in found] == [({"amount", "payment_method_types"}, set())]
    hit, plan = outcome(tmp_path, text, "pay.rb")
    assert hit == (AFFECTED, "payment_method_types is sent here")
    assert len(plan.fixes) == 1 and "params = { allowed_payment_method_types: ['card'] }" in plan.fixes[0].content


# --- the index keeps switches ------------------------------------------------------------------


def test_the_index_keeps_a_switch_and_rejects_a_malformed_one(tmp_path):
    usages = scan(tmp_path, {"pay.js": JS + HELPER})
    assert any(u.switched for u in usages)
    exported = json.loads(json.dumps(inventory.export(ScanResult("r", usages, 1, []), inventory.Recorder(None))))
    parsed = inventory.parse(exported)
    assert [u.switched for u in parsed.usages] == [u.switched for u in usages]
    conn = db.connect(":memory:")
    db.migrate(conn)
    db.upsert_installation(conn, 77, "octo")
    db.upsert_repo(conn, 501, 77, "octo/shop", None)
    inventory.store(conn, 501, "a" * 40, None, parsed, [], {}, "digest", "snap")
    loaded = inventory.load(conn, inventory.get(conn, 501))
    assert [u.switched for u in loaded.usages] == [u.switched for u in usages]
    for bad in (["payment_method_types", "allowed_payment_method_types", "2026-07-29", False],
                ["payment_method_types", "allowed_payment_method_types", ">=2026-07-29", "no"],
                [["x"], "allowed_payment_method_types", ">=2026-07-29", False],
                ["payment_method_types", "allowed_payment_method_types", ">=2026-07-29"]):
        broken = json.loads(json.dumps(exported))
        next(s for s in broken["sites"] if s["switched"])["switched"] = [bad]
        with pytest.raises(inventory.Invalid):
            inventory.parse(broken)
