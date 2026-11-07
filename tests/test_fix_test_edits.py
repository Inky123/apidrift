"""A fix pull request updates a test only where the line expects the exact request the fix changes.

Each language gets the forms that are edited and, in the same file, the hostile neighbours that must stay
listed: a negative expectation, a mock's return value, a Stripe response read, another endpoint, a variable,
a comment or string, and a mock this reader cannot tie to the SDK call.
"""

from test_fixer import plan_for
from test_fixer_v2 import plan_v2

from apidrift.fixedit import apply
from apidrift.fixtestfind import find

OLD, NEW = "payment_method_types", "allowed_payment_method_types"


def edited(plan) -> dict[str, set[int]]:
    return {t.file: {e.line for e in t.edits} for t in plan.test_fixes}


def content(plan, file: str) -> str:
    fix = next(t for t in plan.test_fixes if t.file == file)
    assert apply(fix.original, [_edit(e) for e in fix.edits]) == fix.content  # the payload's edits rebuild it
    return fix.content


def _edit(e):
    from apidrift.fixvalue import Edit

    return Edit(e.start, e.end, e.text, e.target)


def line_of(text: str, needle: str) -> int:
    return text[: text.index(needle)].count("\n") + 1


RUBY_APP = "class Pay\n  def call\n    Stripe::PaymentIntent.create({ payment_method_types: ['card'], amount: 1 })\n  end\nend\n"
RUBY_SPEC = """RSpec.describe Pay do
  it 'creates' do
    Pay.new.call
    expect(Stripe::PaymentIntent).to have_received(:create).with(hash_including(payment_method_types: ['card']))
    expect(::Stripe::PaymentIntent)
      .to receive(:create).once
      .with({ payment_method_types: ['card'], amount: 1 }, anything)
    expect(Stripe::PaymentIntent).to have_received(:create).with("payment_method_types" => ['card'], amount: 1)
    expect(Stripe::PaymentIntent).not_to have_received(:create).with(hash_including(payment_method_types: ['x']))
    allow(Stripe::PaymentIntent).to receive(:create).and_return(payment_method_types: ['card'])
    intent = Stripe::PaymentIntent.retrieve('pi_1')
    expect(intent.payment_method_types).to eq(['card'])
    expect(Stripe::Charge).to have_received(:create).with(hash_including(payment_method_types: ['card']))
    expect(Stripe::PaymentIntent).to have_received(:create).with(hash_including(params))
    expect(Stripe::PaymentIntent).to have_received(:create).with(hash_including('payment_' + 'method_types' => 1))
    # expect(Stripe::PaymentIntent).to have_received(:create).with(payment_method_types: ['card'])
    expect(Stripe::PaymentIntent).to have_received(:create).with(hash_including(payment_method_types: ['card'], **extra))
  end
end
"""


def test_rspec_expectations_of_the_changed_call_are_updated_and_nothing_else(tmp_path):
    plan = plan_for(tmp_path, {"app/pay.rb": RUBY_APP, "spec/pay_spec.rb": RUBY_SPEC})
    assert [f.file for f in plan.fixes] == ["app/pay.rb"]
    after = content(plan, "spec/pay_spec.rb")
    assert edited(plan) == {"spec/pay_spec.rb": {4, 7, 8}}
    assert "with(hash_including(allowed_payment_method_types: ['card']))" in after
    assert ".with({ allowed_payment_method_types: ['card'], amount: 1 }, anything)" in after
    assert 'with("allowed_payment_method_types" => [\'card\'], amount: 1)' in after
    for kept in RUBY_SPEC.splitlines()[8:]:  # the negative, the mock return, the response, Charge, ...: untouched
        assert kept in after.splitlines(), kept
    listed = {line for f, line in plan.tests if f == "spec/pay_spec.rb"}
    assert {4, 7, 8} < listed and {9, 10, 12, 13, 17} <= listed  # the rest stay listed for a person
    assert 16 not in listed  # a comment is not a test that can fail


