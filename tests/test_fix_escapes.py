"""The escape check applies to renames, and reads inside interpolated strings are reads.

A parameters literal that is also aliased, copied, returned, passed to another call or read is never edited,
whatever the action: ``paymentLinks.create(q)`` still takes the old key, so renaming it in ``p`` would send
an unknown parameter there. The only uses that do not block: a declaration, the same variable passed to
another call of the same endpoint (the oracle checks that call's fields) and a read of another key.
Code inside ``${...}``, ``#{...}``, ``"{$p[...]}"`` / ``"$p[k]"`` and f-string holes is code: a read of the
key there is seen by the token count and by the escape check (:mod:`strholes`).
"""

import pytest

from test_fixer import NEW, OLD, STRIPE_JS, changed_lines, plan_for
from test_fixer_v2 import plan_v2

SHARED = "shared_literal"
PHP = "<?php\nuse Stripe\\PaymentIntent;\nclass C {\n    public function pay() {\n%s    }\n}\n"
PHP_P = f"        $p = ['amount' => 100, '{OLD}' => ['card']];\n        PaymentIntent::create($p);\n"
JS_P = f"  const p = {{ amount: 100, {OLD}: ['card'] }};\n  await stripe.paymentIntents.create(p);\n"
PY_P = f"    params = {{'amount': 100, '{OLD}': ['card']}}\n    stripe.PaymentIntent.create(**params)\n"
RB_P = f"    p = {{ amount: 100, {OLD}: ['card'] }}\n    Stripe::PaymentIntent.create(p)\n"


def js(body: str) -> str:
    return STRIPE_JS + "export async function pay(x) {\n" + body + "}\n"


def py(body: str) -> str:
    return "import stripe\n\n\ndef pay(x):\n" + body


def rb(body: str) -> str:
    return "class D\n  def pay(x)\n" + body + "  end\nend\n"


def whys(plan) -> list[str]:
    return sorted(u.why for u in plan.unfixable)


def refused(tmp_path, files) -> list[str]:
    plan = plan_for(tmp_path, files)
    assert plan.fixes == [], plan.to_dict()
    return whys(plan)


def fixed(tmp_path, files) -> str:
    plan = plan_for(tmp_path, files)
    assert plan.unfixable == [] and len(plan.fixes) == 1, plan.to_dict()
    return plan.fixes[0].content


# --- item 1: a rename does not skip the escape check ------------------------------------------------

JS_ESCAPES = {
    "alias": "  const q = p;\n  await stripe.paymentLinks.create(q);\n",
    "bracket_resource": "  await stripe['paymentLinks'].create(p);\n",
    "object_assign": "  await stripe.paymentLinks.create(Object.assign({}, p));\n",
    "return": "  return p;\n",
    "rest_destructure": "  const { ...q } = p;\n  await stripe.paymentLinks.create(q);\n",
    "this_client": "  await this.stripe.paymentLinks.create(p);\n",
    "other_endpoint": "  await stripe.charges.create(p);\n",
    "json_stringify": "  logger.info(JSON.stringify(p));\n",
    "method_on_object": "  audit(p.toJSON());\n",
    "read_of_the_key": "  console.log(p.payment_method_types);\n",
    "quoted_read_of_the_key": "  console.log(p['payment_method_types']);\n",
    "dynamic_read": "  console.log(p[x]);\n",
    "lambda_capture": "  later(() => audit(p));\n",
}


@pytest.mark.parametrize("label", sorted(JS_ESCAPES))
def test_js_rename_is_refused_when_the_literal_escapes(tmp_path, label):
    assert refused(tmp_path, {"pay.js": js(JS_P + JS_ESCAPES[label])}) == [SHARED]


