"""Ruby reading gaps found on real repositories (lago, chatwoot, discourse-subscriptions).

A leading ``::`` (``::Stripe::PaymentIntent.create``) hid every call written that way; a comment ending in
"." swallowed the call on the next line; ``else`` + newline + ``::Stripe::`` was reported on the ``else``
line; ``.rake`` files were not read.
"""

import pytest
from specs import sdk_doc

from apidrift.openapi import load_spec
from apidrift.scanner import scan_repo, scan_text
from apidrift.sdkmap import SdkIndex
from test_fixer import NEW, OLD, plan_for


@pytest.fixture(scope="module")
def index():
    return SdkIndex.build(load_spec(sdk_doc()))


def calls(text, index, rel="billing.rb"):
    usages, unresolved, _ = scan_text(text, rel, index)
    return [(u.line, u.call, u.label, sorted(u.fields), u.complete) for u in usages], unresolved


def test_a_leading_double_colon_is_read_and_shown_without_it(index):
    text = (
        "module Billing\n"
        "  def charge\n"
        "    ::Stripe::Charge.create(\n"
        "      {amount: 100, currency: 'usd'},\n"
        "      {api_key: key, idempotency_key: \"charge-#{id}\"}\n"
        "    )\n"
        "  end\n"
        "end\n"
    )
    found, unresolved = calls(text, index)
    assert found == [(3, "Stripe::Charge.create", "POST /v1/charges", ["amount", "currency"], True)]
    assert unresolved == []


def test_a_leading_double_colon_after_an_operator_or_paren(index):
    text = "x = cond ? nil : ::Stripe::Charge.retrieve(id)\nwrap(::Stripe::Charge.capture(id, amount: 5))\n"
    found, _ = calls(text, index)
    assert [(line, call, label) for line, call, label, _, _ in found] == [
        (1, "Stripe::Charge.retrieve", "GET /v1/charges/{charge}"),
        (2, "Stripe::Charge.capture", "POST /v1/charges/{charge}/capture"),
    ]


def test_else_then_a_call_on_the_next_line_is_reported_on_its_own_line(index):
    text = (
        "if refund\n"
        "  1\n"
        "else\n"
        "  ::Stripe::Charge.create(amount: 1)\n"
        "end\n"
    )
    assert calls(text, index)[0] == [(4, "Stripe::Charge.create", "POST /v1/charges", ["amount"], True)]


def test_a_leading_dot_on_the_next_line_still_continues_the_chain(index):
    text = "Stripe::Charge\n  .create(amount: 1)\n"
    assert calls(text, index)[0] == [(1, "Stripe::Charge.create", "POST /v1/charges", ["amount"], True)]


@pytest.mark.parametrize("rel,text", [
    ("pay.rb", "# Create the charge.\nStripe::Charge.create(amount: 1)\n"),
    ("pay.rb", "  # See Stripe::Charge.\n  ::Stripe::Charge.create(amount: 1)\n"),
    ("pay.js", "const stripe = require('stripe')(key);\n// Uses stripe.\nstripe.charges.create({ amount: 1 });\n"),
    ("pay.ts", "const stripe = new Stripe(key);\n/* done by stripe. */\nstripe.charges.create({ amount: 1 });\n"),
])
def test_a_comment_ending_in_a_dot_does_not_hide_the_next_call(index, rel, text):
    found, _ = calls(text, index, rel)
    assert [(label, fields) for _, _, label, fields, _ in found] == [("POST /v1/charges", ["amount"])]
    assert found[0][0] == text.count("\n")  # the call's own line, the last one


def test_a_call_inside_a_comment_or_string_is_still_not_a_call(index):
    text = "# Stripe::Charge.create(amount: 1)\nputs 'Stripe::Charge.create(amount: 2)'\n"
    assert calls(text, index) == ([], [])


def test_rake_files_are_read_as_ruby(tmp_path, index):
    (tmp_path / "lib" / "tasks").mkdir(parents=True)
    (tmp_path / "lib" / "tasks" / "billing.rake").write_text(
        "task backfill: :environment do\n  ::Stripe::Charge.create(amount: 1, currency: 'usd')\nend\n"
    )
    result = scan_repo(tmp_path, index)
    assert [(u.where, u.language, u.label) for u in result.usages] == [
        ("lib/tasks/billing.rake:2", "ruby", "POST /v1/charges")
    ]


def test_the_fixer_renames_at_a_leading_double_colon_call(tmp_path):
    src = (
        "class Pay\n"
        "  def call\n"
        "    # Create the intent.\n"
        "    ::Stripe::PaymentIntent.create(\n"
        f"      {{amount: 1, {OLD}: ['card']}},\n"
        "      {api_key: key}\n"
        "    )\n"
        "  end\n"
        "end\n"
    )
    plan = plan_for(tmp_path, {"app/services/pay.rb": src})
    assert plan.unfixable == [] and plan.not_attempted == [] and len(plan.fixes) == 1, plan.to_dict()
    fix = plan.fixes[0]
    assert fix.content == src.replace(f"{OLD}:", f"{NEW}:")
    assert [(s.line, s.target, s.replacement) for s in fix.sites] == [(4, OLD, NEW)]


def test_the_fixer_follows_a_binding_to_a_leading_double_colon_call(tmp_path):
    src = (
        "def pay(types)\n"
        f"  payload = {{ amount: 1, {OLD}: types }}\n"
        "  if retry?\n"
        "    nil\n"
        "  else\n"
        "    ::Stripe::PaymentIntent.create(payload)\n"
        "  end\n"
        "end\n"
    )
    plan = plan_for(tmp_path, {"pay.rb": src})
    assert plan.unfixable == [] and len(plan.fixes) == 1, plan.to_dict()
    assert plan.fixes[0].content == src.replace(f"{OLD}:", f"{NEW}:")
    assert [s.line for s in plan.fixes[0].sites] == [6]
