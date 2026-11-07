"""The Python reader: async twins, pagers, aliases, class attributes, every reaching dict, Python 3.14 syntax."""

from pathlib import Path

import pytest

from apidrift import pysyntax
from apidrift.openapi import load_spec
from apidrift.scanner import scan_repo
from apidrift.sdkmap import SdkIndex
from specs import _op, sdk_doc
from test_fixer import NEW, OLD, changed_lines, only_fix, plan_for


@pytest.fixture(scope="module")
def index():
    doc = sdk_doc()
    doc["paths"]["/v1/charges/search"] = {"get": {
        "parameters": [{"name": "query", "in": "query", "required": True, "schema": {"type": "string"}}],
        "responses": {"200": {}},
    }}
    doc["components"]["schemas"]["charge"]["x-stripeOperations"].append(_op("search", "custom", "get", "/v1/charges/search"))
    return SdkIndex.build(load_spec(doc))


def scan(tmp_path: Path, index, text: str, name: str = "pay.py"):
    (tmp_path / name).write_text(text)
    result = scan_repo(tmp_path, index)
    return {u.line: (u.label, u.call, set(u.fields), u.complete) for u in result.usages}, result


# --- stripe-python's async twins and class-level pagers -------------------------------------------


def test_async_twins_map_as_the_sync_method(tmp_path, index):
    got, _ = scan(tmp_path, index, (
        "import stripe\n"
        "async def go(client):\n"
        "    await stripe.Charge.create_async(amount=1, currency='usd')\n"
        "    await stripe.Charge.capture_async('ch_1', amount=1)\n"
        "    await stripe.Charge.list_async(limit=3)\n"
        "    await stripe.checkout.Session.create_async(mode='payment', success_url='u')\n"
        "    await stripe.StripeClient('sk').v1.charges.retrieve_async('ch_1')\n"
        "    await client.v1.charges.create_async(params={'amount': 1, 'currency': 'usd'})\n"
        "    await stripe.Charge.search_async(query='x')\n"
    ))
    assert got[3] == ("POST /v1/charges", "stripe.Charge.create_async", {"amount", "currency"}, True)
    assert got[4][0] == "POST /v1/charges/{charge}/capture" and got[4][2] == {"amount"}
    assert got[5][0] == "GET /v1/charges" and got[6][0] == "POST /v1/checkout/sessions"
    assert got[7][0] == "GET /v1/charges/{charge}"
    assert got[8] == ("POST /v1/charges", "client.v1.charges.create_async", {"amount", "currency"}, True)
    assert got[9][0] == "GET /v1/charges/search"


def test_class_level_pagers_send_list_and_search(tmp_path, index):
    got, _ = scan(tmp_path, index, (
        "import stripe\n"
        "for c in stripe.Charge.auto_paging_iter(limit=3): pass\n"
        "for c in stripe.Charge.search_auto_paging_iter(query='x'): pass\n"
        "async def go():\n"
        "    await stripe.Charge.search_auto_paging_iter_async(query='x')\n"
        "stripe.Charge.list(limit=3).auto_paging_iter()\n"
    ))
    assert got[2][:3] == ("GET /v1/charges", "stripe.Charge.auto_paging_iter", {"limit"})
    assert got[3][0] == got[5][0] == "GET /v1/charges/search"
    assert got[6][0] == "GET /v1/charges"  # the list call; its pager is not another request


def test_not_async_twins(tmp_path, index):
    got, result = scan(tmp_path, index, (
        "import stripe\n"
        "async def go(client):\n"
        "    await stripe.Charge._cls_capture_async('ch_1')\n"
        "    await client.raw_request_async('post', '/v1/charges')\n"
        "    await stripe.Charge.frobnicate_async()\n"
    ))
    assert got == {}
    assert {c for _, _, c in result.unresolved} >= {"stripe.Charge._cls_capture_async", "stripe.Charge.frobnicate_async"}


# --- aliases and class attributes --------------------------------------------------------------


def test_local_and_module_aliases(tmp_path, index):
    got, _ = scan(tmp_path, index, (
        "import stripe\n"
        "Charges = stripe.Charge\n"
        "class Adapter:\n"
        "    async def probe(self):\n"
        "        v1 = self._client.v1\n"
        "        checks = [lambda: v1.charges.list_async(params={'limit': 1})]\n"
        "        return Charges.create(amount=1, currency='usd')\n"
        "def other():\n"
        "    sessions = stripe.checkout.Session\n"
        "    return sessions.create(mode='payment', success_url='u')\n"
    ))
    assert got[6] == ("GET /v1/charges", "v1.charges.list_async", {"limit"}, True)
    assert got[7][:2] == ("POST /v1/charges", "Charges.create")
    assert got[10][0] == "POST /v1/checkout/sessions"


