"""PHP fixes, renames: what is edited, and the hostile cases that are refused with a reason.

Every case ends one of three ways: the exact expected file, a refusal that names why, or "possibly
affected, not attempted" when the arguments are not visible. Nothing is ever edited on a guess: there is no
PHP binary in the sandbox to catch a bad edit afterwards.
"""

import pytest

from apidrift import phpfix
from apidrift.app.prbody import UNFIXABLE_WHY
from apidrift.fixer import WHY
from test_fixer import NEW, OLD, plan_for

H = "<?php\n"
PI = "\\Stripe\\PaymentIntent::create"


def outcome(tmp_path, src, name="app/Pay.php"):
    """("fixed", content) | ("refused", [why, ...]) | ("not attempted", [line, ...]) | ("nothing", None)."""
    plan = plan_for(tmp_path, {name: src})
    if plan.fixes:
        assert plan.unfixable == [] and plan.not_attempted == [] and len(plan.fixes) == 1, plan.to_dict()
        assert all(s.action == "rename" and s.replacement == NEW for s in plan.fixes[0].sites)
        return "fixed", plan.fixes[0].content
    if plan.unfixable:
        assert plan.not_attempted == []
        return "refused", [u.why for u in plan.unfixable]
    if plan.not_attempted:
        return "not attempted", [u.line for u in plan.not_attempted]
    return "nothing", None


K = "@KEY@"  # in the templates below: the key the fix must rename (the old name written out is a decoy)


def renamed(src: str, count: int = 1) -> str:
    """``src`` with ``count`` quoted old keys renamed, left to right (values and other text untouched)."""
    out = src
    for quote in ("'", '"'):
        out = out.replace(f"{quote}{OLD}{quote}", f"{quote}{NEW}{quote}", count)
    return out


# --- fixed ----------------------------------------------------------------------------

FIXED = {
    "direct literal, trailing comma": f"{PI}(['amount' => 1, '{K}' => ['card'],]);\n",
    "array() syntax and a double-quoted key": f"{PI}(array('amount' => 1, \"{K}\" => array('card')));\n",
    "the last entry, no comma": f"{PI}([\n    'amount' => 1,\n    '{K}' => ['card']\n]);\n",
    "a comment between key and value": f"{PI}([\n  '{K}' /* which */ => // the list\n    ['card'],\n  'amount' => 1,\n]);\n",
    "non-ASCII text before the key": f"{PI}(['description' => 'café ☕ – naïve', '{K}' => ['card'], 'statement_descriptor' => 'Ünï']);\n",
    "an interpolated string holding => and the key name":
        f"{PI}(['description' => \"a {{$o['k']}} => '{OLD}' => b\", '{K}' => ['card']]);\n",
    "a short ternary as the value": f"function f($t) {{\n  return {PI}(['{K}' => $t ?: ['card'], 'amount' => 1]);\n}}\n",
    "a spread after the key (as in JavaScript)": f"function f($o) {{\n  return {PI}(['{K}' => ['card'], ...$o]);\n}}\n",
    "request options as the second argument": f"{PI}(['{K}' => ['card']], array_merge($auth, ['idempotency_key' => $k]));\n",
    "a StripeClient service": f"$stripe->paymentIntents->create(['amount' => 1, '{K}' => ['card']]);\n",
    "an imported class": f"use Stripe\\PaymentIntent;\n$i = PaymentIntent::create(['{K}' => ['card']]);\n",
    "a heredoc in another function":
        f"function g() {{\n  return <<<'TXT'\n  '{OLD}' => x\n  TXT;\n}}\nfunction f() {{\n  return {PI}(['{K}' => ['card']]);\n}}\n",
    "an attribute is not a comment": f"#[Route('/pay')]\nfunction f() {{\n  return {PI}(['{K}' => ['card']]);\n}}\n",
    "a binding in the same function":
        f"function f() {{\n  // '{OLD}' => ['old'],\n  $p = [\n    'amount' => 1,\n    '{K}' => ['card'],\n  ];\n\n  return {PI}($p, $opts);\n}}\n",
    "a parameter overwritten before the call": f"function f($p) {{\n  $p = ['{K}' => ['card']];\n  return {PI}($p);\n}}\n",
    "an arrow function sees the binding": f"function f() {{\n  $p = ['{K}' => ['card']];\n  return fn () => {PI}($p);\n}}\n",
    "a top-level script after its require": f"require 'vendor/autoload.php';\n$p = ['{K}' => ['card']];\n{PI}($p);\n",
}


@pytest.mark.parametrize("label", sorted(FIXED))
def test_rename_is_exactly_the_key(tmp_path, label):
    template = H + FIXED[label]
    assert outcome(tmp_path, template.replace(K, OLD)) == ("fixed", template.replace(K, NEW))


def test_crlf_line_endings_survive(tmp_path):
    src = (H + f"{PI}([\n  'amount' => 1,\n  '{OLD}' => ['card'],\n]);\n").replace("\n", "\r\n")
    assert outcome(tmp_path, src) == ("fixed", src.replace(OLD, NEW))


