"""``$x = apply_filters('hook', $x)``: pass-through unless the checkout itself registers a callback on that hook."""

import pytest
from test_php_objects import _doc

from apidrift import phpfilters
from apidrift.openapi import load_spec
from apidrift.scanner import scan_repo
from apidrift.sdkmap import SdkIndex

H = "<?php\n"
CALL = H + (
    "function pay($o) {\n"
    "  $p = ['amount' => 1, 'currency' => 'usd'];\n"
    "  $p = apply_filters(@HOOK@, $p, $o);\n"
    "  return \\Stripe\\Charge::create($p);\n"
    "}\n"
)


def test_registered_hooks_are_the_literal_names_add_filter_names():
    texts = {"a.php": "add_filter( 'one', 'f' ); add_filter(\"two\", [$this, 'g'], 10, 2);", "b.php": "apply_filters('three', $x);"}
    assert phpfilters.registered(texts) == {"one", "two"}
    assert phpfilters.registered({"c.php": "add_filter($name, 'f');"}) == {phpfilters.ANY}
    assert phpfilters.registered({"d.php": "add_filter('links_' . $x, 'f'); add_filter(\"{$p}_x\", 'g');"}) == {
        "links_*", phpfilters.ANY,
    }


@pytest.mark.parametrize("hook,hooks,ok", [
    ("h", set(), True), ("h", {"other"}, True), ("h", {"h"}, False), ("h", {phpfilters.ANY}, False),
    (None, set(), False), ("h", None, False),
    ("links_pay", {"links_*"}, False), ("pay_args", {"links_*"}, True),  # a concatenated name: that prefix only
])
def test_pass_through(hook, hooks, ok):
    assert phpfilters.pass_through(hook, hooks) is ok


def scan(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    result = scan_repo(tmp_path, SdkIndex.build(load_spec(_doc())))
    return [(sorted(u.fields), u.complete) for u in result.usages if u.language == "php"]


@pytest.mark.parametrize("hook,elsewhere,complete", [
    ("'pay_args'", "", True),  # nobody here hooks it: unchanged
    ("'pay_args'", "add_filter('other_hook', 'f');", True),
    ("'pay_args'", "add_filter( 'pay_args', 'my_pay_args' );", False),  # the checkout changes it
    ("'pay_args'", "add_filter($hook, 'f');", False),  # some hook, maybe this one
    ("$hook", "", False),  # not a literal: hides everything
])
def test_a_filter_is_pass_through_unless_this_checkout_registers_on_it(tmp_path, hook, elsewhere, complete):
    found = scan(tmp_path, {"app/Pay.php": CALL.replace("@HOOK@", hook), "app/hooks.php": H + elsewhere + "\n"})
    fields = ["amount", "currency"] if hook.startswith("'") else []
    assert found == [(fields, complete)]
