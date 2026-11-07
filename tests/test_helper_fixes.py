"""Fixes behind a same-file helper: the literal a builder returns, or the one a caller hands a wrapper.

The edit is made only where the helper's literal provably reaches nothing but the planned calls; every
other case is refused with a reason that names the condition.
"""

import pytest
from test_fixer import NEW, OLD, changed_lines, plan_for
from test_helpers import JS, PHP_BUILDER, PHP_WRAPPER, PY, RUBY, STRIPE_JS

from apidrift.app.prbody import UNFIXABLE_WHY
from apidrift.fixer import WHY
from apidrift.fixhelpers import REASONS


def outcome(tmp_path, files):
    plan = plan_for(tmp_path, files)
    if plan.fixes:
        assert plan.unfixable == [], plan.to_dict()
        return "fixed", plan
    if plan.unfixable:
        return "refused", sorted({u.why for u in plan.unfixable})
    return "not attempted", [(u.file, u.line) for u in plan.not_attempted]


def only_change(plan, name):
    fix = next(f for f in plan.fixes if f.file == name)
    return changed_lines(fix)


# --- Ruby -------------------------------------------------------------------------------

def test_a_ruby_builder_is_fixed_in_its_literal_and_nowhere_else(tmp_path):
    kind, plan = outcome(tmp_path, {"app/pay.rb": RUBY})
    assert kind == "fixed"
    assert only_change(plan, "app/pay.rb") == {11: (f"      {OLD}: methods,", f"      {NEW}: methods,")}
    assert [(s.line, s.target) for s in plan.fixes[0].sites] == [(3, OLD)]


def test_a_key_the_builder_writes_by_statement_is_renamed_there(tmp_path):
    src = RUBY.replace(f"      {OLD}: methods,\n", "").replace("    payload\n  end", f"    payload[:{OLD}] = methods\n    payload\n  end")
    kind, plan = outcome(tmp_path, {"app/pay.rb": src})
    assert kind == "fixed"
    assert list(only_change(plan, "app/pay.rb").values()) == [(f"    payload[:{OLD}] = methods", f"    payload[:{NEW}] = methods")]


def test_a_builder_tested_by_a_spec_is_still_fixed(tmp_path):
    spec = "describe Pay do\n  it { expect(subject.send(:intent_payload)).to include(:payment_method_types) }\nend\n"
    kind, plan = outcome(tmp_path, {"app/pay.rb": RUBY, "spec/pay_spec.rb": spec})
    assert kind == "fixed" and [f.file for f in plan.fixes] == ["app/pay.rb"]


@pytest.mark.parametrize("label,files,why", [
    ("used by something that is not a Stripe call", {"app/pay.rb": RUBY.replace(
        "  private\n", "  def log\n    Rails.logger.info(intent_payload)\n  end\n\n  private\n")}, "helper_shared"),
    ("called dynamically", {"app/pay.rb": RUBY.replace("  private\n", "  def dump\n    send(:intent_payload)\n  end\n\n  private\n")},
     "helper_shared"),
    ("also fed to an endpoint outside the table", {"app/pay.rb": RUBY.replace(
        "  private\n", "  def card\n    ::Stripe::Charge.create(intent_payload)\n  end\n\n  private\n")}, "helper_feeds_other_calls"),
    ("named by another file", {"app/pay.rb": RUBY, "app/other.rb": "class Other\n  def x\n    Pay.new.intent_payload\n  end\nend\n"},
     "helper_used_elsewhere"),
    ("merged with something unknown", {"app/pay.rb": RUBY.replace("payload.merge!(balance_fields)", "payload.merge!(options)")},
     "incomplete_binding"),
    ("the key is merged in from another helper", {"app/pay.rb": RUBY.replace(
        "{customer_balance: {funding_type: 'bank_transfer'}}", f"{{{OLD}: ['customer_balance']}}")}, "incomplete_binding"),
])
def test_a_ruby_builder_fix_is_refused_with_the_condition(tmp_path, label, files, why):
    kind, got = outcome(tmp_path, files)
    assert (kind, got) == ("refused", [why]), label


