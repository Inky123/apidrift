"""P13: a regex, generic, heredoc or percent literal blocks an edit only where it can change how the edited
entry is read: inside the entry, before it unless it is plainly division or a comparison, or after it when the
same key appears there again. Division in an unrelated sibling (fxa's ``Math.floor(... / 1000)``) does not."""

import pytest

from apidrift import fixscope
from apidrift.jsparse import Source
from test_fixer_v2 import plan_v2

JS = "const stripe = require('stripe')('sk');\n"
TS = "import Stripe from 'stripe';\nconst stripe = new Stripe('sk');\n"
RB = "require 'stripe'\n\n"
SUB = "stripe.subscriptions.update('sub_1', { %s });\n"
SUB_RB = "Stripe::Subscription.update('sub_1', %s)\n"
PM = "stripe.paymentMethods.update('pm_1', { %s });\n"

FXA = """async function move(subscription, customerId) {
  await stripe.subscriptions.update(subscription.id, {
    proration_behavior: this.prorationBehavior,
    metadata: {
      currency: subscription.currency,
      plan_change_date: Math.floor(new Date().getTime() / 1000),
      previous_plan_id: this.sourcePlanId,
    },
    billing_cycle_anchor: this.resetBillingCycleAnchor
      ? 'now'
      : 'unchanged',
  });
}
"""


def whys(plan):
    return [(u.target, u.why) for u in plan.unfixable]


def test_fxa_division_in_an_earlier_sibling_no_longer_blocks_the_wrap(tmp_path):
    plan = plan_v2(tmp_path, {"a.ts": TS + FXA})
    assert plan.unfixable == [] and len(plan.fixes) == 1, plan.to_dict()
    want = FXA.replace(
        "billing_cycle_anchor: this.resetBillingCycleAnchor\n      ? 'now'\n      : 'unchanged',",
        "billing_cycle_anchor: { type: this.resetBillingCycleAnchor\n      ? 'now'\n      : 'unchanged' },",
    )
    assert plan.fixes[0].content == TS + want


@pytest.mark.parametrize("name,files,before,after", [
    ("js_div_after_name", "a.js", SUB % "metadata: { at: now / 1000 }, billing_cycle_anchor: 'now'",
     "billing_cycle_anchor: { type: 'now' }"),
    ("js_div_after_index", "a.js", SUB % "metadata: { at: xs[0] / 2 }, billing_cycle_anchor: 'now'",
     "billing_cycle_anchor: { type: 'now' }"),
    ("js_comparison", "a.ts", SUB % "metadata: { small: String(n < 10) }, billing_cycle_anchor: 'now'",
     "billing_cycle_anchor: { type: 'now' }"),
    ("js_division_after_the_entry", "a.js", SUB % "billing_cycle_anchor: 'now', metadata: { at: now / 1000 }",
     "billing_cycle_anchor: { type: 'now' }"),
    ("js_url_template_before", "a.js", SUB % "metadata: { url: `${base}/v1/x` }, billing_cycle_anchor: 'now'",
     "billing_cycle_anchor: { type: 'now' }"),
    ("rb_division_spaced", "a.rb", SUB_RB % "metadata: { at: Time.now.to_i / 1000 }, billing_cycle_anchor: 'now'",
     "billing_cycle_anchor: { type: 'now' }"),
    ("rb_division_tight", "a.rb", SUB_RB % "metadata: { at: (t/1000) }, billing_cycle_anchor: 'now'",
     "billing_cycle_anchor: { type: 'now' }"),
])
def test_division_and_comparisons_outside_the_entry_are_read_correctly(tmp_path, name, files, before, after):
    head = {"a.js": JS, "a.ts": TS, "a.rb": RB}[files]
    plan = plan_v2(tmp_path, {files: head + before})
    assert plan.unfixable == [] and len(plan.fixes) == 1, (name, plan.to_dict())
    assert plan.fixes[0].content == head + before.replace("billing_cycle_anchor: 'now'", after)


