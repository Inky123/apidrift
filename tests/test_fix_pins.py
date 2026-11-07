"""P14a: API-version pins written as a conditional, a fallback or a default report every version they can
pin; strings outside the pin's own statement never do."""

import pytest

from apidrift.fixpins import find_pins, pin_versions


def versions(text: str) -> list[tuple[int, str]]:
    return [(text.count("\n", 0, pos) + 1, v) for pos, v in pin_versions(text)]


@pytest.mark.parametrize("text,want", [
    # gumroad config/initializers/003_stripe.rb:3 (its only server pin)
    ('require "stripe"\n\nStripe.api_version = Rails.env.test? ? "2023-10-16" : "2023-10-16; risk_in_requirements_beta=v1;"\n',
     [(3, "2023-10-16")]),
    ("const s = new Stripe(k, { apiVersion: isTest ? '2023-10-16' : '2024-04-10', timeout: 5 });\n",
     [(1, "2023-10-16"), (1, "2024-04-10")]),
    ("const s = new Stripe(k, {\n  apiVersion: isTest\n    ? '2023-10-16'\n    : '2024-04-10.acacia',\n  timeout: 5,\n});\n",
     [(3, "2023-10-16"), (4, "2024-04-10.acacia")]),
    ("const s = new Stripe(k, { apiVersion: process.env.STRIPE_API_VERSION || '2025-04-30.basil' });\n",
     [(1, "2025-04-30.basil")]),
    ("stripe.api_version = '2023-10-16' if settings.TEST else '2024-06-20'\n", [(1, "2023-10-16"), (1, "2024-06-20")]),
    ('stripe.api_version = os.environ.get("STRIPE_API_VERSION", "2020-08-27")\n', [(1, "2020-08-27")]),
    ("$c = new StripeClient(['api_key' => $k, 'stripe_version' => $live ? '2024-06-20' : '2023-10-16']);\n",
     [(1, "2024-06-20"), (1, "2023-10-16")]),
    ("\\Stripe\\Stripe::setApiVersion($beta ? '2024-06-20; x_beta=v1' : '2024-06-20');\n", [(1, "2024-06-20")]),
    # the forms the old pattern read keep reading the same
    ("apiVersion: '2023-10-16',\n", [(1, "2023-10-16")]),
    ("headers: { 'Stripe-Version': \"2022-11-15\" }\n", [(1, "2022-11-15")]),
    ("Stripe.api_version = '2020-08-27'\n", [(1, "2020-08-27")]),
    ('"api_version" => "2019-12-03",\n', [(1, "2019-12-03")]),
])
def test_conditional_and_default_pins_report_each_version(text, want):
    assert versions(text) == want


@pytest.mark.parametrize("text", [
    "const s = new Stripe(k, { apiVersion: cfg.version, created: '2024-01-01' });\n",
    "if (api_version == '2023-10-16') legacy();\n",
    "if api_version == '2023-10-16'\n",
    "const o = { apiVersion: pick() };\nconst since = '2024-01-01';\n",
    "apiVersion: v\nconst since = '2024-01-01'\n",
    "stripe_api_version = '2023-10-16'\n",  # another name: as before, not a pin
    "apiVersion = latest; other = '2024-01-01'\n",
    "apiVersion: someFn(a)\n  , created: '2024-01-01'\n",
    "apiVersion: x // since '2024-01-01'\n",
    "apiVersion: 'v2', released: '2024-01-01'\n",
    "apiVersion: 'not 2024-01-01'\n",
])
def test_strings_outside_the_pin_statement_are_not_pins(text):
    assert versions(text) == []


def test_find_pins_reports_the_gumroad_server_pin(tmp_path):
    (tmp_path / "Gemfile").write_text("gem 'stripe'\n")
    init = tmp_path / "config" / "initializers"
    init.mkdir(parents=True)
    (init / "003_stripe.rb").write_text(
        "# frozen_string_literal: true\n\n"
        'Stripe.api_version = Rails.env.test? ? "2023-10-16" : "2023-10-16; risk_in_requirements_beta=v1;"\n'
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "charge.rb").write_text("Stripe::PaymentIntent.create(amount: 1)\n")
    pins = find_pins(tmp_path, ["app/charge.rb"])
    assert [(p.file, p.line, p.version) for p in pins] == [("config/initializers/003_stripe.rb", 3, "2023-10-16")]