def test_another_file_with_its_own_method_of_the_same_name_does_not_block(tmp_path):
    other = "class Other\n  def go\n    intent_payload\n  end\n\n  def intent_payload\n    {}\n  end\nend\n"
    assert outcome(tmp_path, {"app/pay.rb": RUBY, "app/other.rb": other})[0] == "fixed"


def test_one_builder_feeding_two_planned_calls_is_one_edit(tmp_path):
    src = RUBY.replace("  private\n", "  def again\n    ::Stripe::PaymentIntent.create(intent_payload)\n  end\n\n  private\n")
    kind, plan = outcome(tmp_path, {"app/pay.rb": src})
    assert kind == "fixed" and len(only_change(plan, "app/pay.rb")) == 1
    assert sorted(s.line for s in plan.fixes[0].sites) == [3, 7]


# --- JavaScript / TypeScript ----------------------------------------------------------------

def test_a_js_builder_is_fixed(tmp_path):
    kind, plan = outcome(tmp_path, {"pay.js": JS})
    assert kind == "fixed"
    assert only_change(plan, "pay.js") == {3: (
        f"  return {{ amount: order.total, {OLD}: ['card'] }};", f"  return {{ amount: order.total, {NEW}: ['card'] }};"
    )}


def test_every_literal_return_with_the_key_is_renamed(tmp_path):
    src = JS.replace("  return { amount", f"  if (order.x) return {{ {OLD}: ['sepa_debit'] }};\n  return {{ amount")
    kind, plan = outcome(tmp_path, {"pay.js": src})
    assert kind == "fixed" and len(only_change(plan, "pay.js")) == 2


@pytest.mark.parametrize("label,files,why", [
    ("exported and imported elsewhere", {"pay.js": JS.replace("function intentParams", "export function intentParams"),
                                         "other.js": "import { intentParams } from './pay';\n"}, "helper_used_elsewhere"),
    ("exported by name in the file", {"pay.js": JS + "module.exports = { intentParams };\n"}, "helper_shared"),
    ("also logged", {"pay.js": JS + "console.log(intentParams({}));\n"}, "helper_shared"),
    ("named in a string for a dynamic call", {"pay.js": JS + "obj['intentParams'];\n"}, "helper_shared"),
])
def test_a_js_builder_fix_is_refused_with_the_condition(tmp_path, label, files, why):
    assert outcome(tmp_path, files) == ("refused", [why]), label


def test_a_returned_local_that_is_passed_on_is_not_edited(tmp_path):
    src = STRIPE_JS + (
        "function build(o) {\n"
        f"  const p = {{ amount: 1, {OLD}: ['card'] }};\n"
        "  audit(p);\n"
        "  return p;\n"
        "}\n"
        "stripe.paymentIntents.create(build(o));\n"
    )
    assert outcome(tmp_path, {"pay.js": src}) == ("refused", ["helper_shared"])


# --- Python -----------------------------------------------------------------------------

PY_FIX = PY.replace("'metadata': {'order': basket.number}", f"'{OLD}': ['card']")


def test_a_python_builder_is_fixed(tmp_path):
    kind, plan = outcome(tmp_path, {"pay.py": PY_FIX})
    assert kind == "fixed"
    assert only_change(plan, "pay.py") == {5: (
        f"        return {{'amount': basket.total, '{OLD}': ['card']}}", f"        return {{'amount': basket.total, '{NEW}': ['card']}}"
    )}


@pytest.mark.parametrize("label,extra,why", [
    ("used for something else", "\n    def show(self, b):\n        return self._params(b)\n", "helper_shared"),
    ("looked up by name", "\n    def show(self, b):\n        return getattr(self, '_params')(b)\n", "helper_shared"),
    ("also fed to another endpoint", "\n    def card(self, b):\n        return stripe.Charge.create(**self._params(b))\n",
     "helper_feeds_other_calls"),
])
def test_a_python_builder_fix_is_refused_with_the_condition(tmp_path, label, extra, why):
    assert outcome(tmp_path, {"pay.py": PY_FIX + extra}) == ("refused", [why]), label


