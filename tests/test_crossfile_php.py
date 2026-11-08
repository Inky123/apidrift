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
    "    private StripeDriver $driver;\n"
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


WRAPPER = ("Driver.php", 6, "PaymentIntent::create", (), False)  # its own site: always kept, possible


def test_a_caller_in_another_file_becomes_a_call_site_and_the_wrapper_stays(tmp_path):
    """Every use found is a visible caller, yet the wrapper's own site stays: a framework, a parent class
    outside the checkout or a file the walk does not read may call it too."""
    assert sites(tmp_path, {"Driver.php": DRIVER, "Card.php": CALLER}) == [
        ("Card.php", 10, THROUGH, ("amount", OLD), True), WRAPPER,
    ]


def test_the_caller_is_fixed_at_its_own_literal(tmp_path):
    plan = plan_for(tmp_path, {"Driver.php": DRIVER, "Card.php": CALLER})
    assert plan.unfixable == [] and [(u.file, u.line) for u in plan.not_attempted] == [("Driver.php", 6)]
    assert [f.file for f in plan.fixes] == ["Card.php"]
    assert changed_lines(plan.fixes[0]) == {12: (f"            '{OLD}' => ['card'],", f"            '{NEW}' => ['card'],")}


LOCAL = CALLER.replace(
    "        return $this->driver->createIntent([\n            'amount' => 100,\n"
    f"            '{OLD}' => ['card'],\n        ]);\n",
    f"        $p = ['amount' => 100, '{OLD}' => ['card']];\n        return $this->driver->createIntent($p);\n",
)


def test_a_local_bound_to_one_literal_is_followed_and_fixed(tmp_path):
    plan = plan_for(tmp_path, {"Driver.php": DRIVER, "Card.php": LOCAL})
    assert plan.unfixable == [] and [u.file for u in plan.not_attempted] == ["Driver.php"]
    assert list(changed_lines(plan.fixes[0])) == [10]