@pytest.mark.parametrize("name,file,text", [
    # inside the edited entry: refused whatever it is
    ("value_regex", "a.js", SUB % "billing_cycle_anchor: /x/.test(s) ? 'now' : 'unchanged'"),
    ("value_division", "a.js", SUB % "billing_cycle_anchor: a / b ? 'now' : 'unchanged'"),
    ("value_template_regex", "a.js", PM % "metadata: {}, payto: `${s.replace(/[,]/g, '')}`"),
    ("value_generic", "a.ts", SUB % "billing_cycle_anchor: pick<A, B>(x)"),
    # before it: a regex or anything the cursor cannot place
    ("regex_class_before", "a.js", SUB % "metadata: { re: /[,]/.source }, billing_cycle_anchor: 'now'"),
    ("regex_in_call_before", "a.js", PM % "metadata: { a: x.split(/,/).length }, payto: 1, billing_details: {}"),
    ("regex_after_if_before", "a.js",
     SUB % "metadata: { f: () => { if (x) /[,]/.test(y); } }, billing_cycle_anchor: 'now'"),
    ("regex_after_return_before", "a.js",
     SUB % "metadata: { f: () => { return /[,]/.test(y); } }, billing_cycle_anchor: 'now'"),
    ("regex_after_brace_before", "a.js", SUB % "metadata: {} /[,]/, billing_cycle_anchor: 'now'"),
    ("generic_before", "a.ts", SUB % "metadata: pick<A, B>(x), billing_cycle_anchor: 'now'"),
    ("jsx_before", "a.ts", SUB % "metadata: { label: <b>do not</b> }, billing_cycle_anchor: 'now'"),
    ("template_regex_before", "a.js", SUB % "metadata: { s: `${s.replace(/[,]/g, '')}` }, billing_cycle_anchor: 'now'"),
    ("rb_heredoc_before", "a.rb",
     "Stripe::Subscription.update('sub_1', description: <<~TXT, billing_cycle_anchor: 'now')\n  it's\nTXT\n"),
    ("rb_regex_arg_before", "a.rb", SUB_RB % "metadata: { n: s.split /,/ }, billing_cycle_anchor: 'now'"),
    ("rb_percent_before", "a.rb", SUB_RB % "metadata: { n: %q(a, b) }, billing_cycle_anchor: 'now'"),
    ("rb_percent_r_before", "a.rb", SUB_RB % "metadata: { n: %r{a,b} }, billing_cycle_anchor: 'now'"),
    # after it: a misread there could end the literal early and hide a later key or spread
    ("dup_key_hidden_after", "a.js",
     SUB % "billing_cycle_anchor: 'now', re: /{/, billing_cycle_anchor: 'unchanged', x: /}/"),
    ("regex_after_the_entry", "a.js", SUB % "billing_cycle_anchor: 'now', metadata: { ok: /a,b/.test(s) }"),
    # review r2: the `}` in the regex ended the literal for the cursor, so the last copy (which wins) was unseen
    ("review_r2", "a.js",
     "stripe.subscriptions.update('sub_1', {\n  billing_cycle_anchor: 'now',\n  re: /}/,\n"
     "  billing_cycle_anchor: 'unchanged'\n});\n"),
    # review r3: the same `}` hid a spread that may send the key
    ("review_r3", "a.js",
     "function go(o) {\n  return stripe.subscriptions.update('sub_1', {\n    billing_cycle_anchor: 'now',\n"
     "    re: /}/,\n    ...o\n  });\n}\n"),
    # review r8: a heredoc after the entry hides the later copy
    ("review_r8", "a.rb",
     "Stripe::Subscription.update('sub_1',\n  billing_cycle_anchor: 'now',\n  description: <<~EOS,\n"
     "    see note :)\n  EOS\n  billing_cycle_anchor: 'unchanged')\n"),
    # review r9: `for await (...)`: a regex after a statement head, not a division
    ("review_r9", "a.js",
     "stripe.subscriptions.update('sub_1', {\n  metadata: { f: async () => { for await (const x of xs) /\"/.test(x); } },\n"
     "  description: \"} }, billing_cycle_anchor: 'now', w: { v: { u: \",\n"
     "  tail: { g: async () => { for await (const x of xs) /\"/.test(x); } },\n});\n"),
    ("rb_next_regex", "a.rb", SUB_RB % "metadata: { f: -> { next/,/ } }, billing_cycle_anchor: 'now'"),
    ("rb_break_regex", "a.rb", SUB_RB % "metadata: { f: -> { break/,/ } }, billing_cycle_anchor: 'now'"),
    ("rb_char_literal", "a.rb", SUB_RB % "metadata: { open: ?' }, billing_cycle_anchor: 'now', x: { close: ?' }"),
])
def test_text_the_cursor_may_misread_still_blocks_the_edit(tmp_path, name, file, text):
    head = {"a.js": JS, "a.ts": TS, "a.rb": RB}[file]
    plan = plan_v2(tmp_path, {file: head + text})
    assert plan.fixes == [], (name, plan.fixes[0].diff() if plan.fixes else "")
    if name not in NOT_FOUND:
        target = "payto" if "payto" in text else "billing_cycle_anchor"
        assert (target, "unsupported_value") in whys(plan), (name, plan.to_dict())  # found, and refused here


NOT_FOUND = {"rb_char_literal"}  # cases where the cursor loses the key or call itself (nothing is written either way)


def test_a_refused_site_says_unsupported_value(tmp_path):
    plan = plan_v2(tmp_path, {"a.js": JS + SUB % "metadata: { re: /a,b/.source }, billing_cycle_anchor: 'now'"})
    assert whys(plan) == [("billing_cycle_anchor", "unsupported_value")]