def test_named_class_attribute_and_its_single_in_file_base(tmp_path, index):
    got, _ = scan(tmp_path, index, (
        "import stripe\n"
        "class Base:\n"
        "    stripe_class = stripe.Charge\n"
        "class Charge(Base):\n"
        "    pass\n"
        "def go():\n"
        "    Charge.stripe_class.create(amount=1, currency='usd')\n"
        "    Base.stripe_class.retrieve('ch_1')\n"
    ))
    assert got[7][:2] == ("POST /v1/charges", "Charge.stripe_class.create")
    assert got[8][0] == "GET /v1/charges/{charge}"


@pytest.mark.parametrize("text", [
    # bound twice / in a loop / declared global / deleted: which value is unknown
    "import stripe\nA = stripe.Charge\nA = stripe.checkout.Session\nA.create(amount=1)\n",
    "import stripe\nA = stripe.Charge\nfor A in []: pass\nA.create(amount=1)\n",
    "import stripe\nA = stripe.Charge\ndef f():\n    global A\n    A = 1\nA.create(amount=1)\n",
    "import stripe\nA = stripe.Charge\ndel A\nA.create(amount=1)\n",
    "import stripe\nA, B = stripe.Charge, 1\nA.create(amount=1)\n",
    # a parameter or local of the same name shadows the module alias
    "import stripe\nA = stripe.Charge\ndef f(A):\n    return A.create(amount=1)\n",
    "import stripe\nA = stripe.Charge\ndef f():\n    A = make()\n    return A.create(amount=1)\n",
    # not the SDK
    "import stripe\nA = other.Charge\nA.create(amount=1)\n",
    "import stripe\nA = stripe.Charge.retrieve('ch_1')\nA.create(amount=1)\n",
    # a class body is not visible from its methods
    "import stripe\nclass M:\n    A = stripe.Charge\n    def f(self):\n        return A.create(amount=1)\n",
])
def test_aliases_that_are_not_proven_are_not_followed(tmp_path, index, text):
    got, _ = scan(tmp_path, index, text)
    assert all(call.startswith("stripe.") for _, call, _, _ in got.values())  # only a direct call, never the alias


@pytest.mark.parametrize("text", [
    # written elsewhere, decorated, metaclass, two bases, bound twice, setattr
    "import stripe\nclass M:\n    stripe_class = stripe.Charge\nM.stripe_class = stripe.checkout.Session\nM.stripe_class.create(amount=1)\n",
    "import stripe\n@deco\nclass M:\n    stripe_class = stripe.Charge\nM.stripe_class.create(amount=1)\n",
    "import stripe\nclass M(metaclass=Meta):\n    stripe_class = stripe.Charge\nM.stripe_class.create(amount=1)\n",
    "import stripe\nclass B:\n    stripe_class = stripe.Charge\nclass M(B, Other):\n    pass\nM.stripe_class.create(amount=1)\n",
    "import stripe\nclass M:\n    stripe_class = stripe.Charge\n    if x:\n        stripe_class = stripe.Refund\nM.stripe_class.create(amount=1)\n",
    "import stripe\nclass M:\n    stripe_class = stripe.Charge\nsetattr(M, name, 1)\nM.stripe_class.create(amount=1)\n",
    "import stripe\nfrom models import M\nM.stripe_class.create(amount=1)\n",
    "import stripe\nclass M:\n    stripe_class = None\nM.stripe_class.create(amount=1)\n",
])
def test_class_attributes_that_are_not_proven_are_not_followed(tmp_path, index, text):
    got, _ = scan(tmp_path, index, text)
    assert got == {}


# --- every dict literal that can reach the call --------------------------------------------------


def test_every_reaching_dict_literal_is_read(tmp_path, index):
    got, _ = scan(tmp_path, index, (
        "import stripe\n"
        "def a(flag):\n"
        "    if flag:\n"
        "        p = {'description': 'x'}\n"
        "    else:\n"
        "        p = {'amount': 1}\n"
        "    stripe.Charge.create(**p)\n"
        "def b(flag):\n"
        "    if flag:\n"
        "        p = {'description': 'x'}\n"
        "    else:\n"
        "        p = build()\n"
        "    stripe.Charge.create(**p)\n"
        "def c(items):\n"
        "    p = {'amount': 1}\n"
        "    for i in items:\n"
        "        stripe.Charge.create(**p)\n"
        "        p = dict(description='x')\n"
        "def d(flag):\n"
        "    p = {'amount': 1}\n"
        "    if flag:\n"
        "        p = {'currency': 'usd'}\n"
        "    stripe.Charge.create(**p)\n"
        "    p = {'description': 'later'}\n"
    ))
    assert got[7][2:] == ({"amount", "description"}, True)
    assert got[13][2:] == ({"description"}, False)  # one branch is unreadable: hidden, not narrowed
    assert got[17][2:] == ({"amount", "description"}, True)  # the loop brings the later binding round
    assert got[23][2:] == ({"amount", "currency"}, True)  # a binding after the call does not reach it