def test_both_branch_bindings_are_renamed(tmp_path):
    src = H + (
        "function f($o) {\n"
        "  if ($o) {\n"
        f"    $p = ['{OLD}' => ['card']];\n"
        "  } else {\n"
        f"    $p = ['{OLD}' => ['link'], 'amount' => 2];\n"
        "  }\n"
        f"  return {PI}($p);\n"
        "}\n"
    )
    assert outcome(tmp_path, src) == ("fixed", renamed(src, 2))


def test_a_key_written_by_a_statement_is_renamed_in_the_statement(tmp_path):
    src = H + f"function f() {{\n  $p = ['amount' => 1];\n  $p['{OLD}'] = ['card'];\n  return {PI}($p);\n}}\n"
    assert outcome(tmp_path, src) == ("fixed", src.replace(f"$p['{OLD}']", f"$p['{NEW}']"))


def test_a_key_written_twice_in_one_literal_is_renamed_twice(tmp_path):
    src = H + f"{PI}(['{OLD}' => ['card'], 'amount' => 1, '{OLD}' => ['link']]);\n"
    assert outcome(tmp_path, src) == ("fixed", renamed(src, 2))


def test_one_binding_feeding_two_calls_of_the_same_endpoint_is_one_edit_and_two_sites(tmp_path):
    src = H + f"function f() {{\n  $p = ['amount' => 1, '{OLD}' => ['card']];\n  {PI}($p);\n  {PI}($p);\n}}\n"
    plan = plan_for(tmp_path, {"a.php": src})
    assert plan.unfixable == [] and [s.line for s in plan.fixes[0].sites] == [4, 5]
    assert plan.fixes[0].content == renamed(src)


# --- refused, with a reason -------------------------------------------------------------

REFUSED = {
    "one binding feeding another endpoint too": (
        f"function f() {{\n  $p = ['amount' => 1, '{OLD}' => ['card']];\n  {PI}($p);\n  \\Stripe\\Charge::create($p);\n}}\n",
        "oracle_changed_other_calls"),
    "array_merge around the literal": (f"function f($o) {{\n  return {PI}(array_merge(['{OLD}' => ['card']], $o));\n}}\n", "incomplete_binding"),
    "a union with more parameters": (f"function f($o) {{\n  return {PI}(['{OLD}' => ['card']] + $o);\n}}\n", "incomplete_binding"),
    "the key appended to after the literal":
        (f"function f() {{\n  $p = ['{OLD}' => ['card']];\n  $p['{OLD}'][] = 'link';\n  return {PI}($p);\n}}\n", "incomplete_binding"),
    "the key read back before the call":
        (f"function f() {{\n  $p = ['{OLD}' => ['card']];\n  if (count($p['{OLD}'])) {{ g(); }}\n  return {PI}($p);\n}}\n", "incomplete_binding"),
    "the same key nested under another": (f"{PI}(['{OLD}' => ['card'], 'metadata' => ['{OLD}' => 'x']]);\n", "leftover_tokens"),
    "the same key elsewhere in the file":
        (f"function rules() {{\n  return ['{OLD}' => 'required|array'];\n}}\nfunction f() {{\n  return {PI}(['{OLD}' => ['card']]);\n}}\n", "leftover_tokens"),
    "the same key in the request options": (f"{PI}(['{OLD}' => ['card']], ['{OLD}' => 1]);\n", "leftover_tokens"),
    "a heredoc in the same function":
        (f"function f() {{\n  $sql = <<<SQL\n  select 1\n  SQL;\n  return {PI}(['{OLD}' => ['card']]);\n}}\n", "unbounded"),
    "a heredoc whose end differs between PHP versions": (f"$a = <<<EOT\n  EOT is here\nEOT;\n{PI}(['{OLD}' => ['card']]);\n", "unbounded"),
    "HTML between the binding and the call":
        (f"function f() {{\n  $p = ['{OLD}' => ['card']];\n  ?><b>x</b><?php\n  return {PI}($p);\n}}\n", "unbounded"),
    "compact() in the function": (f"function f() {{\n  $p = ['{OLD}' => ['card']];\n  log(compact('p'));\n  return {PI}($p);\n}}\n", "unbounded"),
    "a file with an unclosed bracket": (f"function f() {{\n  return {PI}(['{OLD}' => ['card']]);\n", "unbounded"),
    "a file with an unterminated string": (f"{PI}(['{OLD}' => ['card']]);\n$x = 'oops;\n", "unbounded"),
}


@pytest.mark.parametrize("label", sorted(REFUSED))
def test_rename_is_refused_with_a_reason(tmp_path, label):
    src, why = REFUSED[label]
    assert outcome(tmp_path, H + src) == ("refused", [why])


# --- not attempted: the arguments are not visible ----------------------------------------

