"""The independent review's repros for spread reading and version switches (tests/fixtures/review_spreads).

Each case is a way the reader could have claimed to see every key while some code may still add one (a write
after the read in a loop, a hoisted mutator, a callback, a module-level object returned by a helper, a rest that
is written later), or trusted a comparison with a version the client is not configured with. Each must now be
hidden or read as sending both keys, and the fixer must not edit inside a version-shaped helper.
"""

import time
from pathlib import Path

import pytest
from test_fixer import plan_for
from test_helpers import scan

CASES = Path(__file__).parent / "fixtures" / "review_spreads"
SWITCH = ("payment_method_types", "allowed_payment_method_types")


def site(tmp_path, name):
    path = CASES / name
    found = [u for u in scan(tmp_path, {path.name: path.read_text()}) if u.path in ("/v1/payment_intents", "/v1/checkout/sessions")]
    assert len(found) == 1, found
    u = found[0]
    return set(u.fields), set(u.hidden or ()), {s[:2] for s in u.switched}


@pytest.mark.parametrize("name", [
    "c01_loop_write_after_spread.ts",  # written after the spread, in the loop that repeats it
    "c02_hoisted_mutator.ts",  # written by a function declared after the call
    "c03_shared_mutable_helper.ts",  # a helper returning a module-level object another function writes
    "c04_callback_later.ts",  # spread inside a callback, written after the callback exists
    "c05_helper_returns_param_mutated.ts",
    "c10_rest_then_mutated_in_loop.ts",  # the rest is written later in the loop
    "d01_loop_direct.ts",  # the same, passed directly
    "d02_callback_direct.ts",
    "d03_shared_direct.ts",
    "g01_fix_spread_mutated_after.ts",  # passed to a function after the call, in its loop
    "g02_fix_callback.ts",
    "h01_callback_write_direct.ts",  # written by a forEach callback
    "h03_spread_helper_shared.ts",  # the helper's object escapes through this.last
])
def test_a_local_that_other_code_may_still_change_is_hidden(tmp_path, name):
    fields, hidden, switched = site(tmp_path, name)
    assert "" in hidden and SWITCH not in switched


@pytest.mark.parametrize("name", [
    "c06_version_other_var.ts",  # event.api_version: not what the client sends
    "d04_version_other_direct.ts",
    "e01_version_param_literal_old.ts",  # a parameter, called with an old literal
    "e02_version_local_fixed_old.ts",  # a local fixed to an old version
    "e03_version_module_const_old.ts",  # a module const that is not the client's apiVersion
    "e04_reverse_compare_both_dates.ts",
    "c07_later_branch_both.ts",  # the later branch sends the old key too
    "c08_second_path_write.ts",  # the old key also written into the local
    "c09_rest_misuse.ts",  # another spread of the whole data
    "f01_switch_later_direct.ts",
    "f02_switch_later_spread.ts",
])
def test_no_switch_is_recorded_unless_the_client_version_picks_the_replacement_alone(tmp_path, name):
    fields, _, switched = site(tmp_path, name)
    assert "payment_method_types" in fields and SWITCH not in switched


def test_the_fixer_does_not_edit_inside_a_version_shaped_helper(tmp_path):
    path = CASES / "f01_switch_later_direct.ts"
    plan = plan_for(tmp_path, {path.name: path.read_text()})
    assert plan.fixes == []
    assert [(u.target, u.why) for u in plan.unfixable] == [("payment_method_types", "helper_version_switch")]


def test_a_choice_on_anything_else_is_still_fixed_in_its_branch(tmp_path):
    path = CASES / "f03_ternary_helper_direct.ts"
    plan = plan_for(tmp_path, {path.name: path.read_text()})
    assert plan.unfixable == [] and len(plan.fixes) == 1
    assert "flag ? { allowed_payment_method_types: types, amount: 1 } : { amount: 2 }" in plan.fixes[0].content


def test_a_ruby_merge_bang_of_an_unknown_value_hides_every_key(tmp_path):
    path = CASES / "h02_ruby_merge_bang.rb"
    found = [u for u in scan(tmp_path, {path.name: path.read_text()}) if u.path == "/v1/payment_intents"]
    assert [("" in u.hidden, "payment_method_types" in u.fields) for u in found] == [(True, True)]


# --- the cost of sibling spreads ------------------------------------------------------------------


