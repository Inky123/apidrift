"""Spreads of local objects and Ruby merges of same-file helper hashes, read exactly or left hidden.

Shapes from corpus B: Ghost (``{ ...urls, mode }`` of a const), langfuse (``const cancellationPayload = c ? { a } :
{ b }`` spread twice), vercel's subscription starter (``params = { ...params, mode }`` in both branches), and the pay
gem (``{cancel_at_period_end: true}.merge(expand_options)``, ``stripe_options`` as the request options argument).
"""

import pytest
from test_fixer import plan_for
from test_helpers import scan

JS = "const stripe = require('stripe')('sk');\n"


def sessions(tmp_path, text, name="pay.js", path="/v1/checkout/sessions"):
    found = [u for u in scan(tmp_path, {name: text}) if u.path == path]
    assert len(found) == 1, found
    return set(found[0].fields), set(found[0].hidden or ())


def in_function(body: str) -> str:
    return JS + "async function go(c, extra) {\n" + body + "}\n"


def test_a_spread_const_object_adds_its_keys(tmp_path):
    text = in_function(
        "  const urls = { success_url: 's', cancel_url: 'c' };\n"
        "  await stripe.checkout.sessions.create({ ...urls, mode: 'subscription' });\n")
    assert sessions(tmp_path, text) == ({"success_url", "cancel_url", "mode"}, set())


def test_a_spread_const_choice_of_two_literals_adds_both_branches(tmp_path):
    text = in_function(
        "  const pick = c\n    ? { success_url: 's' }\n    : { cancel_url: 'c' };\n"
        "  await stripe.checkout.sessions.create({ mode: 'payment', ...pick });\n")
    assert sessions(tmp_path, text) == ({"success_url", "cancel_url", "mode"}, set())


def test_a_rebinding_that_spreads_the_earlier_value_in_each_branch(tmp_path):
    text = in_function(
        "  let params = { success_url: 's' };\n"
        "  if (c) {\n    params = { ...params, mode: 'subscription', subscription_data: { trial_end: 1 } };\n"
        "  } else {\n    params = { ...params, mode: 'payment' };\n  }\n"
        "  await stripe.checkout.sessions.create(params);\n")
    assert sessions(tmp_path, text) == (
        {"success_url", "mode", "subscription_data", "subscription_data.trial_end"}, set())


def test_a_written_local_spread_keeps_the_written_key(tmp_path):
    text = in_function(
        "  const urls = { success_url: 's' };\n  urls.cancel_url = 'c';\n"
        "  await stripe.checkout.sessions.create({ ...urls });\n")
    assert sessions(tmp_path, text) == ({"success_url", "cancel_url"}, {"cancel_url."})


@pytest.mark.parametrize("body", [
    # passed to a function before the spread: it may have gained keys
    "  const urls = { success_url: 's' };\n  fill(urls);\n",
    # aliased
    "  const urls = { success_url: 's' };\n  const other = urls;\n  other.payment_method_types = ['card'];\n",
    # written in a callback
    "  const urls = { success_url: 's' };\n  later(() => { urls.payment_method_types = ['card']; });\n",
    # a parameter
    "  const unused = 1;\n",
    # a binding that is a call
    "  const urls = build();\n",
    # a choice whose branch is not a literal
    "  const urls = c ? { success_url: 's' } : extra;\n",
    # a computed write
    "  const urls = { success_url: 's' };\n  urls[key] = 1;\n",
])
def test_a_local_that_may_hold_more_than_its_literal_stays_hidden(tmp_path, body):
    name = "extra" if "unused" in body else "urls"
    text = in_function(body + f"  await stripe.checkout.sessions.create({{ mode: 'payment', ...{name} }});\n")
    fields, hidden = sessions(tmp_path, text)
    assert "" in hidden and "payment_method_types" not in fields


def test_a_module_const_spread_inside_a_function_stays_hidden(tmp_path):
    text = JS + "const urls = { success_url: 's' };\nfunction tweak() { urls.payment_method_types = ['card']; }\n" + (
        "async function go() { await stripe.checkout.sessions.create({ ...urls }); }\n")
    assert "" in sessions(tmp_path, text)[1]


