"""Go wrappers followed across files: a typed params literal handed to a wrapper of that params type."""

import pytest
from gospec import OLD, plan, specs
from test_fixer import changed_lines

from apidrift.scanner import scan_repo

CARD = 'stripe.StringSlice([]string{"card"})'
HELPER = (
    "package stripe_helper\n\nimport (\n\t\"context\"\n\n\t\"github.com/stripe/stripe-go/v86\"\n"
    "\t\"github.com/stripe/stripe-go/v86/client\"\n)\n\n"
    "type Stripe interface {\n"
    "\tCreateIntent(ctx context.Context, params *stripe.PaymentIntentParams) (*stripe.PaymentIntent, error)\n"
    "}\n\n"
    "type stripeBase struct {\n\tclient *client.API\n}\n\n"
    "func (s *stripeBase) CreateIntent(ctx context.Context, params *stripe.PaymentIntentParams) (*stripe.PaymentIntent, error) {\n"
    "\tparams.Context = ctx\n"
    "\treturn s.client.PaymentIntents.New(params)\n"
    "}\n"
)
CALLER = (
    "package billing\n\nimport (\n\t\"context\"\n\n\t\"github.com/stripe/stripe-go/v86\"\n"
    "\t\"example.com/app/stripe_helper\"\n)\n\n"
    "type billing struct {\n\tstripe stripe_helper.Stripe\n}\n\n"
    "func (b *billing) Pay(ctx context.Context) error {\n"
    "\t_, err := b.stripe.CreateIntent(ctx, &stripe.PaymentIntentParams{\n"
    "\t\tAmount:             stripe.Int64(100),\n"
    f"\t\tPaymentMethodTypes: {CARD},\n"
    "\t})\n"
    "\treturn err\n"
    "}\n"
)
FILES = {"go.mod": "module example.com/app\n", "stripe_helper/stripe.go": HELPER, "billing/checkout.go": CALLER}
THROUGH = "s.client.PaymentIntents.New [through CreateIntent, stripe_helper/stripe.go:20]"
WRAPPER = ("stripe_helper/stripe.go", 20, "s.client.PaymentIntents.New")


def sites(root, files):
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return sorted((u.file, u.line, u.call, tuple(sorted(u.fields)), u.complete) for u in scan_repo(root, specs()[0]).usages)


def test_a_typed_literal_passed_through_an_interface_to_the_one_wrapper_is_a_site(tmp_path):
    assert sites(tmp_path, FILES) == [
        ("billing/checkout.go", 15, THROUGH, ("amount", OLD), True),
        (*WRAPPER, (), False),  # its own site stays
    ]


def test_the_caller_is_fixed_at_its_literal(tmp_path):
    p = plan(tmp_path, FILES)
    assert p.unfixable == [] and [u.file for u in p.not_attempted] == ["stripe_helper/stripe.go"]
    assert [f.file for f in p.fixes] == ["billing/checkout.go"]
    assert changed_lines(p.fixes[0]) == {
        16: ("\t\tAmount:             stripe.Int64(100),", "\t\tAmount:                    stripe.Int64(100),"),
        17: (f"\t\tPaymentMethodTypes: {CARD},", f"\t\tAllowedPaymentMethodTypes: {CARD},"),
    }


def test_a_variable_that_also_feeds_another_call_is_refused(tmp_path):
    caller = CALLER.replace(
        "\t_, err := b.stripe.CreateIntent(ctx, &stripe.PaymentIntentParams{\n\t\tAmount:             stripe.Int64(100),\n"
        f"\t\tPaymentMethodTypes: {CARD},\n\t}})\n",
        f"\tp := &stripe.PaymentIntentParams{{PaymentMethodTypes: {CARD}}}\n\taudit(p)\n\t_, err := b.stripe.CreateIntent(ctx, p)\n")
    p = plan(tmp_path, {**FILES, "billing/checkout.go": caller})
    assert p.fixes == [], p.fixes[0].diff()


def test_a_variable_bound_to_a_typed_literal_and_a_same_package_function_are_followed(tmp_path):
    helper = HELPER.replace(
        "func (s *stripeBase) CreateIntent(ctx context.Context, params *stripe.PaymentIntentParams) (*stripe.PaymentIntent, error) {\n"
        "\tparams.Context = ctx\n\treturn s.client.PaymentIntents.New(params)\n}\n",
        "func CreateIntent(sc *client.API, params *stripe.PaymentIntentParams) (*stripe.PaymentIntent, error) {\n"
        "\treturn sc.PaymentIntents.New(params)\n}\n\n"
        "func pay(sc *client.API) {\n"
        f"\tp := &stripe.PaymentIntentParams{{PaymentMethodTypes: {CARD}}}\n"
        "\tp.Amount = stripe.Int64(5)\n"
        "\tCreateIntent(sc, p)\n}\n").replace("type Stripe interface {\n\tCreateIntent(ctx context.Context, params *stripe.PaymentIntentParams) (*stripe.PaymentIntent, error)\n}\n\n", "")
    found = [s for s in sites(tmp_path, {"go.mod": "module example.com/app\n", "stripe_helper/stripe.go": helper}) if "[through " in s[2]]
    assert [(s[0], s[3], s[4]) for s in found] == [("stripe_helper/stripe.go", ("amount", OLD), True)]