@pytest.mark.parametrize("label,extra", [
    ("a declaration without a value", "  let p;\n  p = { amount: 100, %s: ['card'] };\n  await stripe.paymentIntents.create(p);\n"),
    ("a second call of the same endpoint", JS_P + "  await stripe.paymentIntents.create(p);\n"),
    ("a read of another key", JS_P + "  console.log(p.amount, p['amount'], p?.amount);\n"),
    ("a write of another key", JS_P + "  p.amount = 200;\n  p.currency += 'x';\n"),
])
def test_js_rename_still_happens_for_uses_that_cannot_reach_the_key(tmp_path, label, extra):
    body = extra % OLD if "%s" in extra else extra
    content = fixed(tmp_path, {"pay.js": js(body)})
    assert NEW in content and OLD not in content.replace(NEW, "")


PY_ESCAPES = {
    "alias": "    q = params\n    stripe.PaymentLink.create(**q)\n",
    "dict_copy": "    stripe.PaymentLink.create(**dict(params))\n",
    "getattr": "    getattr(stripe, 'PaymentLink').create(**params)\n",
    "self_client": "    self.client.PaymentLink.create(**params)\n",
    "return": "    return params\n",
    "lambda_capture": "    later(lambda: audit(params))\n",
    "other_endpoint": "    stripe.Charge.create(**params)\n",
    "method_on_dict": "    audit(params.copy())\n",
    "read_of_the_key": f"    log.info(params['{OLD}'])\n",
    "get_of_the_key": f"    log.info(params.get('{OLD}'))\n",
}


@pytest.mark.parametrize("label", sorted(PY_ESCAPES))
def test_py_rename_is_refused_when_the_dict_escapes(tmp_path, label):
    assert refused(tmp_path, {"pay.py": py(PY_P + PY_ESCAPES[label])}) == [SHARED]


@pytest.mark.parametrize("label,extra", [
    ("a second call of the same endpoint", PY_P + "    stripe.PaymentIntent.create(**params)\n"),
    ("a read of another key", PY_P + "    log.info(params['amount'], params.get('amount'))\n"),
    ("a write of another key", PY_P + "    params['amount'] = 200\n"),
])
def test_py_rename_still_happens_for_uses_that_cannot_reach_the_key(tmp_path, label, extra):
    content = fixed(tmp_path, {"pay.py": py(extra)})
    assert NEW in content and OLD not in content.replace(NEW, "")


PHP_ESCAPES = {
    "alias": "        $q = $p;\n        \\Stripe\\PaymentLink::create($q);\n",
    "client_property": "        $this->client->paymentLinks->create($p);\n",
    "class_in_variable": "        $cls = '\\Stripe\\PaymentLink';\n        $cls::create($p);\n",
    "call_user_func": "        call_user_func(['\\Stripe\\PaymentLink', 'create'], $p);\n",
    "return": "        return $p;\n",
    "other_endpoint": "        \\Stripe\\Charge::create($p);\n",
    "json_encode": "        $this->log(json_encode($p));\n",
    "read_of_the_key": f"        $this->log($p['{OLD}']);\n",
    "closure_use": "        $f = function () use ($p) { return $p; };\n",
}


@pytest.mark.parametrize("label", sorted(PHP_ESCAPES))
def test_php_rename_is_refused_when_the_array_escapes(tmp_path, label):
    assert refused(tmp_path, {"C.php": PHP % (PHP_P + PHP_ESCAPES[label])}) == [SHARED]


@pytest.mark.parametrize("label,extra", [
    ("a second call of the same endpoint", PHP_P + "        PaymentIntent::create($p);\n"),
    ("a read of another key", PHP_P + "        $this->log($p['amount']);\n"),
    ("a write of another key", PHP_P + "        $p['amount'] = 200;\n"),
    ("a use of a later, different binding", PHP_P + "        $p = ['status' => 1];\n        $this->log($p);\n"),
])
def test_php_rename_still_happens_for_uses_that_cannot_reach_the_key(tmp_path, label, extra):
    content = fixed(tmp_path, {"C.php": PHP % extra})
    assert NEW in content and OLD not in content.replace(NEW, "")


