"""A change to the values a request field accepts is judged by the value a call sends, where the reader saw it."""

from apidrift import plaidjs
from apidrift.diff import diff_specs
from apidrift.impact import AFFECTED, POSSIBLE, assess
from apidrift.openapi import load_spec
from apidrift.plaidmap import PlaidIndex
from apidrift.scanner import Usage
from plaidspec import plaid_doc, schemas
from test_plaid_impact import NODE_HEAD, impacts, specs


def drop_eur(doc):
    schemas(doc)["PaymentAmountCurrency"]["enum"] = ["GBP"]


def test_a_removed_enum_value_hits_only_the_calls_that_send_it_and_possibly_the_ones_whose_value_is_unseen():
    found = impacts(drop_eur, """
        import { PaymentAmountCurrency, Products } from 'plaid';
        client.paymentInitiationPaymentCreate({ recipient_id: r, reference: 'x', amount: { currency: 'EUR', value: 1 } });
        client.paymentInitiationPaymentCreate({ recipient_id: r, reference: 'x', amount: { currency: 'GBP', value: 1 } });
        client.paymentInitiationPaymentCreate({ recipient_id: r, reference: 'x', amount: { currency: "EUR" as PaymentAmountCurrency, value: 1 } });
        client.paymentInitiationPaymentCreate({ recipient_id: r, reference: 'x', amount: { currency: PaymentAmountCurrency.Eur, value: 1 } });
        client.paymentInitiationPaymentCreate({ recipient_id: r, reference: 'x', amount: { currency: PaymentAmountCurrency.Gbp, value: 1 } });
        client.paymentInitiationPaymentCreate({ recipient_id: r, reference: 'x', amount: { currency: Currency.Eur, value: 1 } });
        client.paymentInitiationPaymentCreate({ recipient_id: r, reference: 'x', amount: { currency, value: 1 } });
        client.paymentInitiationPaymentCreate({ recipient_id: r, reference: 'x', amount: { currency: `${c}`, value: 1 } });
        client.paymentInitiationPaymentCreate({ recipient_id: r, reference: 'x', amount });
        client.paymentInitiationPaymentCreate({ recipient_id: r, reference: 'x' });
        const request = { recipient_id: r, amount: { value: 1 } };
        request.amount.currency = 'GBP';
        client.paymentInitiationPaymentCreate(request);
    """)
    assert {(kind, target) for _, kind, target, _ in found} == {("body_enum_removed", "amount.currency")}
    assert [(line, certainty) for line, _, _, certainty in found] == [
        (3, AFFECTED), (5, AFFECTED), (6, AFFECTED), (8, POSSIBLE), (9, POSSIBLE), (10, POSSIBLE), (11, POSSIBLE), (15, POSSIBLE),
    ]


def test_the_reason_names_the_value_sent():
    old, new = specs(drop_eur)
    source = NODE_HEAD + "client.paymentInitiationPaymentCreate({ amount: { currency: 'EUR', value: 1 } });\nclient.paymentInitiationPaymentCreate({ amount: { currency: cur } });\n"
    from apidrift.plaidscan import scan_text

    usages, _, _ = scan_text(source, "pay.js", PlaidIndex.build(old, new))
    reasons = [i.reason for i in assess(usages, diff_specs(old, new))]
    assert reasons == ["amount.currency is sent here as EUR", "amount.currency is sent here; its value is not visible"]


def test_enum_values_are_read_from_python_models_and_dicts():
    found = impacts(drop_eur, """
        from plaid.model.payment_create_request import PaymentCreateRequest
        from plaid.model.payment_amount import PaymentAmount
        from plaid.model.payment_amount_currency import PaymentAmountCurrency
        client.payment_initiation_payment_create(PaymentCreateRequest(recipient_id=r, reference="x", amount=PaymentAmount(currency=PaymentAmountCurrency("EUR"), value=1.0)))
        client.payment_initiation_payment_create(PaymentCreateRequest(recipient_id=r, reference="x", amount=PaymentAmount(currency=PaymentAmountCurrency("GBP"), value=1.0)))
        client.payment_initiation_payment_create({"recipient_id": r, "reference": "x", "amount": {"currency": "EUR", "value": 1.0}})
        client.payment_initiation_payment_create(PaymentCreateRequest(recipient_id=r, reference="x", amount=PaymentAmount(currency=currency, value=1.0)))
        client.payment_initiation_payment_create(PaymentCreateRequest(recipient_id=r, reference="x", amount=PaymentAmount(PaymentAmountCurrency("EUR"), 1.0)))
    """, rel="pay.py")
    assert [(line, certainty) for line, _, _, certainty in found] == [(5, AFFECTED), (7, AFFECTED), (8, POSSIBLE), (9, POSSIBLE)]


def test_a_field_newly_limited_to_listed_values_hits_a_call_that_sends_another_one():
    def restrict(doc):
        schemas(doc)["PaymentCreateRequest"]["properties"]["reference"] = {"type": "string", "enum": ["rent", "invoice"]}

    found = impacts(restrict, """
        client.paymentInitiationPaymentCreate({ recipient_id: r, reference: 'gift' });
        client.paymentInitiationPaymentCreate({ recipient_id: r, reference: 'rent' });
        client.paymentInitiationPaymentCreate({ recipient_id: r, reference });
    """)
    assert [(line, kind, certainty) for line, kind, _, certainty in found] == [
        (2, "body_enum_restricted", AFFECTED), (4, "body_enum_restricted", POSSIBLE),
    ]


def test_a_reader_that_collects_no_values_keeps_judging_a_sent_enum_field_as_affected():
    old, new = specs(drop_eur)
    [change] = [c for c in diff_specs(old, new) if c.kind == "body_enum_removed"]
    usage = Usage("a.py", 1, "post", "/payment_initiation/payment/create", "x", frozenset({"amount", "amount.currency"}), True, "python")
    [impact] = assess([usage], [change])
    assert (impact.certainty, impact.reason) == (AFFECTED, "amount.currency is sent here")


def test_plain_values_cover_lists_and_leave_objects_out():
    [call], _ = plaidjs.extract(
        NODE_HEAD + "import { Products } from 'plaid';\n"
        "client.linkTokenCreate({ client_name: 'A', products: ['auth', Products.Identity, extra], user: { client_user_id: id }, webhook });\n",
        PlaidIndex.build(load_spec(plaid_doc())),
    )
    assert call.values == {
        ("client_name", "A"), ("products", "auth"), ("products", "~Identity"), ("products", None),
        ("user.client_user_id", None), ("webhook", None),
    }
