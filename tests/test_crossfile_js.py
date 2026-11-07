"""JS / TS wrappers followed across files: through a typed injected service, or an imported function."""

import pytest
from test_fixer import NEW, OLD, changed_lines, plan_for
from test_helpers import scan

SERVICE = (
    "import Stripe from 'stripe';\n"
    "const stripe = new Stripe('sk');\n"
    "export class PaymentService {\n"
    "  async createIntent(params: Stripe.PaymentIntentCreateParams) {\n"
    "    return stripe.paymentIntents.create(params);\n"
    "  }\n"
    "}\n"
)
CALLER = (
    "import { PaymentService } from '../stripe/payment.service';\n"
    "export class Checkout {\n"
    "  constructor(private readonly paymentService: PaymentService) {}\n"
    "  async pay() {\n"
    "    return this.paymentService.createIntent({\n"
    "      amount: 100,\n"
    f"      {OLD}: ['card'],\n"
    "    });\n"
    "  }\n"
    "}\n"
)
FILES = {"src/stripe/payment.service.ts": SERVICE, "src/checkout/checkout.ts": CALLER}
THROUGH = "stripe.paymentIntents.create [through createIntent, src/stripe/payment.service.ts:5]"


def sites(tmp_path, files):
    return sorted((u.file, u.line, u.call, tuple(sorted(u.fields)), u.complete) for u in scan(tmp_path, files))


def test_a_typed_injected_service_caller_is_a_site_and_the_wrapper_is_dropped(tmp_path):
    assert sites(tmp_path, FILES) == [("src/checkout/checkout.ts", 5, THROUGH, ("amount", OLD), True)]


def test_the_caller_is_fixed_at_its_own_literal(tmp_path):
    plan = plan_for(tmp_path, FILES)
    assert plan.unfixable == [] and plan.not_attempted == [] and [f.file for f in plan.fixes] == ["src/checkout/checkout.ts"]
    assert changed_lines(plan.fixes[0]) == {7: (f"      {OLD}: ['card'],", f"      {NEW}: ['card'],")}


def test_a_local_bound_to_one_literal_is_fixed(tmp_path):
    caller = CALLER.replace(
        "    return this.paymentService.createIntent({\n      amount: 100,\n" f"      {OLD}: ['card'],\n    }});\n",
        f"    const p = {{ amount: 100, {OLD}: ['card'] }};\n    return this.paymentService.createIntent(p);\n",
    )
    plan = plan_for(tmp_path, {**FILES, "src/checkout/checkout.ts": caller})
    assert plan.unfixable == [] and list(changed_lines(plan.fixes[0])) == [5]


HIDDEN = [
    ("an untyped receiver", "private readonly paymentService: PaymentService", "private readonly paymentService: any"),
    ("a type imported from elsewhere", "from '../stripe/payment.service'", "from '../other/payment.service'"),
    ("the property is reassigned", "  async pay() {\n", "  swap(x) { this.paymentService = x; }\n  async pay() {\n"),
    ("a spread in the caller's literal", "      amount: 100,\n", "      ...extra,\n"),
    ("the caller passes its own parameter", "  async pay() {\n    return this.paymentService.createIntent({\n      amount: 100,\n"
     f"      {OLD}: ['card'],\n    }});\n", "  async pay(p) {\n    return this.paymentService.createIntent(p);\n"),
    ("a receiver that is not this.<property>", "this.paymentService.createIntent(", "getService().createIntent("),
    ("a public writable property", "private readonly paymentService", "public paymentService"),
]


@pytest.mark.parametrize("label,old,new", HIDDEN, ids=[h[0] for h in HIDDEN])
def test_a_caller_not_provably_reaching_the_wrapper_is_not_a_site(tmp_path, label, old, new):
    assert old in CALLER
    found = sites(tmp_path, {**FILES, "src/checkout/checkout.ts": CALLER.replace(old, new)})
    assert not any("[through " in s[2] for s in found), found
    assert ("src/stripe/payment.service.ts", 5, "stripe.paymentIntents.create", (), False) in found


@pytest.mark.parametrize("label,extra", [
    ("the class is declared twice", {"src/other.ts": "export class PaymentService { createIntent(p) { return p; } }\n"}),
    ("a subclass exists", {"src/sub.ts": "import { PaymentService } from './stripe/payment.service';\n"
                                         "export class Sub extends PaymentService {}\n"}),
    ("dependency injection replaces it", {"src/module.ts": "export const m = { provide: PaymentService, useValue: {} };\n"}),
    ("a published package", {"src/stripe/package.json": '{"name": "@acme/pay", "main": "index.js"}'}),
])
def test_an_owner_that_may_be_replaced_is_not_followed(tmp_path, label, extra):
    found = sites(tmp_path, {**FILES, **extra})
    assert not any("[through " in s[2] for s in found)


