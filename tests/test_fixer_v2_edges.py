"""Fixer v2 hostile set: what each action edits, and what it refuses with a reason."""

import pytest

from apidrift import migrations
from apidrift.fixedit import Locator, apply
from apidrift.fixer import _candidate_edits
from apidrift.migrations import Migration
from apidrift.scanner import scan_repo
from test_fixer_v2 import PM, PREVIEW, SUB, plan_v2, specs

JS = "const stripe = require('stripe')('sk');\n"
PY = "import stripe\n\n\n"
RB = "require 'stripe'\n\n"


def whys(plan) -> list[tuple[int, str, str]]:
    return [(u.line, u.target, u.why) for u in plan.unfixable]


def content(plan) -> str:
    assert plan.unfixable == [] and len(plan.fixes) == 1, plan.to_dict()
    return plan.fixes[0].content


# --- delete ---------------------------------------------------------------------------


@pytest.mark.parametrize("call,after", [
    ("{ payto: { pay_id: 'a' }, metadata: {} }", "{ metadata: {} }"),
    ("{ metadata: {}, payto: { pay_id: 'a' } }", "{ metadata: {} }"),
    ("{ metadata: {}, payto: { pay_id: 'a' }, billing_details: {} }", "{ metadata: {}, billing_details: {} }"),
    ("{ metadata: {}, /* why */ payto: 1 }", "{ metadata: {} }"),
])
def test_delete_inline_removes_one_adjacent_comma(tmp_path, call, after):
    plan = plan_v2(tmp_path, {"a.js": JS + f"stripe.paymentMethods.update('pm_1', {call});\n"})
    assert content(plan) == JS + f"stripe.paymentMethods.update('pm_1', {after});\n"


def test_delete_whole_lines_with_crlf_keeps_the_neighbours_comment(tmp_path):
    src = JS + "stripe.paymentMethods.update('pm_1', {\r\n  metadata: {}, // keep\r\n  payto: {\r\n    pay_id: 'a',\r\n  }\r\n});\r\n"
    plan = plan_v2(tmp_path, {"a.js": src})
    assert content(plan) == JS + "stripe.paymentMethods.update('pm_1', {\r\n  metadata: {} // keep\r\n});\r\n"


def test_delete_of_the_only_key_would_empty_the_literal(tmp_path):
    plan = plan_v2(tmp_path, {
        "a.js": JS + "stripe.paymentMethods.update('pm_1', { payto: { pay_id: 'a' } });\n",
        "b.py": PY + "stripe.PaymentMethod.modify('pm_1', **{'payto': {'pay_id': 'a'}})\n",
    })
    assert whys(plan) == [(2, "payto", "would_empty_literal"), (4, "payto", "would_empty_literal")]


def test_python_keyword_delete_may_leave_the_call_with_only_its_id(tmp_path):
    plan = plan_v2(tmp_path, {"b.py": PY + "stripe.PaymentMethod.modify(\n    'pm_1',\n    payto={'pay_id': 'a'},\n)\n"})
    assert content(plan) == PY + "stripe.PaymentMethod.modify(\n    'pm_1',\n)\n"


@pytest.mark.parametrize("name,src", [
    ("a.js", JS + "const p = { metadata: {} };\np.payto = { pay_id: 'a' };\nstripe.paymentMethods.update('pm_1', p);\n"),
    ("b.py", PY + "def f():\n    p = {'metadata': {}}\n    p['payto'] = {'pay_id': 'a'}\n    stripe.PaymentMethod.modify('pm_1', **p)\n"),
    ("c.rb", RB + "def f\n  p = { metadata: {} }\n  p[:payto] = { pay_id: 'a' }\n  Stripe::PaymentMethod.update('pm_1', p)\nend\n"),
])
def test_a_key_written_by_a_statement_is_not_deleted(tmp_path, name, src):
    plan = plan_v2(tmp_path, {name: src})
    assert plan.fixes == [] and [w for _, _, w in whys(plan)] == ["incomplete_binding"]


