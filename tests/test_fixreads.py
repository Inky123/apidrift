"""The leftover backstop keeps a PHP webhook handler's tested reads of the old key, and nothing else."""

import pytest
from test_fixer import NEW, OLD, changed_lines, plan_for

from apidrift.app.prbody import _kept

HOOKS = (
    "<?php\n"
    "use Stripe\\PaymentIntent;\n"
    "class Hooks {\n"
    "    public function pay() {\n"
    f"        return PaymentIntent::create(['amount' => 1, '{OLD}' => ['card']]);\n"
    "    }\n"
    "    public function handle(array $event): void {\n"
    "        $intent = $event['data']['object'];\n"
    f"        if (!isset($intent['{OLD}']) || !in_array('card', $intent['{OLD}'] ?? [], true)) {{\n"
    "            return;\n"
    "        }\n"
    "        $id = $intent['id'];\n"
    "    }\n"
    "}\n"
)


def test_a_webhook_handlers_tested_reads_are_kept_and_the_request_is_fixed(tmp_path):
    plan = plan_for(tmp_path, {"Hooks.php": HOOKS, "Router.php": "<?php\n$h->handle($request->all());\n"})
    assert plan.unfixable == [] and len(plan.fixes) == 1
    assert list(changed_lines(plan.fixes[0])) == [5] and NEW in plan.fixes[0].content.splitlines()[4]
    assert plan.kept == [("Hooks.php", 9)] and plan.leftovers == []
    assert plan.to_dict()["kept"] == [{"file": "Hooks.php", "line": 9}]
    assert "Kept on purpose (reads of a webhook or response payload, unchanged)" in _kept(plan.to_dict(), "o/r", "s")[1]


REFUSED = [
    ("the read is assigned", "        $id = $intent['id'];\n", f"        $types = $intent['{OLD}'];\n", {}),
    ("the read goes into a request", "        $id = $intent['id'];\n",
     f"        PaymentIntent::update('pi', ['metadata' => ['t' => $intent['{OLD}']]]);\n", {}),
    ("the test result is stored", "        $id = $intent['id'];\n", f"        $ok = isset($intent['{OLD}']);\n", {}),
    ("the payload is written later", "        $id = $intent['id'];\n", "        $intent['id'] = 1;\n", {}),
    ("the payload is passed to a function", "        $id = $intent['id'];\n", "        log($intent);\n", {}),
    ("the payload is not a parameter's", "        $intent = $event['data']['object'];\n", "        $intent = $this->load();\n", {}),
    ("a caller passes a literal with the old key", "", "",
     {"Other.php": f"<?php\n$h->handle(['data' => ['object' => ['{OLD}' => ['card']]]]);\n"}),
    ("a caller passes a local bound to a literal", "", "",
     {"Other.php": "<?php\nfunction f($h) { $e = ['data' => []]; $h->handle($e); }\n"}),
    ("a test passes a literal with the old key", "", "",
     {"tests/HooksTest.php": f"<?php\n$h->handle(['data' => ['object' => ['{OLD}' => []]]]);\n"}),
]


@pytest.mark.parametrize("label,old,new,extra", REFUSED, ids=[r[0] for r in REFUSED])
def test_every_other_read_stays_a_leftover(tmp_path, label, old, new, extra):
    assert old in HOOKS
    plan = plan_for(tmp_path, {"Hooks.php": HOOKS.replace(old, new) if old else HOOKS, **extra})
    assert plan.fixes == [] and [u.why for u in plan.unfixable] == ["leftover_tokens"] and plan.kept == []


def test_a_test_literal_without_the_key_does_not_block(tmp_path):
    test = "<?php\nfunction t($h, $o) { $e = ['data' => ['object' => $o]]; $h->handle($e); }\n"
    plan = plan_for(tmp_path, {"Hooks.php": HOOKS, "tests/HooksTest.php": test})
    assert len(plan.fixes) == 1 and plan.kept == [("Hooks.php", 9)]
