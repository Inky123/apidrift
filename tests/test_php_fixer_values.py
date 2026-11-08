"""PHP fixes that touch more than a key: delete, rename_first and wrap_type, with their refusals.

Value rewrites are an allow-list: a value outside it is ``unsupported_value``, never a guess.
"""

import pytest

from test_fixer_v2 import plan_v2

H = "<?php\n"
PM = "\\Stripe\\PaymentMethod::update"
SUB = "\\Stripe\\Subscription::update"
FC = "\\Stripe\\FinancialConnections\\Session::create"


def fixed(tmp_path, src) -> str:
    plan = plan_v2(tmp_path, {"app/Billing.php": src})
    assert plan.unfixable == [] and plan.not_attempted == [] and len(plan.fixes) == 1, plan.to_dict()
    return plan.fixes[0].content


def refused(tmp_path, src) -> list[str]:
    plan = plan_v2(tmp_path, {"app/Billing.php": src})
    assert plan.fixes == [] and plan.not_attempted == [], plan.to_dict()
    return [u.why for u in plan.unfixable]


# --- delete -----------------------------------------------------------------------------


@pytest.mark.parametrize("args,after", [
    ("['payto' => ['pay_id' => 'a'], 'metadata' => []]", "['metadata' => []]"),
    ("['metadata' => [], 'payto' => ['pay_id' => 'a']]", "['metadata' => []]"),
    ("['metadata' => [], 'payto' => ['pay_id' => 'a'],]", "['metadata' => [], ]"),
    ("['metadata' => [], 'payto' => $p, 'billing_details' => []]", "['metadata' => [], 'billing_details' => []]"),
    ("array('metadata' => array(), \"payto\" => array('pay_id' => 'a'))", "array('metadata' => array())"),
    ("['metadata' => [], /* why */ 'payto' => 1]", "['metadata' => []]"),
    ("['metadata' => ['ça' => '☕'], 'payto' => ['pay_id' => 'é'], 'billing_details' => []]", "['metadata' => ['ça' => '☕'], 'billing_details' => []]"),
])
def test_delete_inline_removes_the_entry_and_one_comma(tmp_path, args, after):
    assert fixed(tmp_path, H + f"{PM}($id, {args});\n") == H + f"{PM}($id, {after});\n"


def test_delete_whole_lines_with_crlf(tmp_path):
    src = H + f"{PM}($id, [\r\n    'metadata' => [], // keep\r\n    'payto' => [\r\n        'pay_id' => 'a',\r\n    ],\r\n    'billing_details' => ['name' => 'J'],\r\n]);\r\n"
    assert fixed(tmp_path, src) == H + f"{PM}($id, [\r\n    'metadata' => [], // keep\r\n    'billing_details' => ['name' => 'J'],\r\n]);\r\n"


def test_delete_of_the_last_line_takes_the_comma_before_it_and_keeps_that_lines_comment(tmp_path):
    src = H + f"{PM}($id, [\n    'metadata' => [], # keep\n    'payto' => ['pay_id' => 'a']\n]);\n"
    assert fixed(tmp_path, src) == H + f"{PM}($id, [\n    'metadata' => [] # keep\n]);\n"


def test_delete_takes_a_slash_comment_with_its_line_and_leaves_a_hash_comment_in_place(tmp_path):
    src = H + f"{PM}($id, [\n    'payto' => ['pay_id' => 'a'], // legacy\n    'metadata' => [],\n]);\n"
    assert fixed(tmp_path, src) == H + f"{PM}($id, [\n    'metadata' => [],\n]);\n"
    src = H + f"{PM}($id, [\n    'payto' => ['pay_id' => 'a'], # legacy\n    'metadata' => [],\n]);\n"
    assert fixed(tmp_path, src) == H + f"{PM}($id, [\n    # legacy\n    'metadata' => [],\n]);\n"


def test_delete_in_a_binding_used_only_by_the_call(tmp_path):
    src = H + f"function f($id) {{\n  $p = ['payto' => ['pay_id' => 'a'], 'metadata' => []];\n  return {PM}($id, $p);\n}}\n"
    assert fixed(tmp_path, src) == src.replace("'payto' => ['pay_id' => 'a'], ", "")


