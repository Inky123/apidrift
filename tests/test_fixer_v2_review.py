"""Regression tests for the adversarial review of fixer v2 (H1-H8, M1): each probe is either refused with a
reason or fixed with exactly the text below. Values are rewritten from an allow-list only."""

import pytest

from apidrift import fixer, fixvalue
from test_fixer_v2 import plan_v2

HEAD = {
    "a.js": "const stripe = require('stripe')('sk');\n",
    "a.ts": "import Stripe from 'stripe';\nconst stripe = new Stripe('sk');\n",
    "a.py": "import stripe\n\n\n",
    "a.rb": "require 'stripe'\n\n",
}
FC = "stripe.financialConnections.sessions.create({ filters: { countries: %s } });\n"
SUB = "stripe.subscriptions.update('sub_1', { billing_cycle_anchor: %s, proration_behavior: 'none' });\n"
PM = "stripe.paymentMethods.update('pm_1', %s);\n"
FC_PY = "stripe.financial_connections.Session.create(filters={'countries': %s})\n"
FC_RB = "Stripe::FinancialConnections::Session.create(filters: { countries: %s })\n"
SUB_PY = "stripe.Subscription.modify('sub_1', billing_cycle_anchor=%s, proration_behavior='none')\n"
SUB_RB = "Stripe::Subscription.update('sub_1', billing_cycle_anchor: %s, proration_behavior: 'none')\n"

