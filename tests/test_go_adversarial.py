"""Go source built to mislead a reader that has no compiler: every case is read correctly or marked uncertain."""

import pytest

from apidrift.golex import BLOCK, LIT, TYPE, GoSource
from gospec import go, one, usages

PI = "paymentintent"


def test_raw_strings_runes_and_comments_never_leak_braces_or_keys():
    text = go(
        "const tmpl = `{\"payment_method_types\": [\"card\"]} // not a comment\n"
        "PaymentMethodTypes: nope }}}`\n\n"
        "func f() {\n"
        "\tparams := &stripe.PaymentIntentParams{\n"
        "\t\tAmount: stripe.Int64(100), // Currency: stripe.String(\"usd\"),\n"
        "\t\t/* Customer: stripe.String(\"cus\"),\n"
        "\t\t   } */\n"
        "\t\tDescription: stripe.String(`{ \"Customer\": \"}\" `),\n"
        "\t\tStatementDescriptor: stripe.String(\"a } b // c\" + string('}') + string('\\'')),\n"
        "\t}\n"
        "\tpaymentintent.New(params)\n}\n", PI,
    )
    u = one(text)
    assert u.fields == {"amount", "description", "statement_descriptor"} and u.complete and u.line == 21


def test_params_built_across_an_if_else_report_every_branch_and_stay_uncertain():
    text = go(
        "func f(card bool) {\n"
        "\tvar params *stripe.PaymentIntentParams\n"
        "\tif card {\n"
        "\t\tparams = &stripe.PaymentIntentParams{PaymentMethodTypes: stripe.StringSlice([]string{\"card\"})}\n"
        "\t} else {\n"
        "\t\tparams = &stripe.PaymentIntentParams{Customer: stripe.String(\"cus_1\")}\n"
        "\t}\n"
        "\tpaymentintent.New(params)\n}\n", PI,
    )
    u = one(text)
    assert u.fields == {"payment_method_types", "customer"} and not u.complete and u.hides("amount")


def test_a_switch_keeps_its_clauses_apart_and_a_field_set_in_one_clause_is_still_reported():
    text = go(
        "func f(kind string) {\n"
        "\tparams := &stripe.PaymentIntentParams{Amount: stripe.Int64(1)}\n"
        "\tswitch kind {\n"
        "\tcase \"card\":\n"
        "\t\tparams.PaymentMethodTypes = stripe.StringSlice([]string{\"card\"})\n"
        "\tcase \"other\":\n"
        "\t\tparams = legacy()\n"
        "\t\tpaymentintent.Update(\"pi_1\", params)\n"
        "\tdefault:\n"
        "\t\treturn\n"
        "\t}\n"
        "\tpaymentintent.New(params)\n}\n", PI,
    )
    update, create = usages(text)
    assert not update.complete and update.fields == frozenset()  # rebound in its own clause: nothing visible
    assert create.fields == {"amount", "payment_method_types"} and not create.complete  # may be the rebound value


def test_params_shared_by_two_calls_and_a_field_assigned_after_the_first():
    text = go(
        "func f() {\n"
        "\tparams := &stripe.PaymentIntentParams{Amount: stripe.Int64(1)}\n"
        "\tpaymentintent.New(params)\n"
        "\tparams.Customer = stripe.String(\"cus_1\")\n"
        "\tpaymentintent.Update(\"pi_1\", params)\n}\n", PI,
    )
    create, update = usages(text)
    assert create.fields == {"amount"} and create.complete
    assert update.fields == {"amount", "customer"} and update.complete


def test_a_variable_named_like_the_package_shadows_it_only_after_its_declaration():
    text = go(
        "func f() {\n"
        "\tcustomer, err := customer.New(&stripe.CustomerParams{Email: stripe.String(\"a@b.c\")})\n"
        "\tif err != nil {\n\t\treturn\n\t}\n"
        "\tcustomer.Update(\"cus_1\", nil)\n"  # a method on the *stripe.Customer value: not the package
        "}\n\n"
        "func g(customer *Account) {\n\tcustomer.New(nil)\n}\n\n"
        "func h() {\n\tif customer := lookup(); customer != nil {\n\t\tcustomer.Del(\"x\", nil)\n\t}\n\tcustomer.Del(\"cus_2\", nil)\n}\n",
        "customer",
    )
    assert [(u.line, u.label) for u in usages(text)] == [(11, "POST /v1/customers"), (26, "DELETE /v1/customers/{customer}")]


