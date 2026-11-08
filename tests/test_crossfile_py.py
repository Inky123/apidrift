"""Python wrappers followed across files: polar's ``stripe_service.create_payment_intent(**params)``."""

import pytest
from test_fixer import NEW, OLD, changed_lines, plan_for
from test_helpers import scan

from apidrift import pysyntax

SERVICE = (
    "import stripe as stripe_lib\n"
    "\n"
    "class StripeService:\n"
    "    def create_payment_intent(self, **params):\n"
    "        if params.get('setup_future_usage') == 'off_session':\n"
    "            params['excluded_payment_method_types'] = [*params.get('excluded_payment_method_types', [])]\n"
    "        log(amount=params.get('amount'))\n"
    "        return stripe_lib.PaymentIntent.create(**params)\n"
    "\n"
    "    def setup(self):\n"
    "        try:\n"
    "            pass\n"
    "        except KeyError, ValueError:\n"  # Python 3.14 syntax: the file is still read
    "            pass\n"
    "\n"
    "stripe = StripeService()\n"
)
ORDER = (
    "from typing import Any\n"
    "from polar.integrations.stripe.service import (\n"
    "    stripe as stripe_service,\n"
    ")\n"
    "\n"
    "def pay(order, method):\n"
    "    params: dict[str, Any] = {'automatic_payment_methods': {'enabled': True}}\n"
    "    if method == 'sepa_debit':\n"
    f"        params = {{'{OLD}': ['sepa_debit']}}\n"
    "    return stripe_service.create_payment_intent(\n"
    "        amount=order.amount,\n"
    "        currency='usd',\n"
    "        idempotency_key='k',\n"
    "        **params,\n"
    "    )\n"
)
FILES = {"server/polar/__init__.py": "", "server/polar/integrations/__init__.py": "",
         "server/polar/integrations/stripe/__init__.py": "",
         "server/polar/integrations/stripe/service.py": SERVICE, "server/polar/order/service.py": ORDER}
THROUGH = "stripe.PaymentIntent.create [through create_payment_intent, server/polar/integrations/stripe/service.py:8]"
WRAPPER = ("server/polar/integrations/stripe/service.py", 8, "stripe.PaymentIntent.create")


def sites(tmp_path, files):
    return sorted((u.file, u.line, u.call, tuple(sorted(u.fields)), u.complete) for u in scan(tmp_path, files))


def test_a_caller_through_a_module_level_instance_is_a_site_with_every_reaching_dict(tmp_path):
    found = sites(tmp_path, FILES)
    assert found == [
        (*WRAPPER, (), False),  # its own site stays
        ("server/polar/order/service.py", 10, THROUGH,
         ("amount", "automatic_payment_methods", "automatic_payment_methods.enabled", "currency",
          "excluded_payment_method_types", OLD), True),
    ]


def test_the_caller_is_fixed_at_its_own_dict_and_the_wrapper_is_not_edited(tmp_path):
    plan = plan_for(tmp_path, FILES)
    assert plan.unfixable == [] and [u.file for u in plan.not_attempted] == [WRAPPER[0]]
    assert [f.file for f in plan.fixes] == ["server/polar/order/service.py"]
    assert changed_lines(plan.fixes[0]) == {9: (f"        params = {{'{OLD}': ['sepa_debit']}}", f"        params = {{'{NEW}': ['sepa_debit']}}")}


def test_a_keyword_the_caller_passes_is_renamed(tmp_path):
    order = ORDER.replace("        currency='usd',\n", f"        currency='usd',\n        {OLD}=['card'],\n")
    plan = plan_for(tmp_path, {**FILES, "server/polar/order/service.py": order})
    assert plan.unfixable == [] and len(plan.fixes) == 1
    assert {13: (f"        {OLD}=['card'],", f"        {NEW}=['card'],")}.items() <= changed_lines(plan.fixes[0]).items()


