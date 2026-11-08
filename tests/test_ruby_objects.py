"""API calls on objects Stripe returned, in Ruby (rbobjects): instance methods of a local bound to a
resolved retrieve or create, and only of such a local."""

import pytest
from sdkmore import more_doc

from apidrift.openapi import load_spec
from apidrift.scanner import scan_text
from apidrift.sdkmap import SdkIndex


@pytest.fixture(scope="module")
def index():
    return SdkIndex.build(load_spec(more_doc()))


def instance(index, text):
    usages, _, _ = scan_text(text, "a.rb", index)
    return sorted((u.line, u.call, u.label, sorted(u.fields), u.complete) for u in usages if "." in u.call and "::" not in u.call)


def test_confirm_on_a_retrieved_intent_sends_its_arguments(index):
    text = (
        "def go(id)\n"
        "  pi = Stripe::PaymentIntent.retrieve(\"pi_1\")\n"
        "  pi.confirm(payment_method_types: [\"card\"])\n"
        "  pi.cancel cancellation_reason: \"abandoned\"\n"
        "end\n"
    )
    assert instance(index, text) == [
        (3, "pi.confirm", "POST /v1/payment_intents/{intent}/confirm", ["payment_method_types"], True),
        (4, "pi.cancel", "POST /v1/payment_intents/{intent}/cancel", ["cancellation_reason"], True),
    ]


def test_an_action_on_the_item_itself(index):
    text = "def go(id)\n  sub = Stripe::Subscription.retrieve(id)\n  sub.cancel(prorate: true)\nend\n"
    assert instance(index, text) == [
        (3, "sub.cancel", "DELETE /v1/subscriptions/{subscription_exposed_id}", ["prorate"], True),
    ]


def test_save_sends_the_attributes_written_before_it(index):
    text = (
        "def go(id)\n"
        "  sub = Stripe::Subscription.retrieve(id)\n"
        "  sub.billing_cycle_anchor = \"now\"\n"
        "  sub.metadata[:k] = \"v\"\n"
        "  sub.save\n"
        "  sub.cancel_at_period_end = true\n"
        "end\n"
        "def one_line(id)\n"
        "  sub = Stripe::Subscription.retrieve(id); sub.billing_cycle_anchor = \"now\"; sub.save\n"
        "end\n"
    )
    assert instance(index, text) == [
        (5, "sub.save", "POST /v1/subscriptions/{subscription_exposed_id}", ["billing_cycle_anchor", "metadata.k"], True),
        (9, "sub.save", "POST /v1/subscriptions/{subscription_exposed_id}", ["billing_cycle_anchor"], True),
    ]


@pytest.mark.parametrize("between", [
    "  apply(sub, opts)\n",
    "  sub.send(\"billing_cycle_anchor=\", x)\n",
    "  sub.metadata.merge!(extra)\n",
    "  sub.update_attributes(opts)\n",
    "  sub.metadata = opts\n",
])
def test_save_after_an_unseen_write_is_hidden(index, between):
    text = "def go(id, opts)\n  sub = Stripe::Subscription.retrieve(id)\n" + between + "  sub.save\nend\n"
    (found,) = instance(index, text)
    assert found[2] == "POST /v1/subscriptions/{subscription_exposed_id}" and found[4] is False


def test_bindings_in_branches_and_a_ternary_of_the_same_resource(index):
    text = (
        "def go(m, id, params, opts)\n"
        "  if m\n"
        "    pi = Stripe::PaymentIntent.retrieve(id, { stripe_account: m })\n"
        "  else\n"
        "    pi = Stripe::PaymentIntent.retrieve(id)\n"
        "  end\n"
        "  pi.confirm unless pi.status == \"succeeded\"\n"
        "end\n"
        "def made(params, opts)\n"
        "  pi = opts.present? ? Stripe::PaymentIntent.create(params, opts) : Stripe::PaymentIntent.create(params)\n"
        "  pi.confirm if pi.status == \"requires_confirmation\"\n"
        "end\n"
        "def gone(id)\n"
        "  acct = Stripe::Account.retrieve(id)\n"
        "  acct.delete\n"
        "  acct.refresh\n"
        "end\n"
    )
    assert [(line, call, label) for line, call, label, _, _ in instance(index, text)] == [
        (7, "pi.confirm", "POST /v1/payment_intents/{intent}/confirm"),
        (11, "pi.confirm", "POST /v1/payment_intents/{intent}/confirm"),
        (15, "acct.delete", "DELETE /v1/accounts/{account}"),
        (16, "acct.refresh", "GET /v1/accounts/{account}"),
    ]