RB_ESCAPES = {
    "alias": "    q = p\n    Stripe::PaymentLink.create(q)\n",
    "dup": "    Stripe::PaymentLink.create(p.dup)\n",
    "double_splat": "    Stripe::PaymentLink.create(**p)\n",
    "other_client": "    x.payment_links.create(p)\n",
    "last_expression": "    p\n",
    "other_endpoint": "    Stripe::Charge.create(p)\n",
    "to_json": "    Rails.logger.info(p.to_json)\n",
    "read_of_the_key": f"    Rails.logger.info(p[:{OLD}])\n",
    "fetch": "    Rails.logger.info(p.fetch(:amount))\n",
}


@pytest.mark.parametrize("label", sorted(RB_ESCAPES))
def test_rb_rename_is_refused_when_the_hash_escapes(tmp_path, label):
    assert refused(tmp_path, {"d.rb": rb(RB_P + RB_ESCAPES[label])}) == [SHARED]


@pytest.mark.parametrize("label,extra", [
    ("a second call of the same endpoint", RB_P + "    Stripe::PaymentIntent.create(p)\n"),
    ("a read of another key", RB_P + "    Rails.logger.info(p[:amount], p['amount'])\n"),
    ("a write of another key", RB_P + "    p[:amount] = 200\n"),
])
def test_rb_rename_still_happens_for_uses_that_cannot_reach_the_key(tmp_path, label, extra):
    content = fixed(tmp_path, {"d.rb": rb(extra)})
    assert NEW in content and OLD not in content.replace(NEW, "")


# --- item 2: reads inside interpolated strings -------------------------------------------------------

LITERALS = {  # action -> (JS literal, Python literal, Ruby literal, PHP literal, key read, call per language)
    "rename": (
        f"{{ amount: 100, {OLD}: ['card'] }}", f"{{'amount': 100, '{OLD}': ['card']}}", f"{{ amount: 100, {OLD}: ['card'] }}",
        f"['amount' => 100, '{OLD}' => ['card']]", OLD,
        {"js": "stripe.paymentIntents.create(p)", "py": "stripe.PaymentIntent.create(**p)",
         "rb": "Stripe::PaymentIntent.create(p)", "php": "\\Stripe\\PaymentIntent::create($p)"},
    ),
    "delete": (
        "{ payto: { pay_id: 'a' }, metadata: {} }", "{'payto': {'pay_id': 'a'}, 'metadata': {}}", "{ payto: { pay_id: 'a' }, metadata: {} }",
        "['payto' => ['pay_id' => 'a'], 'metadata' => []]", "payto",
        {"js": "stripe.paymentMethods.update('pm_1', p)", "py": "stripe.PaymentMethod.modify('pm_1', **p)",
         "rb": "Stripe::PaymentMethod.update('pm_1', p)", "php": "\\Stripe\\PaymentMethod::update('pm_1', $p)"},
    ),
    "wrap_type": (
        "{ billing_cycle_anchor: 'now', proration_behavior: 'none' }", "{'billing_cycle_anchor': 'now', 'proration_behavior': 'none'}",
        "{ billing_cycle_anchor: 'now', proration_behavior: 'none' }", "['billing_cycle_anchor' => 'now', 'proration_behavior' => 'none']",
        "billing_cycle_anchor",
        {"js": "stripe.subscriptions.update('sub_1', p)", "py": "stripe.Subscription.modify('sub_1', **p)",
         "rb": "Stripe::Subscription.update('sub_1', p)", "php": "\\Stripe\\Subscription::update('sub_1', $p)"},
    ),
}
PLAN = {"rename": plan_for, "delete": plan_v2, "wrap_type": plan_v2}


def interpolated(action: str, language: str, after: bool = True) -> dict[str, str]:
    js_lit, py_lit, rb_lit, php_lit, key, call = LITERALS[action]
    if language == "js":
        lines = [f"  const p = {js_lit};", f"  await {call['js']};", f"  logger.info(`k ${{p.{key}}}`);"]
        head, tail, name = STRIPE_JS + "export async function pay() {\n", "}\n", "a.js"
    elif language == "py":
        lines = [f"    p = {py_lit}", f"    {call['py']}", f"    log.info(f\"k {{p['{key}']}}\")"]
        head, tail, name = "import stripe\n\n\ndef pay():\n", "", "a.py"
    elif language == "rb":
        lines = [f"    p = {rb_lit}", f"    {call['rb']}", f"    Rails.logger.info(\"k #{{p[:{key}]}}\")"]
        head, tail, name = "class D\n  def pay\n", "  end\nend\n", "a.rb"
    else:
        lines = [f"        $p = {php_lit};", f"        {call['php']};", f"        $this->log(\"k {{$p['{key}']}}\");"]
        head, tail, name = "<?php\nclass C {\n    public function pay() {\n", "    }\n}\n", "a.php"
    if not after:
        lines[1], lines[2] = lines[2], lines[1]
    return {name: head + "\n".join(lines) + "\n" + tail}


