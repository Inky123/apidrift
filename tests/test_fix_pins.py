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
    assert [(p.file, p.line, p.version, p.kind) for p in pins] == [
        ("config/initializers/003_stripe.rb", 3, "2023-10-16", "server")]


@pytest.mark.parametrize("rel,text,kind", [
    ("spec/models/charge_spec.rb", "Stripe.api_version = '2023-10-16'\n", "test"),
    ("app/x.test.ts", "const s = new Stripe(k, { apiVersion: '2023-10-16' });\n", "test"),
    ("test/fixtures/x.py", "stripe.api_version = '2023-10-16'\n", "test"),
    ("e2e/setup.js", "const s = new Stripe(k, { apiVersion: '2023-10-16' });\n", "test"),
    # browser Stripe.js: loadStripe always; the global Stripe() only where the server SDK is never imported
    ("web/pay.tsx", "const p = loadStripe(pk, {\n  stripeAccount: a,\n  apiVersion: '2023-10-16',\n});\n", "browser"),
    ("client/card.js", "const stripe = Stripe(publishableKey, { apiVersion: '2020-08-27' });\n", "browser"),
    ("client/konbini.js", "const stripe = Stripe(pk, {\n  betas: ['b'],\n  apiVersion: '2020-08-27; konbini_beta=v1',\n});\n",
     "browser"),
    # invoiceninja stripe-browserpay.js: a bare Stripe() alone is not evidence; its public/ copy or Stripe.js is
    ("resources/js/pay.js", "let config = {};\nconfig.apiVersion = '2020-08-27';\nthis.stripe = Stripe(key, config);\n",
     "server"),
    ("public/js/pay.js", "let config = {};\nconfig.apiVersion = '2020-08-27';\nthis.stripe = Stripe(key, config);\n",
     "browser"),
    ("resources/js/pay2.js", "// <script src=\"https://js.stripe.com/v3/\">\nconfig.apiVersion = '2020-08-27';\n", "browser"),
    ("static/x.js", "// new Stripe( in a comment is not code\nconst s = Stripe(pk, { apiVersion: '2020-08-27' });\n",
     "browser"),
    ("web/y.tsx", "const s = '<loadStripe(pk, {'; const o = { apiVersion: '2020-08-27' };\n", "server"),  # in a string
    # the server SDK, imported in any form (review item 4)
    ("server/api.js", "const { default: Stripe } = await import('stripe');\n"
     "const stripe = Stripe(process.env.SK, { apiVersion: '2020-08-27' });\n", "server"),
    ("supabase/functions/x/index.ts", "import Stripe from 'npm:stripe@14';\n"
     "export const stripe = Stripe(Deno.env.get('SK')!, { apiVersion: '2023-10-16' });\n", "server"),
    ("public/fn/index.ts", "import Stripe from 'https://esm.sh/stripe@14?target=deno';\n"
     "const stripe = Stripe(key, { apiVersion: '2023-10-16' });\n", "server"),
    ("client/stripe.js", "const stripeLib = require('stripe');\nconst stripe = stripeLib(key, { apiVersion: '2020-08-27' });\n",
     "server"),
    ("config/stripe.js", "export const STRIPE = { apiVersion: '2020-08-27' };\n// used by Stripe(...) in client\n", "server"),
    ("lib/stripe.js", "const cfg = { apiVersion: '2020-08-27' }\nmodule.exports = (k) => require(\"stripe\")(k, cfg)\n",
     "server"),
    ("client/n.js", "import S from 'stripe/lib/stripe.js';\nconst s = S(k, { apiVersion: '2020-08-27' });\n", "server"),
    ("client/o.js", "import { loadStripe } from '@stripe/stripe-js';\nloadStripe(pk, { apiVersion: '2020-08-27' });\n",
     "browser"),
    # the server SDK
    ("server/g.js", "let config = {};\nconfig.apiVersion = '2020-08-27';\nconst s = new Stripe(key, config);\n", "server"),
    ("server/h.js", "const S = require('stripe');\nconfig.apiVersion = '2020-08-27';\nconst s = Stripe(key);\n", "server"),
    ("server/a.js", "const stripe = require('stripe')(key, { apiVersion: '2023-10-16' });\n", "server"),
    ("server/b.ts", "import Stripe from 'stripe';\nconst s = new Stripe(key, { apiVersion: '2023-10-16' });\n", "server"),
    ("server/c.ts", "import Stripe from 'stripe';\nconst s = Stripe(key, { apiVersion: '2023-10-16' });\n", "server"),
    ("server/d.js", "const s = new Stripe(key, { apiVersion: '2023-10-16' });\n", "server"),
    ("server/e.js", "const p = loadStripe(pk);\nconst s = new Stripe(key, { apiVersion: '2023-10-16' });\n", "server"),
    ("server/f.py", "stripe.api_version = '2023-10-16'  # loadStripe(\n", "server"),
    ("app/stripe_loader.rb", "Stripe.api_version = '2023-10-16'\n", "server"),
])
def test_pin_kind(tmp_path, rel, text, kind):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("// stripe\n" + text if not rel.endswith((".py", ".rb")) else "# stripe\n" + text)
    (tmp_path / "package.json").write_text("{}")
    pins = find_pins(tmp_path, [rel])
    assert [p.kind for p in pins if p.file == rel] == [kind]


def test_a_file_is_read_for_kinds_once_however_many_pins_it_has(tmp_path, monkeypatch):
    """Review item 10: the browser evidence used to be re-read per pin."""
    from apidrift import fixpins

    made = []
    real = fixpins.Kinds
    monkeypatch.setattr(fixpins, "Kinds", lambda rel, text: made.append(rel) or real(rel, text))
    body = "loadStripe(pk, {\n" + "".join(f"  apiVersion: '2020-08-{i % 28 + 1:02d}',\n" for i in range(80)) + "});\n"
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "a.js").write_text("// stripe\n" + body + "x = 1;\n" * 20000)
    pins = find_pins(tmp_path, ["a.js"])
    assert len(pins) == 80 and {p.kind for p in pins} == {"browser"} and made == ["a.js"]
