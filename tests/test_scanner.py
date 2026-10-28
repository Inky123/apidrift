import pytest

from apidrift.openapi import load_spec
import json

from apidrift.scanner import render_scan_json, render_scan_text, scan_repo, unread_sentence
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


# --- receivers, scoping, Ruby clients, documentation, v2, skips ------------------

LAZY_TS = '''import Stripe from 'stripe';
let _stripe: Stripe | null = null;
export function getClient(): Stripe {
  if (!_stripe) _stripe = new Stripe(process.env.KEY!);
  return _stripe;
}
export async function a() { return getClient().charges.create({ amount: 1, currency: 'usd' }); }
export async function b() { return (await getClient()).charges.create({ amount: 2 }); }
export async function c() { return new Stripe('sk').charges.create({ amount: 3 }); }
export async function d() { const { charges } = getClient(); return charges.create({ amount: 4 }); }
export async function e() { const s = getClient(); return s.charges.create({ amount: 5 }); }
export async function f() { return _stripe!.charges.create({ amount: 6 }); }
export async function g() { return getClient().charges.create().then((c) => c.id); }
'''

SCOPE_JS = '''const stripe = require('stripe')('sk');
function one() {
  const params = { amount: 1, description: 'x' };
  return stripe.charges.create(params);
}
function two() {
  const params = { amount: 2 };
  return stripe.charges.create(params);
}
function three() {
  const params = build();
  return stripe.charges.create(params);
}
function four() {
  const params = { amount: 4 };
  if (flag) params.description = 'late';
  return stripe.charges.create(params);
}
function five() {
  const params = {};
  Object.assign(params, { currency: 'usd' });
  return stripe.charges.create(params);
}
const shared = { amount: 6 };
function six(id) {
  return stripe.charges.capture(id, shared);
}
'''

SCOPE_PY = '''import stripe
SHARED = {"amount": 0}
def one():
    params = {"amount": 1, "description": "x"}
    return stripe.Charge.create(**params)
def two():
    params = {"amount": 2}
    return stripe.Charge.create(**params)
def three():
    params = build()
    return stripe.Charge.create(**params)
def four():
    params = {"amount": 4}
    if flag:
        params["description"] = "late"
    return stripe.Charge.create(**params)
def five():
    params = {"amount": 5}
    params.update(currency="usd")
    return stripe.Charge.create(**params)
def six():
    return stripe.Charge.create(**SHARED)
def seven():
    return client.v2.charges.create(amount=1)
'''

CLIENT_RB = '''require 'stripe'
client = Stripe::StripeClient.new(ENV['KEY'])
client.v1.charges.create({ amount: 1, currency: 'usd' })
Stripe::Charge.create amount: 2, currency: 'usd',
  description: 'two lines'
Stripe::Charge.create({ amount: 3 }.merge(extra))
Stripe::Charge.create(amount: 4)
'''

V2_JS = '''const stripe = require('stripe')('sk');
stripe.v2.charges.create({ amount: 1 });
stripe.charges.create({ amount: 2 });
'''

README = "Try it:\n\n    curl https://api.stripe.com/v1/charges -d amount=1\n"
CASSETTE = "- request:\n    method: post\n    uri: https://api.stripe.com/v1/charges\n"


@pytest.fixture(scope="module")
def repo2(tmp_path_factory):
    repo = tmp_path_factory.mktemp("repo2")
    for name, text in {
        "lazy.ts": LAZY_TS, "scope.js": SCOPE_JS, "scope.py": SCOPE_PY, "client.rb": CLIENT_RB, "v2.js": V2_JS,
        "README.md": README, "docs/api.txt": README, "spec/cassettes/charge.yml": CASSETTE, "notes/stripe.sh": README,
        "build/pay.js": V2_JS, "src/build/pay.js": V2_JS, "app/package.json": "{}", "app/dist/pay.js": V2_JS,
    }.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (repo / "huge.js").write_text("stripe" + "x" * 1_600_000)
    return repo


@pytest.fixture(scope="module")
def result2(repo2):
    return scan_repo(repo2, SdkIndex.build(load_spec(sdk_doc())))


def test_call_expression_receivers_and_client_aliases_in_typescript(result2):
    sites = {u.line: u for u in result2.usages if u.file == "lazy.ts"}
    assert sorted(sites) == [7, 8, 9, 10, 11, 12, 13]
    assert all(u.label == "POST /v1/charges" and u.complete for u in sites.values())
    assert sites[7].call == "getClient().charges.create" and sites[7].fields == {"amount", "currency"}
    assert sites[8].call == "(await getClient()).charges.create" and sites[8].fields == {"amount"}
    assert sites[9].call == "new Stripe().charges.create"
    assert sites[10].call == "charges.create" and sites[11].call == "s.charges.create"
    assert sites[12].call == "_stripe.charges.create"
    assert sites[13].fields == frozenset()


def test_javascript_variables_resolve_to_the_nearest_literal_in_scope(result2):
    assert by(result2, "scope.js", 4).fields == {"amount", "description"} and by(result2, "scope.js", 4).complete
    assert by(result2, "scope.js", 8).fields == {"amount"} and by(result2, "scope.js", 8).complete
    three = by(result2, "scope.js", 12)
    assert three.fields == frozenset() and not three.complete  # bound to a call, not a borrowed literal
    four = by(result2, "scope.js", 17)
    assert four.fields == {"amount", "description"} and not four.complete  # mutated after the literal
    five = by(result2, "scope.js", 22)
    assert five.fields == {"currency"} and not five.complete
    six = by(result2, "scope.js", 26)
    assert six.label == "POST /v1/charges/{charge}/capture" and six.fields == {"amount"} and six.complete


