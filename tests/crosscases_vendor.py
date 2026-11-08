from crosscases_php import DRIVER, CALLER, OLD
D = DRIVER.replace("class StripeDriver {", "class StripeDriver extends \\Acme\\Gateway\\AbstractGateway {")
BASE = ("<?php\nnamespace Acme\\Gateway;\nabstract class AbstractGateway {\n"
        "    abstract public function createIntent(array $params);\n"
        f"    public function checkout() {{ return $this->createIntent(['amount' => 1, '{OLD}' => ['card']]); }}\n}}\n")
CASES = [
    ("vendor parent absent (app clone)", {"composer.json": '{"type": "project"}', "Driver.php": D, "Card.php": CALLER}),
    ("vendor parent installed", {"composer.json": '{"type": "project"}', "Driver.php": D, "Card.php": CALLER,
                                 "vendor/acme/gateway/src/AbstractGateway.php": BASE}),
]
