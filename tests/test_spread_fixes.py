"""Keys inside a fully read literal spread branch are edited in that branch; every spread the reader does not
read exactly keeps the call unfixable. A request-options argument written as a choice is not parameters.

Cases come from formbricks (``...(upgradeIntent ? { payment_method_types: ["card"] } : {})``) and dub
(``idempotencyKey ? { idempotencyKey } : undefined`` as the options argument).
"""

import pytest

from apidrift import jsparse
from apidrift.jsparse import Source
from test_fixer import plan_for
from test_fixer_v2 import plan_v2

JS = "const stripe = require('stripe')('sk');\n"
PI = "stripe.paymentIntents.create(%s);\n"


def renamed(tmp_path, body):
    plan = plan_for(tmp_path, {"a.js": JS + body})
    assert plan.unfixable == [] and len(plan.fixes) == 1, plan.to_dict()
    return plan.fixes[0].content[len(JS):]


FIXED = {
    "branch": ("{ amount: 1, ...(c ? { payment_method_types: ['card'] } : {}) }",
               "{ amount: 1, ...(c ? { allowed_payment_method_types: ['card'] } : {}) }"),
    "and": ("{ amount: 1, ...(c && { payment_method_types: ['card'] }) }",
            "{ amount: 1, ...(c && { allowed_payment_method_types: ['card'] }) }"),
    "plain_spread": ("{ amount: 1, ...{ payment_method_types: ['card'] } }",
                     "{ amount: 1, ...{ allowed_payment_method_types: ['card'] } }"),
    "both_branches": ("{ ...(c ? { payment_method_types: ['card'] } : { payment_method_types: ['link'] }) }",
                      "{ ...(c ? { allowed_payment_method_types: ['card'] } : { allowed_payment_method_types: ['link'] }) }"),
    "spread_first": ("{ ...(c ? { payment_method_types: ['card'] } : undefined), amount: 1 }",
                     "{ ...(c ? { allowed_payment_method_types: ['card'] } : undefined), amount: 1 }"),
    "spread_last_trailing": ("{\n  amount: 1,\n  ...(c ? { payment_method_types: ['card'], } : {}),\n}",
                             "{\n  amount: 1,\n  ...(c ? { allowed_payment_method_types: ['card'], } : {}),\n}"),
    "comments": ("{ ...(c\n  ? { /* pm */ payment_method_types: ['card'], // only cards\n    }\n  : null) }",
                 "{ ...(c\n  ? { /* pm */ allowed_payment_method_types: ['card'], // only cards\n    }\n  : null) }"),
    "sibling_too": ("{ payment_method_types: ['a'], ...(c ? { payment_method_types: ['b'] } : {}) }",
                    "{ allowed_payment_method_types: ['a'], ...(c ? { allowed_payment_method_types: ['b'] } : {}) }"),
    "shorthand": ("{ amount: 1, ...(c ? { payment_method_types } : {}) }",
                  "{ amount: 1, ...(c ? { allowed_payment_method_types: payment_method_types } : {}) }"),
    "dub": ("{\n  amount,\n  ...(invoiceId ? { transfer_group: invoiceId } : {}),\n  payment_method_types: ['card', 'link'],\n},\n"
            "idempotencyKey ? { idempotencyKey } : undefined",
            "{\n  amount,\n  ...(invoiceId ? { transfer_group: invoiceId } : {}),\n  allowed_payment_method_types: ['card', 'link'],\n},\n"
            "idempotencyKey ? { idempotencyKey } : undefined"),
    "options_and": ("{ payment_method_types: ['card'] }, acct && { stripeAccount: acct }",
                    "{ allowed_payment_method_types: ['card'] }, acct && { stripeAccount: acct }"),
}


@pytest.mark.parametrize("case", sorted(FIXED))
def test_a_key_in_a_literal_spread_branch_is_renamed_where_it_is_written(tmp_path, case):
    before, after = FIXED[case]
    assert renamed(tmp_path, PI % before) == PI % after


def test_a_spread_branch_of_a_bound_literal_is_renamed(tmp_path):
    body = "const params = { amount: 1, ...(c ? { payment_method_types: ['card'] } : {}) };\n" + PI % "params"
    assert "...(c ? { allowed_payment_method_types: ['card'] } : {}) };" in renamed(tmp_path, body)


NOT_READ = {  # the spread is not read exactly: the key is never seen, so the call is only a possible hit
    "variable_branch": "{ amount: 1, ...(c ? { payment_method_types: ['card'] } : extra) }",
    "as_type": "{ amount: 1, ...(c ? { payment_method_types: ['card'] } as P : {}) }",
    "member_after": "{ amount: 1, ...({ payment_method_types: ['card'] }).valueOf() }",
    "arrow": "{ amount: 1, ...(c ? () => ({ payment_method_types: ['card'] }) : {}) }",
    "nested_ternary": "{ amount: 1, ...(a ? { payment_method_types: ['card'] } : b ? { payment_method_types: ['link'] } : {}) }",
    "or": "{ amount: 1, ...(extra || { payment_method_types: ['card'] }) }",
    "call": "{ amount: 1, ...pick({ payment_method_types: ['card'] }) }",
}