NOT_VISIBLE = {
    "a parameter": f"function f($p) {{\n  return {PI}($p);\n}}\n$p = ['{OLD}' => ['card']];\n",
    "a key built by concatenation": f"function f() {{\n  return {PI}(['payment_method_' . 'types' => ['card'], 'amount' => 1]);\n}}\n",
    "a constant as the key": f"function f() {{\n  return {PI}([self::KEY => ['card'], 'amount' => 1]);\n}}\n",
    "extract() between binding and call": f"function f($o) {{\n  $p = ['{OLD}' => ['card']];\n  extract($o);\n  return {PI}($p);\n}}\n",
    "a variable variable": f"function f($n) {{\n  $p = ['{OLD}' => ['card']];\n  $$n = [];\n  return {PI}($p);\n}}\n",
    "an include between binding and call": f"$p = ['{OLD}' => ['card']];\nrequire 'extra.php';\n{PI}($p);\n",
    "a closure that imports the variable":
        f"function f() {{\n  $p = ['{OLD}' => ['card']];\n  return function () use ($p) {{\n    return {PI}($p);\n  }};\n}}\n",
    "an arrow function parameter of the same name": f"function f() {{\n  $p = ['{OLD}' => ['card']];\n  return fn ($p) => {PI}($p);\n}}\n",
    "a reference to the variable": f"function f() {{\n  $p = ['{OLD}' => ['card']];\n  $q = &$p;\n  return {PI}($p);\n}}\n",
    "foreach rebinding it": f"function f($all) {{\n  $p = ['{OLD}' => ['card']];\n  foreach ($all as $p) {{\n    {PI}($p);\n  }}\n}}\n",
    "a rebinding later in the loop":
        f"function f($xs) {{\n  $p = ['{OLD}' => ['card']];\n  foreach ($xs as $x) {{\n    {PI}($p);\n    $p = $x;\n  }}\n}}\n",
    "unset of the key": f"function f() {{\n  $p = ['amount' => 1, '{OLD}' => ['card']];\n  unset($p['{OLD}']);\n  return {PI}($p);\n}}\n",
    "an assignment inside an expression": f"function f($c) {{\n  $c and $p = ['{OLD}' => ['card']];\n  return {PI}($p);\n}}\n",
    "another case of the same switch":
        f"function f($k) {{\n  switch ($k) {{\n    case 1:\n      $p = ['{OLD}' => ['card']];\n      break;\n    default:\n      return {PI}($p);\n  }}\n}}\n",
    "if/endif syntax": f"function f($c) {{\n  $p = ['{OLD}' => ['card']];\n  if ($c):\n    $p = [];\n  endif;\n  return {PI}($p);\n}}\n",
}


@pytest.mark.parametrize("label", sorted(NOT_VISIBLE))
def test_rename_is_not_attempted_when_the_arguments_are_not_visible(tmp_path, label):
    kind, lines = outcome(tmp_path, H + NOT_VISIBLE[label])
    assert kind == "not attempted" and len(lines) == 1


# --- not code at all --------------------------------------------------------------------


@pytest.mark.parametrize("src", [
    f"$doc = <<<'EOT'\n{PI}(['{OLD}' => ['card']]);\nEOT;\n",
    f"// {PI}(['{OLD}' => ['card']]);\n/* {PI}(['{OLD}' => ['x']]); */\n# {PI}(['{OLD}' => ['y']]);\n{PI}(['amount' => 1]);\n",
    f"$s = '{PI}([\"{OLD}\" => [1]]);';\n",
    f"{PI}(['amount' => 1, 'metadata' => ['{OLD}' => 'x']]);\n",  # another field that happens to share the name
    f"class A {{ private $p = ['{OLD}' => ['x']]; }}\n",
])
def test_text_that_only_looks_like_a_call_is_left_alone(tmp_path, src):
    assert outcome(tmp_path, H + src) == ("nothing", None)


# --- the backstops ------------------------------------------------------------------------


def test_an_edit_that_would_unbalance_the_file_is_dropped_as_syntax(tmp_path, monkeypatch):
    """No PHP binary checks the result, so the structure check must: a rewrite that loses a bracket is refused."""
    from test_fixer_v2 import plan_v2

    monkeypatch.setattr(phpfix.Locator, "_rewrite", lambda self, m, start, end, q: ("['type' => 'now'", False))
    plan = plan_v2(tmp_path, {"a.php": H + "\\Stripe\\Subscription::update($id, ['billing_cycle_anchor' => 'now']);\n"})
    assert plan.fixes == [] and [u.why for u in plan.unfixable] == ["syntax"]


def test_every_reason_the_fixer_can_give_has_a_sentence_for_the_pull_request():
    assert set(WHY) <= set(UNFIXABLE_WHY) and "unbounded" in WHY


def test_php_pins_are_reported(tmp_path):
    files = {
        "app/Pay.php": H + f"{PI}(['{OLD}' => ['card']]);\n",
        "config/stripe.php": H + "return ['secret' => env('STRIPE_SECRET'), 'stripe_version' => '2025-03-31.basil'];\n",
        "bootstrap.php": H + "\\Stripe\\Stripe::setApiVersion('2024-06-20');\n",
    }
    plan = plan_for(tmp_path, files)
    assert [(p.file, p.line, p.version) for p in plan.pins] == [
        ("bootstrap.php", 2, "2024-06-20"), ("config/stripe.php", 2, "2025-03-31.basil"),
    ]