def test_a_function_called_through_its_package_import_is_followed(tmp_path):
    helper = HELPER.replace("func (s *stripeBase) CreateIntent(ctx context.Context,", "func CreateIntent(s *stripeBase, ctx context.Context,")
    helper = helper.replace("\tCreateIntent(ctx context.Context, params *stripe.PaymentIntentParams) (*stripe.PaymentIntent, error)\n", "")
    caller = CALLER.replace("b.stripe.CreateIntent(ctx,", "stripe_helper.CreateIntent(nil, ctx,")
    found = [s[:2] for s in sites(tmp_path, {**FILES, "stripe_helper/stripe.go": helper, "billing/checkout.go": caller}) if "[through " in s[2]]
    assert found == [("billing/checkout.go", 15)]
    other = caller.replace('"example.com/app/stripe_helper"', 'stripe_helper "example.com/other/stripe_helper"')
    found = [s for s in sites(tmp_path / "b", {**FILES, "stripe_helper/stripe.go": helper, "billing/checkout.go": other}) if "[through " in s[2]]
    assert found == []


NOT_VISIBLE = [
    ("a literal of another params type", "billing/checkout.go", "&stripe.PaymentIntentParams{", "&stripe.SetupIntentParams{"),
    ("a value built elsewhere", "billing/checkout.go", "b.stripe.CreateIntent(ctx, &stripe.PaymentIntentParams{",
     "b.stripe.CreateIntent(ctx, build(&stripe.PaymentIntentParams{"),
    ("a variable that is a parameter", "billing/checkout.go", "func (b *billing) Pay(ctx context.Context) error {\n\t_, err := b.stripe.CreateIntent(ctx, &stripe.PaymentIntentParams{\n"
     "\t\tAmount:             stripe.Int64(100),\n" f"\t\tPaymentMethodTypes: {CARD},\n\t}})\n",
     "func (b *billing) Pay(ctx context.Context, p *stripe.PaymentIntentParams) error {\n\t_, err := b.stripe.CreateIntent(ctx, p)\n"),
    ("the wrapper reads its params", "stripe_helper/stripe.go", "\tparams.Context = ctx\n", "\tlog(params.Amount)\n"),
    ("the wrapper adds a field", "stripe_helper/stripe.go", "\tparams.Context = ctx\n", "\tparams.AddExpand(\"customer\")\n"),
    ("the wrapper sets a request field", "stripe_helper/stripe.go", "\tparams.Context = ctx\n", "\tparams.Currency = stripe.String(\"usd\")\n"),
    ("the wrapper passes its params on", "stripe_helper/stripe.go", "\tparams.Context = ctx\n", "\taudit(params)\n"),
    ("the wrapper rebinds its params", "stripe_helper/stripe.go", "\tparams.Context = ctx\n", "\tparams = &stripe.PaymentIntentParams{}\n"),
    ("the wrapper's params are not a stripe type", "stripe_helper/stripe.go", "params *stripe.PaymentIntentParams) (*stripe.PaymentIntent, error) {",
     "params *PaymentIntentParams) (*stripe.PaymentIntent, error) {"),
    ("a second definition (a mock outside tests)", "mocks/stripe.go", None,
     "package mocks\n\nimport \"github.com/stripe/stripe-go/v86\"\n\ntype M struct{}\n\n"
     "func (m *M) CreateIntent(ctx any, params *stripe.PaymentIntentParams) (*stripe.PaymentIntent, error) {\n\treturn nil, nil\n}\n"),
]


@pytest.mark.parametrize("label,file,old,new", NOT_VISIBLE, ids=[n[0] for n in NOT_VISIBLE])
def test_a_caller_that_is_not_provably_visible_is_not_a_site(tmp_path, label, file, old, new):
    files = dict(FILES)
    if old is None:
        files[file] = new
    else:
        assert old in files[file]
        files[file] = files[file].replace(old, new, 1)
    if "built elsewhere" in label:
        files[file] = files[file].replace("\t})\n\treturn err", "\t}))\n\treturn err")
    found = sites(tmp_path, files)
    assert not any("[through " in s[2] for s in found), found


def test_a_test_file_mock_does_not_block_and_a_test_caller_is_not_a_site(tmp_path):
    mock = ("package stripe_helper\n\nimport \"github.com/stripe/stripe-go/v86\"\n\ntype mock struct{}\n\n"
            "func (m *mock) CreateIntent(ctx any, params *stripe.PaymentIntentParams) (*stripe.PaymentIntent, error) {\n\treturn nil, nil\n}\n")
    found = sites(tmp_path, {**FILES, "stripe_helper/mock_test.go": mock, "billing/checkout_test.go": CALLER})
    assert [s[0] for s in found if "[through " in s[2]] == ["billing/checkout.go"]