def chain(width: int, depth: int) -> str:
    lines = ["const stripe = require('stripe')('sk');", "export function go() {", "  const a0 = { description: 'x' }"]
    for d in range(1, depth + 1):
        lines.append(f"  const a{d} = {{ " + ", ".join([f"...a{d - 1}"] * width) + " }")
    lines += [f"  return stripe.paymentIntents.create({{ amount: 1, ...a{depth} }})", "}"]
    return "\n".join(lines) + "\n"


def test_sibling_spreads_are_read_once_per_binding_not_once_per_path(tmp_path):
    start = time.monotonic()
    fields, hidden, _ = site(tmp_path, "perf_k25.ts")  # 25 wide, 5 deep: the review's 255 s case
    assert time.monotonic() - start < 5
    assert "" in hidden  # deeper than MAX_DEPTH: hidden
    start = time.monotonic()
    (tmp_path / "w").mkdir()
    found = [u for u in scan(tmp_path / "w", {"w.ts": chain(40, 3)}) if u.path == "/v1/payment_intents"]
    assert time.monotonic() - start < 5
    assert [(set(u.fields), set(u.hidden)) for u in found] == [({"amount", "description"}, set())]


def test_past_the_per_call_budget_a_spread_is_hidden(tmp_path):
    from apidrift import jsspread

    many = "const stripe = require('stripe')('sk');\nexport function go() {\n" + "".join(
        f"  const b{k} = {{ k{k}: 1 }}\n" for k in range(jsspread.MAX_WORK + 5)
    ) + "  return stripe.paymentIntents.create({ " + ", ".join(f"...b{k}" for k in range(jsspread.MAX_WORK + 5)) + " })\n}\n"
    found = [u for u in scan(tmp_path, {"many.ts": many}) if u.path == "/v1/payment_intents"]
    assert len(found) == 1 and "" in found[0].hidden and "k0" in found[0].fields


# --- what still counts --------------------------------------------------------------------------

MEDUSA_SHAPE = """
import Stripe from 'stripe'
const SINCE = '2026-07-29'
class Provider {
  constructor(options) {
    this.options_ = options
    this.stripe_ = new Stripe(options.apiKey, { apiVersion: (options.apiVersion ?? '2024-04-10') as Stripe.LatestApiVersion })
  }
  params(types) {
    const apiVersion = this.options_.apiVersion ?? '2024-04-10'
    return apiVersion >= SINCE ? { allowed_payment_method_types: types } : { payment_method_types: types }
  }
  async pay(types, who) {
    const request = { amount: 1, ...this.params(types) }
    request.customer = who
    return await this.retry(() => this.stripe_.paymentIntents.create(request))
  }
}
"""


def test_the_client_version_through_a_constructor_alias_is_a_switch_and_a_retry_callback_reads_settled_keys(tmp_path):
    found = [u for u in scan(tmp_path, {"p.ts": MEDUSA_SHAPE}) if u.path == "/v1/payment_intents"]
    assert len(found) == 1
    u = found[0]
    assert set(u.hidden) == {"customer."} and "customer" in u.fields
    assert {s[:2] for s in u.switched} == {SWITCH}


@pytest.mark.parametrize("change", [
    ("this.options_ = options\n", "this.options_ = options\n    this.options_ = other\n"),  # the alias is not unique
    ("apiVersion: (options.apiVersion ?? '2024-04-10')", "apiVersion: '2026-09-30.endive'"),  # another version
    ("const apiVersion = this.options_.apiVersion ?? '2024-04-10'", "const apiVersion = this.options_.version"),
])
def test_a_compared_version_that_is_not_the_clients_is_no_switch(tmp_path, change):
    found = [u for u in scan(tmp_path, {"p.ts": MEDUSA_SHAPE.replace(*change)}) if u.path == "/v1/payment_intents"]
    assert len(found) == 1 and SWITCH not in {s[:2] for s in found[0].switched}


def test_a_write_after_the_retry_callback_is_written_hides_every_key(tmp_path):
    text = MEDUSA_SHAPE.replace(
        "    return await this.retry(() => this.stripe_.paymentIntents.create(request))\n",
        "    const p = this.retry(() => this.stripe_.paymentIntents.create(request))\n    request.metadata = m\n    return p\n")
    assert text != MEDUSA_SHAPE
    found = [u for u in scan(tmp_path, {"p.ts": text}) if u.path == "/v1/payment_intents"]
    assert "" in found[0].hidden
