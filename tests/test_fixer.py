import json
import shutil
from pathlib import Path

import pytest

from apidrift.cli import main
from apidrift.diff import BREAKING, diff_chain
from apidrift.fixer import FixPlan, blob_sha, plan_fixes
from apidrift.impact import assess
from apidrift.openapi import load_spec
from apidrift.report import filter_changes
from apidrift.scanner import scan_repo
from apidrift.sdkmap import SdkIndex
from specs import _form, _op, sdk_doc

FIXTURES = Path(__file__).parent / "fixtures" / "fixer"
OLD, NEW = "payment_method_types", "allowed_payment_method_types"


def docs():
    """Old and new specs: payment_intents (in the migration table) and charges (not) both lose the field."""
    old = sdk_doc()
    props = {"amount": {"type": "integer"}, "currency": {"type": "string"}, OLD: {"type": "array"}, NEW: {"type": "array"}}
    old["paths"]["/v1/payment_intents"] = {"post": {"requestBody": _form(dict(props)), "responses": {"200": {}}}}
    old["components"]["schemas"]["payment_intent"] = {
        "type": "object", "properties": {"id": {"type": "string"}},
        "x-stripeResource": {"class_name": "PaymentIntent", "in_package": ""},
        "x-stripeOperations": [_op("create", "create", "post", "/v1/payment_intents")],
    }
    charges = old["paths"]["/v1/charges"]["post"]["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]
    charges["properties"][OLD] = {"type": "array"}
    new = json.loads(json.dumps(old))
    new["info"]["version"] = "2026-02-01.test"
    new["paths"]["/v1/payment_intents"]["post"]["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]["properties"].pop(OLD)
    new["paths"]["/v1/charges"]["post"]["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]["properties"].pop(OLD)
    return old, new


def plan_for(root: Path, files: dict[str, str | bytes] | None = None) -> FixPlan:
    for name, text in (files or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    old, new = (load_spec(d) for d in docs())
    index = SdkIndex.build(old, new)
    changes = filter_changes(diff_chain([old, new])[0], min_severity=BREAKING)
    result = scan_repo(root, index)
    return plan_fixes(result, assess(result.usages, changes), index, root)


def changed_lines(fix) -> dict[int, tuple[str, str]]:
    before, after = fix.original.splitlines(), fix.content.splitlines()
    assert len(before) == len(after)
    return {n: (a, b) for n, (a, b) in enumerate(zip(before, after), start=1) if a != b}


def only_fix(plan: FixPlan):
    assert plan.unfixable == [] and len(plan.fixes) == 1, plan.to_dict()
    return plan.fixes[0]


# --- the five Stripe sample servers -------------------------------------------------

SAMPLES = {
    "route.ts": (111, [55, 63]), "server.ts": (145, [104, 112]), "server.js": (151, [96, 104]),
    "server.py": (118, [89, 98]), "server.rb": (82, [58]),
}


@pytest.fixture(scope="module")
def sample_plan(tmp_path_factory):
    root = tmp_path_factory.mktemp("samples")
    for name in SAMPLES:
        shutil.copy(FIXTURES / name, root / name)
    return plan_for(root)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_every_branch_binding_is_renamed_and_values_are_untouched(sample_plan, name):
    site_line, edited = SAMPLES[name]
    fix = next(f for f in sample_plan.fixes if f.file == name)
    assert [(s.line, s.path, s.target, s.replacement) for s in fix.sites] == [(site_line, "/v1/payment_intents", OLD, NEW)]
    changes = changed_lines(fix)
    assert sorted(changes) == edited
    for before, after in changes.values():
        assert after == before.replace(OLD, NEW, 1)
    assert fix.blob_sha == blob_sha((FIXTURES / name).read_bytes())
    assert OLD not in fix.content.replace(NEW, "")


def test_sample_plan_reports_the_pins_and_no_leftovers(sample_plan):
    assert sample_plan.unfixable == [] and sample_plan.leftovers == []
    assert [(p.file, p.line, p.version) for p in sample_plan.pins] == [
        ("server.js", 15, "2023-10-16"), ("server.py", 18, "2023-10-16"), ("server.rb", 17, "2020-08-27"),
        ("server.ts", 14, "2025-12-15.clover"),
    ]
    assert [m.to_dict()["path"] for m in sample_plan.migrations] == ["/v1/payment_intents"]


def test_payload_shape_is_the_frozen_contract(sample_plan):
    data = sample_plan.to_dict()
    assert set(data) == {"fixes", "unfixable", "pins", "migrations", "leftovers", "not_attempted"}
    assert set(data["fixes"][0]) == {"file", "blob_sha", "content", "sites"}
    assert set(data["fixes"][0]["sites"][0]) == {"line", "path", "target", "replacement"}
    assert set(data["pins"][0]) == {"file", "line", "version"}
    assert set(data["migrations"][0]) >= {"method", "path", "target", "replacement", "doc", "note"}
    assert data["migrations"][0]["method"] == "post"
    json.dumps(data)


# --- the hostile set --------------------------------------------------------------

STRIPE_JS = "const stripe = require('stripe')('sk');\n"


def test_a_literal_feeding_another_endpoint_is_never_edited_and_its_key_is_a_blocking_leftover(tmp_path):
    plan = plan_for(tmp_path, {"pay.js": STRIPE_JS + (
        "async function go() {\n"
        "  const charge = { payment_method_types: ['card'], amount: 1 };\n"
        "  await stripe.charges.create(charge);\n"
        "  const params = { payment_method_types: ['card'], amount: 2 };\n"
        "  await stripe.paymentIntents.create(params);\n"
        "}\n")})
    assert plan.fixes == []
    assert [(u.line, u.why) for u in plan.unfixable] == [(6, "leftover_tokens")]
    assert plan.leftovers == [("pay.js", 3)]


def test_a_shared_literal_fed_to_an_endpoint_outside_the_table_drops_the_file(tmp_path):
    plan = plan_for(tmp_path, {"pay.js": STRIPE_JS + (
        "async function go() {\n"
        "  const params = { payment_method_types: ['card'], amount: 2 };\n"
        "  await stripe.paymentIntents.create(params);\n"
        "  await stripe.charges.create(params);\n"
        "}\n")})
    assert plan.fixes == []
    assert [(u.file, u.line, u.why) for u in plan.unfixable] == [("pay.js", 4, "oracle_changed_other_calls")]


def test_if_else_bindings_are_all_renamed_but_an_unconditional_rebinding_ends_the_walk(tmp_path):
    fix = only_fix(plan_for(tmp_path, {"pay.js": STRIPE_JS + (
        "async function go(flag) {\n"
        "  let params = { amount: 0 };\n"  # killed by the unconditional rebinding below
        "  params = { payment_method_types: ['base'], amount: 1 };\n"
        "  if (flag) { params = { payment_method_types: ['a'], amount: 2 }; }\n"
        "  else { params = { payment_method_types: ['b'], amount: 3 }; }\n"
        "  params.currency = 'usd';\n"
        "  await stripe.paymentIntents.create(params);\n"
        "}\n")}))
    assert sorted(changed_lines(fix)) == [4, 5, 6]
    assert fix.sites[0].line == 8


def test_a_binding_that_is_not_a_literal_makes_the_site_unfixable(tmp_path):
    plan = plan_for(tmp_path, {"pay.js": STRIPE_JS + (
        "async function go(flag) {\n"
        "  let params = build();\n"
        "  if (flag) { params = { payment_method_types: ['a'], amount: 2 }; }\n"
        "  await stripe.paymentIntents.create(params);\n"
        "}\n")})
    assert plan.fixes == [] and [u.why for u in plan.unfixable] == ["incomplete_binding"]


def test_keys_inside_strings_and_comments_are_not_counted_or_touched(tmp_path):
    src = STRIPE_JS + (
        "// payment_method_types is going away\n"
        "const note = 'payment_method_types removed'; /* payment_method_types */\n"
        "stripe.paymentIntents.create({ payment_method_types: ['card'], amount: 1 });\n"
    )
    fix = only_fix(plan_for(tmp_path, {"pay.js": src}))
    assert sorted(changed_lines(fix)) == [4]
    assert "// payment_method_types is going away" in fix.content and "'payment_method_types removed'" in fix.content
    assert fix.content.count(NEW) == 1


def test_shorthand_property_becomes_an_explicit_key_and_the_variable_is_a_leftover(tmp_path):
    plan = plan_for(tmp_path, {"pay.js": STRIPE_JS + (
        "const payment_method_types = ['card'];\n"
        "stripe.paymentIntents.create({ payment_method_types, amount: 1 });\n")})
    fix = only_fix(plan)
    assert "create({ allowed_payment_method_types: payment_method_types, amount: 1 })" in fix.content
    assert plan.leftovers == [("pay.js", 2), ("pay.js", 3)]


def test_quoted_keys_keep_their_quotes_and_mutations_are_renamed(tmp_path):
    fix = only_fix(plan_for(tmp_path, {"pay.js": STRIPE_JS + (
        'const params = { "payment_method_types": ["card"] };\n'
        "params['amount'] = 1;\n"
        "stripe.paymentIntents.create(params);\n"
        "const other = {};\n"
        "other.payment_method_types = ['card'];\n"
        "other['currency'] = 'usd';\n"
        "stripe.paymentIntents.create(other);\n")}))
    assert '{ "allowed_payment_method_types": ["card"] }' in fix.content
    assert "other.allowed_payment_method_types = ['card'];" in fix.content
    assert sorted(changed_lines(fix)) == [2, 6] and [s.line for s in fix.sites] == [4, 8]


def test_no_trailing_comma_and_crlf_round_trip_byte_exact(tmp_path):
    raw = (STRIPE_JS + "stripe.paymentIntents.create({\n  amount: 1,\n  payment_method_types: ['card']\n});\n").replace("\n", "\r\n").encode()
    fix = only_fix(plan_for(tmp_path, {"pay.js": raw}))
    assert fix.content.encode("utf-8").replace(NEW.encode(), OLD.encode()) == raw
    assert "\r\n  allowed_payment_method_types: ['card']\r\n});" in fix.content


def test_ruby_rockets_symbols_quoted_keys_and_paren_less_calls(tmp_path):
    fix = only_fix(plan_for(tmp_path, {"pay.rb": (
        "require 'stripe'\n"
        "Stripe::PaymentIntent.create({ :payment_method_types => ['card'], 'amount' => 1 })\n"
        'Stripe::PaymentIntent.create("payment_method_types" => ["card"], amount: 1)\n'
        "Stripe::PaymentIntent.create payment_method_types: ['card'],\n"
        "  amount: 1\n"
        "post '/x' do\n"
        "  params = { amount: 1 }\n"
        "  params[:payment_method_types] = ['card']\n"
        "  Stripe::PaymentIntent.create(params)\n"
        "end\n")}))
    lines = fix.content.splitlines()
    assert lines[1] == "Stripe::PaymentIntent.create({ :allowed_payment_method_types => ['card'], 'amount' => 1 })"
    assert lines[2] == 'Stripe::PaymentIntent.create("allowed_payment_method_types" => ["card"], amount: 1)'
    assert lines[3] == "Stripe::PaymentIntent.create allowed_payment_method_types: ['card'],"
    assert lines[7] == "  params[:allowed_payment_method_types] = ['card']"
    assert sorted(changed_lines(fix)) == [2, 3, 4, 8]


def test_ruby_window_takes_every_binding_since_the_route_keyword(tmp_path):
    fix = only_fix(plan_for(tmp_path, {"pay.rb": (
        "require 'stripe'\n"
        "post '/x' do\n"
        "  params = { payment_method_types: ['a'], amount: 1 } if flag\n"
        "  params = { payment_method_types: ['b'], amount: 2 } unless flag\n"
        "  Stripe::PaymentIntent.create(params)\n"
        "end\n")}))
    assert sorted(changed_lines(fix)) == [3, 4]


def test_python_non_ascii_before_the_key_keyword_arguments_and_mutations(tmp_path):
    fix = only_fix(plan_for(tmp_path, {"pay.py": (
        "import stripe\n"
        "# — ünïcode comment —\n"
        "def a(flag):\n"
        "    if flag:\n"
        "        params = {'ünï': 'é', 'payment_method_types': ['card'], 'amount': 1}\n"
        "    else:\n"
        "        params = dict(payment_method_types=['card'], amount=2)\n"
        "    params['currency'] = 'usd'\n"
        "    return stripe.PaymentIntent.create(**params)\n"
        "def b():\n"
        "    return stripe.PaymentIntent.create(amount=1, payment_method_types=['card'])\n"
        "def c():\n"
        "    p = {'amount': 1}\n"
        "    p['payment_method_types'] = ['card']\n"
        "    p.update(payment_method_types=['card'])\n"
        "    return stripe.PaymentIntent.create(**p)\n")}))
    lines = fix.content.splitlines()
    assert lines[4] == "        params = {'ünï': 'é', 'allowed_payment_method_types': ['card'], 'amount': 1}"
    assert lines[6] == "        params = dict(allowed_payment_method_types=['card'], amount=2)"
    assert lines[10] == "    return stripe.PaymentIntent.create(amount=1, allowed_payment_method_types=['card'])"
    assert lines[13] == "    p['allowed_payment_method_types'] = ['card']"
    assert lines[14] == "    p.update(allowed_payment_method_types=['card'])"
    assert sorted(changed_lines(fix)) == [5, 7, 11, 14, 15]


def test_a_latin_1_file_is_skipped_as_not_utf8(tmp_path):
    raw = "import stripe\n# caf\xe9\nstripe.PaymentIntent.create(amount=1, payment_method_types=['card'])\n".encode("latin-1")
    plan = plan_for(tmp_path, {"pay.py": raw})
    assert plan.fixes == [] and [(u.line, u.why) for u in plan.unfixable] == [(3, "not_utf8")]


def test_removals_outside_the_table_are_not_candidates(tmp_path):
    plan = plan_for(tmp_path, {"pay.js": STRIPE_JS + "stripe.charges.create({ payment_method_types: ['card'], amount: 1 });\n"})
    assert plan.fixes == [] and plan.unfixable == [] and plan.pins == [] and plan.migrations == []


def test_fix_command_prints_diffs_pins_and_exits_zero(tmp_path, capsys):
    old, new = docs()
    (tmp_path / "a.json").write_text(json.dumps(old))
    (tmp_path / "b.json").write_text(json.dumps(new))
    repo = tmp_path / "repo"
    repo.mkdir()
    pinned = STRIPE_JS.replace("('sk')", "('sk', { apiVersion: '2023-10-16' })")
    (repo / "inline.js").write_text(pinned + "stripe.paymentIntents.create({ payment_method_types: ['card'], amount: 1 });\n")
    (repo / "shared.js").write_text(pinned + (
        "const shared = { payment_method_types: ['card'] };\n"
        "stripe.paymentIntents.create(shared);\n"
        "stripe.charges.create(shared);\n"))
    assert main(["fix", str(repo), "--old", str(tmp_path / "a.json"), "--new", str(tmp_path / "b.json")]) == 0
    out = capsys.readouterr().out
    assert "--- a/inline.js" in out and "+stripe.paymentIntents.create({ allowed_payment_method_types: ['card'], amount: 1 });" in out
    assert "--- a/shared.js" not in out
    assert "API-version pins" in out and "inline.js:1  2023-10-16" in out
    assert "left for you:" in out and "shared.js:3  payment_method_types  /v1/payment_intents  (oracle_changed_other_calls)" in out
    assert main(["fix", str(repo), "--old", str(tmp_path / "a.json"), "--new", str(tmp_path / "b.json"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"fixes", "unfixable", "pins", "migrations", "leftovers", "not_attempted"}

