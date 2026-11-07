"""PHP wrappers followed across files: a caller in another file becomes a call site, and is fixed at its
own literal, only when the wrapper's name and the caller's receiver resolve to the one definition."""

import pytest
from test_fixer import NEW, OLD, changed_lines, plan_for
from test_helpers import scan

DRIVER = (
    "<?php\n"
    "namespace App\\Pay;\n"
    "use Stripe\\PaymentIntent;\n"
    "class StripeDriver {\n"
    "    public function createIntent(array $params) {\n"
    "        return PaymentIntent::create($params, ['idempotency_key' => 'k']);\n"
    "    }\n"
    "}\n"
)
CALLER = (
    "<?php\n"
    "namespace App\\Pay\\Methods;\n"
    "use App\\Pay\\StripeDriver;\n"
    "class Card {\n"
    "    public $driver;\n"
    "    public function __construct(StripeDriver $driver) {\n"
    "        $this->driver = $driver;\n"
    "    }\n"
    "    public function pay() {\n"
    "        return $this->driver->createIntent([\n"
    "            'amount' => 100,\n"
    f"            '{OLD}' => ['card'],\n"
    "        ]);\n"
    "    }\n"
    "}\n"
)
THROUGH = "PaymentIntent::create [through createIntent, Driver.php:6]"


def sites(tmp_path, files):
    return sorted((u.file, u.line, u.call, tuple(sorted(u.fields)), u.complete) for u in scan(tmp_path, files))


def test_a_caller_in_another_file_becomes_a_call_site_and_the_wrapper_is_dropped(tmp_path):
    assert sites(tmp_path, {"Driver.php": DRIVER, "Card.php": CALLER}) == [
        ("Card.php", 10, THROUGH, ("amount", OLD), True),
    ]


def test_the_caller_is_fixed_at_its_own_literal(tmp_path):
    plan = plan_for(tmp_path, {"Driver.php": DRIVER, "Card.php": CALLER})
    assert plan.unfixable == [] and plan.not_attempted == [] and [f.file for f in plan.fixes] == ["Card.php"]
    assert changed_lines(plan.fixes[0]) == {12: (f"            '{OLD}' => ['card'],", f"            '{NEW}' => ['card'],")}


LOCAL = CALLER.replace(
    "        return $this->driver->createIntent([\n            'amount' => 100,\n"
    f"            '{OLD}' => ['card'],\n        ]);\n",
    f"        $p = ['amount' => 100, '{OLD}' => ['card']];\n        return $this->driver->createIntent($p);\n",
)


def test_a_local_bound_to_one_literal_is_followed_and_fixed(tmp_path):
    plan = plan_for(tmp_path, {"Driver.php": DRIVER, "Card.php": LOCAL})
    assert plan.unfixable == [] and plan.not_attempted == []
    assert list(changed_lines(plan.fixes[0])) == [10]


VISIBLE_HIDDEN = [
    ("the caller passes a variable from elsewhere", "Card.php", "createIntent([", "createIntent($this->opts() + ["),
    ("the caller passes its own parameter", "Card.php", "public function pay() {\n        return $this->driver->createIntent([",
     "public function pay(array $p) {\n        $q = $p;\n        return $this->driver->createIntent($p + ["),
    ("an untyped receiver", "Card.php", "__construct(StripeDriver $driver)", "__construct($driver)"),
    ("a receiver of another type", "Card.php", "__construct(StripeDriver $driver)", "__construct(OtherDriver $driver)"),
    ("the property is assigned from outside", "Card.php", "class Card {", "function f($c) { $c->driver = new X(); }\nclass Card {"),
    ("the wrapper's parameter is also used", "Driver.php", "        return PaymentIntent::create($params,",
     "        log($params);\n        return PaymentIntent::create($params,"),
    ("the wrapper's parameter is interpolated", "Driver.php", "        return PaymentIntent::create($params,",
     "        log(\"p {$this->dump($params)}\");\n        return PaymentIntent::create($params,"),
]


