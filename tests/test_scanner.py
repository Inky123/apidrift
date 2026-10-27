import pytest

from apidrift.openapi import load_spec
from apidrift.scanner import scan_repo
from apidrift.sdkmap import SdkIndex
from specs import sdk_doc

PY = '''import stripe
from stripe.checkout import Session as CheckoutSession

def pay(amount, extra):
    stripe.Charge.create(amount=amount, currency="usd", shipping={"name": "Z", "address": {"city": "Miami"}}, api_key="sk")
    params = {"mode": "payment", "line_items": [{"price": "p", "quantity": 1}]}
    CheckoutSession.create(**params)
    stripe.Charge.capture("ch_1", **extra)
    charge = stripe.Charge.retrieve("ch_1")
    stripe.Webhook.construct_event("", "", "")
    db.charges.create(amount=1)
    return charge.receipt_url
'''

JS = '''const Stripe = require('stripe');
const client = new Stripe(process.env.KEY);

async function go(req) {
  const session = await client.checkout.sessions.create({
    mode: 'payment',
    line_items: [{ price: 'p', quantity: 1 }],
    success_url: `${req.origin}/ok?s={CHECKOUT_SESSION_ID}`, // trailing comment
  }, { idempotencyKey: 'k' });
  await client.charges.capture(req.params.id, { ...req.body, amount: 5 });
  const charge = await client.charges.retrieve('ch_1');
  const base = { currency: 'usd' };
  await client.charges.create(base);
  db.charges.create({ amount: 1 });
  client.webhooks.constructEvent(a, b, c);
  // client.charges.create({ amount: 999 });
  const url = 'https://x/ok?session_id={CHECKOUT_SESSION_ID}';
  const session_id = req.query.id;
  await client.charges.retrieve(session_id);
  return charge.receipt_url;
}
'''

RB = '''require 'stripe'
Stripe::Charge.create(amount: 100, currency: 'usd', shipping: { name: 'Z' })
session = Stripe::Checkout::Session.create({ mode: 'payment', line_items: [{ price: 'p', quantity: 1 }] })
Stripe::Charge.capture('ch_1', amount: 5)
url = ENV['DOMAIN'] + '/success.html?session_id={CHECKOUT_SESSION_ID}'
Stripe::Charge.retrieve(session_id)
'''

SH = '''#!/bin/sh
curl https://api.stripe.com/v1/charges/ch_123/capture -u sk_test: -d amount=5
curl https://api.stripe.com/v1/charges -X POST -u sk_test: -d amount=5
'''

GO = '''resp, err := http.Get("https://api.stripe.com/v1/charges")  // stripe
'''


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    repo = tmp_path_factory.mktemp("repo")
    (repo / "pay.py").write_text(PY)
    (repo / "pay.js").write_text(JS)
    (repo / "pay.rb").write_text(RB)
    (repo / "pay.sh").write_text(SH)
    (repo / "pay.go").write_text(GO)
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "stripe.js").write_text("stripe.charges.create({amount: 1});")
    return scan_repo(repo, SdkIndex.build(load_spec(sdk_doc())))


def by(result, file, line):
    matches = [u for u in result.usages if u.file == file and u.line == line]
    assert len(matches) == 1, [(u.file, u.line, u.call) for u in result.usages]
    return matches[0]


def test_python_keyword_arguments_and_nested_dicts(result):
    u = by(result, "pay.py", 5)
    assert u.label == "POST /v1/charges" and u.language == "python" and u.complete
    assert u.fields == {"amount", "currency", "shipping", "shipping.name", "shipping.address", "shipping.address.city"}
    assert "receipt_url" in u.reads


def test_python_import_alias_and_dict_variable_unpacked(result):
    u = by(result, "pay.py", 7)
    assert u.label == "POST /v1/checkout/sessions" and u.call == "stripe.checkout.Session.create"
    assert u.fields == {"mode", "line_items", "line_items[].price", "line_items[].quantity"} and u.complete


def test_python_unknown_kwargs_mark_fields_incomplete(result):
    u = by(result, "pay.py", 8)
    assert u.label == "POST /v1/charges/{charge}/capture" and u.fields == frozenset() and not u.complete
    assert by(result, "pay.py", 9).complete


def test_python_ignores_other_libraries_and_reports_unmapped_stripe_calls(result):
    assert not any(u.call.startswith("db.") for u in result.usages)
    assert ("pay.py", 10, "stripe.Webhook.construct_event") in result.unresolved


def test_javascript_object_literal_with_options_argument(result):
    u = by(result, "pay.js", 5)
    assert u.label == "POST /v1/checkout/sessions" and u.language == "javascript" and u.complete
    assert u.fields == {"mode", "line_items", "line_items[].price", "line_items[].quantity", "success_url"}


def test_javascript_spread_and_variables(result):
    capture = by(result, "pay.js", 10)
    assert capture.fields == {"amount"} and not capture.complete
    assert by(result, "pay.js", 11).complete
    assert by(result, "pay.js", 13).fields == {"currency"}
    assert ("pay.js", 15, "client.webhooks.constructEvent") in result.unresolved
    assert not any(u.file == "pay.js" and u.line == 14 for u in result.usages)


def test_strings_and_comments_are_not_code(result):
    assert not any(u.file == "pay.js" and u.line == 16 for u in result.usages)
    assert by(result, "pay.js", 19).fields == frozenset() and by(result, "pay.js", 19).complete
    assert by(result, "pay.rb", 6).fields == frozenset()


def test_ruby_keyword_arguments_and_hashes(result):
    assert by(result, "pay.rb", 2).fields == {"amount", "currency", "shipping", "shipping.name"}
    session = by(result, "pay.rb", 3)
    assert session.label == "POST /v1/checkout/sessions" and session.call == "Stripe::Checkout::Session.create"
    assert session.fields == {"mode", "line_items", "line_items[].price", "line_items[].quantity"}
    assert by(result, "pay.rb", 4).fields == {"amount"}


def test_literal_urls_identify_the_endpoint_and_method(result):
    capture = by(result, "pay.sh", 2)
    assert capture.label == "POST /v1/charges/{charge}/capture" and capture.language == "http" and not capture.complete
    assert by(result, "pay.sh", 3).method == "post"
    assert by(result, "pay.go", 1).label == "GET /v1/charges"


def test_skips_dependency_directories(result):
    assert not any(u.file.startswith("node_modules") for u in result.usages)
