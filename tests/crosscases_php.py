OLD = "payment_method_types"

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
TYPED = CALLER.replace("    public $driver;\n", "    private StripeDriver $driver;\n")
BASE = {"Driver.php": DRIVER, "Card.php": CALLER}


def local(extra_after="", extra_before=""):
    return TYPED.replace(
        "        return $this->driver->createIntent([\n            'amount' => 100,\n"
        f"            '{OLD}' => ['card'],\n        ]);\n",
        f"        $p = ['amount' => 100, '{OLD}' => ['card']];\n{extra_before}"
        f"        $r = $this->driver->createIntent($p);\n{extra_after}        return $r;\n",
    )


CASES = [
    ("inc file caller", {**BASE, "legacy/checkout.inc":
        "<?php\nuse App\\Pay\\StripeDriver;\nfunction legacy_pay(StripeDriver $d) {\n"
        f"    return $d->createIntent(['amount' => 5, '{OLD}' => ['card']]);\n}}\n"}),
    ("attribute listener", {
        "Driver.php": DRIVER.replace("    public function createIntent", "    #[AsEventListener(event: IntentRequested::class)]\n    public function createIntent"),
        "Card.php": CALLER}),
    ("dynamic method name", {**BASE, "Admin.php":
        "<?php\nnamespace App\\Pay;\nclass Admin {\n    public function __construct(private StripeDriver $d) {}\n"
        f"    public function run($op) {{ $m = $op . 'Intent'; return $this->d->$m(['amount' => 1, '{OLD}' => ['card']]); }}\n}}\n"}),
    ("destructuring writes public property", {**BASE, "Swap.php":
        "<?php\nnamespace App;\nfunction swap($card) { [$card->driver] = [new \\App\\Legacy\\Driver()]; }\n"}),
    ("closure bind writes property", {**BASE, "Swap.php":
        "<?php\nnamespace App;\nfunction swap($card) { \\Closure::bind(function () { $this->driver = new \\App\\Legacy\\Driver(); }, $card, \\App\\Pay\\Methods\\Card::class)(); }\n"}),
    ("brace property write", {**BASE, "Swap.php":
        "<?php\nnamespace App;\nfunction swap($card) { $card->{'driver'} = new \\App\\Legacy\\Driver(); }\n"}),
    ("local passed to logger", {"Driver.php": DRIVER, "Card.php": local("        $this->logger->info('x', $p);\n")}),
    ("local captured by-ref closure", {"Driver.php": DRIVER, "Card.php": local(extra_before="        $f = function () use (&$p) { $p['metadata'] = []; };\n")}),
    ("local by-ref fn before", {"Driver.php": DRIVER, "Card.php": local(extra_before="        normalize($p);\n")}),
    ("local alias by ref", {"Driver.php": DRIVER, "Card.php": local(extra_before="        $q = &$p;\n        $q['amount'] = 3;\n")}),
    ("local read other key after", {"Driver.php": DRIVER, "Card.php": local("        echo $p['amount'];\n")}),
    ("local read old key after", {"Driver.php": DRIVER, "Card.php": local(f"        echo $p['{OLD}'][0];\n")}),
    ("local json_encode", {"Driver.php": DRIVER, "Card.php": local("        $this->audit(json_encode($p));\n")}),
    ("local compact", {"Driver.php": DRIVER, "Card.php": local("        return view('x', compact('p'));\n")}),
    ("local via $$", {"Driver.php": DRIVER, "Card.php": local("        $n = 'p'; log($$n);\n")}),
    ("no composer wp plugin", {**BASE, "plugin.php": "<?php\n/*\nPlugin Name: Pay\n*/\n"}),
    ("wrapper in global ns, caller namespaced", {
        "Driver.php": DRIVER.replace("namespace App\\Pay;\n", ""),
        "Card.php": CALLER.replace("use App\\Pay\\StripeDriver;", "use StripeDriver;")}),
    ("static self call from subclass of caller", {**BASE}),
    ("__call proxy", {**BASE, "Proxy.php":
        "<?php\nnamespace App\\Pay;\nclass Proxy {\n    public function __construct(private StripeDriver $d) {}\n"
        "    public function __call($n, $a) { return $this->d->$n(...$a); }\n}\n"
        "class Use1 {\n    public function __construct(private Proxy $p) {}\n"
        f"    public function go() {{ return $this->p->createIntent(['amount' => 1, '{OLD}' => ['c']]); }}\n}}\n"}),
    ("caller with named arg", {**BASE, "N.php":
        "<?php\nnamespace App\\Pay;\nclass N {\n    public function __construct(private StripeDriver $d) {}\n"
        f"    public function go() {{ return $this->d->createIntent(params: ['amount' => 1, '{OLD}' => ['c']]); }}\n}}\n"}),
    ("caller in heredoc-heavy file", {**BASE}),
]