def test_a_method_of_the_same_name_on_an_unrelated_class_does_not_block(tmp_path):
    other = "export class Mailer {\n  createIntent(x) { return x; }\n  run() { return this.createIntent(1); }\n}\n"
    found = sites(tmp_path, {**FILES, "src/mailer.ts": other})
    assert [s[:3] for s in found] == [("src/checkout/checkout.ts", 5, THROUGH)]


def test_a_test_file_caller_is_ignored_and_a_mock_does_not_block(tmp_path):
    spec = CALLER + "const mock = { provide: PaymentService, useValue: { createIntent: jest.fn() } };\n"
    found = sites(tmp_path, {**FILES, "src/checkout/checkout.spec.ts": spec})
    assert [s[:2] for s in found] == [("src/checkout/checkout.ts", 5)]


def test_a_shared_local_is_refused(tmp_path):
    caller = CALLER.replace(
        "    return this.paymentService.createIntent({\n      amount: 100,\n" f"      {OLD}: ['card'],\n    }});\n",
        f"    const p = {{ amount: 100, {OLD}: ['card'] }};\n    audit(p);\n    return this.paymentService.createIntent(p);\n",
    )
    plan = plan_for(tmp_path, {**FILES, "src/checkout/checkout.ts": caller})
    assert plan.fixes == [] and [u.why for u in plan.unfixable] == ["helper_shared"]


FUNC = (
    "const stripe = require('stripe')('sk');\n"
    "export async function createIntent(params) {\n"
    "  return stripe.paymentIntents.create(params);\n"
    "}\n"
)
USER = f"import {{ createIntent as make }} from './billing';\nexport const go = () => make({{ amount: 1, {OLD}: ['card'] }});\n"


def test_an_imported_function_is_followed_through_a_renamed_import(tmp_path):
    found = sites(tmp_path, {"lib/billing.js": FUNC, "lib/go.js": USER})
    assert [s[:3] for s in found] == [("lib/go.js", 2, "stripe.paymentIntents.create [through createIntent, lib/billing.js:3]")]
    plan = plan_for(tmp_path, {"lib/billing.js": FUNC, "lib/go.js": USER})
    assert [f.file for f in plan.fixes] == ["lib/go.js"] and plan.unfixable == []


def test_a_tsconfig_path_alias_resolves(tmp_path):
    files = {
        "tsconfig.json": '{\n  // comment\n  "compilerOptions": {"paths": {"src/*": ["./src/*"]},},\n}\n',
        "src/billing.ts": FUNC, "src/go.ts": USER.replace("'./billing'", "'src/billing'"),
    }
    assert [s[0] for s in sites(tmp_path, files)] == ["src/go.ts"]


@pytest.mark.parametrize("label,extra,user", [
    ("defined twice", {"lib/other.js": "export function createIntent(x) { return x; }\n"}, USER),
    ("imported from another module", {}, USER.replace("'./billing'", "'./other'")),
    ("not imported", {}, f"export const go = () => createIntent({{ amount: 1, {OLD}: ['card'] }});\n"),
])
def test_a_function_that_does_not_resolve_is_not_followed(tmp_path, label, extra, user):
    found = sites(tmp_path, {"lib/billing.js": FUNC, "lib/go.js": user, **extra})
    assert not any("[through " in s[2] for s in found)
    assert any(s[0] == "lib/billing.js" for s in found)


def test_a_wrapper_whose_parameter_is_also_read_is_not_followed(tmp_path):
    func = FUNC.replace("  return stripe.paymentIntents.create(params);\n",
                        "  if (!params) return createIntent({ amount: 1 });\n  return stripe.paymentIntents.create(params);\n")
    found = sites(tmp_path, {"lib/billing.js": func, "lib/go.js": USER})
    assert any(s[0] == "lib/billing.js" for s in found)


@pytest.mark.parametrize("export", [
    "export default createIntent;\n", "export { createIntent as default };\n",
])
def test_a_default_export_is_not_followed(tmp_path, export):
    """A default import binds any name: its calls cannot be found by the function's own name."""
    func = FUNC.replace("export async function", "async function") + export
    other = f"import make from './billing';\nexport const go2 = () => make({{ amount: 2, {OLD}: [] }});\n"
    found = sites(tmp_path, {"lib/billing.js": func, "lib/go.js": USER, "lib/go2.js": other})
    assert not any("[through " in s[2] for s in found)
    assert any(s[0] == "lib/billing.js" for s in found)
