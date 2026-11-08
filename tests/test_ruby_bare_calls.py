"""Ruby calls without parentheses (jsbare): a zero-argument call, a call whose result is read on, and a bare
positional argument are requests; attribute and constant reads, service objects and Ruby keywords are not."""

import pytest
from sdkmore import more_doc

from apidrift.openapi import load_spec
from apidrift.scanner import scan_text
from apidrift.sdkmap import SdkIndex


@pytest.fixture(scope="module")
def index():
    return SdkIndex.build(load_spec(more_doc()))


def calls(index, text, rel="a.rb"):
    usages, unresolved, _ = scan_text(text, rel, index)
    return [(u.line, u.call, u.label, sorted(u.fields), u.complete) for u in usages], [c for _, _, c in unresolved]


def test_zero_argument_calls_and_calls_read_on(index):
    text = (
        "b = Stripe::Balance.retrieve\n"
        "id = Stripe::Account.retrieve.id\n"
        "h = Stripe::Balance.retrieve.to_hash\n"
        "t = StripeService::Customer.list[:data]\n"
        "cli = Stripe::StripeClient.new(key)\n"
        "s = cli.v1.customers.list\n"
        "Stripe::Customer.list.auto_paging_each { |c| p c }\n"
        "x = Stripe::Balance.retrieve if ready\n"
        "y = cond ? Stripe::Balance.retrieve : nil\n"
    )
    found, _ = calls(index, text)
    assert found == [
        (1, "Stripe::Balance.retrieve", "GET /v1/balance", [], True),
        (2, "Stripe::Account.retrieve", "GET /v1/account", [], True),
        (3, "Stripe::Balance.retrieve", "GET /v1/balance", [], True),
        (4, "StripeService::Customer.list", "GET /v1/customers", [], True),
        (6, "cli.v1.customers.list", "GET /v1/customers", [], True),
        (7, "Stripe::Customer.list", "GET /v1/customers", [], True),
        (8, "Stripe::Balance.retrieve", "GET /v1/balance", [], True),
        (9, "Stripe::Balance.retrieve", "GET /v1/balance", [], True),
    ]


def test_a_bare_positional_argument_is_the_id_and_keywords_after_it_are_fields(index):
    text = (
        "c = Stripe::Customer.retrieve id\n"
        "c = Stripe::Customer.retrieve @customer_id, expand: [\"sources\"]\n"
        "a = Stripe::Account.retrieve user.stripe_id\n"
        "a = Stripe::Account.retrieve \"acct_1\"\n"
    )
    found, _ = calls(index, text)
    assert found == [
        (1, "Stripe::Customer.retrieve", "GET /v1/customers/{customer}", [], True),
        (2, "Stripe::Customer.retrieve", "GET /v1/customers/{customer}", ["expand"], True),
        (3, "Stripe::Account.retrieve", "GET /v1/accounts/{account}", [], True),
        (4, "Stripe::Account.retrieve", "GET /v1/accounts/{account}", [], True),
    ]


def test_calls_with_parentheses_or_keywords_are_read_once(index):
    text = "Stripe::Customer.list(limit: 3)\nStripe::Customer.list limit: 3\nStripe::Customer.list().each { }\n"
    found, _ = calls(index, text)
    assert [(line, label, fields) for line, _, label, fields, _ in found] == [
        (1, "GET /v1/customers", ["limit"]), (2, "GET /v1/customers", ["limit"]), (3, "GET /v1/customers", []),
    ]


def test_reads_that_are_not_requests(index):
    text = (
        "Stripe.api_key = ENV['K']\n"
        "v = Stripe.api_version\n"
        "caps = cli.v1.accounts.capabilities\n"  # a service object: its methods are the calls
        "status = stripe_account.capabilities\n"  # a response attribute
        "k = Stripe::Customer\n"
        "# Stripe::Balance.retrieve in a comment\n"
        "s = \"Stripe::Balance.retrieve in a string\"\n"
        "allow(Stripe::Balance).to receive(:retrieve)\n"
        "expect(Stripe::Customer).to receive(:list).and_return(x)\n"
        "user.stripe_customer.list_items\n"
    )
    found, unresolved = calls(index, text)
    assert found == []
    assert unresolved == []  # a chain with no parentheses that resolves nowhere is not listed either


def test_ruby_keywords_after_a_bare_call_are_not_arguments(index):
    text = (
        "a = Stripe::Account.retrieve unless id\n"
        "a = Stripe::Account.retrieve rescue nil\n"
        "a = Stripe::Account.retrieve or raise\n"
        "Stripe::Customer.list do |c|\nend\n"
    )
    found, _ = calls(index, text)
    assert [(line, label) for line, _, label, _, _ in found] == [
        (1, "GET /v1/account"), (2, "GET /v1/account"), (3, "GET /v1/account"), (4, "GET /v1/customers"),
    ]


def test_javascript_has_no_bare_calls(index):
    text = "const stripe = new Stripe(k);\nconst x = stripe.customers.list;\nstripe.balance.retrieve;\n"
    assert calls(index, text, "a.js")[0] == []


def test_bang_and_predicate_methods_are_the_apps_own(index):
    text = (
        "def x\n"
        "  Stripe::Customer.create!(email: e)\n"
        "  Stripe::Customer.list?\n"
        "  StripeService::Customer.update!\n"
        "  ok = Stripe::Customer.list.empty?\n"  # the call is list; empty? reads its result
        "  same = Stripe::Balance.retrieve != cached\n"
        "end\n"
    )
    assert [(line, label) for line, _, label, _, _ in calls(index, text)[0]] == [
        (5, "GET /v1/customers"), (6, "GET /v1/balance"),
    ]


def test_heredocs_and_block_comments_are_text(index):
    text = (
        "def x\n"
        "  t = <<~TXT\n"
        "    Stripe::Balance.retrieve\n"
        "  TXT\n"
        "  u = <<-'SQL'\n"
        "    Stripe::Customer.list\n"
        "  SQL\n"
        "=begin\n"
        "  Stripe::Balance.retrieve\n"
        "=end\n"
        "  Stripe::Customer.list\n"
        "  items << Stripe::Balance.retrieve\n"  # << is an append here, not a heredoc
        "end\n"
    )
    assert [(line, label) for line, _, label, _, _ in calls(index, text)[0]] == [
        (11, "GET /v1/customers"), (12, "GET /v1/balance"),
    ]


def test_a_bare_call_starts_at_the_sdk(index):
    text = (
        "def x\n"
        "  stripe_customer.subscriptions.create attrs\n"
        "  @stripe_account.customers.list\n"
        "  user.stripe_customers.list\n"
        "  cli = Stripe::StripeClient.new(k)\n"
        "  cli.customers.list\n"
        "  @client.v1.customers.list\n"
        "  StripeService.customers.list\n"
        "end\n"
    )
    assert [(line, call) for line, call, _, _, _ in calls(index, text)[0]] == [
        (6, "cli.customers.list"), (7, "client.v1.customers.list"), (8, "StripeService.customers.list"),
    ]


def test_a_chain_read_without_parentheses_is_not_also_unresolved(index):
    found, unresolved = calls(index, "x = Stripe::Customer.list.data.first(3)\ny = Stripe::Charge.frobnicate(1)\n")
    assert [(line, label) for line, _, label, _, _ in found] == [(1, "GET /v1/customers")]
    assert unresolved == ["Stripe::Charge.frobnicate"]
