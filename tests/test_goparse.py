"""The Go reader: stripe-go imports, the three call styles, and what a params value sends."""

import pytest

from apidrift.gomap import snake_guess
from apidrift.scanner import scan_text
from gospec import go, impacts, one, specs, usages

CREATE = "\tparams := &stripe.PaymentIntentParams{Amount: stripe.Int64(2000), Currency: stripe.String(\"usd\")}\n"


def labels(text: str) -> list[tuple[str, str]]:
    return [(u.call, u.label) for u in usages(text)]


def unresolved(text: str) -> list[str]:
    return [call for _, _, call in scan_text(text, "pay.go", specs()[0])[1]]


# --- imports ------------------------------------------------------------------------


@pytest.mark.parametrize("line, call", [
    ('"github.com/stripe/stripe-go/v82/paymentintent"', "paymentintent.New"),
    ('"github.com/stripe/stripe-go/paymentintent"', "paymentintent.New"),
    ('"github.com/stripe/stripe-go/v108/paymentintent"', "paymentintent.New"),
    ('pi "github.com/stripe/stripe-go/v82/paymentintent"', "pi.New"),
])
def test_any_major_version_and_aliased_sub_packages_resolve(line, call):
    text = f'package x\n\nimport (\n\t"github.com/stripe/stripe-go/v82"\n\t{line}\n)\n\nfunc f() {{\n{CREATE}\t{call}(params)\n}}\n'
    u = one(text)
    assert (u.call, u.label, u.language) == (call, "POST /v1/payment_intents", "go")
    assert u.fields == {"amount", "currency"} and u.complete and u.line == 10


def test_a_single_line_import_and_an_aliased_root_package():
    text = (
        'package x\n\nimport stripeapi "github.com/stripe/stripe-go/v82"\nimport "github.com/stripe/stripe-go/v82/customer"\n\n'
        'func f() {\n\tcustomer.New(&stripeapi.CustomerParams{Email: stripeapi.String("a@b.c")})\n}\n'
    )
    assert one(text).fields == {"email"}


def test_nested_sub_packages_and_the_legacy_sub_package_name():
    text = go(
        "func f(id string) {\n"
        "\tsession.New(&stripe.CheckoutSessionParams{Mode: stripe.String(\"payment\")})\n"
        "\tfc.New(&stripe.FinancialConnectionsSessionParams{})\n"
        "\tsub.Update(id, &stripe.SubscriptionParams{ProrationBehavior: stripe.String(\"none\")})\n"
        "}\n",
        "checkout/session", "sub",
    ).replace('\t"github.com/stripe/stripe-go/v86/sub"\n', '\t"github.com/stripe/stripe-go/v86/sub"\n\tfc "github.com/stripe/stripe-go/v86/financialconnections/session"\n')
    assert labels(text) == [
        ("session.New", "POST /v1/checkout/sessions"), ("fc.New", "POST /v1/financial_connections/sessions"),
        ("sub.Update", "POST /v1/subscriptions/{subscription_exposed_id}"),
    ]


# --- the three call styles ------------------------------------------------------------


@pytest.mark.parametrize("call, label", [
    ("paymentintent.New(p)", "POST /v1/payment_intents"),
    ("paymentintent.Get(id, nil)", "GET /v1/payment_intents/{intent}"),
    ("paymentintent.Update(id, p)", "POST /v1/payment_intents/{intent}"),
    ("paymentintent.List(nil)", "GET /v1/payment_intents"),
    ("paymentintent.Confirm(id, nil)", "POST /v1/payment_intents/{intent}/confirm"),
    ("paymentintent.Cancel(id, nil)", "POST /v1/payment_intents/{intent}/cancel"),
    ("customer.Del(id, nil)", "DELETE /v1/customers/{customer}"),
    ("sc.PaymentIntents.New(p)", "POST /v1/payment_intents"),
    ("c.client.Customers.Get(id, nil)", "GET /v1/customers/{customer}"),
    ("s.api().CheckoutSessions.New(nil)", "POST /v1/checkout/sessions"),
    ("clients[0].Customers.List(nil)", "GET /v1/customers"),
    ("sc.Accounts.Get()", "GET /v1/account"),
    ("sc.Accounts.GetByID(id, nil)", "GET /v1/accounts/{account}"),
    ("sc.Balance.Get(nil)", "GET /v1/balance"),
    ("sc.Invoices.ListLines(nil)", "GET /v1/invoices/{invoice}/lines"),
    ("sc.V1PaymentIntents.Create(ctx, p)", "POST /v1/payment_intents"),
    ("sc.V1PaymentIntents.Retrieve(ctx, id, nil)", "GET /v1/payment_intents/{intent}"),
    ("sc.V1PaymentIntents.Update(ctx, id, p)", "POST /v1/payment_intents/{intent}"),
    ("sc.V1PaymentIntents.Confirm(ctx, id, nil)", "POST /v1/payment_intents/{intent}/confirm"),
    ("sc.V1Customers.Delete(ctx, id, nil)", "DELETE /v1/customers/{customer}"),
    ("sc.V1Customers.List(ctx, nil)", "GET /v1/customers"),
    ("sc.V1CheckoutSessions.Create(ctx, nil)", "POST /v1/checkout/sessions"),
    ("sc.V1FinancialConnectionsSessions.Create(context.TODO(), nil)", "POST /v1/financial_connections/sessions"),
])
def test_every_style_maps_its_verbs_onto_the_endpoint(call, label):
    text = go(f"func f(ctx context.Context, id string, p *stripe.PaymentIntentParams) {{\n\t{call}\n}}\n", "paymentintent", "customer")
    u = one(text)
    assert u.label == label and u.call == call.replace("()", "").replace("[0]", "").split("(")[0]