@pytest.mark.parametrize("text", [
    "def go(pi)\n  pi.confirm\nend\n",  # a parameter
    "def go(ids)\n  ids.each do |pi|\n    pi.confirm\n  end\nend\n",  # a block variable
    "def go(id, x)\n  pi = Stripe::PaymentIntent.retrieve(id)\n  pi = x if x\n  pi.confirm\nend\n",  # another value
    "def go(id, x)\n  pi = Stripe::PaymentIntent.retrieve(id)\n  pi ||= x\n  pi.confirm\nend\n",
    "def go(id)\n  pi, other = Stripe::PaymentIntent.retrieve(id), 1\n  pi.confirm\nend\n",
    "def go(id)\n  pi = Stripe::PaymentIntent.retrieve(id).id\n  pi.confirm\nend\n",  # the id, not the object
    "def go(id)\n  pi = wrap(Stripe::PaymentIntent.retrieve(id))\n  pi.confirm\nend\n",
    "def go(id, x)\n  pi = x ? Stripe::PaymentIntent.retrieve(id) : Stripe::Subscription.retrieve(id)\n  pi.confirm\nend\n",
    "def go(id)\n  pi.confirm\n  pi = Stripe::PaymentIntent.retrieve(id)\nend\n",  # used before it is bound
    "def go(id)\n  begin\n    x\n  rescue => pi\n    pi.confirm\n  end\n  pi = Stripe::PaymentIntent.retrieve(id)\nend\n",
    "def a(id)\n  pi = Stripe::PaymentIntent.retrieve(id)\nend\ndef b\n  pi.confirm\nend\n",  # another method
    "def go(ids)\n  ids.each do |id|\n    pi = Stripe::PaymentIntent.retrieve(id)\n  end\n  pi.confirm\nend\n",  # block-local
    "def go(id)\n  pi = Stripe::PaymentIntent.retrieve(id)\n  # pi.confirm\n  s = \"pi.confirm\"\nend\n",
    "def go(id)\n  pi = Stripe::PaymentIntent.retrieve(id)\n  pi.status\n  pi.payment_method_types\n  pi.update\nend\n",
    "def go(id)\n  acct = Stripe::Account.retrieve(id)\n  acct.capabilities\nend\n",  # a GET action is an attribute
    "def go\n  me = Stripe::Account.retrieve\n  me.delete\nend\n",  # the singleton has no item path
    "def go\n  list = Stripe::Customer.list\n  list.delete\nend\n",  # a list is not the resource
])
def test_what_is_not_an_instance_call(index, text):
    assert instance(index, text) == []


def test_javascript_objects_are_not_read(index):
    text = "const stripe = new Stripe(k);\nasync function f(id) {\n  const pi = await stripe.paymentIntents.retrieve(id);\n  pi.confirm();\n}\n"
    usages, _, _ = scan_text(text, "a.js", index)
    assert [u.call for u in usages] == ["stripe.paymentIntents.retrieve"]


def test_save_reads_only_writes_that_reach_it(index):
    text = (
        "RSpec.describe Billing do\n"
        "  it \"ends the trial\" do\n"
        "    sub = Stripe::Subscription.retrieve(\"sub_1\")\n"
        "    sub.billing_cycle_anchor = \"now\"\n"
        "    sub.save\n"
        "  end\n"
        "\n"
        "  it \"tags it\" do\n"
        "    sub = Stripe::Subscription.retrieve(\"sub_2\")\n"
        "    sub.metadata = { a: 1 }\n"
        "    sub.save\n"
        "  end\n"
        "\n"
        "  it \"passes it on\" do\n"
        "    sub = Stripe::Subscription.retrieve(\"sub_3\")\n"
        "    notify(sub)\n"
        "  end\n"
        "\n"
        "  it \"saves plainly\" do\n"
        "    sub = Stripe::Subscription.retrieve(\"sub_4\")\n"
        "    sub.save\n"
        "  end\n"
        "end\n"
        "def rebound(a, b)\n"
        "  sub = Stripe::Subscription.retrieve(a)\n"
        "  sub.cancel_at_period_end = true\n"
        "  sub = Stripe::Subscription.retrieve(b)\n"
        "  sub.metadata = { k: 1 }\n"
        "  sub.save\n"
        "end\n"
        "def twice(id)\n"
        "  sub = Stripe::Subscription.retrieve(id)\n"
        "  sub.cancel_at_period_end = true\n"
        "  sub.save\n"
        "  sub.billing_cycle_anchor = \"now\"\n"
        "  sub.save\n"
        "end\n"
        "def nested(id, xs)\n"
        "  sub = Stripe::Subscription.retrieve(id)\n"
        "  xs.each do |x|\n"
        "    sub.metadata = { x: 1 }\n"
        "  end\n"
        "  sub.save\n"
        "end\n"
    )
    saves = [(line, fields, complete) for line, call, _, fields, complete in instance(index, text) if call == "sub.save"]
    assert saves == [
        (5, ["billing_cycle_anchor"], True),
        (11, ["metadata", "metadata.a"], True),
        (21, [], True),
        (29, ["metadata", "metadata.k"], True),  # not the write before the rebinding
        (34, ["cancel_at_period_end"], True),
        (36, ["billing_cycle_anchor"], True),  # only what changed since the first save
        (43, [], False),  # written in a nested block: hidden
    ]


def test_a_write_through_a_method_is_under_its_first_field(index):
    text = "def go(id, p)\n  sub = Stripe::Subscription.retrieve(id)\n  sub.items.first.price = p\n  sub.save\nend\n"
    (found,) = instance(index, text)
    assert found[3] == ["items"] and found[4] is False


def test_many_examples_read_in_linear_time(index):
    import time
    block = (
        "  it 'case {n}' do\n    sub = Stripe::Subscription.retrieve(id)\n    sub.metadata = {{a: {n}}}\n"
        "    sub.save\n    pi = Stripe::PaymentIntent.create(amount: {n})\n    pi.confirm\n  end\n"
    )
    text = "RSpec.describe Billing do\n" + "".join(block.format(n=n) for n in range(1500)) + "end\n"
    started = time.perf_counter()
    found = instance(index, text)
    assert len(found) == 3000
    assert time.perf_counter() - started < 5  # about 0.2 s; quadratic reading took minutes


def test_past_the_work_cap_no_instance_calls_are_read(index, monkeypatch):
    from apidrift import rbobjects
    monkeypatch.setattr(rbobjects, "MAX_WORK", 2)  # three mentions of pi
    text = "def go(id)\n  pi = Stripe::PaymentIntent.retrieve(id)\n  pi.confirm\n  pi.cancel\nend\n"
    assert instance(index, text) == []