def test_delete_next_to_a_spread_is_done_and_reported_as_possibly_still_sent(tmp_path):
    src = H + f"function f($id, $o) {{\n  return {PM}($id, ['payto' => ['pay_id' => 'a'], ...$o]);\n}}\n"
    plan = plan_v2(tmp_path, {"a.php": src})
    assert plan.fixes[0].content == src.replace("'payto' => ['pay_id' => 'a'], ", "")
    assert [p["target"] for p in plan.possibly_remaining] == ["payto"]


@pytest.mark.parametrize("body,why", [
    (f"{PM}($id, ['payto' => ['pay_id' => 'a']]);\n", "would_empty_literal"),
    (f"function f($id) {{\n  $p = ['payto' => ['pay_id' => 'a'], 'metadata' => []];\n  {PM}($id, $p);\n  audit($p);\n}}\n", "shared_literal"),
    (f"function f($id) {{\n  $p = ['metadata' => []];\n  $p['payto'] = ['pay_id' => 'a'];\n  return {PM}($id, $p);\n}}\n", "incomplete_binding"),
    (f"function f($id, $o) {{\n  return {PM}($id, array_merge(['payto' => 1, 'metadata' => []], $o));\n}}\n", "incomplete_binding"),
    (f"function f($id) {{\n  return {PM}($id, ['payto' => <<<X\n  a\n  X, 'metadata' => []]);\n}}\n", "unbounded"),
])
def test_delete_is_refused_with_a_reason(tmp_path, body, why):
    assert refused(tmp_path, H + body) == [why]


# --- rename_first ---------------------------------------------------------------------


@pytest.mark.parametrize("value,after", [
    ("['US', 'CA']", "'US'"),
    ("array('US')", "'US'"),
    ("[\n        'US',\n        'CA',\n    ]", "'US'"),
    ("array(strtoupper($a), 'CA')", "strtoupper($a)"),
    ("[$request->country, 'CA']", "$request->country"),
    ("$countries", "$countries[0] ?? null"),
    ("$request->countries", "$request->countries[0] ?? null"),
    ("$this->countries", "$this->countries[0] ?? null"),
])
def test_rename_first_takes_the_first_element(tmp_path, value, after):
    src = H + f"class A {{ function f($a, $request, $countries) {{\n    return {FC}(['permissions' => ['bälances'], /* ☕ */ 'filters' => ['countries' => {value}]]);\n}} }}\n"
    assert fixed(tmp_path, src) == src.replace(f"'countries' => {value}", f"'country' => {after}")


def test_rename_first_on_a_variable_bound_to_a_list_in_view(tmp_path):
    src = H + f"function f() {{\n  $c = ['US', 'CA'];\n  return {FC}(['filters' => ['countries' => $c]]);\n}}\n"
    assert fixed(tmp_path, src) == src.replace("'countries' => $c", "'country' => $c[0] ?? null")


@pytest.mark.parametrize("value", [
    "[]",  # nothing to take
    "['a' => 'US']",  # a map, not a list
    "[...$more, 'US']",
    "getCountries()",
    "$request->countries()",
    "self::COUNTRIES",
    "$countries ?: ['US']",
    "array_values($countries)",
    "'US'",
    "$map['countries']",
])
def test_rename_first_refuses_a_value_outside_the_allow_list(tmp_path, value):
    src = H + f"function f($countries, $request, $map, $more) {{\n    return {FC}(['filters' => ['countries' => {value}]]);\n}}\n"
    assert refused(tmp_path, src) == ["unsupported_value"]


@pytest.mark.parametrize("binding", ["'US'", "['a' => 'US']", "[]", "null", "5"])
def test_rename_first_refuses_a_variable_whose_visible_binding_is_not_a_list(tmp_path, binding):
    src = H + f"function f() {{\n  $c = {binding};\n  return {FC}(['filters' => ['countries' => $c]]);\n}}\n"
    assert refused(tmp_path, src) == ["unsupported_value"]


def test_rename_first_is_not_attempted_when_the_parent_is_a_variable(tmp_path):
    plan = plan_v2(tmp_path, {"a.php": H + f"function f($filters) {{\n  return {FC}(['filters' => $filters]);\n}}\n"})
    assert plan.fixes == [] and plan.unfixable == [] and [u.target for u in plan.not_attempted] == ["filters.countries"]


