"""Go calls whose receiver does not name the resource (gopick), and the params structs stripe-go embeds."""

import pytest

from apidrift.scanner import scan_text
from gospec import go, specs, usages


def found(text):
    return [(u.call, u.label, sorted(u.fields), u.complete) for u in usages(text)]


def unresolved(text):
    return [call for _, _, call in scan_text(text, "pay.go", specs()[0])[1]]


def test_an_app_accessor_named_its_own_way_is_read_by_its_params_type():
    text = go(
        "func f(s *svc, ctx context.Context) {\n"
        "\ts.Intents().New(&stripe.PaymentIntentParams{Amount: stripe.Int64(5)})\n"
        "\ts.Intents().List(&stripe.PaymentIntentListParams{Customer: stripe.String(\"cus\")})\n"
        "\tp := &stripe.PaymentIntentParams{Amount: stripe.Int64(5)}\n"
        "\ts.Intents().New(p)\n"
        "}\n", "client",
    )
    assert found(text) == [
        ("s.Intents.New", "POST /v1/payment_intents", ["amount"], True),
        ("s.Intents.List", "GET /v1/payment_intents", ["customer"], True),
        ("s.Intents.New", "POST /v1/payment_intents", ["amount"], True),
    ]


@pytest.mark.parametrize("call", [
    "s.Intents().New(params)",  # a parameter: its type is not read
    "s.Intents().New(ctx, &stripe.PaymentIntentParams{})",  # a context first: not client.API's signature
    "s.Intents().New(&stripe.PaymentIntentListParams{})",  # the list params do not belong to New
    "s.Intents().Frobnicate(&stripe.PaymentIntentParams{})",  # no such method
    "s.Intents().New(&other.PaymentIntentParams{})",  # not stripe-go's type
    "s.Things().New(&stripe.WidgetParams{})",  # no such resource
    "s.Intents().New(nil)",
])
def test_what_the_params_type_does_not_decide(call):
    text = go(f"func f(s *svc, ctx context.Context, params *stripe.PaymentIntentParams) {{\n\t{call}\n}}\n", "client")
    assert found(text) == [] and unresolved(text) == []


def test_the_parent_field_picks_the_nested_tax_id_path():
    text = go(
        "func f(sc *client.API, cus string) {\n"
        "\tsc.TaxIDs.New(&stripe.TaxIDParams{Customer: stripe.String(cus), Type: stripe.String(\"eu_vat\")})\n"
        "\tsc.TaxIDs.New(&stripe.TaxIDParams{Type: stripe.String(\"eu_vat\"), Value: stripe.String(\"x\")})\n"
        "\tparams := &stripe.TaxIDParams{Type: stripe.String(\"eu_vat\")}\n"
        "\tparams.Customer = stripe.String(cus)\n"
        "\ttaxid.New(params)\n"
        "}\n", "client", "taxid",
    )
    assert found(text) == [
        ("sc.TaxIDs.New", "POST /v1/customers/{customer}/tax_ids", ["type"], True),
        ("sc.TaxIDs.New", "POST /v1/tax_ids", ["type", "value"], True),
        ("taxid.New", "POST /v1/customers/{customer}/tax_ids", ["type"], True),
    ]


@pytest.mark.parametrize("call", [
    "sc.TaxIDs.New(p)",  # params from the caller: Customer may be set there
    "sc.TaxIDs.New(nil)",
    "taxid.New(build())",
])
def test_params_that_may_hold_the_parent_decide_nothing(call):
    text = go(f"func f(sc *client.API, p *stripe.TaxIDParams) {{\n\t{call}\n}}\n", "client", "taxid")
    assert found(text) == [] and unresolved(text) == [call.split("(")[0]]


def test_embedded_list_params_send_their_fields_at_the_top_level():
    text = go(
        "func f(x stripe.ListParams) {\n"
        "\tcustomer.List(&stripe.CustomerListParams{ListParams: stripe.ListParams{Limit: stripe.Int64(3), Single: true}})\n"
        "\tlp := &stripe.CustomerListParams{}\n"
        "\tlp.ListParams.Limit = stripe.Int64(5)\n"
        "\tlp.Single = true\n"
        "\tcustomer.List(lp)\n"
        "\tother := &stripe.CustomerListParams{}\n"
        "\tother.ListParams = x\n"
        "\tcustomer.List(other)\n"
        "}\n", "customer",
    )
    assert found(text) == [
        ("customer.List", "GET /v1/customers", ["limit"], True),
        ("customer.List", "GET /v1/customers", ["limit"], True),
        ("customer.List", "GET /v1/customers", [], False),  # a whole ListParams from elsewhere: anything
    ]