@pytest.mark.parametrize("call", [
    "taxid.New(nil)",  # /v1/tax_ids or /v1/customers/{customer}/tax_ids, depending on the params
    "sc.TaxIDs.New(nil)", "sc.V1TaxIDs.Create(ctx, nil)", "sc.V1PaymentIntents.Frobnicate(ctx)",
    "webhook.ConstructEvent(body, sig, secret)",
])
def test_a_call_that_names_no_single_endpoint_is_listed_not_guessed(call):
    text = go(f"func f(ctx context.Context) {{\n\t{call}\n}}\n", "taxid", "webhook")
    assert usages(text) == [] and unresolved(text) == [call.split("(")[0]]


@pytest.mark.parametrize("call", [
    "s.repo.Customers.Get(ctx, id)",  # a context first: not client.API's signature
    "s.repo.Customers.List(r.Context(), filter)",
    "store.Customers.Update(a, b, c)",  # three arguments
    "db.Customers.Find(id)",  # no such stripe-go method
    "Customers.New(nil)",  # no receiver: a local value
    "stripe.String(\"x\")", "refund.list(nil)", "x.customer.New(nil)",
])
def test_lookalike_calls_in_a_file_that_imports_stripe_go_are_not_stripe_calls(call):
    text = go(f"func f(ctx context.Context, id string) {{\n\t_ = {call}\n}}\n", "customer", "refund")
    assert usages(text) == [] and unresolved(text) == []


def test_a_webhook_handler_that_only_uses_stripe_types_has_no_call_site():
    text = go(
        "func handle(w http.ResponseWriter, r *http.Request) {\n"
        "\tevent := stripe.Event{}\n"
        "\tif event.Type == \"payment_intent.succeeded\" {\n"
        "\t\tvar pi stripe.PaymentIntent\n"
        "\t\tjson.Unmarshal(event.Data.Raw, &pi)\n"
        "\t\ts.orders.Update(r.Context(), pi.ID)\n"
        "\t}\n}\n"
    )
    found, missing, unread = scan_text(text, "hook.go", specs()[0])
    assert found == [] and missing == [] and unread is None
    assert scan_text("package x\n// stripe\nfunc f() { customer.New(nil) }\n", "x.go", specs()[0])[0] == []


def test_a_collection_only_method_is_not_a_stripe_go_function():
    assert unresolved(go("func f() {\n\trefund.List(nil)\n}\n", "refund")) == ["refund.List"]


# --- params ---------------------------------------------------------------------------


def test_nested_structs_and_slices_of_structs_are_read_to_field_paths():
    text = go(
        "func f(ctx context.Context, sc *stripe.Client) {\n"
        "\tsc.V1CheckoutSessions.Create(ctx, &stripe.CheckoutSessionCreateParams{\n"
        "\t\tParams: stripe.Params{Context: ctx},\n"
        "\t\tMode:   stripe.String(string(stripe.CheckoutSessionModePayment)),\n"
        "\t\tLineItems: []*stripe.CheckoutSessionCreateLineItemParams{\n"
        "\t\t\t{\n"
        "\t\t\t\tPriceData: &stripe.CheckoutSessionCreateLineItemPriceDataParams{\n"
        "\t\t\t\t\tCurrency:    stripe.String(\"usd\"),\n"
        "\t\t\t\t\tProductData: &stripe.CheckoutSessionCreateLineItemPriceDataProductDataParams{Name: stripe.String(\"T-shirt\")},\n"
        "\t\t\t\t},\n"
        "\t\t\t\tQuantity: stripe.Int64(1),\n"
        "\t\t\t},\n"
        "\t\t\t&stripe.CheckoutSessionCreateLineItemParams{Price: stripe.String(\"price_1\")},\n"
        "\t\t},\n"
        "\t\tSuccessURL: stripe.String(\"https://example.com/ok\"),\n"
        "\t})\n}\n"
    )
    u = one(text)
    assert u.fields == {
        "mode", "success_url", "line_items", "line_items[].price_data", "line_items[].price_data.currency",
        "line_items[].price_data.product_data", "line_items[].price_data.product_data.name", "line_items[].quantity",
        "line_items[].price",
    }
    assert u.complete and u.hidden == frozenset()