VISIBLE_HIDDEN = [
    ("the caller passes a variable from elsewhere", "Card.php", "createIntent([", "createIntent($this->opts() + ["),
    ("the caller passes its own parameter", "Card.php", "public function pay() {\n        return $this->driver->createIntent([",
     "public function pay(array $p) {\n        $q = $p;\n        return $this->driver->createIntent($p + ["),
    ("a receiver of another type", "Card.php", "private StripeDriver $driver;", "private OtherDriver $driver;"),
    ("an untyped property, even assigned only from a typed parameter", "Card.php", "private StripeDriver $driver;",
     "public $driver;"),
    ("an untyped private property", "Card.php", "private StripeDriver $driver;", "private $driver;"),
    ("a property the class does not declare", "Card.php", "    private StripeDriver $driver;\n", ""),
    ("a property declared twice (a nested class)", "Card.php", "    public function pay() {",
     "    public function f() { return new class { public Other $driver; }; }\n    public function pay() {"),
    ("a call inside a closure, whose $this can be rebound", "Card.php",
     "        return $this->driver->createIntent([", "        return (function () { return $this->driver->createIntent(["),
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
    if "closure" in label:
        files[file] = files[file].replace("        ]);\n    }\n}\n", "        ]); })();\n    }\n}\n")
    found = sites(tmp_path, files)
    assert not any("[through " in s[2] for s in found), found
    assert [(s[0], s[2], s[4]) for s in found if s[0] == "Driver.php"] == [("Driver.php", "PaymentIntent::create", False)]
    plan = plan_for(tmp_path, files)
    assert plan.fixes == [] and [u.file for u in plan.not_attempted] == ["Driver.php"]


def test_a_use_that_is_not_a_visible_call_keeps_the_wrapper_and_the_visible_caller(tmp_path):
    """A string naming the method may be a dynamic call: the wrapper's own site stays possible."""
    card = CALLER.replace("class Card {", "$f = 'createIntent';\nclass Card {")
    found = sites(tmp_path, {"Driver.php": DRIVER, "Card.php": card})
    assert [(s[0], s[2]) for s in found] == [("Card.php", THROUGH), ("Driver.php", "PaymentIntent::create")]
    plan = plan_for(tmp_path, {"Driver.php": DRIVER, "Card.php": card})
    assert [f.file for f in plan.fixes] == ["Card.php"] and [u.file for u in plan.not_attempted] == ["Driver.php"]


AMBIGUOUS = [
    ("the name is defined twice", {"Other.php": "<?php\nclass Other {\n    public function createIntent($x) { return 1; }\n}\n"}),
    ("a test defines the name", {"tests/FakeDriver.php": "<?php\nclass Fake {\n    function createintent($p) {}\n}\n"}),
    ("a .inc file defines the name", {"legacy/pay.inc": "<?php\nfunction createIntent($p) { return $p; }\n"}),
    ("a Drupal .module file defines the name", {"pay.module": "<?php\nfunction createintent($p) { return $p; }\n"}),
    ("the repository is a library", {"composer.json": '{"name": "acme/pay"}'}),
    ("the repository is a WordPress plugin", {"composer.json": '{"name": "acme/pay", "type": "wordpress-plugin"}'}),
    ("a WordPress plugin header, no composer.json", {"pay.php": "<?php\n/*\n * Plugin Name: Pay\n */\n"}),
    ("an unreadable composer.json", {"composer.json": "{not json"}),
]


@pytest.mark.parametrize("label,extra", AMBIGUOUS, ids=[a[0] for a in AMBIGUOUS])
def test_an_ambiguous_name_is_not_followed(tmp_path, label, extra):
    found = sites(tmp_path, {"Driver.php": DRIVER, "Card.php": CALLER, **extra})
    assert not any("[through " in s[2] for s in found)
    assert ("Driver.php", 6, "PaymentIntent::create", (), False) in found


def test_a_wrapper_in_a_nested_library_package_is_not_followed_but_one_in_the_application_is(tmp_path):
    """A monorepo: the root is an application, ``packages/pay`` a library others install."""
    nested = {"composer.json": '{"type": "project"}', "packages/pay/composer.json": '{"name": "acme/pay"}',
              "packages/pay/Driver.php": DRIVER, "Card.php": CALLER}
    assert not any("[through " in s[2] for s in sites(tmp_path / "a", nested))
    app = {**nested, "packages/pay/composer.json": '{"name": "acme/app", "type": "project"}'}
    assert any("[through " in s[2] for s in sites(tmp_path / "b", app))


def test_a_caller_in_a_test_file_is_ignored_and_does_not_block(tmp_path):
    test = CALLER.replace("class Card", "class CardTest")
    found = sites(tmp_path, {"Driver.php": DRIVER, "Card.php": CALLER, "tests/CardTest.php": test})
    assert [s[:2] for s in found] == [("Card.php", 10), ("Driver.php", 6)]


def test_vendored_files_are_never_read_so_every_machine_finds_the_same(tmp_path):
    """Neither a caller there nor a definition there counts: the App's fresh clone has no vendor/."""
    found = sites(tmp_path / "a", {"Driver.php": DRIVER, "vendor/x/Card.php": CALLER})
    assert [s[:3] for s in found] == [("Driver.php", 6, "PaymentIntent::create")]
    lib = "<?php\nclass Lib {\n    function createIntent($p) {}\n}\n"
    plain = sites(tmp_path / "b", {"Driver.php": DRIVER, "Card.php": CALLER})
    assert sites(tmp_path / "c", {"Driver.php": DRIVER, "Card.php": CALLER, "vendor/acme/Lib.php": lib}) == plain


def test_a_method_of_the_same_name_on_another_class_blocks_the_fix(tmp_path):
    other = "<?php\nnamespace App;\nclass Mailer {\n    public function createIntent(array $a) { return $a; }\n}\n"
    plan = plan_for(tmp_path, {"Driver.php": DRIVER, "Card.php": CALLER, "Mailer.php": other})
    assert plan.fixes == [] and [(u.file, u.line) for u in plan.not_attempted] == [("Driver.php", 6)]


@pytest.mark.parametrize("label,after", [
    ("a variable variable", "        $n = 'p'; log($$n);\n"),
    ("a ${} variable", "        log(${'p'});\n"),
    ("an include, which shares the scope", "        include 'audit.php';\n"),
    ("compact", "        return view('x', compact('p'));\n"),
])
def test_a_local_read_by_computed_name_anywhere_in_the_function_refuses(tmp_path, label, after):
    card = LOCAL.replace("        return $this->driver->createIntent($p);\n",
                         f"        $r = $this->driver->createIntent($p);\n{after}        return $r;\n")
    plan = plan_for(tmp_path, {"Driver.php": DRIVER, "Card.php": card})
    assert plan.fixes == []


def test_a_shared_local_is_refused(tmp_path):
    shared = LOCAL.replace("        return $this->driver->createIntent($p);\n",
                           "        $this->audit($p);\n        return $this->driver->createIntent($p);\n")
    plan = plan_for(tmp_path, {"Driver.php": DRIVER, "Card.php": shared})
    assert plan.fixes == []


def test_a_typed_property_and_a_typed_parameter_are_receivers(tmp_path):
    typed = CALLER.replace("    private StripeDriver $driver;\n", "    protected ?StripeDriver $driver;\n")
    param = CALLER.replace("    public function pay() {\n        return $this->driver->",
                           "    public function pay(StripeDriver $d) {\n        return $d->")
    promoted = CALLER.replace("    private StripeDriver $driver;\n", "").replace(
        "    public function __construct(StripeDriver $driver) {\n        $this->driver = $driver;\n    }\n",
        "    public function __construct(private readonly StripeDriver $driver) {}\n")
    for k, text in enumerate((typed, param, promoted)):
        found = sites(tmp_path / str(k), {"Driver.php": DRIVER, "Card.php": text})
        assert [s[2] for s in found] == [THROUGH, "PaymentIntent::create"], found


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
        ("Driver.php", 9, "PaymentIntent::create"),
    ]
    plan = plan_for(tmp_path, {"Driver.php": driver, "Card.php": CALLER})
    assert sorted(f.file for f in plan.fixes) == ["Card.php", "Driver.php"]
    assert [(u.file, u.line) for u in plan.not_attempted] == [("Driver.php", 9)]


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
    assert plan.unfixable == [] and [u.file for u in plan.not_attempted] == ["Driver.php"]
    assert changed_lines(plan.fixes[0]) == {
        12: (f"            $p['{OLD}'] = ['sepa_debit'];", f"            $p['{NEW}'] = ['sepa_debit'];")
    }


@pytest.mark.parametrize("label,old,new", [
    ("a read of the key itself", "$label = $p['amount'];", f"$label = $p['{OLD}'];"),
    ("the array passed on", "$label = $p['amount'];", "$label = json_encode($p);"),
])
def test_a_later_use_the_literal_reaches_refuses(tmp_path, label, old, new):
    plan = plan_for(tmp_path, {"Driver.php": DRIVER, "Card.php": WRITES.replace(old, new)})
    assert plan.fixes == [] and [u.why for u in plan.unfixable] == ["shared_literal"]


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