@pytest.mark.parametrize("action", sorted(LITERALS))
@pytest.mark.parametrize("language", ["js", "py", "rb", "php"])
@pytest.mark.parametrize("after", [True, False])
def test_a_read_inside_an_interpolated_string_refuses_the_edit(tmp_path, action, language, after):
    plan = PLAN[action](tmp_path, interpolated(action, language, after))
    assert plan.fixes == [] and len(plan.unfixable) == 1, plan.to_dict()
    assert plan.unfixable[0].why in (SHARED, "incomplete_binding")


@pytest.mark.parametrize("name,text", [
    ("a.js", STRIPE_JS + f"async function pay() {{\n  const p = {{ amount: 1, {OLD}: ['card'] }};\n  await stripe.paymentIntents.create(p);\n"
     f"  logger.info(`k ${{p['{OLD}'].join(',')}}`);\n}}\n"),
    ("a.rb", f"class D\n  def pay\n    p = {{ amount: 1, {OLD}: ['card'] }}\n    Stripe::PaymentIntent.create(p)\n"
     f"    Rails.logger.info(<<~MSG)\n      sent #{{p[:{OLD}].join(',')}}\n    MSG\n  end\nend\n"),
    ("a.php", f"<?php\nclass C {{\n    public function pay() {{\n        $p = ['amount' => 1, '{OLD}' => ['card']];\n"
     f"        \\Stripe\\PaymentIntent::create($p);\n        $this->log(\"sent $p[{OLD}]\");\n    }}\n}}\n"),
    ("a.php", f"<?php\nclass C {{\n    public function pay() {{\n        $p = ['amount' => 1, '{OLD}' => ['card']];\n"
     f"        \\Stripe\\PaymentIntent::create($p);\n        $this->log(\"sent ${{p}}\");\n    }}\n}}\n"),
    ("a.py", f"import stripe\n\n\ndef pay():\n    p = {{'amount': 1, '{OLD}': ['card']}}\n    stripe.PaymentIntent.create(**p)\n"
     f"    log.info(f\"sent {{p!r}}\")\n"),
])
def test_other_interpolation_forms_refuse_too(tmp_path, name, text):
    plan = plan_for(tmp_path, {name: text})
    assert plan.fixes == [] and [u.why for u in plan.unfixable] in ([SHARED], ["unbounded"]), plan.to_dict()


@pytest.mark.parametrize("name,text", [
    ("a.js", STRIPE_JS + f"async function pay() {{\n  const p = {{ amount: 1, {OLD}: ['card'] }};\n  await stripe.paymentIntents.create(p);\n"
     f"  logger.info(`sent {OLD} ok`);\n}}\n"),
    ("a.rb", f"class D\n  def pay\n    p = {{ amount: 1, {OLD}: ['card'] }}\n    Stripe::PaymentIntent.create(p)\n"
     f"    Rails.logger.info(\"sent {OLD} #{{1 + 1}}\")\n  end\nend\n"),
    ("a.py", f"import stripe\n\n\ndef pay():\n    p = {{'amount': 1, '{OLD}': ['card']}}\n    stripe.PaymentIntent.create(**p)\n"
     f"    log.info(f\"sent {OLD} {{1 + 1}}\")\n"),
])
def test_string_text_around_a_hole_stays_text(tmp_path, name, text):
    content = fixed(tmp_path, {name: text})
    assert content.replace(NEW, "").count(OLD) == 1 and f"sent {OLD}" in content  # text is not a token: no leftover
