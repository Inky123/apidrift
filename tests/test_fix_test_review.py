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