def test_ruby_rocket_and_keyword_deletes(tmp_path):
    plan = plan_v2(tmp_path, {"c.rb": RB + (
        "Stripe::PaymentMethod.update('pm_1', { 'payto' => { pay_id: 'a' }, :metadata => {} })\n"
        "Stripe::PaymentMethod.update('pm_1', metadata: {}, payto: { pay_id: 'a' })\n"
    )})
    assert content(plan) == RB + (
        "Stripe::PaymentMethod.update('pm_1', { :metadata => {} })\n"
        "Stripe::PaymentMethod.update('pm_1', metadata: {})\n"
    )


# --- rename_first and nested paths ------------------------------------------------------


@pytest.mark.parametrize("value,after", [
    ("['US']", "'US'"),
    ("[ 'GB' , 'US' ]", "'GB'"),
    ("countriesFor(user)", "(countriesFor(user))[0]"),
    ("user.countries", "user.countries[0]"),
    ("loadCountries(user)[1]", "(loadCountries(user)[1])[0]"),
])
def test_rename_first_value_forms(tmp_path, value, after):
    call = "stripe.financialConnections.sessions.create({ filters: { countries: %s }, permissions: ['balances'] });\n"
    plan = plan_v2(tmp_path, {"a.js": JS + call % value})
    assert content(plan) == JS + (call % after).replace("countries: ", "country: ", 1)


@pytest.mark.parametrize("value", ["[]", "[...more]", "'US'", "null", "flag ? ['US'] : ['CA']"])
def test_rename_first_refuses_values_it_cannot_take_the_first_of(tmp_path, value):
    plan = plan_v2(tmp_path, {"a.js": JS + "stripe.financialConnections.sessions.create({ filters: { countries: %s } });\n" % value})
    assert whys(plan) == [(2, "filters.countries", "unsupported_value")]


def test_python_nested_value_bound_to_a_variable_is_an_incomplete_binding(tmp_path):
    src = PY + "def f():\n    filters = {'countries': ['US']}\n    stripe.financial_connections.Session.create(filters=filters)\n"
    plan = plan_v2(tmp_path, {"b.py": src})
    assert whys(plan) == [(6, "filters.countries", "incomplete_binding")]


def test_python_name_becomes_a_guarded_index_and_non_ascii_before_the_value(tmp_path):
    src = PY + "def f(picked):\n    stripe.financial_connections.Session.create(filters={'countries': picked})  # café é\n"
    plan = plan_v2(tmp_path, {"b.py": src})
    assert content(plan) == src.replace("'countries': picked", "'country': picked[0] if picked else None")
    plan = plan_v2(tmp_path, {"b.py": PY + "stripe.financial_connections.Session.create(filters={'countries': list(pick('é'))})\n"})
    assert whys(plan) == [(4, "filters.countries", "unsupported_value")]  # a call may return an empty list


def test_a_bracket_segment_is_a_nested_array(tmp_path):
    text = JS + "stripe.paymentMethods.update('pm_1', { payto: [{ pay_id: 'a' }] });\n"
    (tmp_path / "a.js").write_text(text)
    index, _ = specs()
    usage = scan_repo(tmp_path, index).usages[0]
    m = Migration("post", PM, "payto[].pay_id", "", "x", "doc", "note", action="delete")
    assert _candidate_edits(Locator(text, "javascript", index), usage, m) == ("nested_array", [])


# --- wrap_type ------------------------------------------------------------------------


@pytest.mark.parametrize("value,after", [
    ("reset ? 'now' : 'unchanged'", "{ type: reset ? 'now' : 'unchanged' }"),
    ("\"now\"", "{ type: \"now\" }"),
    ("ANCHOR", "{ type: ANCHOR }"),
])
def test_wrap_any_on_subscription_update(tmp_path, value, after):
    plan = plan_v2(tmp_path, {"a.js": JS + f"stripe.subscriptions.update('sub_1', {{ billing_cycle_anchor: {value} }});\n"})
    assert content(plan) == JS + f"stripe.subscriptions.update('sub_1', {{ billing_cycle_anchor: {after} }});\n"
    assert plan.not_attempted == []  # the possible required .type is answered by the same wrap


