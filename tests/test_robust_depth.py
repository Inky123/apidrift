"""Literals nested hundreds of levels deep must never abort a scan or a fix: a call too deep to read counts as
hidden, a file too deep to walk is counted in ``unread``, and the calls around them are still read."""

import pytest
import test_crossfile_js as crossjs
from test_fixer import NEW, OLD, changed_lines, docs, plan_for

from apidrift.openapi import load_spec
from apidrift.scanner import scan_repo
from apidrift.sdkmap import SdkIndex

JS_HEAD = "const stripe = require('stripe')('sk');\n"
TS_HEAD = "import Stripe from 'stripe';\nconst stripe = new Stripe('sk');\n"
RB_HEAD = "require 'stripe'\n"
PHP_HEAD = "<?php\n$stripe = new \\Stripe\\StripeClient('sk');\n"


def nest(opener: str, closer: str, depth: int) -> str:
    return opener * depth + "1" + closer * depth


# (file name, plain call, deep call shape with {deep} for the literal, (opener, closer), unread label)
SHAPES = {
    "obj.js": (JS_HEAD + f"stripe.paymentIntents.create({{amount: 1, {OLD}: ['card']}});\n",
               JS_HEAD + f"stripe.paymentIntents.create({{{OLD}: ['card'], metadata: {{deep}}}});\n", ("{a:", "}"), "JavaScript"),
    "arr.js": (JS_HEAD + f"stripe.paymentIntents.create({{amount: 1, {OLD}: ['card']}});\n",
               JS_HEAD + f"stripe.paymentIntents.create({{{OLD}: ['card'], metadata: {{deep}}}});\n", ("[", "]"), "JavaScript"),
    "paren.js": (JS_HEAD + f"stripe.paymentIntents.create({{amount: 1, {OLD}: ['card']}});\n",
                 JS_HEAD + f"stripe.paymentIntents.create({{{OLD}: ['card'], amount: {{deep}}}});\n", ("(", ")"), "JavaScript"),
    "obj.ts": (TS_HEAD + f"stripe.paymentIntents.create({{amount: 1, {OLD}: ['card']}});\n",
               TS_HEAD + f"stripe.paymentIntents.create({{{OLD}: ['card'], metadata: {{deep}}}});\n", ("{a:", "}"), "TypeScript"),
    "hash.rb": (RB_HEAD + f"Stripe::PaymentIntent.create(amount: 1, {OLD}: ['card'])\n",
                RB_HEAD + f"Stripe::PaymentIntent.create({OLD}: ['card'], metadata: {{deep}})\n", ("{a: ", "}"), "Ruby"),
    "arr.rb": (RB_HEAD + f"Stripe::PaymentIntent.create(amount: 1, {OLD}: ['card'])\n",
               RB_HEAD + f"Stripe::PaymentIntent.create({OLD}: ['card'], metadata: {{deep}})\n", ("[", "]"), "Ruby"),
    "arr.php": (PHP_HEAD + f"$stripe->paymentIntents->create(['amount' => 1, '{OLD}' => ['card']]);\n",
                PHP_HEAD + f"$stripe->paymentIntents->create(['{OLD}' => ['card'], 'metadata' => {{deep}}]);\n", ("array(", ")"), "PHP"),
    "keyed.php": (PHP_HEAD + f"$stripe->paymentIntents->create(['amount' => 1, '{OLD}' => ['card']]);\n",
                  PHP_HEAD + f"$stripe->paymentIntents->create(['{OLD}' => ['card'], 'metadata' => {{deep}}]);\n", ("['k' => ", "]"), "PHP"),
    "paren.php": (PHP_HEAD + f"$stripe->paymentIntents->create(['amount' => 1, '{OLD}' => ['card']]);\n",
                  PHP_HEAD + f"$stripe->paymentIntents->create(['{OLD}' => ['card'], 'amount' => {{deep}}]);\n", ("(", ")"), "PHP"),
}


def files_for(name: str, depth: int) -> dict[str, str]:
    plain, deep, (opener, closer), _ = SHAPES[name]
    stem, ext = name.split(".")
    return {f"src/plain.{ext}": plain, f"src/deep_{stem}.{ext}": deep.replace("{deep}", nest(opener, closer, depth))}


def index():
    return SdkIndex.build(*(load_spec(d) for d in docs()))


@pytest.mark.parametrize("depth", [600, 1200])
@pytest.mark.parametrize("name", sorted(SHAPES))
def test_a_literal_nested_too_deeply_hides_the_call_or_leaves_the_file_unread_and_the_scan_goes_on(tmp_path, name, depth):
    files = files_for(name, depth)
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    result = scan_repo(tmp_path, index())
    deep_file = next(rel for rel in files if "deep" in rel)
    plain = [u for u in result.usages if u.file != deep_file]
    assert [(u.path, u.complete, u.fields) for u in plain] == [("/v1/payment_intents", True, frozenset({"amount", OLD}))]
    deep = [u for u in result.usages if u.file == deep_file]
    label = f"{SHAPES[name][3]} (nested too deeply)"
    if deep and deep[0].complete:  # shallow enough for this reader: read in full
        assert [(u.path, u.fields >= {OLD}) for u in deep] == [("/v1/payment_intents", True)] and result.unread == {}
    elif deep:  # the call was found, with nothing of its arguments trusted
        assert [(u.path, u.complete, u.hidden) for u in deep] == [("/v1/payment_intents", False, frozenset({""}))]
        assert result.unread == {}
    else:  # the file could not be walked at all: it is counted, never silently dropped
        assert result.unread == {label: 1}, result.unread


@pytest.mark.parametrize("depth", [600, 1200])
@pytest.mark.parametrize("name", ["obj.js", "arr.js", "obj.ts", "hash.rb", "arr.rb", "arr.php", "keyed.php"])
def test_the_fix_next_to_a_too_deep_call_is_still_planned_and_the_deep_call_is_left_alone(tmp_path, name, depth):
    plan = plan_for(tmp_path, files_for(name, depth))
    plain = f"src/plain.{name.split('.')[1]}"
    assert plain in [f.file for f in plan.fixes] and plan.unfixable == []
    for fix in plan.fixes:  # the plain call always; the deep one only when its literal was read in full
        lines = changed_lines(fix)
        assert lines and all(OLD in before and NEW in after for before, after in lines.values())
    assert all("deep" in u.file for u in plan.not_attempted)  # a hidden deep call is a possible hit, never edited


def test_a_caller_file_nested_too_deeply_does_not_abort_the_cross_file_pass(tmp_path):
    deep = ("import { PaymentService } from '../stripe/payment.service';\n"
            + "function f(svc: PaymentService) {" * 1200 + "return svc.createIntent({amount: 1});" + "}" * 1200 + "\n")
    plan = plan_for(tmp_path, {**crossjs.FILES, "src/deep/deep.ts": deep})
    assert [f.file for f in plan.fixes] == ["src/checkout/checkout.ts"] and plan.unfixable == []