def test_a_field_some_call_still_sends_is_never_updated_in_tests(tmp_path):
    shared = "params = { payment_method_types: ['card'] }\nStripe::PaymentIntent.create(params)\nStripe::Charge.create(params)\n"
    plan = plan_for(tmp_path, {"app/pay.rb": RUBY_APP, "app/other.rb": shared, "spec/pay_spec.rb": RUBY_SPEC})
    assert plan.unfixable and plan.fixes  # app/other.rb cannot be fixed: it still sends the old key
    assert plan.test_fixes == []


def test_a_builder_spec_compared_with_eq_is_updated_through_its_lets(tmp_path):
    app = (
        "module Billing\n  class Pay\n    def call\n      Stripe::PaymentIntent.create(payload)\n    end\n\n"
        "    private\n\n    def payload\n      {\n        amount: 1,\n        payment_method_types: ['card']\n      }\n    end\n"
        "  end\nend\n"
    )
    spec = (
        "RSpec.describe Billing::Pay do\n"
        "  let(:built) { described_class.new.__send__(:payload) }\n"
        "  let(:expected) do\n    {\n      amount: 1,\n      payment_method_types: ['card']\n    }\n  end\n"
        "  let(:bigger) { expected.merge(extra: 1) }\n"
        "  it { expect(built).to eq(expected) }\n"
        "  it do\n    want = bigger.merge(more: 2)\n    expect(built).to eq(want)\n  end\n"
        "end\n"
    )
    plan = plan_for(tmp_path, {"app/billing/pay.rb": app, "spec/billing/pay_spec.rb": spec})
    assert [f.file for f in plan.fixes] == ["app/billing/pay.rb"]
    assert edited(plan) == {"spec/billing/pay_spec.rb": {6}}
    assert "      allowed_payment_method_types: ['card']\n" in content(plan, "spec/billing/pay_spec.rb")
    # The same literal used for anything else, another class, or a spec that is not this file's: listed only.
    for other in (
        spec.replace("  it { expect(built)", "  it { create(:charge, params: expected) }\n  it { expect(built)"),
        spec.replace("RSpec.describe Billing::Pay", "RSpec.describe Billing::Refund"),
        spec.replace("__send__(:payload)", "__send__(:payload).merge(x: 1)"),
        spec.replace("eq(expected)", "eq(expected.merge(x: 1)) }\n  it { foo(expected)"),
    ):
        root = tmp_path / str(hash(other))
        plan = plan_for(root, {"app/billing/pay.rb": app, "spec/billing/pay_spec.rb": other})
        assert plan.test_fixes == [] and any(f == "spec/billing/pay_spec.rb" for f, _ in plan.tests)
    plan = plan_for(tmp_path / "elsewhere", {"app/billing/pay.rb": app, "spec/other/pay_spec.rb": spec})
    assert plan.test_fixes == []


JS_APP = "const stripe = require('stripe')('sk');\nstripe.paymentIntents.create({ payment_method_types: ['card'], amount: 1 });\n"
JS_TEST = """const mocks = vi.hoisted(() => ({ create: vi.fn(), list: vi.fn() }));
vi.mock("stripe", () => ({
  default: class MockStripe {
    paymentIntents = {
      create: mocks.create,
    };
  },
}));
test("pays", async () => {
  expect(mocks.create).toHaveBeenCalledWith(expect.objectContaining({ payment_method_types: ["card"] }));
  expect(stripe.paymentIntents.create).toHaveBeenCalledWith({ "payment_method_types": ["card"], amount: 1 }, { idempotencyKey: "k" });
  expect(mocks.create).not.toHaveBeenCalledWith(expect.objectContaining({ payment_method_types: ["x"] }));
  mocks.create.mockResolvedValue({ payment_method_types: ["card"] });
  expect(db.paymentIntents.create).toHaveBeenCalledWith({ payment_method_types: ["card"] });
  expect(intent.payment_method_types).toEqual(["card"]);
  expect(mocks.create.mock.calls[0][0]).not.toHaveProperty("payment_method_types");
  expect(mocks.list).toHaveBeenCalledWith({ payment_method_types: ["card"] });
  expect(mocks.create).toHaveBeenCalledWith({ ...base, payment_method_types: ["card"] });
});
"""


