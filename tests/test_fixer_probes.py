"""The adversarial probes: never a wrong edit, never success with a known leftover."""

from test_fixer import NEW, OLD, STRIPE_JS, FixPlan, changed_lines, only_fix, plan_for
def unfixable_why(plan: FixPlan) -> list[tuple[int, str]]:
    assert plan.fixes == [], plan.to_dict()
    return [(u.line, u.why) for u in plan.unfixable]


def test_dead_binding_that_still_names_the_key_blocks_the_file(tmp_path):
    plan = plan_for(tmp_path, {"pay.js": STRIPE_JS + (
        "async function go() {\n"
        "  let params = { payment_method_types: ['old'] };\n"
        "  params = { payment_method_types: ['new'], amount: 1 };\n"
        "  return stripe.paymentIntents.create(params);\n"
        "}\n")})
    assert unfixable_why(plan) == [(5, "leftover_tokens")] and plan.leftovers == [("pay.js", 3)]


def test_braceless_if_else_bodies_are_conditional_bindings(tmp_path):
    fix = only_fix(plan_for(tmp_path, {"pay.js": STRIPE_JS + (
        "async function go(flag) {\n"
        "  let params;\n"
        "  if (flag) params = { payment_method_types: ['card'], amount: 1 };\n"
        "  else params = { payment_method_types: ['ideal'], amount: 2 };\n"
        "  return stripe.paymentIntents.create(params);\n"
        "}\n"
        "async function go2(flag) {\n"
        "  let params = { payment_method_types: ['card'], amount: 1 };\n"
        "  if (flag) params = { payment_method_types: ['ideal'], amount: 2 };\n"
        "  return stripe.paymentIntents.create(params);\n"
        "}\n")}))
    assert sorted(changed_lines(fix)) == [4, 5, 9, 10] and plan_leftovers_empty(fix)


def plan_leftovers_empty(fix) -> bool:
    return OLD not in fix.content.replace(NEW, "")


def test_switch_case_and_default_labels_do_not_swallow_the_binding(tmp_path):
    fix = only_fix(plan_for(tmp_path, {"pay.ts": (
        "import Stripe from 'stripe';\n"
        "const stripe = new Stripe('sk');\n"
        "const A = 'a';\n"
        "enum Kind { A, B }\n"
        "export async function go(kind: Kind) {\n"
        "  let params: Stripe.PaymentIntentCreateParams;\n"
        "  switch (kind) {\n"
        "    case Kind.A:\n"
        "      params = { payment_method_types: ['card'], amount: 1, currency: 'usd' };\n"
        "      break;\n"
        "    case A: params = { payment_method_types: ['sepa'], amount: 2, currency: 'eur' }; break;\n"
        "    default:\n"
        "      params = { payment_method_types: ['ideal'], amount: 2, currency: 'eur' };\n"
        "  }\n"
        "  return stripe.paymentIntents.create(params);\n"
        "}\n")}))
    assert sorted(changed_lines(fix)) == [9, 11, 13] and plan_leftovers_empty(fix)


def test_a_parameter_shadowing_the_variable_makes_the_site_unfixable(tmp_path):
    js = plan_for(tmp_path / "js", {"pay.js": STRIPE_JS + (
        "const params = { payment_method_types: ['card'], amount: 1 };\n"
        "async function go(params) { return stripe.paymentIntents.create(params); }\n")})
    assert unfixable_why(js) == [(3, "incomplete_binding")]
    py = plan_for(tmp_path / "py", {"pay.py": (
        "import stripe\n"
        "params = {'payment_method_types': ['card'], 'amount': 1}\n"
        "def go(params):\n"
        "    return stripe.PaymentIntent.create(**params)\n")})
    assert unfixable_why(py) == [(4, "incomplete_binding")]
    rb = plan_for(tmp_path / "rb", {"pay.rb": (
        "require 'stripe'\n"
        "def build\n"
        "  params = { payment_method_types: ['card'], amount: 1 }\n"
        "  params\n"
        "end\n"
        "def create_intent(params)\n"
        "  Stripe::PaymentIntent.create(params)\n"
        "end\n")})
    assert unfixable_why(rb) == [(7, "incomplete_binding")]


def test_a_mutation_in_another_function_is_not_an_edit_site(tmp_path):
    plan = plan_for(tmp_path, {"pay.js": STRIPE_JS + (
        "const params = { payment_method_types: ['card'], amount: 1 };\n"
        "function audit() {\n"
        "  const params = { amount: 2 };\n"
        "  params.payment_method_types = ['x'];\n"
        "}\n"
        "async function go() { return stripe.paymentIntents.create(params); }\n")})
    # the other function's write is left alone, and because it still names the key the file is not offered
    assert unfixable_why(plan) == [(7, "leftover_tokens")] and plan.leftovers == [("pay.js", 5)]


