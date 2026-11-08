"""The independent review's repros against the whole-checkout proofs (pyrepo / pyself): each must fail closed,
so a call through ``self`` is not resolved and ``**self.api_kwargs`` stays hidden, as on main."""

import time
from pathlib import Path

import pytest

from apidrift.openapi import load_spec
from apidrift.scanner import scan_repo
from apidrift.sdkmap import SdkIndex
from specs import sdk_doc

BASE = (
    "import stripe\n"
    "class Base:\n"
    "    stripe_class = stripe.Charge\n"
    "    @property\n"
    "    def api_kwargs(self):\n"
    "        return {'api_key': 'sk'}\n"
    "    def make(self):\n"
    "        return self.stripe_class.create(amount=1, currency='usd')\n"
    "    def pay(self):\n"
    "        return stripe.Charge.capture('ch_1', amount=1, **self.api_kwargs)\n"
)
MAKE, PAY = 8, 10
OVERRIDE = "    @property\n    def api_kwargs(self):\n        return {'api_key': 'sk', 'description': 'x'}\n"
SERVICE = (
    "import stripe\n"
    "class BillingService:\n"
    "    def __init__(self, api):\n"
    "        self.api = api\n"
    "    def charge(self):\n"
    "        return self.api.Charge.create(amount=1, currency='usd')\n"
    "def default():\n"
    "    return BillingService(stripe)\n"
)
CHARGE = 6


@pytest.fixture(scope="module")
def index():
    return SdkIndex.build(load_spec(sdk_doc()))


def scan(root: Path, index, files: dict[str, str | bytes], target: Path | None = None):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
    return {(u.file, u.line): u for u in scan_repo(target or root, index).usages}


def test_the_base_alone_is_proven(tmp_path, index):
    got = scan(tmp_path, index, {"app/base.py": BASE})
    assert got[("app/base.py", MAKE)].label == "POST /v1/charges" and got[("app/base.py", PAY)].complete


@pytest.mark.parametrize("sub", [
    # c4 / c1: a base ahead of the owner in the MRO binds the name
    "from app.base import Base\nclass Mixin:\n" + OVERRIDE + "    stripe_class = None\nclass Sub(Mixin, Base):\n    pass\n",
    # c4 with the mixin outside the checkout
    "from app.base import Base\nfrom lib import Mixin\nclass Sub(Mixin, Base):\n    pass\n",
    # c2: a subclass through an import alias
    "from app.base import Base as B\nclass Sub(B):\n    stripe_class = None\n" + OVERRIDE,
    # c3: a generic subscript base
    "from app.base import Base\nclass Sub(Base[int]):\n    stripe_class = None\n" + OVERRIDE,
    # c5: built with type()
    "from app.base import Base\nSub = type('Sub', (Base,), {'api_kwargs': None, 'stripe_class': None})\n",
    # a base we cannot name, in a file naming the class
    "from app.base import Base\nclass Sub(make(Base)):\n    pass\n",
    # metaclass / decorator ahead of the owner
    "from app.base import Base\nclass Sub(Base, metaclass=Meta):\n    pass\n",
    "from app.base import Base\n@swap\nclass Sub(Base):\n    pass\n",
    # s3 / s4: written elsewhere, in any spelling
    "from app.base import Base\ndef f(b):\n    object.__setattr__(b, 'stripe_class', 1)\n    object.__setattr__(b, 'api_kwargs', 1)\n",
    "from app.base import Base\nBase.stripe_class, Base.api_kwargs = 1, 2\n",
    "from app.base import Base\ndef f(b):\n    for k in ('stripe_class', 'api_kwargs'):\n        setattr(b, k, 1)\n",
    "from app.base import Base\ndef f(b):\n    b.__dict__.update({'stripe_class': 1, 'api_kwargs': 2})\n",
])
def test_a_rebinding_the_proof_cannot_rule_out_leaves_both_unproven(tmp_path, index, sub):
    got = scan(tmp_path, index, {"app/base.py": BASE, "app/sub.py": sub})
    assert ("app/base.py", MAKE) not in got
    assert not got[("app/base.py", PAY)].complete


