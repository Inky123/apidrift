"""A name the call sees through a loop, comprehension, ``with``, unpacking or inner scope is never read as an
outer literal of the same name, by the reader or the fixer (review k1 and the fixer bug it found on main)."""

import pytest

from apidrift.openapi import load_spec
from apidrift.scanner import scan_repo
from apidrift.sdkmap import SdkIndex
from specs import sdk_doc
from test_fixer import plan_for

SHADOWS = [
    # comprehension target
    "def go(datas):\n    p = {'amount': 1, 'payment_method_types': ['card']}\n"
    "    return [stripe.PaymentIntent.create(**p) for p in datas]\n",
    "def go(datas):\n    p = {'amount': 1, 'payment_method_types': ['card']}\n"
    "    return {k: stripe.PaymentIntent.create(**p) for k, p in datas}\n",
    # for loop target, also at module level
    "def go(datas):\n    p = {'amount': 1, 'payment_method_types': ['card']}\n"
    "    for p in datas:\n        stripe.PaymentIntent.create(**p)\n",
    "p = {'amount': 1, 'payment_method_types': ['card']}\n"
    "def go(datas):\n    for p in datas:\n        stripe.PaymentIntent.create(**p)\n",
    # with / tuple unpacking / walrus / except
    "def go(cm):\n    p = {'amount': 1, 'payment_method_types': ['card']}\n"
    "    with cm as p:\n        stripe.PaymentIntent.create(**p)\n",
    "def go(pair):\n    p = {'amount': 1, 'payment_method_types': ['card']}\n"
    "    p, q = pair\n    stripe.PaymentIntent.create(**p)\n",
    "def go(f):\n    p = {'amount': 1, 'payment_method_types': ['card']}\n"
    "    if (p := f()):\n        stripe.PaymentIntent.create(**p)\n",
    # inner scopes: lambda and nested def parameters
    "p = {'amount': 1, 'payment_method_types': ['card']}\n"
    "go = lambda p: stripe.PaymentIntent.create(**p)\n",
    "p = {'amount': 1, 'payment_method_types': ['card']}\n"
    "def outer():\n    def go(p):\n        return stripe.PaymentIntent.create(**p)\n    return go\n",
    # the StripeClient form
    "def go(client, datas):\n    p = {'amount': 1, 'payment_method_types': ['card']}\n"
    "    return [client.v1.payment_intents.create(params=p) for p in datas]\n",
]


@pytest.mark.parametrize("body", SHADOWS)
def test_the_fixer_never_edits_an_outer_literal_the_call_does_not_send(tmp_path, body):
    plan = plan_for(tmp_path, {"pay.py": "import stripe\n" + body})
    assert plan.fixes == [], [f.content for f in plan.fixes]


@pytest.mark.parametrize("body", SHADOWS)
def test_the_reader_does_not_read_the_outer_literal(tmp_path, body):
    (tmp_path / "pay.py").write_text("import stripe\n" + body)
    usages = scan_repo(tmp_path, SdkIndex.build(load_spec(sdk_doc()))).usages
    for u in usages:
        assert not u.complete and "amount" not in u.fields, (u.line, u.fields)


def test_comprehension_items_do_not_read_an_outer_binding_of_the_loop_variable(tmp_path):
    (tmp_path / "a.py").write_text(
        "import stripe\n"
        "def checkout(cart):\n"
        "    price_data = {'currency': 'usd', 'unit_amount': 100}\n"
        "    datas = [build(i) for i in cart]\n"
        "    return stripe.checkout.Session.create(\n"
        "        success_url='u',\n"
        "        line_items=[{'price_data': price_data, 'quantity': 1} for price_data in datas],\n"
        "    )\n"
        "def checkout2(cart):\n"
        "    return stripe.checkout.Session.create(success_url='u', line_items=[{'price': p, **extra} for p, extra in cart])\n"
    )
    by_line = {u.line: u for u in scan_repo(tmp_path, SdkIndex.build(load_spec(sdk_doc()))).usages}
    assert by_line[5].fields == {"success_url", "line_items", "line_items[].price_data", "line_items[].quantity"}
    assert not any(f.startswith("line_items[].price_data.") for f in by_line[5].fields)
    assert "line_items[]." in by_line[10].hidden


def test_a_later_unrelated_loop_does_not_stop_a_plain_binding_elsewhere(tmp_path):
    plan = plan_for(tmp_path, {"pay.py": (
        "import stripe\n"
        "def go():\n"
        "    p = {'amount': 1, 'payment_method_types': ['card']}\n"
        "    return stripe.PaymentIntent.create(**p)\n"
        "def other(xs):\n"
        "    for p in xs:\n"
        "        print(p)\n"
    )})
    assert len(plan.fixes) == 1  # the loop variable lives in another function