def test_a_slice_element_built_elsewhere_hides_only_that_slice():
    text = go(
        "func f(item *stripe.CheckoutSessionLineItemParams) {\n"
        "\tsession.New(&stripe.CheckoutSessionParams{\n"
        "\t\tLineItems:          []*stripe.CheckoutSessionLineItemParams{item},\n"
        "\t\tPaymentMethodTypes: []*string{stripe.String(\"card\")},\n"
        "\t})\n}\n", "checkout/session",
    )
    u = one(text)
    assert u.fields == {"line_items", "payment_method_types"} and u.hidden == {"line_items[]."} and not u.complete
    kinds = {(i.change.target, i.certainty) for i in impacts(text)}
    assert ("payment_method_types", "affected") in kinds


def test_a_variable_bound_in_the_function_with_assignments_after_the_literal():
    text = go(
        "func f(req Request) (*stripe.PaymentIntent, error) {\n"
        "\tparams := &stripe.PaymentIntentParams{\n"
        "\t\tAmount:   stripe.Int64(req.Amount),\n"
        "\t\tCurrency: stripe.String(\"usd\"),\n"
        "\t}\n"
        "\tparams.Customer = stripe.String(req.Customer)\n"
        "\tparams.Shipping = &stripe.ShippingDetailsParams{Name: stripe.String(req.Name)}\n"
        "\tparams.Shipping.Address = &stripe.AddressParams{City: stripe.String(req.City)}\n"
        "\tif req.Cards {\n"
        "\t\tparams.PaymentMethodTypes = stripe.StringSlice([]string{\"card\"})\n"
        "\t}\n"
        "\tparams.AddExpand(\"latest_charge\")\n"
        "\tparams.AddMetadata(\"order\", req.Order)\n"
        "\tparams.SetIdempotencyKey(req.Key)\n"
        "\tparams.Context = req.Context()\n"
        "\treturn paymentintent.New(params)\n}\n", "paymentintent",
    )
    u = one(text)
    assert u.fields == {
        "amount", "currency", "customer", "shipping", "shipping.name", "shipping.address", "shipping.address.city",
        "payment_method_types", "expand", "metadata",
    }
    assert u.complete and u.line == 25


@pytest.mark.parametrize("binding, arg", [
    ("params := new(stripe.CustomerParams)", "params"),
    ("var params stripe.CustomerParams", "&params"),
    ("params := stripe.CustomerParams{}", "&params"),
    ("var params = &stripe.CustomerParams{}", "params"),
    ("var params *stripe.CustomerParams = &stripe.CustomerParams{}", "params"),
    ("var params *stripe.CustomerParams\n\tparams = &stripe.CustomerParams{}", "params"),
])
def test_every_way_to_bind_an_empty_params_struct_then_fill_it(binding, arg):
    text = go(f"func f() {{\n\t{binding}\n\tparams.Email = stripe.String(\"a@b.c\")\n\tcustomer.New({arg})\n}}\n", "customer")
    u = one(text)
    assert u.fields == {"email"} and u.complete