def test_reads_in_place_operations_and_deletes_of_the_key_make_the_site_unfixable(tmp_path):
    js = plan_for(tmp_path / "js", {"pay.js": STRIPE_JS + (
        "async function a(eu) {\n"
        "  const params = { payment_method_types: ['card'], amount: 1 };\n"
        "  if (eu) params.payment_method_types.push('sepa_debit');\n"
        "  return stripe.paymentIntents.create(params);\n"
        "}\n"
        "async function b() {\n"
        "  const params = { payment_method_types: ['card'], amount: 1 };\n"
        "  delete params.payment_method_types;\n"
        "  return stripe.paymentIntents.create(params);\n"
        "}\n")})
    assert unfixable_why(js) == [(5, "incomplete_binding"), (10, "incomplete_binding")]
    py = plan_for(tmp_path / "py", {"pay.py": (
        "import stripe\n"
        "def a(eu):\n"
        "    params = {'payment_method_types': ['card'], 'amount': 1}\n"
        "    if eu:\n"
        "        params['payment_method_types'].append('sepa_debit')\n"
        "    return stripe.PaymentIntent.create(**params)\n"
        "def b():\n"
        "    params = {'payment_method_types': ['card'], 'amount': 1}\n"
        "    del params['payment_method_types']\n"
        "    return stripe.PaymentIntent.create(**params)\n"
        "def c():\n"
        "    params = {'payment_method_types': ['card'], 'amount': 1}\n"
        "    params.pop('payment_method_types')\n"
        "    return stripe.PaymentIntent.create(**params)\n")})
    assert unfixable_why(py) == [(6, "incomplete_binding"), (10, "incomplete_binding"), (14, "incomplete_binding")]
    rb = plan_for(tmp_path / "rb", {"pay.rb": (
        "require 'stripe'\n"
        "post '/pay' do\n"
        "  opts = { payment_method_types: ['card'], amount: 1 }\n"
        "  opts[:payment_method_types] << 'sepa_debit' if eu?\n"
        "  Stripe::PaymentIntent.create(opts)\n"
        "end\n")})
    assert unfixable_why(rb) == [(5, "incomplete_binding")]


def test_a_read_after_the_call_is_a_blocking_leftover(tmp_path):
    plan = plan_for(tmp_path, {"pay.js": STRIPE_JS + (
        "async function go() {\n"
        "  const params = { payment_method_types: ['card'], amount: 1 };\n"
        "  const intent = await stripe.paymentIntents.create(params);\n"
        "  log(params.payment_method_types);\n"
        "}\n")})
    assert unfixable_why(plan) == [(4, "leftover_tokens")] and plan.leftovers == [("pay.js", 5)]


def test_a_rebinding_after_the_call_inside_a_loop_makes_the_site_unfixable(tmp_path):
    js = plan_for(tmp_path / "js", {"pay.js": STRIPE_JS + (
        "async function go(items) {\n"
        "  let params = { payment_method_types: ['card'], amount: 1 };\n"
        "  for (const it of items) {\n"
        "    await stripe.paymentIntents.create(params);\n"
        "    params = { payment_method_types: ['ideal'], amount: it };\n"
        "  }\n"
        "}\n")})
    assert unfixable_why(js) == [(5, "incomplete_binding")]
    py = plan_for(tmp_path / "py", {"pay.py": (
        "import stripe\n"
        "def go(items):\n"
        "    params = {'payment_method_types': ['card'], 'amount': 1}\n"
        "    for it in items:\n"
        "        stripe.PaymentIntent.create(**params)\n"
        "        params = {'payment_method_types': ['ideal'], 'amount': it}\n")})
    assert unfixable_why(py) == [(5, "incomplete_binding")]


def test_ruby_merge_bang_with_a_literal_is_renamed_and_without_one_is_not_attempted(tmp_path):
    plan = plan_for(tmp_path, {"pay.rb": (
        "require 'stripe'\n"
        "post '/a' do\n"
        "  opts = { amount: 1 }\n"
        "  opts.merge!({ payment_method_types: ['card'] })\n"
        "  Stripe::PaymentIntent.create(opts)\n"
        "end\n"), "other.rb": (
        "require 'stripe'\n"
        "post '/b' do\n"
        "  opts = { amount: 1, currency: 'usd' }\n"
        "  opts.merge!(payment_method_types: ['card'])\n"
        "  Stripe::PaymentIntent.create(opts)\n"
        "end\n")})
    assert [(f.file, sorted(changed_lines(f))) for f in plan.fixes] == [("pay.rb", [4])]
    assert plan.unfixable == []
    assert [(u.file, u.line, u.why) for u in plan.not_attempted] == [("other.rb", 5, "arguments_not_visible")]


def test_possible_hits_are_listed_as_not_attempted(tmp_path):
    plan = plan_for(tmp_path, {"pay.js": STRIPE_JS + (
        "async function go(cond) {\n"
        "  const params = cond ? { payment_method_types: ['card'], amount: 1 } : { payment_method_types: ['ideal'] };\n"
        "  return stripe.paymentIntents.create(params);\n"
        "}\n")})
    assert plan.fixes == [] and plan.unfixable == []
    assert plan.to_dict()["not_attempted"] == [
        {"file": "pay.js", "line": 4, "path": "/v1/payment_intents", "target": OLD, "why": "arguments_not_visible"}
    ]


def test_a_raw_string_key_is_an_unsupported_key_form(tmp_path):
    plan = plan_for(tmp_path, {"pay.py": (
        "import stripe\n"
        "def go():\n"
        "    params = {r'payment_method_types': ['card'], 'amount': 1}\n"
        "    return stripe.PaymentIntent.create(**params)\n")})
    assert unfixable_why(plan) == [(4, "unsupported_key_form")]


def test_two_table_endpoints_fixed_but_a_third_outside_the_table_blocks_the_file(tmp_path):
    plan = plan_for(tmp_path, {"pay.js": STRIPE_JS + (
        "async function go() {\n"
        "  const intent = await stripe.paymentIntents.create({ payment_method_types: ['card'], amount: 1 });\n"
        "  const link = await stripe.paymentLinks.create({ payment_method_types: ['card'], line_items: [] });\n"
        "}\n")})
    assert unfixable_why(plan) == [(3, "leftover_tokens")] and plan.leftovers == [("pay.js", 4)]