REFUSED = {
    # H1: an array literal is only the value when it is the whole value
    "r_arr_sort": ("a.js", FC % "['US', 'CA'].sort()", "unsupported_value"),
    "r_arr_filter": ("a.js", FC % "['US', 'CA'].filter((c) => allowed.has(c))", "unsupported_value"),
    "r_arr_reverse": ("a.js", FC % "[home, 'US'].reverse()", "unsupported_value"),
    "r_arr_index": ("a.js", FC % "[['US'], ['CA']][i]", "unsupported_value"),
    "r_rb_arr_chain": ("a.rb", FC_RB % "['US', 'CA'].reverse", "unsupported_value"),
    # H2/H3: generics and regex literals are not read by the cursor; nothing near them is edited
    "w_ts_generic": ("a.ts", SUB % "pick<A, B>(x)", "unsupported_value"),
    "w_ts_generic_last": ("a.ts", "stripe.subscriptions.update('sub_1', { proration_behavior: 'none', billing_cycle_anchor: pick<A, B>(x) });\n", "unsupported_value"),
    "r_ts_generic": ("a.ts", FC % "pick<string, number>(x)", "unsupported_value"),
    "d_generic_ml": ("a.ts", PM % "{\n  metadata: {},\n  payto: build<A, B>(x),\n  billing_details: {},\n}", "unsupported_value"),
    "d_generic_last": ("a.ts", PM % "{ metadata: {}, payto: build<A, B>(x) }", "unsupported_value"),
    "w_regex_comma": ("a.js", SUB % "/a,}/.test(s) ? 'now' : 'unchanged'", "unsupported_value"),
    "d_regex_middle": ("a.js", PM % "{ metadata: {}, payto: /a,b/, billing_details: {} }", "unsupported_value"),
    "d_regex_brace": ("a.js", PM % "{ metadata: {}, payto: x.split(/}/), billing_details: {} }", "unsupported_value"),
    "r_js_regex_first": ("a.js", FC % "[/,]/.source]", "unsupported_value"),
    "d_value_regex": ("a.js", PM % "{ payto: /,}/.test(s), metadata: {} }", "unsupported_value"),
    "d_value_generic": ("a.ts", PM % "{ payto: f<A, B>(x), metadata: {} }", "unsupported_value"),
    # H4: a name bound to an object is already wrapped (or unknowable); never wrap it again
    "w_obj_var": ("a.js", "const a = { type: 'now' };\n" + SUB % "a", "unsupported_value"),
    "w_obj_var_rb": ("a.rb", "def f\n  a = { type: 'now' }\n  " + SUB_RB % "a" + "end\n", "unsupported_value"),
    "w_obj_freeze": ("a.js", SUB % "Object.freeze({ type: 'now' })", "unsupported_value"),
    "w_obj_from_params_var": ("a.js", "const anchor = { type: 'now' };\nconst params = { billing_cycle_anchor: anchor };\nstripe.subscriptions.update('sub_1', params);\n", "unsupported_value"),
    # H5: the subscription anchor was an enum: numbers and non-enum literals are not wrapped
    "w_number": ("a.js", SUB % "1767225600", "unsupported_value"),
    "w_float": ("a.js", SUB % "1.5", "unsupported_value"),
    "w_neg": ("a.js", SUB % "-1", "unsupported_value"),
    "w_ternary_num": ("a.js", SUB % "cond ? 'now' : 1767225600", "unsupported_value"),
    "w_var_int_js": ("a.js", "const anchor = 1767225600;\n" + SUB % "anchor", "unsupported_value"),
    "w_py_int": ("a.py", SUB_PY % "1767225600", "unsupported_value"),
    "w_rb_int": ("a.rb", SUB_RB % "1767225600", "unsupported_value"),
    "w_py_fstring": ("a.py", SUB_PY % "f'now'", "unsupported_value"),
    "w_call": ("a.js", SUB % "pickAnchor()", "unsupported_value"),
    # H6/H7: rename_first takes the first element only of what is known to be a list
    "r_var_string_js": ("a.js", "const cs = 'US';\n" + FC % "cs", "unsupported_value"),
    "r_var_string_py": ("a.py", "def f():\n    cs = 'US'\n    " + FC_PY % "cs", "unsupported_value"),
    "r_var_string_rb": ("a.rb", "def f\n  cs = 'US'\n  " + FC_RB % "cs" + "end\n", "unsupported_value"),
    "r_shorthand_string": ("a.js", "const countries = 'US';\nstripe.financialConnections.sessions.create({ filters: { countries } });\n", "unsupported_value"),
    "r_template": ("a.js", FC % "`US`", "unsupported_value"),
    "r_js_typeof": ("a.js", FC % "typeof x", "unsupported_value"),
    "r_optional_chain": ("a.js", FC % "user?.countries", "unsupported_value"),
    "r_ternary": ("a.js", FC % "c ? ['US'] : ['CA']", "unsupported_value"),
    "r_py_set": ("a.py", FC_PY % "{'US'}", "unsupported_value"),
    "r_py_generator": ("a.py", FC_PY % "(c for c in cs)", "unsupported_value"),
    "r_py_listcomp": ("a.py", FC_PY % "[c.upper() for c in cs]", "unsupported_value"),
    "r_py_tuple_call": ("a.py", FC_PY % "tuple(x)", "unsupported_value"),
    "r_py_or": ("a.py", FC_PY % "cs or ['US']", "unsupported_value"),
    "r_rb_bang": ("a.rb", FC_RB % "cs.compact!", "unsupported_value"),
    "r_rb_block": ("a.rb", FC_RB % "cs.map { |c| c.upcase }", "unsupported_value"),
    # M1: the bound literal is also used elsewhere, so editing it would change that use too
    "x_returned": ("a.js", "function f() {\n  const p = { payto: { pay_id: 'a' }, metadata: {} };\n  stripe.paymentMethods.update('pm_1', p);\n  return p;\n}\n", "incomplete_binding"),
    "x_passed_to_fn": ("a.js", "const p = { payto: { pay_id: 'a' }, metadata: {} };\nstripe.paymentMethods.update('pm_1', p);\nauditLog(p);\n", "incomplete_binding"),
    "x_wrap_shared_spread": ("a.js", "const p = { billing_cycle_anchor: 'now' };\nstripe.subscriptions.update('sub_1', p);\nlegacyClient.subscriptions.update('sub_2', { ...p });\n", "incomplete_binding"),
    "x_wrap_py_read_after": ("a.py", "def f():\n    p = {'billing_cycle_anchor': 'now'}\n    stripe.Subscription.modify('sub_1', **p)\n    if p['billing_cycle_anchor'] == 'now':\n        notify()\n", "incomplete_binding"),
    "x_rb_aliased": ("a.rb", "def f\n  p = { payto: { pay_id: 'a' }, metadata: {} }\n  q = p\n  Stripe::PaymentMethod.update('pm_1', p)\nend\n", "incomplete_binding"),
    # H8: a read of the wrapped key elsewhere would now see an object
    "x_wrap_read_elsewhere": ("a.js", SUB % "'now'" + "if (current.billing_cycle_anchor === 'now') notify();\n", "leftover_tokens"),
    "x_wrap_py_read_elsewhere": ("a.py", SUB_PY % "'now'" + "print(current['billing_cycle_anchor'])\n", "leftover_tokens"),
    "x_wrap_rb_read_elsewhere": ("a.rb", SUB_RB % "'now'" + "puts current[:billing_cycle_anchor]\n", "leftover_tokens"),
}


