"""Same-file helpers: a call whose parameters a builder returns is read exactly, with where they came from.

Shapes from real repositories: lago (Ruby methods that build ``payload`` and write keys into it), openedx
(``**self._build_payment_intent_parameters(basket)``), invoiceninja (a PHP wrapper whose caller in the same
file passes the array). Every adversarial case must be read correctly or left hidden, never guessed.
"""

import pytest
from test_fixer import docs

from apidrift.openapi import load_spec
from apidrift.scanner import scan_repo
from apidrift.sdkmap import SdkIndex

STRIPE_JS = "const stripe = require('stripe')('sk');\n"
PHP = "<?php\nuse Stripe\\PaymentIntent;\n"


def scan(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    old, new = (load_spec(d) for d in docs())
    return scan_repo(root, SdkIndex.build(old, new)).usages


def one(root, name, text):
    """(line, call, fields, hidden) of the single payment-intent call site in ``text``."""
    found = [u for u in scan(root, {name: text}) if u.path == "/v1/payment_intents"]
    assert len(found) == 1, found
    u = found[0]
    return u.line, u.call, set(u.fields), set(u.hidden or ())


# --- Ruby -------------------------------------------------------------------------------

RUBY = (
    "class Pay\n"
    "  def charge\n"
    "    ::Stripe::PaymentIntent.create(intent_payload, {api_key: key})\n"
    "  end\n"
    "\n"
    "  private\n"
    "\n"
    "  def intent_payload\n"
    "    payload = {\n"
    "      amount: 1,\n"
    "      payment_method_types: methods,\n"
    "    }\n"
    "    if balance?\n"
    "      payload.merge!(balance_fields)\n"
    "    else\n"
    "      payload[:payment_method_data] = {token: token}\n"
    "      payload.delete(:amount)\n"
    "    end\n"
    "    payload\n"
    "  end\n"
    "\n"
    "  def balance_fields\n"
    "    {customer_balance: {funding_type: 'bank_transfer'}}\n"
    "  end\n"
    "end\n"
)


def test_a_ruby_builder_is_read_with_its_writes_and_named(tmp_path):
    line, call, fields, hidden = one(tmp_path, "pay.rb", RUBY)
    assert line == 3 and call == "Stripe::PaymentIntent.create [fields from intent_payload, line 8]"
    assert fields == {
        "amount", "payment_method_types", "payment_method_data", "payment_method_data.token", "customer_balance",
        "customer_balance.funding_type",
    }
    assert hidden == set()


def test_a_ruby_builder_bound_to_a_local_first_is_read(tmp_path):
    src = RUBY.replace("create(intent_payload,", "create(payload,").replace(
        "  def charge\n", "  def charge\n    payload = intent_payload\n    payload[:extra] = 1\n"
    )
    _, call, fields, hidden = one(tmp_path, "pay.rb", src)
    assert "intent_payload" in call and {"extra", "payment_method_types"} <= fields and hidden == set()


@pytest.mark.parametrize("label,change", [
    ("defined twice", ("  def balance_fields\n", "  def intent_payload\n    {}\n  end\n\n  def balance_fields\n")),
    ("an early return", ("    payload = {\n", "    return {} if skip?\n    payload = {\n")),
    ("a branch as the last expression", ("    payload\n  end\n", "    if x\n      payload\n    else\n      {}\n    end\n  end\n")),
    ("a parameter of the same name", ("  def charge\n", "  def charge(intent_payload)\n")),
    ("a block parameter of the same name", ("    ::Stripe", "    items.each do |intent_payload|\n    ::Stripe")),
    ("a local of the same name", ("  def charge\n", "  def charge\n    intent_payload = other\n")),
    ("a dynamic call", ("create(intent_payload,", "create(send(:intent_payload),")),
    ("a class method called from an instance", ("  def intent_payload\n", "  def self.intent_payload\n")),
    ("the helper calls itself", ("    payload\n  end\n", "    intent_payload\n  end\n")),
])
def test_a_ruby_helper_that_cannot_be_followed_stays_hidden(tmp_path, label, change):
    if label == "a block parameter of the same name":
        src = RUBY.replace(*change).replace("key})\n  end", "key})\n    end\n  end")
    else:
        src = RUBY.replace(*change)
    _, call, _, hidden = one(tmp_path, "pay.rb", src)
    assert "[fields from" not in call and "" in hidden, label


def test_a_merge_of_something_unknown_hides_the_top_level_but_keeps_the_literal(tmp_path):
    src = RUBY.replace("payload.merge!(balance_fields)", "payload.merge!(options)")
    _, call, fields, hidden = one(tmp_path, "pay.rb", src)
    assert "intent_payload" in call and "payment_method_types" in fields and "" in hidden


def test_request_options_built_by_a_helper_hide_nothing_and_are_not_named(tmp_path):
    src = (
        "class Pay\n  def charge\n    Stripe::PaymentIntent.create({amount: 1}, request_options)\n  end\n\n"
        "  def request_options\n    {api_key: key, stripe_account: account}\n  end\nend\n"
    )
    _, call, fields, hidden = one(tmp_path, "pay.rb", src)
    assert call == "Stripe::PaymentIntent.create" and fields == {"amount"} and hidden == set()


def test_a_ruby_helper_defined_after_its_use_and_returning_a_literal(tmp_path):
    src = (
        "class Pay\n  def charge\n    Stripe::PaymentIntent.create(params_for(order))\n  end\n\n"
        "  def params_for(order)\n    {\n      amount: order.total,\n      currency: 'usd'\n    }\n  end\nend\n"
    )
    assert one(tmp_path, "pay.rb", src)[2:] == ({"amount", "currency"}, set())


# --- JavaScript / TypeScript ---------------------------------------------------------------


JS = STRIPE_JS + (
    "function intentParams(order) {\n"
    "  return { amount: order.total, payment_method_types: ['card'] };\n"
    "}\n"
    "async function pay(order) {\n"
    "  return stripe.paymentIntents.create(intentParams(order));\n"
    "}\n"
)


def test_a_js_function_returning_a_literal_is_read(tmp_path):
    line, call, fields, hidden = one(tmp_path, "pay.js", JS)
    assert line == 6 and call.endswith("[fields from intentParams, line 2]")
    assert fields == {"amount", "payment_method_types"} and hidden == set()


def test_a_ts_method_returning_a_local_with_writes_and_an_arrow_with_an_expression_body(tmp_path):
    src = STRIPE_JS + (
        "const extra = (o) => ({ metadata: { id: o.id } });\n"
        "class Svc {\n"
        "  private build(o: Order): Params {\n"
        "    const p: Params = { amount: o.total };\n"
        "    if (o.card) {\n"
        "      p.payment_method_types = ['card'];\n"
        "    }\n"
        "    Object.assign(p, { currency: 'usd' });\n"
        "    return p;\n"
        "  }\n"
        "  async pay(o: Order) {\n"
        "    await stripe.paymentIntents.create(this.build(o));\n"
        "    await stripe.charges.create(extra(o));\n"
        "  }\n"
        "}\n"
    )
    usages = {u.path: u for u in scan(tmp_path, {"svc.ts": src})}
    pi, ch = usages["/v1/payment_intents"], usages["/v1/charges"]
    assert set(pi.fields) == {"amount", "payment_method_types", "currency"} and pi.complete
    assert set(ch.fields) == {"metadata", "metadata.id"} and "extra" in ch.call


def test_two_literal_returns_are_read_together(tmp_path):
    src = JS.replace("  return { amount", "  if (order.x) return { currency: 'usd' };\n  return { amount")
    assert one(tmp_path, "pay.js", src)[2] == {"amount", "payment_method_types", "currency"}


@pytest.mark.parametrize("label,old,new", [
    ("a return of something else", "  return { amount", "  if (order.x) return defaults(order);\n  return { amount"),
    ("a parameter of the same name", "async function pay(order) {", "async function pay(order, intentParams) {"),
    ("also assigned", "async function pay", "intentParams = other;\nasync function pay"),
    ("a dynamic name", "create(intentParams(order))", "create(this['intentParams'](order))"),
    ("the helper calls itself", "  return { amount: order.total, payment_method_types: ['card'] };",
     "  return stripe.paymentIntents.create(intentParams(order));"),
])
def test_a_js_helper_that_cannot_be_followed_stays_hidden(tmp_path, label, old, new):
    src = JS.replace(old, new)
    found = [u for u in scan(tmp_path, {"pay.js": src}) if u.path == "/v1/payment_intents"]
    assert found and all("[fields from" not in u.call and "" in (u.hidden or ()) for u in found), label


@pytest.mark.parametrize("name,text", [
    ("svc.ts", STRIPE_JS + "class A {\n  build() { return { amount: 1 }; }\n}\nclass B extends Base {\n"
               "  pay() { return stripe.paymentIntents.create(this.build()); }\n}\n"),
    ("pay.rb", "class A\n  def build\n    {amount: 1}\n  end\nend\n\nclass B < Base\n  def pay\n"
               "    Stripe::PaymentIntent.create(build)\n  end\nend\n"),
    ("D.php", PHP + "class A {\n    private function build() { return ['amount' => 1]; }\n}\nclass B extends Base {\n"
              "    public function pay() { return PaymentIntent::create($this->build()); }\n}\n"),
])
def test_a_method_of_another_class_in_the_same_file_is_not_the_helper(tmp_path, name, text):
    assert one(tmp_path, name, text)[2:] == (set(), {""})


def test_an_imported_helper_is_not_followed_into_another_file(tmp_path):
    """One hop across files is not read: a name imported from either of two modules stays hidden."""
    files = {
        "a.js": "export function build(o) { return { amount: 1 }; }\n",
        "b.js": "export function build(o) { return { payment_method_types: ['card'] }; }\n",
        "pay.js": STRIPE_JS + "import { build } from './b';\nstripe.paymentIntents.create(build(o));\n",
    }
    found = [u for u in scan(tmp_path, files) if u.path == "/v1/payment_intents"]
    assert [(u.file, set(u.fields), set(u.hidden)) for u in found] == [("pay.js", set(), {""})]


def test_a_helper_result_written_into_and_passed_on_at_the_call_site(tmp_path):
    src = JS.replace(
        "  return stripe.paymentIntents.create(intentParams(order));",
        "  const p = intentParams(order);\n  p.description = 'x';\n  audit(p);\n  return stripe.paymentIntents.create(p);",
    )
    _, call, fields, hidden = one(tmp_path, "pay.js", src)
    assert "intentParams" in call and {"description", "payment_method_types"} <= fields and "" in hidden


# --- Python -------------------------------------------------------------------------------

PY = (
    "import stripe\n\n"
    "class Processor:\n"
    "    def _params(self, basket):\n"
    "        return {'amount': basket.total, 'metadata': {'order': basket.number}}\n\n"
    "    def pay(self, basket):\n"
    "        return stripe.PaymentIntent.create(**self._params(basket), idempotency_key='k')\n"
)


def test_a_python_method_returning_a_dict_is_read(tmp_path):
    line, call, fields, hidden = one(tmp_path, "pay.py", PY)
    assert line == 8 and call == "stripe.PaymentIntent.create [fields from _params, line 4]"
    assert fields == {"amount", "metadata", "metadata.order"} and hidden == set()


def test_a_python_function_returning_a_local_with_writes_bound_before_the_call(tmp_path):
    src = (
        "import stripe\n\n"
        "def build(order):\n"
        "    params = {'amount': order.total}\n"
        "    if order.card:\n"
        "        params['payment_method_types'] = ['card']\n"
        "    params.update(currency='usd')\n"
        "    return params\n\n"
        "def pay(order):\n"
        "    kwargs = build(order)\n"
        "    return stripe.PaymentIntent.create(**kwargs)\n"
    )
    _, call, fields, hidden = one(tmp_path, "pay.py", src)
    assert "build" in call and fields == {"amount", "payment_method_types", "currency"} and hidden == set()


@pytest.mark.parametrize("label,old,new", [
    ("decorated", "    def _params", "    @cached\n    def _params"),
    ("a generator", "        return {'amount'", "        yield 1\n        return {'amount'"),
    ("a bare return", "        return {'amount'", "        if basket.empty:\n            return\n        return {'amount'"),
    ("a return of something else", "        return {'amount'", "        if basket.x:\n            return other()\n        return {'amount'"),
    ("defined twice", "    def pay", "    def _params(self, basket):\n        return {}\n\n    def pay"),
    ("the helper calls itself", "        return {'amount': basket.total, 'metadata': {'order': basket.number}}",
     "        return stripe.PaymentIntent.create(**self._params(basket))"),
])
def test_a_python_helper_that_cannot_be_followed_stays_hidden(tmp_path, label, old, new):
    found = [u for u in scan(tmp_path, {"pay.py": PY.replace(old, new)}) if u.path == "/v1/payment_intents"]
    assert found and all("[fields from" not in u.call and "" in (u.hidden or ()) for u in found), label


def test_a_python_name_imported_or_shadowed_is_not_the_helper(tmp_path):
    src = (
        "import stripe\nfrom .p import build\n\n"
        "def build(o):\n    return {'amount': 1}\n\n"
        "def pay(o):\n    return stripe.PaymentIntent.create(**build(o))\n"
    )
    assert "" in one(tmp_path, "pay.py", src)[3]
    src = "import stripe\n\ndef build(o):\n    return {'amount': 1}\n\ndef pay(o, build):\n    return stripe.PaymentIntent.create(**build(o))\n"
    assert "" in one(tmp_path, "pay.py", src)[3]


# --- PHP ----------------------------------------------------------------------------------

PHP_BUILDER = PHP + (
    "class Driver {\n"
    "    public function pay($order) {\n"
    "        return PaymentIntent::create($this->params($order), ['idempotency_key' => 'k']);\n"
    "    }\n"
    "    private function params($order) {\n"
    "        $p = ['amount' => $order->total];\n"
    "        $p['payment_method_types'] = ['card'];\n"
    "        return $p;\n"
    "    }\n"
    "}\n"
)


def test_a_php_builder_is_read(tmp_path):
    line, call, fields, hidden = one(tmp_path, "Driver.php", PHP_BUILDER)
    assert line == 5 and call == "PaymentIntent::create [fields from params, line 7]"
    assert fields == {"amount", "payment_method_types"} and hidden == set()


PHP_WRAPPER = PHP + (
    "class Driver {\n"
    "    public function setup() {\n"
    "        return $this->createIntent([\n"
    "            'usage' => 'off_session',\n"
    "            'payment_method_types' => ['card'],\n"
    "        ]);\n"
    "    }\n"
    "    public function createIntent(array $params) {\n"
    "        return PaymentIntent::create($params, ['idempotency_key' => 'k']);\n"
    "    }\n"
    "}\n"
)


def test_a_php_wrapper_caller_becomes_a_call_site_and_the_wrapper_stays_hidden(tmp_path):
    found = sorted((u.line, u.call, set(u.fields), set(u.hidden or ())) for u in scan(tmp_path, {"D.php": PHP_WRAPPER}))
    assert found == [
        (5, "PaymentIntent::create [through createIntent, line 11]", {"usage", "payment_method_types"}, set()),
        (11, "PaymentIntent::create", set(), {""}),
    ]


@pytest.mark.parametrize("label,old,new", [
    ("the parameter is also written", "        return PaymentIntent::create($params,",
     "        $params['x'] = 1;\n        return PaymentIntent::create($params,"),
    ("the caller passes something unknown", "return $this->createIntent([", "return $this->createIntent($this->opts() + ["),
    ("the caller unpacks", "return $this->createIntent([", "return $this->createIntent(...$args, ...["),
    ("a named argument", "return $this->createIntent([", "return $this->createIntent(params: ["),
    ("no caller in the file", "return $this->createIntent([", "return $other->createIntent(["),
])
def test_a_php_wrapper_without_a_readable_caller_adds_nothing(tmp_path, label, old, new):
    found = scan(tmp_path, {"D.php": PHP_WRAPPER.replace(old, new)})
    assert not any("[through" in u.call for u in found), label