@pytest.mark.parametrize("call", [
    "stripe.subscriptions.update('sub_1', { billing_cycle_anchor: 'later' });",
    "stripe.subscriptions.update('sub_1', { billing_cycle_anchor: null });",
    "stripe.invoices.createPreview({ subscription_details: { billing_cycle_anchor: anchor } });",
    "stripe.invoices.createPreview({ subscription_details: { billing_cycle_anchor: 'soon' } });",
])
def test_wrap_refuses_values_the_documentation_does_not_cover(tmp_path, call):
    plan = plan_v2(tmp_path, {"a.js": JS + call + "\n"})
    assert plan.fixes == [] and [w for _, _, w in whys(plan)] == ["unsupported_value"]
    assert plan.not_attempted == []


def test_an_object_without_type_is_one_unfixable_site_not_two(tmp_path):
    plan = plan_v2(tmp_path, {"a.js": JS + "stripe.invoices.createPreview({ subscription_details: { billing_cycle_anchor: { timestamp: 1 } } });\n"})
    assert whys(plan) == [(2, "subscription_details.billing_cycle_anchor", "unsupported_value")]


def test_an_already_wrapped_value_is_not_a_finding(tmp_path):
    plan = plan_v2(tmp_path, {
        "a.js": JS + "stripe.subscriptions.update('sub_1', { billing_cycle_anchor: { type: 'now' } });\n",
        "b.py": PY + "stripe.Subscription.modify('sub_1', billing_cycle_anchor={'type': 'now'})\n",
    })
    assert plan.fixes == [] and plan.unfixable == [] and plan.not_attempted == []


def test_python_wrap_timestamp_and_ruby_paren_less_call(tmp_path):
    plan = plan_v2(tmp_path, {
        "b.py": PY + "stripe.Invoice.create_preview(subscription_details=dict(billing_cycle_anchor=1767225600))\n",
        "c.rb": RB + "def f\n  Stripe::Invoice.create_preview customer: 'cus_1', subscription_details: { billing_cycle_anchor: 'now' }\nend\n",
    })
    assert plan.unfixable == [] and len(plan.fixes) == 2
    by_file = {f.file: f.content for f in plan.fixes}
    assert by_file["b.py"] == PY + (
        "stripe.Invoice.create_preview(subscription_details=dict(billing_cycle_anchor="
        "{'type': 'timestamp', 'timestamp': 1767225600}))\n"
    )
    assert by_file["c.rb"] == RB + (
        "def f\n  Stripe::Invoice.create_preview customer: 'cus_1', subscription_details: { billing_cycle_anchor: { type: 'now' } }\nend\n"
    )


def test_wrap_does_not_count_as_a_leftover_and_the_text_outside_is_untouched(tmp_path):
    src = JS + "// billing_cycle_anchor: 'now'\nstripe.subscriptions.update('sub_1', { billing_cycle_anchor: 'now' });\n"
    plan = plan_v2(tmp_path, {"a.js": src})
    assert content(plan).startswith(JS + "// billing_cycle_anchor: 'now'\n") and plan.leftovers == []


def test_every_site_payload_carries_its_action(tmp_path):
    plan = plan_v2(tmp_path, {"a.js": JS + (
        "stripe.subscriptions.update('sub_1', { billing_cycle_anchor: 'now' });\n"
        "stripe.paymentMethods.update('pm_1', { payto: {}, metadata: {} });\n"
        "stripe.financialConnections.sessions.create({ filters: { countries: ['US'] } });\n"
    )})
    sites = sorted((s["target"], s["replacement"], s["action"]) for s in plan.to_dict()["fixes"][0]["sites"])
    assert sites == [
        ("billing_cycle_anchor", "billing_cycle_anchor", "wrap_type"),
        ("filters.countries", "filters.country", "rename_first"),
        ("payto", "", "delete"),
    ]
    assert {m.action for m in plan.migrations} == {"wrap_type", "rename_first", "delete"}
    assert migrations.for_site(SUB, "billing_cycle_anchor") in plan.migrations
    assert migrations.for_site(PREVIEW, "subscription_details.billing_cycle_anchor") not in plan.migrations


