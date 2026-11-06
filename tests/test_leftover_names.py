"""The leftover-token backstop accepts an old field name only where it is provably the app's own name.

Accepted: a Python variable or parameter (saleor's ``def create_payment_intent(..., payment_method_types=None)``
copied into ``additional_params["payment_method_types"]``), a JS/TS/Ruby variable or parameter in an
expression, and in Go a field the app's own struct declares, reached from a parameter typed as that struct
(openmeter's ``input.Options.PaymentMethodTypes``, flexprice's ``req.PaymentMethodTypes``). Everything that is
or could be a key sent to Stripe stays ``leftover_tokens``: keyword arguments, keys, class attributes and
fields, strings, member reads (a response read cannot be told from a request write here), a ``locals()``
file, and Go selectors on stripe-go types, locals, or structs that do not declare the field themselves.
"""

import pytest

from apidrift.fixtokens import app_names, tokens
from gospec import go, plan as go_plan
from test_fixer import plan_for

JS = "const stripe = require('stripe')('sk');\n"
TS = "import Stripe from 'stripe';\nconst stripe = new Stripe('sk');\n"
PY = "import stripe\n\n\n"
RB = "require 'stripe'\n\n"
PHP = "<?php\n\n"
HEAD = {"a.js": JS, "a.ts": TS, "a.py": PY, "a.rb": RB, "a.php": PHP}

SALEOR = (
    "def create_payment_intent(api_key, amount, payment_method_types=None):\n"
    "    additional_params = {}\n"
    "    if payment_method_types and isinstance(payment_method_types, list):\n"
    "        additional_params['payment_method_types'] = payment_method_types\n"
    "    return stripe.PaymentIntent.create(api_key=api_key, amount=amount, **additional_params)\n"
)

FIXED = {
    "py_saleor": ("a.py", SALEOR, "additional_params['allowed_payment_method_types'] = payment_method_types"),
    "py_local": ("a.py", "def f():\n    payment_method_types = ['card']\n    stripe.PaymentIntent.create(amount=1, payment_method_types=payment_method_types)\n",
                 "stripe.PaymentIntent.create(amount=1, allowed_payment_method_types=payment_method_types)"),
    "js_param": ("a.js", "function f(payment_method_types) {\n  return stripe.paymentIntents.create({ amount: 1, payment_method_types: payment_method_types });\n}\n",
                 "{ amount: 1, allowed_payment_method_types: payment_method_types }"),
    "js_value_in_call": ("a.js", "const payment_method_types = pick(user);\nlog(payment_method_types.length);\nstripe.paymentIntents.create({ amount: 1, payment_method_types: payment_method_types });\n",
                         "allowed_payment_method_types: payment_method_types }"),
    "rb_param": ("a.rb", "def f(payment_method_types)\n  Stripe::PaymentIntent.create(amount: 1, payment_method_types: payment_method_types)\nend\n",
                 "Stripe::PaymentIntent.create(amount: 1, allowed_payment_method_types: payment_method_types)"),
    "php_variable": ("a.php", "function f($payment_method_types) {\n    return \\Stripe\\PaymentIntent::create(['amount' => 1, 'payment_method_types' => $payment_method_types]);\n}\n",
                     "'allowed_payment_method_types' => $payment_method_types"),
}


@pytest.mark.parametrize("case", sorted(FIXED))
def test_the_apps_own_names_spelled_like_the_key_are_not_leftovers(tmp_path, case):
    name, body, expected = FIXED[case]
    plan = plan_for(tmp_path, {name: HEAD[name] + body})
    assert plan.unfixable == [] and len(plan.fixes) == 1, plan.to_dict()
    assert expected in plan.fixes[0].content
    assert plan.fixes[0].content.count("payment_method_types") == (HEAD[name] + body).count("payment_method_types")


def test_saleor_reports_the_names_it_kept_as_mentions(tmp_path):
    plan = plan_for(tmp_path, {"a.py": PY + SALEOR})
    assert plan.to_dict()["leftovers"] == [{"file": "a.py", "line": 4}, {"file": "a.py", "line": 6}, {"file": "a.py", "line": 7}]


CALL_PY = "stripe.PaymentIntent.create(amount=1, payment_method_types=['card'])\n"
CALL_JS = "stripe.paymentIntents.create({ amount: 1, payment_method_types: ['card'] });\n"
CALL_RB = "Stripe::PaymentIntent.create(amount: 1, payment_method_types: ['card'])\n"
CALL_PHP = "\\Stripe\\PaymentIntent::create(['amount' => 1, 'payment_method_types' => ['card']]);\n"