@pytest.mark.parametrize("label,file,old,new", VISIBLE_HIDDEN, ids=[v[0] for v in VISIBLE_HIDDEN])
def test_a_caller_that_is_not_provably_visible_is_not_a_site_and_the_wrapper_stays(tmp_path, label, file, old, new):
    files = {"Driver.php": DRIVER, "Card.php": CALLER}
    assert old in files[file]
    files[file] = files[file].replace(old, new, 1)
    found = sites(tmp_path, files)
    assert not any("[through " in s[2] for s in found), found
    assert [(s[0], s[2], s[4]) for s in found if s[0] == "Driver.php"] == [("Driver.php", "PaymentIntent::create", False)]
    plan = plan_for(tmp_path, files)
    assert plan.fixes == [] and [u.file for u in plan.not_attempted] == ["Driver.php"]


def test_a_use_that_is_not_a_visible_call_keeps_the_wrapper_but_not_the_visible_caller(tmp_path):
    """A string naming the method may be a dynamic call: the wrapper's own site stays possible."""
    card = CALLER.replace("class Card {", "$f = 'createIntent';\nclass Card {")
    found = sites(tmp_path, {"Driver.php": DRIVER, "Card.php": card})
    assert [(s[0], s[2]) for s in found] == [("Card.php", THROUGH), ("Driver.php", "PaymentIntent::create")]
    plan = plan_for(tmp_path, {"Driver.php": DRIVER, "Card.php": card})
    assert [f.file for f in plan.fixes] == ["Card.php"] and [u.file for u in plan.not_attempted] == ["Driver.php"]


AMBIGUOUS = [
    ("the name is defined twice", {"Other.php": "<?php\nclass Other {\n    public function createIntent($x) { return 1; }\n}\n"}),
    ("a test defines the name", {"tests/FakeDriver.php": "<?php\nclass Fake {\n    function createintent($p) {}\n}\n"}),
    ("a vendored file defines the name", {"vendor/acme/Lib.php": "<?php\nclass Lib {\n    function createIntent($p) {}\n}\n"}),
    ("the repository is a library", {"composer.json": '{"name": "acme/pay"}'}),
]


@pytest.mark.parametrize("label,extra", AMBIGUOUS, ids=[a[0] for a in AMBIGUOUS])
def test_an_ambiguous_name_is_not_followed(tmp_path, label, extra):
    found = sites(tmp_path, {"Driver.php": DRIVER, "Card.php": CALLER, **extra})
    assert not any("[through " in s[2] for s in found)
    assert ("Driver.php", 6, "PaymentIntent::create", (), False) in found


def test_a_caller_in_a_test_file_is_ignored_and_does_not_block(tmp_path):
    test = CALLER.replace("class Card", "class CardTest")
    found = sites(tmp_path, {"Driver.php": DRIVER, "Card.php": CALLER, "tests/CardTest.php": test})
    assert [s[:2] for s in found] == [("Card.php", 10)]


def test_a_caller_in_a_vendored_dir_is_not_a_site(tmp_path):
    found = sites(tmp_path, {"Driver.php": DRIVER, "vendor/x/Card.php": CALLER})
    assert [s[:3] for s in found] == [("Driver.php", 6, "PaymentIntent::create")]


def test_a_method_of_the_same_name_on_another_class_blocks_the_fix(tmp_path):
    other = "<?php\nnamespace App;\nclass Mailer {\n    public function createIntent(array $a) { return $a; }\n}\n"
    plan = plan_for(tmp_path, {"Driver.php": DRIVER, "Card.php": CALLER, "Mailer.php": other})
    assert plan.fixes == [] and [(u.file, u.line) for u in plan.not_attempted] == [("Driver.php", 6)]


def test_a_shared_local_is_refused(tmp_path):
    shared = LOCAL.replace("        return $this->driver->createIntent($p);\n",
                           "        $this->audit($p);\n        return $this->driver->createIntent($p);\n")
    plan = plan_for(tmp_path, {"Driver.php": DRIVER, "Card.php": shared})
    assert plan.fixes == []