def test_jest_expectations_of_the_mocked_client_call_are_updated(tmp_path):
    plan = plan_for(tmp_path, {"src/pay.js": JS_APP, "src/pay.test.js": JS_TEST})
    after = content(plan, "src/pay.test.js")
    assert edited(plan) == {"src/pay.test.js": {10, 11}}
    assert 'expect.objectContaining({ allowed_payment_method_types: ["card"] })' in after
    assert '{ "allowed_payment_method_types": ["card"], amount: 1 }, { idempotencyKey: "k" }' in after
    assert after.splitlines()[11:] == JS_TEST.splitlines()[11:]


def test_a_mock_wired_twice_or_inside_a_function_is_not_tied(tmp_path):
    twice = JS_TEST.replace("      create: mocks.create,\n", "      create: mocks.create,\n    };\n    customers = {\n      create: mocks.create,\n")
    body = JS_TEST.replace("default: class MockStripe {\n    paymentIntents = {", "default: () => {\n    const paymentIntents = {")
    for n, test in enumerate((twice, body)):
        plan = plan_for(tmp_path / str(n), {"src/pay.js": JS_APP, "src/pay.test.js": test})
        assert edited(plan) == {"src/pay.test.js": {line_of(test, "stripe.paymentIntents.create)")}}


PY_APP = "import stripe\n\n\ndef pay():\n    return stripe.PaymentIntent.create(amount=1, payment_method_types=['card'])\n"
PY_TEST = """from unittest.mock import Mock, patch


@patch("app.pay.stripe.Customer.create")
@patch("app.pay.stripe.PaymentIntent.create")
def test_pay(create_intent, create_customer):
    create_intent.assert_called_once_with(amount=1, payment_method_types=["card"])
    create_customer.assert_called_once_with(payment_method_types=["card"])
    assert create_intent.call_args.kwargs["payment_method_types"] == ["card"]
    create_intent.return_value = {"payment_method_types": ["card"]}


def test_with():
    with patch("stripe.PaymentIntent.create") as created:
        pass
    created.assert_called_with(
        amount=1,
        payment_method_types=["card"],
    )


def test_rebound(mocker):
    created = mocker.patch("stripe.PaymentIntent.create")
    created = Mock()
    created.assert_called_with(payment_method_types=["card"])


@patch.multiple("stripe.PaymentIntent", create=None)
def test_unknown(create):
    create.assert_called_with(payment_method_types=["card"])
"""


def test_python_patch_assertions_are_updated(tmp_path):
    plan = plan_for(tmp_path, {"app/pay.py": PY_APP, "tests/test_pay.py": PY_TEST})
    after = content(plan, "tests/test_pay.py")
    assert edited(plan) == {"tests/test_pay.py": {7, 18}}
    assert 'create_intent.assert_called_once_with(amount=1, allowed_payment_method_types=["card"])' in after
    assert '        allowed_payment_method_types=["card"],\n' in after
    for kept in (8, 9, 10, 25, 30):
        assert after.splitlines()[kept - 1] == PY_TEST.splitlines()[kept - 1]


PHP_APP = "<?php\n\\Stripe\\PaymentIntent::create(['payment_method_types' => ['card'], 'amount' => 1]);\n"
PHP_TEST = """<?php
use Stripe\\Service\\PaymentIntentService;

class PayTest extends TestCase
{
    public function testAlias(): void
    {
        $mock = Mockery::mock('alias:Stripe\\PaymentIntent');
        $mock->shouldReceive('create')->once()->with(['payment_method_types' => ['card'], 'amount' => 1]);
    }

    public function testService(): void
    {
        $svc = $this->createMock(PaymentIntentService::class);
        $svc->expects($this->once())->method('create')->with($this->equalTo(['payment_method_types' => ['card']]));
    }

    public function testNever(): void
    {
        $mock = Mockery::mock('alias:Stripe\\PaymentIntent');
        $mock->shouldReceive('create')->never()->with(['payment_method_types' => ['card']]);
    }

    public function testDriver(): void
    {
        $driver = Mockery::mock(StripePaymentDriver::class);
        $driver->shouldReceive('createSetupIntent')->once()->with(['payment_method_types' => ['card']]);
    }

    public function testFixture(): void
    {
        $event = ['data' => ['object' => ['payment_method_types' => ['card']]]];
        $mock = Mockery::mock('alias:Stripe\\PaymentIntent');
        $mock = Mockery::mock('alias:Stripe\\Charge');
        $mock->shouldReceive('create')->once()->with(['payment_method_types' => ['card']]);
    }
}
"""