@pytest.mark.parametrize("label,file,old,new", [
    ("the wrapper reads the key", "server/polar/integrations/stripe/service.py", "        log(amount=params.get('amount'))\n",
     f"        log(types=params.get('{OLD}'))\n"),
    ("the wrapper tests the key", "server/polar/integrations/stripe/service.py", "        log(amount=params.get('amount'))\n",
     f"        if '{OLD}' in params:\n            log(1)\n"),
    ("the caller's dict is also used elsewhere", "server/polar/order/service.py", "    return stripe_service",
     "    audit(params)\n    return stripe_service"),
])
def test_a_rename_the_wrapper_or_another_use_would_see_is_refused(tmp_path, label, file, old, new):
    files = dict(FILES)
    assert old in files[file]
    files[file] = files[file].replace(old, new, 1)
    plan = plan_for(tmp_path, files)
    assert plan.fixes == [] and [u.why for u in plan.unfixable] == ["helper_shared"], plan.to_dict()


def test_a_keyed_write_into_the_callers_dict_is_read_as_the_direct_reader_reads_it(tmp_path):
    order = ORDER.replace(
        "    params: dict[str, Any] = {'automatic_payment_methods': {'enabled': True}}\n"
        f"    if method == 'sepa_debit':\n        params = {{'{OLD}': ['sepa_debit']}}\n",
        f"    params: dict[str, Any] = {{}}\n    if method == 'sepa_debit':\n        params['{OLD}'] = ['sepa_debit']\n")
    found = [s for s in sites(tmp_path, {**FILES, "server/polar/order/service.py": order}) if "[through " in s[2]]
    assert [(s[3], s[4]) for s in found] == [(("amount", "currency", "excluded_payment_method_types", OLD), False)]


FUNC = "import stripe\n\ndef create_intent(params):\n    return stripe.PaymentIntent.create(**params)\n"


def test_an_imported_module_function_and_a_relative_import_are_followed(tmp_path):
    files = {"app/__init__.py": "", "app/billing.py": FUNC,
             "app/go.py": f"from app.billing import create_intent as make\n\ndef go():\n    return make({{'amount': 1, '{OLD}': []}})\n",
             "app/rel.py": f"from .billing import create_intent\n\ndef go(p):\n    return create_intent(params={{'currency': 'usd'}})\n"}
    found = [s[:4] for s in sites(tmp_path, files) if "[through " in s[2]]
    assert found == [("app/go.py", 4, "stripe.PaymentIntent.create [through create_intent, app/billing.py:4]", ("amount", OLD)),
                     ("app/rel.py", 4, "stripe.PaymentIntent.create [through create_intent, app/billing.py:4]", ("currency",))]


def test_self_calls_in_the_owner_class_are_followed_when_nothing_subclasses_it(tmp_path):
    service = SERVICE.replace("    def setup(self):\n", f"    def setup(self):\n        self.create_payment_intent(amount=5, {OLD}=['card'])\n")
    found = [s[:2] for s in sites(tmp_path / "a", {**FILES, "server/polar/integrations/stripe/service.py": service}) if "[through " in s[2]]
    assert ("server/polar/integrations/stripe/service.py", 11) in found
    sub = {"server/polar/sub.py": "from polar.integrations.stripe.service import StripeService\nclass Mine(StripeService):\n    pass\n"}
    found = [s[:2] for s in sites(tmp_path / "b", {**FILES, **sub, "server/polar/integrations/stripe/service.py": service}) if "[through " in s[2]]
    assert ("server/polar/integrations/stripe/service.py", 11) not in found
    lib = {"server/setup.py": "from setuptools import setup\nsetup()\n"}
    found = [s[:2] for s in sites(tmp_path / "c", {**FILES, **lib, "server/polar/integrations/stripe/service.py": service}) if "[through " in s[2]]
    assert ("server/polar/integrations/stripe/service.py", 11) not in found