def test_a_typed_property_and_a_typed_parameter_are_receivers(tmp_path):
    typed = CALLER.replace("    public $driver;\n", "    protected StripeDriver $driver;\n")
    param = CALLER.replace("    public function pay() {\n        return $this->driver->",
                           "    public function pay(StripeDriver $d) {\n        return $d->")
    for text in (typed, param):
        found = sites(tmp_path, {"Driver.php": DRIVER, "Card.php": text})
        assert [s[2] for s in found] == [THROUGH], found


def test_recursion_and_a_same_file_caller_still_work(tmp_path):
    driver = DRIVER.replace(
        "    public function createIntent(",
        f"    public function setup() {{\n        return $this->createIntent(['{OLD}' => ['card']]);\n    }}\n"
        "    public function createIntent(",
    )
    found = sites(tmp_path, {"Driver.php": driver, "Card.php": CALLER})
    assert [s[:3] for s in found] == [
        ("Card.php", 10, "PaymentIntent::create [through createIntent, Driver.php:9]"),
        ("Driver.php", 6, "PaymentIntent::create [through createIntent, line 9]"),
    ]
    plan = plan_for(tmp_path, {"Driver.php": driver, "Card.php": CALLER})
    assert sorted(f.file for f in plan.fixes) == ["Card.php", "Driver.php"] and plan.not_attempted == []


WRITES = CALLER.replace(
    "        return $this->driver->createIntent([\n            'amount' => 100,\n"
    f"            '{OLD}' => ['card'],\n        ]);\n",
    "        $p = ['amount' => 100];\n"
    f"        if ($this->sepa) {{\n            $p['{OLD}'] = ['sepa_debit'];\n        }}\n"
    "        $r = $this->driver->createIntent($p);\n"
    "        $label = $p['amount'];\n"
    "        try { } catch (\\Exception $e) { $p = ['status' => 1]; log($p); }\n"
    "        return $r;\n",
)


def test_a_key_the_caller_writes_by_statement_is_renamed_there(tmp_path):
    """Read as for a direct call; a later read of another key and a use of a rebound local do not block."""
    plan = plan_for(tmp_path, {"Driver.php": DRIVER, "Card.php": WRITES})
    assert plan.unfixable == [] and plan.not_attempted == []
    assert changed_lines(plan.fixes[0]) == {
        12: (f"            $p['{OLD}'] = ['sepa_debit'];", f"            $p['{NEW}'] = ['sepa_debit'];")
    }


@pytest.mark.parametrize("label,old,new", [
    ("a read of the key itself", "$label = $p['amount'];", f"$label = $p['{OLD}'];"),
    ("the array passed on", "$label = $p['amount'];", "$label = json_encode($p);"),
])
def test_a_later_use_the_literal_reaches_refuses(tmp_path, label, old, new):
    plan = plan_for(tmp_path, {"Driver.php": DRIVER, "Card.php": WRITES.replace(old, new)})
    assert plan.fixes == [] and [u.why for u in plan.unfixable] == ["helper_shared"]


def test_a_merge_into_the_callers_local_is_not_visible(tmp_path):
    card = WRITES.replace("        $r = $this->driver", "        $p += $this->defaults();\n        $r = $this->driver")
    found = sites(tmp_path, {"Driver.php": DRIVER, "Card.php": card})
    assert not any("[through " in s[2] for s in found)


def test_a_generic_method_name_is_never_followed(tmp_path):
    """GiveWP: ``$gateway->payment_intent->create($args)`` / ``give(PaymentIntent::class)->create($args)``."""
    driver = DRIVER.replace("createIntent(", "create(")
    card = CALLER.replace("->createIntent(", "->create(").replace(
        "    public function pay() {", "    public function other() { return give(StripeDriver::class)->create(['x' => 1]); }\n"
        "    public function pay() {")
    found = sites(tmp_path, {"Driver.php": driver, "Card.php": card})
    assert not any("[through " in s[2] for s in found)
    plan = plan_for(tmp_path, {"Driver.php": driver, "Card.php": card})
    assert plan.fixes == [] and [u.file for u in plan.not_attempted] == ["Driver.php"]