@pytest.mark.parametrize("files", [
    {"build/sub.py": "from app.base import Base\nclass Sub(Base):\n    stripe_class = None\n" + OVERRIDE},  # c6: skipped dir
    {".hidden/sub.py": "from app.base import Base\nclass Sub(Base):\n    stripe_class = None\n"},
    {"app/big.py": b"# Base\n" + b"x = 1\n" * 300_000},  # d5: over the scanner's size cap, names the class
    {"app/gen.py": "# used by Base\nTABLE = " + "+".join(["1"] * 20000) + "\n"},  # r1: too deep to parse
])
def test_python_the_scanner_cannot_read_fails_closed(tmp_path, index, files):
    got = scan(tmp_path, index, {"app/base.py": BASE, **files})
    assert ("app/base.py", MAKE) not in got
    assert not got[("app/base.py", PAY)].complete


def test_a_single_file_scan_proves_nothing(tmp_path, index):
    got = scan(tmp_path, index, {"app/base.py": BASE}, target=tmp_path / "app" / "base.py")
    assert ("base.py", MAKE) not in got and not got[("base.py", PAY)].complete


def test_module_aliases_and_named_class_attributes_need_the_checkout(tmp_path, index):
    text = "import stripe\nPI = stripe.Charge\nclass M:\n    stripe_class = stripe.Charge\nPI.create(amount=1)\nM.stripe_class.create(amount=1)\n"
    alone = scan(tmp_path / "a", index, {"m.py": text}, target=tmp_path / "a" / "m.py")
    assert alone == {}
    whole = scan(tmp_path / "b", index, {"m.py": text})
    assert {k[1] for k in whole} == {5, 6}
    written = scan(tmp_path / "c", index, {"m.py": text, "cfg.py": "import m\nm.M.stripe_class = None\nm.PI = None\n"})
    assert written == {}


def test_the_service_alone_is_proven(tmp_path, index):
    assert ("app/svc.py", CHARGE) in scan(tmp_path, index, {"app/svc.py": SERVICE})


@pytest.mark.parametrize("other", [
    "from app import fakepay\nfrom app.svc import BillingService as BS\nsvc = BS(fakepay)\n",  # d1
    "from app import svc as m\nfrom app import fakepay\nx = getattr(m, 'BillingService')(fakepay)\n",  # d2
    "from app.svc import BillingService\nclass FakeBilling(BillingService):\n    pass\nservices = {'fake': FakeBilling}\n",  # d3
    "import app.svc\nfrom app import fakepay\nsvc = app.svc.__dict__['BillingService'](fakepay)\n",  # d4
    "from app.svc import BillingService\ndef f(s):\n    s.api, s.mode = None, 'test'\n",  # s1
    "from app.svc import BillingService\ndef f(s):\n    s.api: object = None\n",  # s2
    "from app.svc import BillingService\ndef f(s):\n    object.__setattr__(s, 'api', None)\n",  # s3
    "from app.svc import BillingService\nclass Sub(BillingService):\n    @property\n    def api(self):\n        return None\n",
])
def test_an_injection_the_proof_cannot_rule_out_is_not_followed(tmp_path, index, other):
    assert ("app/svc.py", CHARGE) not in scan(tmp_path, index, {"app/svc.py": SERVICE, "app/other.py": other})


def test_inherited_and_decorated_factories_count_as_constructions(tmp_path, index):
    d6 = {
        "app/factory.py": "class FromSettings:\n    @classmethod\n    def from_settings(cls, settings):\n        return cls(settings.backend)\n",
        "app/svc.py": SERVICE.replace("class BillingService:", "from app.factory import FromSettings\nclass BillingService(FromSettings):"),
    }
    assert not any(k[0] == "app/svc.py" and "api" in u.call for k, u in scan(tmp_path / "d6", index, d6).items())
    d7 = {"app/svc.py": SERVICE.replace(
        "    def charge(self):",
        "    @classmethod\n    @functools.cache\n    def sandbox(cls):\n        return cls(fakepay)\n    def charge(self):")}
    assert not any("api" in u.call for u in scan(tmp_path / "d7", index, d7).values())


def test_a_large_checkout_is_read_once(tmp_path, index):
    services = "import stripe\n" + "".join(
        f"class Svc{i}:\n    stripe_class = stripe.Charge\n    def go(self):\n        self.stripe_class.create(amount=1)\n"
        for i in range(60)
    )
    files = {"pkg/services.py": services}
    for d in range(20):
        for f in range(100):
            files[f"pkg/m{d}/mod{f}.py"] = f"from pkg import services\nclass Local{d}_{f}:\n    value = {f}\n"
    start = time.monotonic()
    got = scan(tmp_path, index, files)
    elapsed = time.monotonic() - start
    assert len([k for k in got if k[0] == "pkg/services.py"]) == 60
    assert elapsed < 15, elapsed  # one pass over the checkout, not one per class (was minutes on 20k files)
