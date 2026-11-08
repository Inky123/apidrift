"""In TypeScript, ``wrap_type`` edits only values whose type still checks once wrapped (twenty's ``anchor``)."""

import pytest

from test_fixer_v2 import plan_v2

TS = "import Stripe from 'stripe';\nconst stripe = new Stripe('sk');\n"
CALL = "  return stripe.subscriptions.update('sub_1', {{ billing_cycle_anchor: {value} }});\n"


def run(tmp_path, body: str, value: str, name: str = "a.ts"):
    text = TS + "export function go(reset: boolean, opts: any) {\n" + body + CALL.format(value=value) + "}\n"
    return text, plan_v2(tmp_path, {name: text})


@pytest.mark.parametrize("body,value,after", [
    ("", "'now'", "{ type: 'now' }"),
    ("", "reset ? 'now' : 'unchanged'", "{ type: reset ? 'now' : 'unchanged' }"),
    ("  const anchor = 'now';\n", "anchor", "{ type: anchor }"),
    ("  const anchor = 'unchanged' as const;\n", "anchor", "{ type: anchor }"),
    ("  const anchor = 'now';\n", "reset ? anchor : 'unchanged'", "{ type: reset ? anchor : 'unchanged' }"),
])
def test_a_literal_or_a_const_bound_to_one_is_wrapped(tmp_path, body, value, after):
    text, plan = run(tmp_path, body, value)
    assert plan.unfixable == [] and len(plan.fixes) == 1, plan.to_dict()
    assert plan.fixes[0].content == text.replace(f"billing_cycle_anchor: {value} }}", f"billing_cycle_anchor: {after} }}")


@pytest.mark.parametrize("label,body,value", [
    ("twenty: destructured from a helper's result", "  const { anchor } = compute();\n", "anchor"),
    ("a parameter", "", "opts"),
    ("a member", "", "opts.anchor"),
    ("a let widens to string", "  let anchor = 'now';\n", "anchor"),
    ("an annotated const", "  const anchor: Stripe.SubscriptionUpdateParams.BillingCycleAnchor = 'now';\n", "anchor"),
    ("a const of another value", "  const anchor = compute();\n", "anchor"),
    ("a const bound twice", "  const anchor = 'now';\n  if (reset) { const anchor = 'x'; }\n", "anchor"),
    ("a module-level const (another function)", "", "ANCHOR"),
    ("a const in a branch of the value", "  const anchor = 'now';\n", "reset ? anchor : opts"),
])
def test_any_other_value_is_refused_in_typescript(tmp_path, label, body, value):
    text, plan = run(tmp_path, body, value)
    if label.startswith("a module-level"):
        plan = plan_v2(tmp_path, {"a.ts": text.replace("export function", "const ANCHOR = 'now';\nexport function")})
    assert plan.fixes == [] and [u.why for u in plan.unfixable] == ["unsupported_value"], plan.to_dict()


def test_javascript_keeps_wrapping_a_name(tmp_path):
    _, plan = run(tmp_path, "  const { anchor } = compute();\n", "anchor", name="a.js")
    assert len(plan.fixes) == 1 and "billing_cycle_anchor: { type: anchor }" in plan.fixes[0].content