NOT_VISIBLE = [
    ("the dict is a parameter", "server/polar/order/service.py", "def pay(order, method):\n    params: dict[str, Any] = {'automatic_payment_methods': {'enabled': True}}\n",
     "def pay(order, method, params):\n"),
    ("the dict comes from a call", "server/polar/order/service.py", "    params: dict[str, Any] = {'automatic_payment_methods': {'enabled': True}}\n",
     "    params = build()\n"),
    ("the dict is updated from elsewhere", "server/polar/order/service.py", "    return stripe_service",
     "    params.update(other)\n    return stripe_service"),
    ("the import is shadowed by a parameter", "server/polar/order/service.py", "def pay(order, method):", "def pay(order, method, stripe_service=None):"),
    ("the import is rebound", "server/polar/order/service.py", "def pay(", "stripe_service = Fake()\n\ndef pay("),
    ("a star import may rebind it", "server/polar/order/service.py", "from typing import Any\n", "from typing import Any\nfrom other import *\n"),
    ("imported from another module", "server/polar/order/service.py", "polar.integrations.stripe.service", "polar.integrations.paypal.service"),
    ("a module path under a package", "server/polar/order/service.py", "polar.integrations.stripe.service", "stripe.service"),
    ("the instance is rebound", "server/polar/integrations/stripe/service.py", "stripe = StripeService()\n",
     "stripe = StripeService()\nstripe = Other()\n"),
    ("the instance is set from outside", "server/polar/order/service.py", "def pay(", "import polar.integrations.stripe.service as m\nm.stripe = Fake()\n\ndef pay("),
    ("the method is patched", "server/polar/order/service.py", "def pay(", "stripe_service.create_payment_intent = fake\n\ndef pay("),
    ("the wrapper passes its params on", "server/polar/integrations/stripe/service.py", "        log(amount=params.get('amount'))\n",
     "        log(params)\n"),
    ("the wrapper updates its params", "server/polar/integrations/stripe/service.py", "        log(amount=params.get('amount'))\n",
     "        params.update(more)\n"),
    ("the wrapper rebinds its params", "server/polar/integrations/stripe/service.py", "        log(amount=params.get('amount'))\n",
     "        params = dict(params)\n"),
    ("the wrapper's params are captured", "server/polar/integrations/stripe/service.py", "        log(amount=params.get('amount'))\n",
     "        later(lambda: params.get('x'))\n"),
    ("the wrapper sends a field itself", "server/polar/integrations/stripe/service.py", "PaymentIntent.create(**params)",
     "PaymentIntent.create(customer='c', **params)"),
    ("the wrapper is decorated", "server/polar/integrations/stripe/service.py", "    def create_payment_intent", "    @retry\n    def create_payment_intent"),
    ("the name is defined twice", "server/polar/other.py", None, "def create_payment_intent(**kw):\n    return kw\n"),
]


@pytest.mark.parametrize("label,file,old,new", NOT_VISIBLE, ids=[n[0] for n in NOT_VISIBLE])
def test_a_caller_that_is_not_provably_visible_is_not_a_site(tmp_path, label, file, old, new):
    files = dict(FILES)
    if old is None:
        files[file] = new
    else:
        assert old in files[file]
        files[file] = files[file].replace(old, new, 1)
    found = sites(tmp_path, files)
    assert not any("[through " in s[2] for s in found), found
    if "service.py" in file and "stripe" in file and "decorated" not in label and "sends" not in label:
        assert any(s[0] == WRAPPER[0] for s in found)


def test_a_test_file_caller_is_not_a_site_and_does_not_block(tmp_path):
    files = {**FILES, "server/tests/test_order.py": ORDER, "server/tests/conftest.py": "def create_payment_intent(**kw):\n    pass\n"}
    assert [s[0] for s in sites(tmp_path, files) if "[through " in s[2]] == ["server/polar/order/service.py"]


def test_python_3_14_except_tuples_are_parsed_in_place():
    text = "try:\n    f()\nexcept A, B:  # comment\n    pass\n"
    assert pysyntax.parse(text) is not None and pysyntax.parse("x = (\n") is None


@pytest.mark.parametrize("label,setup", [
    ("self rebound in the method", "    def setup(self):\n        self = other\n"),
    ("a nested function's own self", "    def setup(self):\n        def inner(self):\n            self.create_payment_intent(amount=5)\n"),
    ("a static method", "    @staticmethod\n    def setup(self):\n"),
    ("a method of another class", "    def x(self):\n        pass\n\nclass Other:\n    def setup(self):\n"),
])
def test_a_self_call_that_may_reach_another_object_is_not_a_site(tmp_path, label, setup):
    service = SERVICE.replace("    def setup(self):\n", setup + f"        self.create_payment_intent(amount=5, {OLD}=['card'])\n")
    found = sites(tmp_path, {**FILES, "server/polar/integrations/stripe/service.py": service})
    assert not any("[through " in s[2] and s[0].endswith("stripe/service.py") for s in found), found