REFUSED = {
    # a request key apidrift did not find, that looks like an app name
    "py_kwarg_to_helper": ("a.py", CALL_PY + "make_intent(payment_method_types=['card'])\n"),
    "py_dict_kwarg": ("a.py", CALL_PY + "params = dict(payment_method_types=['card'])\n"),
    "py_locals": ("a.py", "def f():\n    payment_method_types = ['card']\n    " + CALL_PY + "    send(**locals())\n"),
    "py_class_attr": ("a.py", "class Params:\n    payment_method_types = ['card']\n\n" + CALL_PY),
    "py_dataclass_field": ("a.py", "@dataclass\nclass Params:\n    payment_method_types: list\n\n" + CALL_PY),
    "py_string_key_sent": ("a.py", CALL_PY + "params['payment_method_types'] = ['card']\n"),
    "py_log_key": ("a.py", CALL_PY + "logger.info('created', extra={'payment_method_types': ['card']})\n"),
    "py_response_read": ("a.py", "intent = " + CALL_PY + "print(intent.payment_method_types)\n"),
    "py_attr_write": ("a.py", CALL_PY + "intent.payment_method_types = ['card']\nintent.save()\n"),
    "js_shorthand_elsewhere": ("a.js", "const payment_method_types = ['card'];\n" + CALL_JS + "helper({ payment_method_types });\n"),
    "js_member_read": ("a.js", "const intent = await " + CALL_JS + "console.log(intent.payment_method_types);\n"),
    "js_string_sent": ("a.js", CALL_JS + "extra['payment_method_types'] = ['card'];\n"),
    "js_class_field": ("a.js", "class Params {\n  payment_method_types = ['card'];\n}\n" + CALL_JS),
    "js_object_method": ("a.js", "const p = {\n  payment_method_types() { return ['card']; },\n};\n" + CALL_JS),
    "js_getter": ("a.js", "const p = { get payment_method_types() { return ['card']; } };\n" + CALL_JS),
    "ts_optional_prop": ("a.ts", "interface P {\n  payment_method_types?: string[];\n}\n" + CALL_JS),
    "ts_definite_field": ("a.ts", "class P {\n  payment_method_types!: string[];\n}\n" + CALL_JS),
    "js_statement_assign": ("a.js", "let payment_method_types;\n" + CALL_JS + "payment_method_types = ['x'];\n"),
    "rb_ivar": ("a.rb", "@payment_method_types = ['card']\n" + CALL_RB),
    "rb_method_def": ("a.rb", "def payment_method_types\n  ['card']\nend\n" + CALL_RB),
    "rb_symbol": ("a.rb", CALL_RB + "attr_reader :payment_method_types\n"),
    "php_response_read": ("a.php", "$intent = " + CALL_PHP + "if (in_array('card', $intent->payment_method_types)) {}\n"),
    "php_webhook_read": ("a.php", CALL_PHP + "$si = $event['data']['object'];\nif (!isset($si['payment_method_types'])) { return; }\n"),
    "php_array_write": ("a.php", CALL_PHP + "$data['payment_method_types'] = ['us_bank_account'];\n"),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_a_leftover_that_is_or_could_be_a_request_key_still_refuses_the_file(tmp_path, case):
    name, body = REFUSED[case]
    plan = plan_for(tmp_path, {name: HEAD[name] + body})
    assert plan.fixes == [] and {u.why for u in plan.unfixable} == {"leftover_tokens"}, plan.to_dict()


def test_mixed_languages_are_each_judged_by_their_own_rules(tmp_path):
    plan = plan_for(tmp_path, {
        "a.py": PY + SALEOR,
        "b.js": JS + "class Params {\n  payment_method_types = ['card'];\n}\n" + CALL_JS,
    })
    assert [f.file for f in plan.fixes] == ["a.py"]
    assert [(u.file, u.why) for u in plan.unfixable] == [("b.js", "leftover_tokens")]


def test_app_names_never_include_keys_members_or_strings():
    text = "x = {'payment_method_types': payment_method_types}\ny.payment_method_types\nf(payment_method_types=1)\n"
    found = tokens(text, "python", "payment_method_types")
    assert [text[t.pos - 1] for t in found if t.pos in app_names(text, "python", found)] == [" "]
    js = "const a = { payment_method_types: payment_method_types, payment_method_types };\nb.payment_method_types;\n"
    found = tokens(js, "javascript", "payment_method_types")
    assert [js[t.pos - 2 : t.pos] for t in found if t.pos in app_names(js, "javascript", found)] == [": "]


# --- Go: a field of the app's own struct --------------------------------------------------------------

GOMOD = "module example.com/shop\n\ngo 1.22\n"
DTO = 'package dto\n\ntype SetupRequest struct {\n\tUsage              string\n\tPaymentMethodTypes []string `json:"payment_method_types"`\n}\n'
SETUP = (
    "\tp := &stripe.SetupIntentParams{}\n"
    "\tfor _, t := range %s {\n\t\tp.PaymentMethodTypes = append(p.PaymentMethodTypes, stripe.String(t))\n\t}\n"
    "\tsetupintent.New(p)\n"
)


def go_with(dto_import: str, body: str) -> str:
    text = go(body, "setupintent")
    return text.replace('import (\n', f"import (\n\t{dto_import}\n", 1)


def go_files(body: str, *, dto: str = DTO, dto_import: str = '"example.com/shop/internal/dto"', extra=None) -> dict:
    files = {"go.mod": GOMOD, "internal/dto/dto.go": dto, "internal/billing/pay.go": go_with(dto_import, body)}
    files.update(extra or {})
    return files


def test_go_flexprice_shape_a_parameter_typed_as_the_apps_request_is_its_own_field(tmp_path):
    body = "func f(req *dto.SetupRequest) {\n\tlog.Print(\"payment_method_types\", req.PaymentMethodTypes)\n" + SETUP % "req.PaymentMethodTypes" + "}\n"
    p = go_plan(tmp_path, go_files(body))
    assert p.unfixable == [] and len(p.fixes) == 1, p.to_dict()
    content = p.fixes[0].content
    assert content.count("p.AllowedPaymentMethodTypes") == 2 and content.count("req.PaymentMethodTypes") == 2
    assert [x["line"] for x in p.to_dict()["leftovers"]] == [12, 14]


def test_go_openmeter_shape_a_chain_through_the_apps_structs(tmp_path):
    local = "type Input struct {\n\tID      string\n\tOptions dto.SetupRequest\n}\n\n"
    body = local + "func (c *client) f(ctx context.Context, input Input) {\n\tif input.Options.PaymentMethodTypes != nil {\n" + SETUP % "input.Options.PaymentMethodTypes" + "\t}\n}\n"
    p = go_plan(tmp_path, go_files(body))
    assert p.unfixable == [] and len(p.fixes) == 1, p.to_dict()


GO_REFUSED = {
    # a stripe-go response type: the field still exists there, but apidrift has no v87 response structs to check
    "stripe_type": ("func f(pi *stripe.PaymentIntent) {\n\tlog.Print(pi.PaymentMethodTypes)\n" + SETUP % "kinds" + "}\n", {}),
    # a local variable, not a parameter: its type is not written where it is declared
    "local": ("func f() {\n\treq := load()\n" + SETUP % "req.PaymentMethodTypes" + "}\n", {}),
    # a parameter shadowed by a local of the same name
    "shadowed": ("func f(req *dto.SetupRequest) {\n\treq := other()\n" + SETUP % "req.PaymentMethodTypes" + "}\n", {}),
    # the app's struct only embeds a stripe-go params struct: the field is stripe-go's
    "embedded_only": ("func f(req *dto.SetupRequest) {\n" + SETUP % "req.PaymentMethodTypes" + "}\n",
                      {"dto": 'package dto\n\nimport "github.com/stripe/stripe-go/v86"\n\ntype SetupRequest struct {\n\tstripe.SetupIntentParams\n}\n'}),
    # a package outside this module
    "other_module": ("func f(req *dto.SetupRequest) {\n" + SETUP % "req.PaymentMethodTypes" + "}\n",
                     {"dto_import": '"github.com/other/dto"'}),
    # an unaliased import whose package clause is not the name used
    "clause_mismatch": ("func f(req *dto.SetupRequest) {\n" + SETUP % "req.PaymentMethodTypes" + "}\n",
                        {"dto": DTO.replace("package dto", "package requests")}),
    # a call in the chain
    "call_in_chain": ("func f(req *dto.SetupRequest) {\n" + SETUP % "req.Get().PaymentMethodTypes" + "}\n", {}),
    # the type is not declared anywhere visible
    "undeclared": ("func f(req *dto.Missing) {\n" + SETUP % "req.PaymentMethodTypes" + "}\n", {}),
    # a generic or slice type
    "slice_param": ("func f(reqs []dto.SetupRequest) {\n" + SETUP % "reqs[0].PaymentMethodTypes" + "}\n", {}),
}


@pytest.mark.parametrize("case", sorted(GO_REFUSED))
def test_go_a_field_name_not_provably_the_apps_own_stays_a_leftover(tmp_path, case):
    body, opts = GO_REFUSED[case]
    files = go_files(body, **{k: v for k, v in opts.items() if k in ("dto", "dto_import")})
    p = go_plan(tmp_path, files)
    assert p.fixes == [] and {u.why for u in p.unfixable} == {"leftover_tokens"}, p.to_dict()


def test_go_a_field_declared_directly_wins_over_an_embedded_stripe_struct(tmp_path):
    dto = ('package dto\n\nimport "github.com/stripe/stripe-go/v86"\n\ntype SetupRequest struct {\n'
           '\tstripe.SetupIntentParams\n\tPaymentMethodTypes []string\n}\n')
    p = go_plan(tmp_path, go_files("func f(req *dto.SetupRequest) {\n" + SETUP % "req.PaymentMethodTypes" + "}\n", dto=dto))
    assert p.unfixable == [] and len(p.fixes) == 1, p.to_dict()
