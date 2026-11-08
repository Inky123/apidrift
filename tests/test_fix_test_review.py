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