@pytest.mark.parametrize("case", sorted(NOT_READ))
def test_a_spread_that_is_not_read_exactly_is_never_edited(tmp_path, case):
    plan = plan_for(tmp_path, {"a.js": JS + PI % NOT_READ[case]})
    assert plan.fixes == [] and plan.unfixable == [] and [u.why for u in plan.not_attempted] == ["arguments_not_visible"]


REFUSED = {
    # a plain key next to an unread spread that writes the key too: the spread's key is never edited
    "plain_key_next_to_unread_spread": ("{ payment_method_types: ['card'], ...(c ? { payment_method_types: ['link'] } : extra) }", "leftover_tokens"),
    # an options choice that also carries a parameter, or is not a literal, hides the call's fields
    "options_with_param": ("{ payment_method_types: ['card'] }, key ? { idempotencyKey: key, amount: 5 } : undefined", "incomplete_binding"),
    "options_variable": ("{ payment_method_types: ['card'] }, key ? opts : undefined", "incomplete_binding"),
    "options_spread": ("{ payment_method_types: ['card'] }, key ? { ...opts } : undefined", "incomplete_binding"),
    "options_cast": ("{ payment_method_types: ['card'] }, key ? { idempotencyKey: key } as O : undefined", "incomplete_binding"),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_spread_or_options_choice_that_may_send_the_key_is_refused(tmp_path, case):
    body, why = REFUSED[case]
    plan = plan_for(tmp_path, {"a.js": JS + PI % body})
    assert plan.fixes == [] and {u.why for u in plan.unfixable} == {why}, plan.to_dict()


V2_FIXED = {
    "delete_in_branch": (
        "stripe.paymentMethods.update('pm_1', { metadata: {}, ...(c ? { payto: { pay_id: 'a' }, billing_details: {} } : {}) });\n",
        "stripe.paymentMethods.update('pm_1', { metadata: {}, ...(c ? { billing_details: {} } : {}) });\n"),
    "delete_in_branch_multiline": (
        "stripe.paymentMethods.update('pm_1', {\n  metadata: {},\n  ...(c\n    ? {\n        payto: { pay_id: 'a' },\n        billing_details: {},\n      }\n    : {}),\n});\n",
        "stripe.paymentMethods.update('pm_1', {\n  metadata: {},\n  ...(c\n    ? {\n        billing_details: {},\n      }\n    : {}),\n});\n"),
    "rename_first_in_branch": (
        "stripe.financialConnections.sessions.create({ ...(c ? { filters: { countries: ['US', 'CA'] } } : {}) });\n",
        "stripe.financialConnections.sessions.create({ ...(c ? { filters: { country: 'US' } } : {}) });\n"),
    "rename_first_branch_in_parent": (
        "stripe.financialConnections.sessions.create({ filters: { ...(c ? { countries: ['US'] } : {}) } });\n",
        "stripe.financialConnections.sessions.create({ filters: { ...(c ? { country: 'US' } : {}) } });\n"),
    "wrap_in_branch": (
        "stripe.subscriptions.update('sub_1', { proration_behavior: 'none', ...(c ? { billing_cycle_anchor: 'now' } : {}) });\n",
        "stripe.subscriptions.update('sub_1', { proration_behavior: 'none', ...(c ? { billing_cycle_anchor: { type: 'now' } } : {}) });\n"),
}


@pytest.mark.parametrize("case", sorted(V2_FIXED))
def test_every_action_edits_a_key_inside_its_spread_branch(tmp_path, case):
    before, after = V2_FIXED[case]
    plan = plan_v2(tmp_path, {"a.js": JS + before})
    assert plan.unfixable == [] and len(plan.fixes) == 1, plan.to_dict()
    assert plan.fixes[0].content == JS + after


def test_deleting_the_only_key_of_a_branch_is_refused_not_emptied(tmp_path):
    body = "stripe.paymentMethods.update('pm_1', { metadata: {}, ...(c ? { payto: { pay_id: 'a' } } : {}) });\n"
    plan = plan_v2(tmp_path, {"a.js": JS + body})
    assert plan.fixes == [] and [u.why for u in plan.unfixable] == ["would_empty_literal"]


def test_keys_in_a_literal_spread_keep_spans_marked_with_their_branch():
    text = "{ a: 1, ...(c ? { payment_method_types: ['card'] } : {}) }"
    fields = jsparse.parse_object(Source(text), 1)
    (span,) = fields.spans["payment_method_types"]
    assert text[span.key_start : span.key_end] == "payment_method_types"
    assert span.holder == text.index("{ payment_method_types") + 1 and fields.spans["a"][0].holder == -1
    nested = "{ ...(c ? { m: { ...(d ? { k: 1 } : {}) } } : {}) }"
    inner = jsparse.parse_object(Source(nested), 1)
    assert inner.spans["m.k"][0].holder == nested.index("{ k") + 1 and inner.spans["m"][0].holder == nested.index("{ m") + 1


def test_ruby_double_splat_is_still_not_read():
    fields = jsparse.parse_object(Source("{ a: 1, **(c ? { payment_method_types: ['card'] } : {}) }", ruby=True), 1)
    assert "payment_method_types" not in fields.paths and not fields.complete
