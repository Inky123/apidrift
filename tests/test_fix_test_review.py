"""The independent review's repros for test edits and test listing, each kept as a test."""

from test_fixer import plan_for

from apidrift.fixtestfind import find

PY_APP = "import stripe\n\n\ndef pay():\n    return stripe.PaymentIntent.create(amount=1, payment_method_types=['card'])\n"


def test_r5_a_nested_function_is_read_once_with_its_own_bindings(tmp_path):
    nested = (
        "from unittest.mock import patch\n\n\ndef test_pay():\n    def check():\n"
        '        with patch("app.pay.stripe.PaymentIntent.create") as create:\n'
        '            create.assert_called_once_with(amount=1, payment_method_types=["card"])\n'
        "    check()\n"
    )
    assert len(find(nested, "python")) == 1
    plan = plan_for(tmp_path, {"app/pay.py": PY_APP, "tests/test_pay.py": nested})
    assert [t.file for t in plan.test_fixes] == ["tests/test_pay.py"]
    closure = (
        "from unittest.mock import patch\n\n\n@patch('stripe.PaymentIntent.create')\ndef test_pay(create):\n"
        "    def check():\n        create.assert_called_once_with(payment_method_types=['card'])\n"
        "    def shadow(create):\n        create.assert_called_once_with(payment_method_types=['card'])\n"
        "    def rebound():\n        create = object()\n        create.assert_called_once_with(payment_method_types=['card'])\n"
    )
    found = find(closure, "python")
    assert [closure.count("\n", 0, e.lo) + 1 for e in found] == [7]  # the closure, not the shadowed or rebound name


BUILDER_APP = (
    "class PayService\n  def call\n    Stripe::PaymentIntent.create(payment_intent_payload)\n  end\n\n  private\n\n"
    "  def payment_intent_payload\n    {\n      amount: 1,\n      payment_method_types: methods\n    }\n  end\nend\n"
)
BUILDER_SPEC = (
    "RSpec.describe PayService do\n  let(:service) { described_class.new }\n"
    "  let(:built) { service.__send__(:payment_intent_payload) }\n"
    "  let(:payload) do\n    {\n      amount: 1,\n      payment_method_types: [\"card\"]\n    }\n  end\n"
    "  let(:other) { payload.merge(amount: 2) }\n"
    "  it { expect(built).to eq(payload) }\n  it { expect(built).to eq(other) }\n"
    "  it do\n    local = payload.deep_merge(\n      amount: 3\n    )\n    expect(built).to eq(local)\n  end\nend\n"
)


def test_r4_a_derivation_that_names_the_old_key_keeps_the_literal_listed(tmp_path):
    plan = plan_for(tmp_path / "plain", {"app/pay_service.rb": BUILDER_APP, "spec/pay_service_spec.rb": BUILDER_SPEC})
    assert [t.file for t in plan.test_fixes] == ["spec/pay_service_spec.rb"]  # derivations that do not name it
    for n, change in enumerate((
        ("payload.merge(amount: 2)", 'payload.merge(payment_method_types: ["customer_balance"])'),
        ("payload.merge(amount: 2)", "payload.except(:payment_method_types)"),
        ("      amount: 3\n", '      "payment_method_types" => []\n'),  # a multi-line local derivation
    )):
        spec = BUILDER_SPEC.replace(*change)
        plan = plan_for(tmp_path / str(n), {"app/pay_service.rb": BUILDER_APP, "spec/pay_service_spec.rb": spec})
        assert plan.test_fixes == [] and ("spec/pay_service_spec.rb", 7) in plan.tests, change


def test_item5_a_test_that_sends_to_stripe_is_listed_whatever_its_language(tmp_path):
    from apidrift.fixtests import stale_tests

    files = {
        "tests/e2e/helpers.ts": "await fetch(`https://api.stripe.com${path}`, { body: { payment_method_types: ['x'] } })\n",
        "tests/e2e/sdk.test.ts": "await stripe.setupIntents.create({ payment_method_types: ['x'] })\n",
        "tests/e2e/browser.test.ts": "render(<Elements options={{ payment_method_types: ['x'] }} />)\n",
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    sites = [("rename", "payment_method_types")]
    assert stale_tests(tmp_path, sites, {"app/Pay.php"}, {"tests/e2e/sdk.test.ts"}) == [
        ("tests/e2e/helpers.ts", 1), ("tests/e2e/sdk.test.ts", 1)]
    app = "Stripe::PaymentIntent.create({ payment_method_types: ['card'], amount: 1 })\n"
    sdk = "const stripe = require('stripe')('k');\nstripe.charges.create({ payment_method_types: ['x'] })\n"
    plan = plan_for(tmp_path / "plan", {"app/pay.rb": app, "tests/sdk.test.js": sdk})
    assert ("tests/sdk.test.js", 2) in plan.tests  # the fixer passes the files with Stripe calls


def test_r1_form_encoded_bodies_in_http_stubs_are_listed(tmp_path):
    from apidrift.fixtests import stale_tests

    files = {
        "spec/pay_spec.rb": (
            "stub_request(:post, 'https://api.stripe.com/v1/payment_intents')\n"
            "  .with(body: 'amount=1&payment_method_types[0]=card')\n"
            "  .with(body: { 'payment_method_types[0]' => 'card' })\n"
            "expect(WebMock).to have_requested(:post, /payment_intents/).with(body: /payment_method_types/)\n"
            "it 'drops my_payment_method_types=1 and payment_method_types from the description' do\n"
        ),
        "tests/test_pay.py": (
            "responses.add(responses.POST, URL, match=[matchers.urlencoded_params_matcher({'payment_method_types[0]': 'card'})])\n"
            "assert 'payment_method_types%5B0%5D=card' in responses.calls[0].request.body\n"
            "# payment_method_types[0]=card in a comment\n"
        ),
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    found = stale_tests(tmp_path, [("rename", "payment_method_types")], {"app/pay.rb", "app/pay.py"})
    assert found == [("spec/pay_spec.rb", 2), ("spec/pay_spec.rb", 3), ("spec/pay_spec.rb", 4),
                     ("tests/test_pay.py", 1), ("tests/test_pay.py", 2)]