def test_a_subclass_in_another_file_that_calls_the_builder_blocks_the_fix(tmp_path):
    sub = "from .pay import Processor\n\nclass Mine(Processor):\n    def go(self, b):\n        return self._params(b)\n"
    assert outcome(tmp_path, {"pay.py": PY_FIX, "mine.py": sub}) == ("refused", ["helper_used_elsewhere"])


# --- PHP --------------------------------------------------------------------------------

def test_a_php_builder_is_fixed_in_the_statement_that_writes_the_key(tmp_path):
    kind, plan = outcome(tmp_path, {"Driver.php": PHP_BUILDER})
    assert kind == "fixed"
    assert list(only_change(plan, "Driver.php").values()) == [
        (f"        $p['{OLD}'] = ['card'];", f"        $p['{NEW}'] = ['card'];")
    ]


def test_a_php_wrapper_caller_is_fixed_and_the_wrapper_with_no_other_use_is_not_a_site(tmp_path):
    """Every use of the wrapper in the repository is that caller (crosstable): its own site is dropped."""
    plan = plan_for(tmp_path, {"D.php": PHP_WRAPPER})
    assert [u.why for u in plan.unfixable] == [] and plan.not_attempted == []
    assert list(only_change(plan, "D.php").values()) == [
        (f"            '{OLD}' => ['card'],", f"            '{NEW}' => ['card'],")
    ]


def test_a_php_wrapper_caller_is_fixed_and_the_wrapper_with_another_use_stays_not_attempted(tmp_path):
    other = "<?php\nfunction later($d, $p) { return $d->createIntent($p); }\n"
    plan = plan_for(tmp_path, {"D.php": PHP_WRAPPER, "Later.php": other})
    assert [u.why for u in plan.unfixable] == [] and [(u.line, u.why) for u in plan.not_attempted] == [(11, "arguments_not_visible")]
    assert list(only_change(plan, "D.php").values()) == [
        (f"            '{OLD}' => ['card'],", f"            '{NEW}' => ['card'],")
    ]


def test_a_php_wrapper_another_file_may_override_is_not_fixed(tmp_path):
    other = "<?php\nclass Child extends Driver {\n    public function createIntent(array $params) { return null; }\n}\n"
    plan = plan_for(tmp_path, {"D.php": PHP_WRAPPER, "Child.php": other})
    assert plan.fixes == [] and [u.why for u in plan.unfixable] == ["helper_used_elsewhere"]


def test_a_php_wrapper_with_no_caller_in_the_repository_is_never_fixed(tmp_path):
    lib = PHP_WRAPPER.replace("return $this->createIntent([", "return $other->createIntent([")
    plan = plan_for(tmp_path, {"D.php": lib})
    assert plan.fixes == [] and plan.unfixable == [] and [u.line for u in plan.not_attempted] == [11]


@pytest.mark.parametrize("marker,text", [
    ("composer.json", '{"name": "acme/billing", "require": {}}'),  # Composer's default type is library
    ("composer.json", '{"name": "acme/billing", "type": "library"}'),
    ("billing.gemspec", "Gem::Specification.new { |s| s.name = 'billing' }\n"),
])
def test_a_package_others_install_never_gets_a_helper_fix(tmp_path, marker, text):
    """Its public methods have callers and overrides outside the repository (cashier is the real case)."""
    src = PHP_WRAPPER if marker == "composer.json" else RUBY
    name = "D.php" if marker == "composer.json" else "app/pay.rb"
    plan = plan_for(tmp_path, {name: src, marker: text})
    assert plan.fixes == [] and [u.why for u in plan.unfixable] == ["helper_used_elsewhere"]


def test_an_application_declared_as_a_composer_project_is_fixed(tmp_path):
    plan = plan_for(tmp_path, {"D.php": PHP_WRAPPER, "composer.json": '{"type": "project", "repositories": [{"type": "library"}]}'})
    assert len(plan.fixes) == 1 and plan.unfixable == []


def test_every_new_reason_is_listed_and_has_a_sentence():
    assert set(REASONS) <= set(WHY) and set(REASONS) <= set(UNFIXABLE_WHY)