def test_a_literal_shared_by_two_calls_is_not_edited(tmp_path):
    plan = plan_v2(tmp_path, {"a.js": JS + (
        "const params = { billing_cycle_anchor: 'now' };\n"
        "stripe.subscriptions.update('sub_1', params);\n"
        "stripe.subscriptions.resume('sub_1', params);\n"
    )})
    assert plan.fixes == [] and [w for _, _, w in whys(plan)] == ["incomplete_binding"] * 2


def test_a_literal_also_fed_to_an_endpoint_outside_the_table_is_not_edited(tmp_path):
    plan = plan_v2(tmp_path, {"a.js": JS + (
        "const params = { payto: { pay_id: 'a' }, metadata: {} };\n"
        "stripe.paymentMethods.update('pm_1', params);\n"
        "stripe.charges.create(params);\n"
    )})
    assert plan.fixes == [] and whys(plan) == [(3, "payto", "incomplete_binding")]


@pytest.mark.parametrize("language,text,expected", [
    ("javascript", "stripe.financialConnections.sessions.create({ filters: { countries: x, other: 1 } });",
     "stripe.financialConnections.sessions.create({ filters: { country: x, other: 1 } });"),
    ("ruby", "Stripe::FinancialConnections::Session.create(filters: { :countries => x })",
     "Stripe::FinancialConnections::Session.create(filters: { :country => x })"),
    ("python", "stripe.financial_connections.Session.create(filters=dict(countries=x))",
     "stripe.financial_connections.Session.create(filters=dict(country=x))"),
])
def test_a_nested_rename_edits_only_the_leaf_and_keeps_the_parent(tmp_path, language, text, expected):
    head = {"javascript": JS, "ruby": RB, "python": PY}[language]
    name = {"javascript": "a.js", "ruby": "a.rb", "python": "a.py"}[language]
    (tmp_path / name).write_text(head + text + "\n")
    index, _ = specs()
    usage = scan_repo(tmp_path, index).usages[0]
    m = Migration("post", usage.path, "filters.countries", "filters.country", "x", "doc", "note")  # action rename
    why, edits = _candidate_edits(Locator(head + text + "\n", language, index), usage, m)
    assert why is None and len(edits) == 1
    assert apply(head + text + "\n", edits) == head + expected + "\n"


@pytest.mark.parametrize("the_id", ["req.params.id", "`sub_${n}`", "ids[0]", "getId()"])
def test_any_expression_in_the_id_slot_is_the_id_not_the_parameters(tmp_path, the_id):
    plan = plan_v2(tmp_path, {"a.js": JS + f"stripe.subscriptions.update({the_id}, {{ billing_cycle_anchor: 'now' }});\n"})
    assert content(plan) == JS + f"stripe.subscriptions.update({the_id}, {{ billing_cycle_anchor: {{ type: 'now' }} }});\n"


def test_ruby_expression_in_the_id_slot(tmp_path):
    plan = plan_v2(tmp_path, {"a.rb": RB + "Stripe::PaymentMethod.update(params[:id], { metadata: {}, payto: { pay_id: 'a' } })\n"})
    assert content(plan) == RB + "Stripe::PaymentMethod.update(params[:id], { metadata: {} })\n"


def test_an_expression_where_parameters_belong_is_still_refused(tmp_path):
    plan = plan_v2(tmp_path, {"a.js": JS + "stripe.subscriptions.update('sub_1', build({ billing_cycle_anchor: 'now' }));\n"})
    assert plan.fixes == []