def test_a_local_package_also_called_customer_is_not_stripe():
    text = (
        'package x\n\nimport (\n\t"example.com/app/customer"\n\t"github.com/stripe/stripe-go/v86"\n'
        '\tstripecustomer "github.com/stripe/stripe-go/v86/customer"\n)\n\n'
        "func f() {\n\tcustomer.New(nil)\n\tcustomer.Get(\"1\", nil)\n\tstripecustomer.Get(\"cus_1\", &stripe.CustomerParams{})\n}\n"
    )
    u = one(text)
    assert (u.line, u.call, u.label) == (12, "stripecustomer.Get", "GET /v1/customers/{customer}")


def test_generics_and_function_literals_inside_the_literal():
    text = go(
        "func Map[T, U any](xs []T, f func(T) U) []U {\n\tout := make([]U, 0, len(xs))\n\tfor _, x := range xs {\n\t\tout = append(out, f(x))\n\t}\n\treturn out\n}\n\n"
        "type Box[T any] struct{ v T }\n\n"
        "func f(types []string) {\n"
        "\tparams := &stripe.PaymentIntentParams{\n"
        "\t\tPaymentMethodTypes: Map[string, *string](types, func(t string) *string { return &t }),\n"
        "\t\tAmount:             Box[int64]{v: 5}.ptr(),\n"
        "\t}\n"
        "\tpaymentintent.New(params)\n}\n", PI,
    )
    u = one(text)
    assert u.fields == {"payment_method_types", "amount"} and u.complete


def test_multi_line_chained_calls_report_the_line_the_chain_starts_on():
    text = go(
        "func (s *svc) f(ctx context.Context) {\n"
        "\ts.\n\t\tclient().\n\t\tV1PaymentIntents.\n\t\tCreate(\n\t\t\tctx,\n\t\t\t&stripe.PaymentIntentCreateParams{\n\t\t\t\tAmount: stripe.Int64(1),\n\t\t\t},\n\t\t)\n}\n"
    )
    u = one(text)
    assert (u.line, u.call, u.fields) == (10, "s.client.V1PaymentIntents.Create", {"amount"})


def test_crlf_line_endings_read_the_same():
    text = go(
        "func f() {\n\tparams := &stripe.PaymentIntentParams{\n\t\tAmount: stripe.Int64(1),\n\t}\n"
        "\tparams.Customer = stripe.String(\"cus_1\")\n\tpaymentintent.New(params)\n}\n", PI,
    )
    unix, dos = one(text), one(text.replace("\n", "\r\n"))
    assert (dos.line, dos.fields, dos.complete) == (unix.line, unix.fields, unix.complete) == (15, {"amount", "customer"}, True)


def test_a_shadowing_inner_variable_does_not_feed_the_outer_call():
    text = go(
        "func f(a bool) {\n"
        "\tparams := &stripe.PaymentIntentParams{Amount: stripe.Int64(1)}\n"
        "\tif a {\n"
        "\t\tparams := &stripe.PaymentIntentParams{Customer: stripe.String(\"cus_1\")}\n"
        "\t\tparams.Description = stripe.String(\"inner\")\n"
        "\t\tpaymentintent.Update(\"pi_1\", params)\n"
        "\t}\n"
        "\tpaymentintent.New(params)\n}\n", PI,
    )
    inner, outer = usages(text)
    assert inner.fields == {"customer", "description"} and inner.complete
    assert outer.fields == {"amount"} and outer.complete