def test_php_mockery_and_phpunit_expectations_of_the_stripe_call_are_updated(tmp_path):
    plan = plan_for(tmp_path, {"app/Pay.php": PHP_APP, "tests/PayTest.php": PHP_TEST})
    after = content(plan, "tests/PayTest.php")
    assert edited(plan) == {"tests/PayTest.php": {9, 15}}
    assert "->with(['allowed_payment_method_types' => ['card'], 'amount' => 1]);" in after
    assert "->with($this->equalTo(['allowed_payment_method_types' => ['card']]));" in after
    assert after.splitlines()[16:] == PHP_TEST.splitlines()[16:]


def test_every_other_action_is_the_same_edit_the_code_got(tmp_path):
    files = {
        "app/pm.rb": "def detach(id)\n  Stripe::PaymentMethod.update(id, { payto: { pay_id: 'x' }, metadata: { a: '1' } })\nend\n",
        "spec/pm_spec.rb": "it do\n  expect(Stripe::PaymentMethod).to have_received(:update).with('pm_1', hash_including(\n"
                           "    payto: { pay_id: 'x' },\n    metadata: { a: '1' },\n  ))\nend\n",
        "app/fc.js": "const stripe = require('stripe')('sk');\nstripe.financialConnections.sessions.create({\n"
                     "  account_holder: { type: 'customer', customer: 'cus_1' }, permissions: ['balances'],\n"
                     "  filters: { countries: ['US', 'CA'] },\n});\n",
        "app/fc.test.js": "expect(stripe.financialConnections.sessions.create).toHaveBeenCalledWith({\n"
                          "  account_holder: { type: 'customer', customer: 'cus_1' }, permissions: ['balances'],\n"
                          "  filters: { countries: ['US', 'CA'] },\n});\n",
        "app/sub.py": "import stripe\n\n\ndef reset(sid):\n    return stripe.Subscription.modify(sid, billing_cycle_anchor='now')\n",
        "tests/test_sub.py": "from unittest.mock import patch\n\n\n@patch('app.sub.stripe.Subscription.modify')\n"
                             "def test_reset(modify):\n    modify.assert_called_once_with('sub_1', billing_cycle_anchor='now')\n",
    }
    plan = plan_v2(tmp_path, files)
    assert {f.file for f in plan.fixes} == {"app/pm.rb", "app/fc.js", "app/sub.py"}, plan.to_dict()
    assert {t.file: [e.action for e in t.edits] for t in plan.test_fixes} == {
        "spec/pm_spec.rb": ["delete"], "app/fc.test.js": ["rename_first"], "tests/test_sub.py": ["wrap_type"],
    }
    assert "hash_including(\n    metadata: { a: '1' },\n  ))" in content(plan, "spec/pm_spec.rb")
    assert "  filters: { country: 'US' },\n" in content(plan, "app/fc.test.js")
    assert "modify.assert_called_once_with('sub_1', billing_cycle_anchor={'type': 'now'})" in content(plan, "tests/test_sub.py")
    assert ("tests/test_sub.py", 6) in plan.tests  # a wrap_type key is listed where it is edited


def test_finders_ignore_text_in_strings_and_comments():
    text = "s = 'expect(Stripe::PaymentIntent).to have_received(:create).with(a: 1)'\n# expect(Stripe::X).to receive(:create).with(a: 1)\n"
    assert find(text, "ruby") == []
    assert find("// expect(stripe.customers.create).toHaveBeenCalledWith({ a: 1 })\n", "javascript") == []
    assert find("<?php // $m->shouldReceive('create')->with(['a' => 1]);\n", "php") == []
    assert find("def f(:\n", "python") == []  # does not parse: nothing is read
    assert find("anything", "go") == []