def test_locals_spreading_each_other_terminate(tmp_path):
    text = in_function(
        "  let a = { success_url: 's' };\n  let b = { ...a };\n  a = { ...b, cancel_url: 'c' };\n  b = { ...a };\n"
        "  await stripe.checkout.sessions.create({ ...b });\n")
    fields, _ = sessions(tmp_path, text)
    assert {"success_url", "cancel_url"} <= fields


# --- Ruby: merges of same-file helper hashes -----------------------------------------------------

PAY = """
class Subscription
  def self.expand_options
    {
      expand: ["latest_invoice"]
    }
  end

  def cancel
    ::Stripe::PaymentIntent.create({cancel_at_period_end: true}.merge(expand_options), stripe_options)
  end

  def pause(**options)
    attributes = {pause_collection: options}
    ::Stripe::PaymentIntent.create(attributes.merge(expand_options), stripe_options)
  end

  def cancel_now!(**options)
    ::Stripe::PaymentIntent.create(options.merge(expand_options), stripe_options)
  end

  def swap(**options)
    ::Stripe::PaymentIntent.create({cancel_at_period_end: false}.merge(expand_options).merge(options))
  end

  private

  def stripe_options
    {stripe_account: stripe_account}.compact
  end

  def expand_options
    self.class.expand_options
  end
end
"""


def ruby_updates(tmp_path, text):
    return [
        (set(u.fields), set(u.hidden or ()))
        for u in scan(tmp_path, {"subscription.rb": text}) if u.path == "/v1/payment_intents"
    ]


def test_a_ruby_hash_merged_with_helper_hashes_is_read(tmp_path):
    assert ruby_updates(tmp_path, PAY) == [
        ({"cancel_at_period_end", "expand"}, set()),  # literal.merge(helper); stripe_options is request options
        ({"pause_collection", "expand"}, set()),  # a local bound to a literal, merged
        (set(), {""}),  # a parameter merged with a helper: hidden, as before
        ({"cancel_at_period_end", "expand"}, {""}),  # a chain ending in a parameter: hidden
    ]


def test_a_ruby_instance_method_handing_on_to_another_classes_method_is_not_followed(tmp_path):
    text = PAY.replace("  def self.expand_options", "end\n\nclass Other\n  def self.expand_options", 1).replace(
        "  def cancel\n", "end\n\nclass Subscription\n  def cancel\n", 1)
    assert ruby_updates(tmp_path, text)[0] == ({"cancel_at_period_end"}, {""})


def test_a_ruby_merge_of_another_objects_method_stays_hidden(tmp_path):
    text = (
        "class Charge\n  def charge\n    params = {amount: 1}\n    params.merge!(chargeable.stripe_charge_params)\n"
        "    Stripe::PaymentIntent.create(params)\n  end\n\n  def stripe_charge_params\n    {customer: 1}\n  end\nend\n")
    found = [u for u in scan(tmp_path, {"charge.rb": text}) if u.path == "/v1/payment_intents"]
    assert [("" in set(u.hidden or ())) for u in found] == [True]


def test_a_key_seen_only_through_a_local_spread_is_reported_and_never_edited(tmp_path):
    text = JS + (
        "async function go() {\n"
        "  const base = { payment_method_types: ['card'] };\n"
        "  await stripe.paymentIntents.create({ amount: 1, ...base });\n"
        "}\n")
    plan = plan_for(tmp_path, {"pay.js": text})
    assert plan.fixes == []
    assert [(u.target, u.why) for u in plan.unfixable] == [("payment_method_types", "not_found")]


def test_a_ruby_literal_merged_with_a_helper_is_never_edited(tmp_path):
    text = (
        "class Pay\n  def charge\n    Stripe::PaymentIntent.create({payment_method_types: ['card']}.merge(extra))\n  end\n\n"
        "  def extra\n    {amount: 1}\n  end\nend\n")
    plan = plan_for(tmp_path, {"pay.rb": text})
    assert plan.fixes == []
    assert [(u.target, u.why) for u in plan.unfixable] == [("payment_method_types", "incomplete_binding")]