def test_reading_a_nested_value_out_of_the_params_makes_only_that_branch_uncertain():
    text = go(
        "func f() {\n"
        "\tparams := &stripe.PaymentIntentParams{Amount: stripe.Int64(1), Shipping: &stripe.ShippingDetailsParams{}}\n"
        "\tshipping := params.Shipping\n"
        "\tshipping.Name = stripe.String(\"x\")\n"
        "\tif params.Customer != nil {\n\t\treturn\n\t}\n"
        "\tpaymentintent.New(params)\n}\n", PI,
    )
    u = one(text)
    assert u.fields == {"amount", "shipping"} and u.hides("shipping.name") and not u.hides("amount") and not u.hides("customer")


def test_a_goto_in_the_function_makes_statement_order_untrustworthy():
    text = go(
        "func f(retry bool) {\n"
        "\tparams := &stripe.PaymentIntentParams{Amount: stripe.Int64(1)}\n"
        "again:\n"
        "\tpaymentintent.New(params)\n"
        "\tif retry {\n\t\tparams.Customer = stripe.String(\"cus_1\")\n\t\tretry = false\n\t\tgoto again\n\t}\n}\n", PI,
    )
    u = one(text)
    assert u.fields == {"amount"} and not u.complete and u.hides("customer")


@pytest.mark.parametrize("text", [
    "", "package x\n", 'package x\nimport "github.com/stripe/stripe-go/v86/customer"\nfunc f() { customer.New(',
    'package x\nimport "github.com/stripe/stripe-go/v86"\nfunc f() { p := &stripe.CustomerParams{ Email: }; sc.V1Customers.Create(ctx, p',
    'import "github.com/stripe/stripe-go/v86"\n}}}} ))) sc.Customers.New(&stripe.CustomerParams{',
    'import ("github.com/stripe/stripe-go/v86"\n x.V1Customers.Create(a, &stripe.',
    'package x\nimport "github.com/stripe/stripe-go/v86"\nvar x = `unterminated\nfunc f() { c.Customers.New(nil) }',
])
def test_truncated_or_broken_source_never_raises(text):
    usages(text)  # no exception: what is found in a file that does not parse is best effort


def test_a_file_the_reader_gives_up_on_is_reported_as_not_read_never_as_clean(monkeypatch):
    from apidrift import goparse
    from apidrift.scanner import scan_text
    from gospec import specs

    def boom(text, index):
        raise IndexError("token stream ended inside a call")

    monkeypatch.setattr(goparse, "extract", boom)
    found, missing, unread = scan_text(go("func f() {\n\tcustomer.New(nil)\n}\n", "customer"), "pay.go", specs()[0])
    assert (found, missing, unread) == ([], [], "Go")


@pytest.mark.parametrize("snippet, kinds", [
    ("func f() { if x == (T{}) { g() } }", [BLOCK, LIT, BLOCK]),
    ("func f() { for _, v := range []string{\"a\"} { g(v) } }", [BLOCK, LIT, BLOCK]),
    ("func f() { for k := range map[string]pkg.T{} { g(k) } }", [BLOCK, LIT, BLOCK]),
    ("func f() { if items[0].ok { g() } else { h() } }", [BLOCK, BLOCK, BLOCK]),
    ("func f() { switch v := x.(type) { case T: g(v) } }", [BLOCK, BLOCK]),
    ("type T struct { a struct{ b int }; c interface{ M() } }", [TYPE, TYPE, TYPE]),
    ("var x = []T{{A: 1}, {A: 2}}", [LIT, LIT, LIT]),
    ("func f() { go func() { g(T{}) }() }", [BLOCK, BLOCK, LIT]),
    ("func f() struct{ a int } { return struct{ a int }{1} }", [TYPE, BLOCK, TYPE, LIT]),
])
def test_every_brace_is_a_block_a_literal_or_a_type(snippet, kinds):
    src = GoSource(snippet)
    assert [src.brace[i] for i in sorted(src.brace)] == kinds


def test_automatic_semicolons_and_string_forms():
    src = GoSource("a := `x\n}`\nb := '\\''\nc := \"\\\"{\" // }\nreturn\n")
    assert [t for t in src.tok if t not in (";",)] == ["a", ":=", "`x\n}`", "b", ":=", "'\\''", "c", ":=", '"\\"{"', "return"]
    assert src.tok.count(";") == 4 and src.brace == {}