@pytest.mark.parametrize("body", [
    "func f(params *stripe.CustomerParams) {\n\tcustomer.New(params)\n}\n",  # passed in from elsewhere
    "func f() {\n\tcustomer.New(build())\n}\n",  # built in another function
    "func f(i item) {\n\tcustomer.New(i.Params)\n}\n",
    "func f() {\n\tparams := build()\n\tcustomer.New(params)\n}\n",
    "func f() {\n\tparams, err := build()\n\t_ = err\n\tcustomer.New(params)\n}\n",
    "func f() {\n\tparams := &stripe.CustomerParams{}\n\tfill(params)\n\tcustomer.New(params)\n}\n",  # passed on first
    "func f() {\n\tparams := &stripe.CustomerParams{}\n\tother := params\n\tother.Name = nil\n\tcustomer.New(params)\n}\n",
    "func f() {\n\tparams := &stripe.CustomerParams{}\n\tparams.AddExtra(\"name\", \"x\")\n\tcustomer.New(params)\n}\n",
    "func f() {\n\tparams := &stripe.CustomerParams{}\n\tparams.Apply(defaults)\n\tcustomer.New(params)\n}\n",
    "func f() {\n\tparams := &local.CustomerParams{}\n\tcustomer.New(params)\n}\n",  # not a stripe-go struct
    "var params = &stripe.CustomerParams{}\n\nfunc f() {\n\tcustomer.New(params)\n}\n",  # package level: set anywhere
    "func f() {\n\tparams := &stripe.CustomerParams{}\n\tgo func() { params.Name = stripe.String(\"x\") }()\n\tcustomer.New(params)\n}\n",
    "func f() {\n\tparams := &stripe.CustomerParams{}\n\trun(func() { customer.New(params) })\n}\n",  # a closure runs later
    "func f(xs []string) {\n\tparams := &stripe.CustomerParams{}\n\tfor _, x := range xs {\n\t\tcustomer.New(params)\n\t\tparams.Name = stripe.String(x)\n\t}\n}\n",
    "func f(a bool) {\n\tparams := &stripe.CustomerParams{}\n\tif a {\n\t\tparams = other()\n\t}\n\tcustomer.New(params)\n}\n",
])
def test_anything_that_makes_the_field_set_uncertain_is_possible_never_certain(body):
    u = one(go(body, "customer"))
    assert not u.complete and u.hides("name") and u.hides("anything")


def test_stripe_go_flags_for_a_sibling_field_and_range_filters_read_as_that_field():
    text = go(
        "func f(id string, since int64) {\n"
        "\tsub.Update(id, &stripe.SubscriptionParams{BillingCycleAnchorNow: stripe.Bool(true)})\n"
        "\tcustomer.List(&stripe.CustomerListParams{\n"
        "\t\tListParams:   stripe.ListParams{Limit: stripe.Int64(10)},\n"
        "\t\tCreatedRange: &stripe.RangeQueryParams{GreaterThanOrEqual: since},\n"
        "\t})\n}\n", "sub", "customer",
    )
    update, listing = usages(text)
    assert update.fields == {"billing_cycle_anchor"} and listing.fields == {"created", "created.gte"}
    hit = [(i.change.kind, i.change.target, i.certainty) for i in impacts(text)]
    assert ("body_type_changed", "billing_cycle_anchor", "affected") in hit


def test_a_field_the_spec_does_not_list_is_named_by_its_spelling_and_matches_no_change():
    text = go("func f() {\n\tcustomer.New(&stripe.CustomerParams{TaxIDData: nil, InvoiceURLPrefix: nil, Customer: nil})\n}\n", "customer")
    assert one(text).fields == {"tax_id_data", "invoice_url_prefix", "customer"}
    assert snake_guess("DefaultPaymentMethodIDs") == "default_payment_method_ids" and snake_guess("IPAddress") == "ip_address"


def test_a_path_parameter_carried_in_the_params_is_not_a_request_field():
    text = go("func f(sc *client.API, inv string) {\n\tsc.Invoices.ListLines(&stripe.InvoiceListLinesParams{Invoice: &inv})\n}\n", "client")
    u = one(text)
    assert u.fields == frozenset() and u.complete


def test_impact_is_certain_only_when_the_params_are_fully_visible():
    seen = go("func f() {\n\tpaymentintent.New(&stripe.PaymentIntentParams{PaymentMethodTypes: stripe.StringSlice([]string{\"card\"})})\n}\n", "paymentintent")
    passed = go("func f(p *stripe.PaymentIntentParams) {\n\tpaymentintent.New(p)\n}\n", "paymentintent")
    clean = go("func f() {\n\tpaymentintent.New(&stripe.PaymentIntentParams{Amount: stripe.Int64(1)})\n}\n", "paymentintent")
    assert [(i.certainty, i.reason) for i in impacts(seen)] == [("affected", "payment_method_types is sent here")]
    assert [(i.certainty, i.reason) for i in impacts(passed)] == [("possible", "arguments are not fully visible at this call")]
    assert impacts(clean) == []


def test_response_members_the_file_reads_are_recorded_as_field_names():
    text = go(
        "func f() string {\n"
        "\tpi, _ := paymentintent.New(&stripe.PaymentIntentParams{Amount: stripe.Int64(1)})\n"
        "\tlog.Println(pi.LatestCharge.ReceiptURL, stripe.PaymentIntentStatusSucceeded)\n"
        "\treturn pi.ClientSecret\n}\n", "paymentintent",
    )
    assert one(text).reads == {"latest_charge", "receipt_url", "client_secret"}