# --- wrap_type ------------------------------------------------------------------------


@pytest.mark.parametrize("value,after", [
    ("'now'", "['type' => 'now']"),
    ("'unchanged'", "['type' => 'unchanged']"),
    ("$anchor", "['type' => $anchor]"),
    ("$this->anchor", "['type' => $this->anchor]"),
    ("$request->anchor", "['type' => $request->anchor]"),
    ("$reset ? 'now' : 'unchanged'", "['type' => $reset ? 'now' : 'unchanged']"),
    ("$anchor ?? 'now'", "['type' => $anchor ?? 'now']"),
    ("$anchor ?: 'unchanged'", "['type' => $anchor ?: 'unchanged']"),
])
def test_wrap_type_wraps_documented_values(tmp_path, value, after):
    src = H + f"class A {{ function f($id, $anchor, $reset, $request) {{\n    return {SUB}($id, ['description' => 'ça – ☕', 'billing_cycle_anchor' => {value}, 'proration_behavior' => 'none']);\n}} }}\n"
    assert fixed(tmp_path, src) == src.replace(f"'billing_cycle_anchor' => {value}", f"'billing_cycle_anchor' => {after}")


def test_wrap_type_uses_the_quote_of_the_key_it_sits_under(tmp_path):
    src = H + f"{SUB}($id, [\"billing_cycle_anchor\" => \"now\"]);\n"
    assert fixed(tmp_path, src) == H + f"{SUB}($id, [\"billing_cycle_anchor\" => [\"type\" => \"now\"]]);\n"


@pytest.mark.parametrize("value", [
    "'later'",  # not a value the old enum held
    "1767225600",  # this endpoint never took a timestamp
    "null",
    "anchor()",
    "self::NOW",
    "$opts['anchor']",
    "\"{$anchor}\"",
    "'no' . 'w'",
    "$a ? 'now' : ($b ? 'now' : 'unchanged')",
    "$a /* why */ ? 'now' : 'unchanged'",
    "$a ? 'now' : 'later'",
])
def test_wrap_type_refuses_a_value_outside_the_allow_list(tmp_path, value):
    src = H + f"function f($id, $a, $b, $anchor, $opts) {{\n    return {SUB}($id, ['billing_cycle_anchor' => {value}]);\n}}\n"
    assert refused(tmp_path, src) == ["unsupported_value"]


@pytest.mark.parametrize("binding", ["['type' => 'now']", "5", "null"])
def test_wrap_type_refuses_a_variable_whose_visible_binding_is_not_a_string(tmp_path, binding):
    src = H + f"function f($id) {{\n  $a = {binding};\n  return {SUB}($id, ['billing_cycle_anchor' => $a]);\n}}\n"
    assert refused(tmp_path, src) == ["unsupported_value"]


def test_wrap_type_refuses_when_the_key_is_read_back_from_the_binding(tmp_path):
    src = H + f"function f($id) {{\n  $p = ['billing_cycle_anchor' => 'now'];\n  if ($p['billing_cycle_anchor'] === 'now') {{ log(); }}\n  return {SUB}($id, $p);\n}}\n"
    assert refused(tmp_path, src) == ["incomplete_binding"]


def test_a_value_already_wrapped_is_not_a_finding(tmp_path):
    plan = plan_v2(tmp_path, {"a.php": H + f"{SUB}($id, ['billing_cycle_anchor' => ['type' => 'now']]);\n"})
    assert plan.fixes == [] and plan.unfixable == [] and plan.not_attempted == []


def test_wrap_type_with_timestamps_takes_literals_only(tmp_path):
    call = "$stripe->invoices->createPreview"
    src = H + f"{call}(['subscription_details' => ['billing_cycle_anchor' => 1767225600]]);\n"
    assert fixed(tmp_path, src) == H + f"{call}(['subscription_details' => ['billing_cycle_anchor' => ['type' => 'timestamp', 'timestamp' => 1767225600]]]);\n"
    src = H + f"function f($stripe, $when) {{\n  return {call}(['subscription_details' => ['billing_cycle_anchor' => $when]]);\n}}\n"
    assert refused(tmp_path, src) == ["unsupported_value"]
