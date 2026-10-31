"""A spread or ``**kwargs`` hides only the keys under the object it sits in, and literal spreads are read.

Cases come from real false "possibly" findings: twenty (``metadata: { ...patch }``), formbricks
(``items: [...deletions, ...items]``), cal.com and dub (``...(cond ? { ... } : {})``).
"""

import pytest
from specs import sdk_doc

from apidrift import jsparse, pyparse
from apidrift.diff import BREAKING, Change
from apidrift.impact import AFFECTED, POSSIBLE, assess
from apidrift.jsparse import Source
from apidrift.openapi import load_spec
from apidrift.scanner import Usage, scan_repo
from apidrift.sdkmap import Resolved, SdkIndex


def read(text, *, ruby=False):
    fields = jsparse.parse_object(Source(text, ruby=ruby), text.index("{") + 1)
    return fields.paths, fields.hidden, fields.complete


@pytest.mark.parametrize("text,paths,hidden", [
    ("{ a: 1, metadata: { ...patch, k: 1 } }", {"a", "metadata", "metadata.k"}, {"metadata."}),
    ("{ items: [...deletions, { price: 1 }] }", {"items", "items[].price"}, {"items[]."}),
    ("{ a: { [KEY]: 1 } }", {"a"}, {"a."}),
    ("{ a: 1, ...rest }", {"a"}, {""}),
    ("{ a: 1, ...(c ? x : {}) }", {"a"}, {""}),
    ("{ a: 1, ...(p ? q ? { b: 1 } : {} : {}) }", {"a"}, {""}),
    ("{ a: 1, ...(c ? { b: 1 } : {}) as Params }", {"a"}, {""}),
    ("{ a: 1, ...(c ? { b: 1 } : {}).x }", {"a"}, {""}),
    ("{ a: 1, ...(c ? { ...y } : {}) }", {"a"}, {""}),
    ("{ a: 1, ...(c ? () => ({ b: 1 }) : {}) }", {"a"}, {""}),
])
def test_a_spread_hides_only_the_keys_under_its_own_object(text, paths, hidden):
    assert read(text) == (paths, hidden, False)


@pytest.mark.parametrize("text,paths", [
    ("{ a: 1, ...(c ? { b: 2 } : {}) }", {"a", "b"}),
    ("{ a: 1, ...(c && { b: 2 }) }", {"a", "b"}),
    ("{ a: 1, ...(c ? { b: 2 } : undefined) }", {"a", "b"}),
    ("{ ...(c ? { b: 2 } : { d: { e: 1 } }) }", {"b", "d", "d.e"}),
    ("{ ...(c\n  ? { b: 2, /* note */ f: [1] }\n  : null), g: 3 }", {"b", "f", "g"}),
    ("{ ...{ b: 2 } }", {"b"}),
    ("{ sub: { ...(c ? { x: 1 } : {}) } }", {"sub", "sub.x"}),
    ("{ ...(s.length > 0 ? { b: `${s}` } : {}) }", {"b"}),
])
def test_literal_spreads_are_read_exactly(text, paths):
    assert read(text) == (paths, set(), True)


def test_keys_in_a_literal_spread_have_no_spans_so_the_fixer_leaves_them_alone():
    src = Source("{ a: 1, ...(c ? { payment_method_types: ['card'] } : {}) }")
    fields = jsparse.parse_object(src, 1)
    assert "payment_method_types" in fields.paths and set(fields.spans) == {"a"}


def test_ruby_double_splat_hides_its_own_hash():
    assert read("{ a: 1, **opts }", ruby=True) == ({"a"}, {""}, False)
    assert read("{ meta: { **m } }", ruby=True) == ({"meta"}, {"meta."}, False)


def test_python_double_star_and_starred_items_hide_their_own_dict():
    text = (
        "import stripe\n"
        "stripe.PaymentIntent.create(amount=1, metadata={**m, 'k': 1}, items=[*xs])\n"
        "stripe.PaymentIntent.create(amount=1, **params)\n"
    )
    found, _ = pyparse.extract(text, lambda chain: Resolved("post", "/v1/payment_intents", "x"))
    nested, top = found
    assert nested.fields == {"amount", "metadata", "metadata.k", "items"} and not nested.complete
    assert nested.hidden == {"metadata.", "items[]."}
    assert top.hidden == {""} and not top.complete


def usage(fields, hidden, complete=False):
    return Usage("pay.ts", 1, "post", "/v1/x", "stripe.x.create", frozenset(fields), complete, "typescript",
                 frozenset(), frozenset(hidden))


def change(kind, target):
    return Change(BREAKING, kind, "msg", "post", "/v1/x", target=target)


def test_a_removed_field_outside_the_hidden_object_is_not_a_possible_finding():
    nested = usage({"amount", "metadata"}, {"metadata."})
    assert assess([nested], [change("body_removed", "payment_method_types")]) == []
    (hit,) = assess([nested], [change("body_removed", "metadata.order")])
    assert hit.certainty == POSSIBLE


def test_a_required_child_needs_its_parent_to_be_possibly_sent():
    preview = usage({"subscription_details", "subscription_details.items"}, {"subscription_details.items[]."})
    required = change("body_added_required", "subscription_details.billing_cycle_anchor.type")
    assert assess([preview], [required]) == []
    (hit,) = assess([usage({"amount"}, {"subscription_details."})], [required])
    assert hit.certainty == POSSIBLE


def test_a_top_level_spread_still_hides_everything_and_old_usages_fall_back_to_complete():
    assert assess([usage({"amount"}, {""})], [change("body_removed", "payment_method_types")])[0].certainty == POSSIBLE
    legacy = Usage("pay.py", 1, "post", "/v1/x", "c", frozenset({"amount"}), False, "python")
    assert legacy.hides("anything") and not Usage("p", 1, "post", "/v1/x", "c", frozenset(), True, "python").hides("x")
    sent = usage({"payment_method_types"}, {""})
    assert assess([sent], [change("body_removed", "payment_method_types")])[0].certainty == AFFECTED


STUB = 'stub_request(:post, "https://api.stripe.com/v1/charges")  # stripe\n'


def test_stripe_urls_in_tests_are_not_call_sites_but_sdk_calls_there_still_are(tmp_path):
    for rel in ("spec/payments_spec.rb", "app/pay_test.rb", "src/__tests__/a.ts", "src/pay.test.ts",
                "tests/e2e/helpers.ts", "test_pay.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(STUB if rel.endswith(".rb") else f"fetch('https://api.stripe.com/v1/charges') // stripe\n")
    (tmp_path / "spec" / "real_spec.rb").write_text("Stripe::Charge.create(amount: 1, currency: 'usd')\n")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "pay.sh").write_text("curl https://api.stripe.com/v1/charges  # stripe\n")
    result = scan_repo(tmp_path, SdkIndex.build(load_spec(sdk_doc())))
    assert sorted(u.file for u in result.counted) == ["bin/pay.sh", "spec/real_spec.rb"]
    assert len(result.docs) == 6