@pytest.mark.parametrize("text", [
    SUB % "metadata: { re: /'/.source }, billing_cycle_anchor: 'now'",
    SUB % "billing_cycle_anchor: 'now', re: /'/, billing_cycle_anchor: 'unchanged', x: '/'",
    "stripe.subscriptions.update('sub_1', {\n  re: /'/, billing_cycle_anchor: 'now', x: '/',\n});\n",
])
def test_a_quote_inside_a_regex_never_yields_an_edit(tmp_path, text):
    """Here the cursor itself loses the call or the key; whichever way, nothing is written."""
    assert plan_v2(tmp_path, {"a.js": JS + text}).fixes == []


@pytest.mark.parametrize("text,ruby,unsafe", [
    ("{ a: x / 2, k: 1 }", False, False),
    ("{ a: f(x) / 2, k: 1 }", False, False),
    ("{ a: (x) /re/g, k: 1 }", False, False),  # JS reads this as division too
    ("{ a: typeof /x/, k: 1 }", False, True),
    ("{ a: x ? /y/ : z, k: 1 }", False, True),
    ("{ a: while (x) /y/, k: 1 }", False, True),
    ("{ a: x <= y, k: 1 }", False, False),
    ("{ a: x<y, k: 1 }", False, True),
    ("{ a: x << 2, k: 1 }", True, False),
    ("{ a: x <<~EOS, k: 1 }", True, True),
    ("{ a: n % 3, k: 1 }", True, False),
    ("{ a: %w[a b], k: 1 }", True, True),
    ("{ a: when /x/, k: 1 }", True, True),
    ("{ a: x / 2, k: 1 }", True, False),
    ("{ a: x/2, k: 1 }", True, False),
    ("{ a: split /,/, k: 1 }", True, True),
    ("{ a: x\n  / 2/, k: 1 }", True, True),  # a Ruby line may start a regex
    ("{ a: x\n  / 2, k: 1 }", False, False),  # JS has no line break before a division
    ("{ a: fmt %q(it), k: 1 }", True, True),
    ("{ a: n % q, k: 1 }", True, False),
    ("{ a: \"#{x / 2}\", k: 1 }", True, False),
    ("{ a: \"#{s =~ /'/}\", k: 1 }", True, True),
    ("{ a: for await (x of y) /re/, k: 1 }", False, True),
    ("{ a: await (x) / 2, k: 1 }", False, True),  # positive-only: a ( after a keyword is never a value
    ("{ a: next/x/, k: 1 }", True, True),
    ("{ a: break/x/, k: 1 }", True, True),
    ("{ a: puts /x/, k: 1 }", True, True),
    ("{ a: return /x/, k: 1 }", False, True),
    ("{ a: } /x/, k: 1 }", False, True),
    ("{ a: ?', k: 1 }", True, True),
    ("{ a: $', k: 1 }", True, True),
    ("{ a: x ? 1 : 2, k: 1 }", True, False),
    ("{ a: x.empty? ? 1 : 2, k: 1 }", True, False),
    ("{ k: 1, b: /}/, k: 2 }", False, True),  # after the entry: checked to the closer
])
def test_the_prefix_classifier(text, ruby, unsafe):
    src = Source(text, ruby=ruby)
    key = text.index("k:")
    end = src.skip_scalar(key)
    assert fixscope.unsafe_entry(src, 0, key, end, src.skip_scalar(1, ""), strict=True) is unsafe


# --- renames: the same guard (review item b) ------------------------------------------------

PI = "stripe.paymentIntents.create({ %s });\n"


@pytest.mark.parametrize("body", [
    "amount: total / 100, payment_method_types: ['card']",  # division in a sibling, and below in the value
    "payment_method_types: ['card'], amount: Math.round(x / 2)",
    "amount: 1, payment_method_types: ids.length / 2 ? ['card'] : []",
])
def test_a_rename_next_to_division_still_goes_through(tmp_path, body):
    from test_fixer import plan_for

    plan = plan_for(tmp_path, {"a.js": JS + PI % body})
    assert plan.unfixable == [] and len(plan.fixes) == 1, plan.to_dict()
    assert plan.fixes[0].content == JS + PI % body.replace("payment_method_types", "allowed_payment_method_types")


@pytest.mark.parametrize("body", [
    "q: /[,]/, payment_method_types: ['card']",
    "payment_method_types: ['card'], r: /}/, payment_method_types: ['sepa_debit']",
    "description: `${s.replace(/[,]/g, '')}`, payment_method_types: ['card']",
])
def test_a_rename_next_to_a_regex_is_refused(tmp_path, body):
    from test_fixer import plan_for

    plan = plan_for(tmp_path, {"a.js": JS + PI % body})
    assert plan.fixes == [] and {u.why for u in plan.unfixable} == {"unsupported_value"}, plan.to_dict()


def test_review_r7_a_key_inside_a_string_after_a_regex_is_never_renamed(tmp_path):
    from test_fixer import plan_for

    text = JS + ("stripe.paymentIntents.create({\n  amount: 100,\n  currency: 'usd',\n  q: /'/,\n"
                 "  description: ' payment_method_types: [\"card\"], ',\n  r: /'/,\n});\n")
    assert plan_for(tmp_path, {"a.js": text}).fixes == []