@pytest.mark.parametrize("probe", sorted(REFUSED))
def test_review_probe_is_refused(tmp_path, probe):
    name, body, why = REFUSED[probe]
    plan = plan_v2(tmp_path, {name: HEAD[name] + body})
    assert plan.fixes == [] and {u.why for u in plan.unfixable} == {why}, plan.to_dict()


FIXED = {
    "r_call": ("a.js", FC % "getCountries()", "country: (getCountries())[0]"),
    "r_member": ("a.js", FC % "user.countries", "country: user.countries[0]"),
    "r_array_first": ("a.js", FC % "[ /* main */ 'US', 'CA' ]", "country: 'US'"),
    "r_py_name": ("a.py", FC_PY % "cs", "'country': cs[0] if cs else None"),
    "r_py_subscript": ("a.py", FC_PY % "request.json['countries']", "'country': request.json['countries'][0] if request.json['countries'] else None"),
    "r_rb_ivar": ("a.rb", FC_RB % "@countries", "country: @countries[0]"),
    "w_ternary": ("a.js", SUB % "cond ? 'now' : 'unchanged'", "billing_cycle_anchor: { type: cond ? 'now' : 'unchanged' }"),
    "w_or": ("a.js", SUB % "anchor || 'unchanged'", "billing_cycle_anchor: { type: anchor || 'unchanged' }"),
    "w_const": ("a.js", "const ANCHOR = 'now';\n" + SUB % "ANCHOR", "billing_cycle_anchor: { type: ANCHOR }"),
    "w_py_ternary": ("a.py", SUB_PY % "'now' if c else 'unchanged'", "billing_cycle_anchor={'type': 'now' if c else 'unchanged'}"),
    "w_rb_ternary": ("a.rb", SUB_RB % "c ? :now : :unchanged", "billing_cycle_anchor: { type: c ? :now : :unchanged }"),
    "w_comment_in_value": ("a.js", "stripe.subscriptions.update('sub_1', {\n  billing_cycle_anchor: /* reset */ 'now' /* x */,\n});\n", "billing_cycle_anchor: /* reset */ { type: 'now' } /* x */,"),
}


@pytest.mark.parametrize("probe", sorted(FIXED))
def test_review_probe_inside_the_allow_list_is_fixed(tmp_path, probe):
    name, body, expected = FIXED[probe]
    plan = plan_v2(tmp_path, {name: HEAD[name] + body})
    assert plan.unfixable == [] and len(plan.fixes) == 1, plan.to_dict()
    assert expected in plan.fixes[0].content


def test_a_python_edit_that_does_not_parse_is_dropped_as_syntax(tmp_path, monkeypatch):
    monkeypatch.setattr(fixvalue, "py_rewrite", lambda *a, **k: ("{'type': 'now'", False))
    plan = plan_v2(tmp_path, {"a.py": HEAD["a.py"] + SUB_PY % "'now'"})
    assert plan.fixes == [] and [u.why for u in plan.unfixable] == ["syntax"]
    assert "syntax" in fixer.WHY