def test_a_parameter_hides_a_module_literal_of_the_same_name(tmp_path, index):
    got, _ = scan(tmp_path, index, "import stripe\np = {'amount': 1}\ndef go(p):\n    stripe.Charge.create(**p)\n")
    assert got[4][2:] == (set(), False)


# --- Python 3.14 syntax -----------------------------------------------------------------------


def test_bare_except_tuples_are_read_in_place():
    text = (
        "try:\n    f()\nexcept A, B:  # comment\n    pass\n"
        "try:\n    f()\nexcept* C, D.E:\n    pass\n"
        "try:\n    f()\nexcept g(a, b):\n    pass\n"
    )
    patched = pysyntax.bare_except_tuples(text)
    assert patched.splitlines()[2] == "except(A, B):  # comment" and patched.splitlines()[6] == "except*(C, D.E):"
    assert patched.splitlines()[10] == "except g(a, b):"
    assert pysyntax.parse(text) is not None
    assert pysyntax.parse("try:\n    f()\nexcept A, B: pass\n") is None  # code after the colon would move
    assert pysyntax.parse("x = (\n") is None
    assert pysyntax.bare_except_tuples("try:\n    f()\nexcept A as e:\n    pass\n") is None


def test_a_file_in_python_314_syntax_is_scanned_and_fixed(tmp_path):
    text = (
        "import stripe\n"
        "async def go():\n"
        "    try:\n"
        "        await stripe.PaymentIntent.create_async(amount=1, currency='usd', payment_method_types=['card'])\n"
        "    except stripe.CardError, stripe.InvalidRequestError:\n"
        "        pass\n"
    )
    fix = only_fix(plan_for(tmp_path, {"pay.py": text}))
    assert changed_lines(fix) == {4: (
        "        await stripe.PaymentIntent.create_async(amount=1, currency='usd', payment_method_types=['card'])",
        "        await stripe.PaymentIntent.create_async(amount=1, currency='usd', allowed_payment_method_types=['card'])",
    )}
    assert "except stripe.CardError, stripe.InvalidRequestError:" in fix.content


# --- the fixer edits these calls exactly as the sync / literal forms -----------------------------


def test_async_calls_are_fixed_exactly_as_sync_calls(tmp_path):
    fix = only_fix(plan_for(tmp_path, {"pay.py": (
        "import stripe\n"
        "async def go(client, params):\n"
        "    await stripe.PaymentIntent.create_async(amount=1, payment_method_types=['card'])\n"
        "    await client.v1.payment_intents.create_async(params={'amount': 1, 'payment_method_types': ['card']})\n"
        "    v1 = client.v1\n"
        "    p = {'amount': 2, 'payment_method_types': ['sepa_debit']}\n"
        "    await v1.payment_intents.create_async(params=p)\n"
        "    PI = stripe.PaymentIntent\n"
        "    await PI.create_async(**p)\n"
    )}))
    assert sorted(changed_lines(fix)) == [3, 4, 6]
    assert all(OLD not in after.replace(NEW, "") for _, after in changed_lines(fix).values())


def test_every_reaching_literal_with_the_key_is_fixed(tmp_path):
    fix = only_fix(plan_for(tmp_path, {"pay.py": (
        "import stripe\n"
        "def go(flag):\n"
        "    if flag:\n"
        "        p = {'amount': 1, 'payment_method_types': ['card']}\n"
        "    else:\n"
        "        p = {'amount': 2}\n"
        "    stripe.PaymentIntent.create(**p)\n"
        "def go2(flag):\n"
        "    if flag:\n"
        "        p = {'amount': 1, 'payment_method_types': ['card']}\n"
        "    else:\n"
        "        p = dict(amount=2, payment_method_types=['ideal'])\n"
        "    stripe.PaymentIntent.create(**p)\n"
    )}))
    assert sorted(changed_lines(fix)) == [4, 10, 12]
    assert OLD not in fix.content.replace(NEW, "")


def test_an_unreadable_reaching_binding_blocks_the_fix(tmp_path):
    plan = plan_for(tmp_path, {"pay.py": (
        "import stripe\n"
        "def go(flag):\n"
        "    if flag:\n"
        "        p = {'amount': 1, 'payment_method_types': ['card']}\n"
        "    else:\n"
        "        p = build()\n"
        "    stripe.PaymentIntent.create(**p)\n"
    )})
    assert plan.fixes == [] and [u.line for u in plan.unfixable + plan.not_attempted] == [7]
