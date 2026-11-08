"""Calls through ``self`` / ``cls`` that the whole checkout proves (pyrepo + pyself)."""

from pathlib import Path

import pytest

from apidrift import pyparse
from apidrift.openapi import load_spec
from apidrift.scanner import scan_repo
from apidrift.sdkmap import SdkIndex
from specs import sdk_doc
from test_fixer import changed_lines, only_fix, plan_for

SERVICE = (
    "import stripe\n"
    "class Billing:\n"
    "    def __init__(self, api, key):\n"
    "        self.api = api\n"
    "        self.key = key\n"
    "    def charge(self):\n"
    "        return self.api.Charge.create(amount=1, currency='usd')\n"
    "    @classmethod\n"
    "    def create_service(cls, request):\n"
    "        return cls(stripe, request.key)\n"
)
MODEL = (
    "import stripe\n"
    "class Model:\n"
    "    stripe_class = stripe.Charge\n"
    "    def get(self):\n"
    "        return self.stripe_class.retrieve('ch_1')\n"
    "    @classmethod\n"
    "    def make(cls):\n"
    "        return cls.stripe_class.create(amount=1, currency='usd')\n"
)
OPTIONS = (
    "import stripe\n"
    "class Method:\n"
    "    @property\n"
    "    def api_kwargs(self):\n"
    "        if self.connect:\n"
    "            kwargs = {'api_key': self.secret, 'stripe_account': self.account}\n"
    "        else:\n"
    "            kwargs = {'api_key': self.secret}\n"
    "        return kwargs\n"
    "    def go(self):\n"
    "        return stripe.Charge.capture('ch_1', amount=1, **self.api_kwargs)\n"
)


@pytest.fixture(scope="module")
def index():
    return SdkIndex.build(load_spec(sdk_doc()))


def scan(root: Path, index, files: dict[str, str]):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return {(u.file, u.line): (u.label, set(u.fields), u.complete) for u in scan_repo(root, index).usages}


def test_injected_module_and_class_attributes_and_option_properties(tmp_path, index):
    got = scan(tmp_path, index, {
        "svc.py": SERVICE, "model.py": MODEL, "method.py": OPTIONS,
        "app.py": "import stripe\nfrom svc import Billing\nservice = Billing(stripe, 'k')\nother = Billing(api=stripe, key='k')\n",
        "tests/test_svc.py": "from svc import Billing\nb = Billing(object(), 'k')\nassert isinstance(b, Billing)\n",
    })
    assert got[("svc.py", 7)] == ("POST /v1/charges", {"amount", "currency"}, True)
    assert got[("model.py", 5)][0] == "GET /v1/charges/{charge}" and got[("model.py", 8)][0] == "POST /v1/charges"
    assert got[("method.py", 11)] == ("POST /v1/charges/{charge}/capture", {"amount"}, True)


@pytest.mark.parametrize("extra", [
    {"app.py": "from svc import Billing\nimport other\nservice = Billing(other, 'k')\n"},  # not the stripe module
    {"app.py": "from svc import Billing\nfrom stripe import Charge\nservice = Billing(Charge, 'k')\n"},
    {"app.py": "import stripe\nfrom svc import Billing\nservice = Billing(*args)\n"},
    {"app.py": "from svc import Billing\nfactory = Billing\n"},  # used as a value: built out of sight
    {"app.py": "from svc import Billing\nclass Mine(Billing):\n    def __init__(self):\n        self.api = 1\n"},
    {"app.py": "from svc import Billing\nclass Mine(Billing):\n    api = None\n"},
    {"app.py": "def f(s):\n    s.api = object()\n"},  # another write of .api
    {"svc2.py": "class Billing2:\n    pass\n", "app.py": "from svc import Billing\nclass Deep(Billing):\n    pass\nclass Deeper(Deep):\n    def __init__(self):\n        pass\n"},
])
def test_injection_that_is_not_proven_is_not_followed(tmp_path, index, extra):
    got = scan(tmp_path, index, {"svc.py": SERVICE, **extra})
    assert ("svc.py", 7) not in got


def test_injection_needs_a_construction_and_the_whole_checkout(tmp_path, index):
    only_tests = SERVICE.replace("        return cls(stripe, request.key)\n", "        return None\n")
    got = scan(tmp_path, index, {"svc.py": only_tests, "tests/test_svc.py": "import stripe\nfrom svc import Billing\nBilling(stripe, 'k')\n"})
    assert ("svc.py", 7) not in got
    calls, _ = pyparse.calls(SERVICE, lambda chain: index.resolve(chain))  # one file alone: never followed
    assert all(not c.call.startswith("self.api") for c in calls)


@pytest.mark.parametrize("extra", [
    {"sub.py": "import stripe\nfrom model import Model\nclass Refundish(Model):\n    stripe_class = stripe.Refund\n"},
    {"sub.py": "from model import Model\nclass A(Model):\n    pass\nclass B(A):\n    stripe_class = None\n"},
    {"sub.py": "from model import Model\nModel.stripe_class = object()\n"},
])
def test_a_class_attribute_rebound_anywhere_is_not_followed(tmp_path, index, extra):
    got = scan(tmp_path, index, {"model.py": MODEL, **extra})
    assert ("model.py", 5) not in got and ("model.py", 8) not in got


@pytest.mark.parametrize("text, extra", [
    (OPTIONS.replace("{'api_key': self.secret}", "{'api_key': self.secret, 'description': 'x'}"), {}),  # a body field
    (OPTIONS.replace("        return kwargs\n", "        kwargs['amount'] = 2\n        return kwargs\n"), {}),
    (OPTIONS.replace("    @property\n", ""), {}),  # a method, not a property
    (OPTIONS, {"sub.py": "from method import Method\nclass Sub(Method):\n    @property\n    def api_kwargs(self):\n        return {'amount': 1}\n"}),
])
def test_an_options_property_that_is_not_proven_stays_hidden(tmp_path, index, text, extra):
    got = scan(tmp_path, index, {"method.py": text, **extra})
    line = next(n for n, t in enumerate(text.splitlines(), 1) if "Charge.capture" in t)
    assert got[("method.py", line)][2] is False


def test_the_fixer_reads_through_self_with_the_same_checkout(tmp_path):
    plan = plan_for(tmp_path, {
        "svc.py": SERVICE.replace("self.api.Charge.create(amount=1, currency='usd')",
                                  "self.api.PaymentIntent.create(amount=1, payment_method_types=['card'])"),
    })
    # the scan's checkout reaches the Locator and the oracle: the same call, edited exactly as a direct one
    assert changed_lines(only_fix(plan)) == {7: (
        "        return self.api.PaymentIntent.create(amount=1, payment_method_types=['card'])",
        "        return self.api.PaymentIntent.create(amount=1, allowed_payment_method_types=['card'])",
    )}


def test_the_fixer_does_not_edit_through_self_when_one_file_is_fixed_alone(tmp_path):
    text = SERVICE.replace("self.api.Charge.create(amount=1, currency='usd')",
                           "self.api.PaymentIntent.create(amount=1, payment_method_types=['card'])")
    (tmp_path / "svc.py").write_text(text)
    plan = plan_for(tmp_path / "svc.py")  # a single-file scan: no checkout, so no call through self
    assert plan.fixes == [] and plan.unfixable == []