def test_python_variables_resolve_per_function(result2):
    assert by(result2, "scope.py", 5).fields == {"amount", "description"} and by(result2, "scope.py", 5).complete
    assert by(result2, "scope.py", 8).fields == {"amount"} and by(result2, "scope.py", 8).complete
    assert not by(result2, "scope.py", 11).complete and by(result2, "scope.py", 11).fields == frozenset()
    four = by(result2, "scope.py", 16)
    assert four.fields == {"amount", "description"} and not four.complete
    five = by(result2, "scope.py", 20)
    assert five.fields == {"amount", "currency"} and not five.complete
    assert by(result2, "scope.py", 22).fields == {"amount"} and by(result2, "scope.py", 22).complete


def test_ruby_stripe_client_roots_paren_less_calls_and_merge(result2):
    one = by(result2, "client.rb", 3)
    assert one.call == "client.v1.charges.create" and one.fields == {"amount", "currency"} and one.complete
    two = by(result2, "client.rb", 4)
    assert two.fields == {"amount", "currency", "description"} and two.complete
    three = by(result2, "client.rb", 6)
    assert three.fields == {"amount"} and not three.complete
    assert by(result2, "client.rb", 7).fields == {"amount"}


def test_v2_chains_never_resolve_to_v1_paths(result2):
    assert not any(u.file == "v2.js" and u.line == 2 for u in result2.usages)
    assert ("v2.js", 2, "stripe.v2.charges.create") in result2.unresolved
    assert by(result2, "v2.js", 3).label == "POST /v1/charges"
    assert ("scope.py", 24, "client.v2.charges.create") in result2.unresolved


def test_documentation_urls_are_tagged_doc_and_not_counted(result2):
    from apidrift.scanner import render_scan_text

    docs = {u.file: u.language for u in result2.docs}
    assert docs == {"README.md": "doc", "docs/api.txt": "doc", "spec/cassettes/charge.yml": "doc"}
    assert by(result2, "notes/stripe.sh", 3).language == "http"
    assert all(u.language != "doc" for u in result2.counted) and len(result2.counted) == len(result2.usages) - 3
    text = render_scan_text(result2)
    assert f"{len(result2.counted)} Stripe call sites" in text and "3 URLs in documentation / fixtures (not counted)" in text
    assert "documentation / fixtures (not counted):\n  README.md:3" in text


def test_build_directories_are_skipped_only_at_project_roots(result2):
    files = {u.file for u in result2.usages}
    assert "src/build/pay.js" in files
    assert not any(f.startswith(("build/", "app/dist/")) for f in files)
    assert result2.skipped_dirs == 2 and result2.skipped_files == 1
    from apidrift.scanner import render_scan_text

    assert "skipped: 1 oversize or binary files, 2 dependency or build directories" in render_scan_text(result2)


def test_scan_repo_rejects_missing_paths(tmp_path):
    with pytest.raises(SystemExit, match="does not exist"):
        scan_repo(tmp_path / "nope", SdkIndex.build(load_spec(sdk_doc())))


def test_reads_skip_generic_receivers_and_client_members():
    from apidrift.scanner import _reads

    reads = _reads(
        "const charge = await stripe.charges.retrieve(id);\n"
        "switch (event.type) {}\n"
        "const total = req.body.amount + (await other()).status;\n"
        "return [charge.receipt_url, data['object'], Stripe.PaymentIntent];\n"
    )
    assert reads == {"receipt_url", "status", "object"}


def test_files_in_unparsed_languages_that_mention_stripe_are_counted_not_passed_as_clean(tmp_path):
    index = SdkIndex.build(load_spec(sdk_doc()))
    (tmp_path / "pay.go").write_text('package main\nimport "github.com/stripe/stripe-go/v82"\nfunc f() { paymentintent.New(params) }\n')
    (tmp_path / "other.go").write_text("package main\nfunc g() {}\n")
    (tmp_path / "Pay.php").write_text("<?php $stripe->paymentIntents->create(['amount' => 1]);\n")
    (tmp_path / "Pay.java").write_text("class Pay { void f() { PaymentIntent.create(params); } }\n")
    result = scan_repo(tmp_path, index)
    assert result.unread == {"Go": 1, "PHP": 1}
    assert result.usages == []
    text = render_scan_text(result)
    assert "not read: apidrift does not read Go or PHP call sites yet: 1 Go file and 1 PHP file mention Stripe" in text
    assert json.loads(render_scan_json(result))["unread"] == {"Go": 1, "PHP": 1}


def test_the_module_fixture_counts_its_go_file_as_unread(result):
    assert result.unread == {"Go": 1}


def test_unread_sentence_wording():
    assert unread_sentence({}) == ""
    assert unread_sentence({"Go": 12}) == (
        "apidrift does not read Go call sites yet: 12 Go files mention Stripe and were not checked."
    )
    assert unread_sentence({"C#": 2, "Go": 1, "PHP": 3}).startswith(
        "apidrift does not read C#, Go or PHP call sites yet: 2 C# files, 1 Go file and 3 PHP files mention Stripe"
    )


def test_scan_text_matches_scan_repo_for_one_file_and_needs_no_filesystem(result):
    from apidrift.scanner import scan_text

    index = SdkIndex.build(load_spec(sdk_doc()))
    usages, unresolved, unread = scan_text(JS, "pay.js", index)
    assert usages == [u for u in result.usages if u.file == "pay.js"]
    assert unresolved == [(f, line, call) for f, line, call in result.unresolved if f == "pay.js"] and unread is None
    assert scan_text(GO, "x/main.go", index)[2] == "Go"
    assert scan_text(README, "docs/x.md", index)[0][0].language == "doc"
