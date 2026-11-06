"""A small document shaped like Plaid's: one POST per operation, every request and response a named schema."""

import copy

from apidrift.openapi import load_spec
from apidrift.plaidmap import PlaidIndex


def ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def _op(operation_id, request, response):
    body = {"content": {"application/json": {"schema": ref(request)}}}
    return {"post": {"operationId": operation_id, "requestBody": body, "responses": {"200": {"content": {"application/json": {"schema": ref(response)}}}}}}


def _obj(required=(), **properties):
    return {"type": "object", "required": list(required), "properties": properties}


STRING = {"type": "string"}

_DOC = {
    "openapi": "3.0.0",
    "info": {"version": "2020-09-14_1.0.0"},
    "paths": {
        "/link/token/create": _op("linkTokenCreate", "LinkTokenCreateRequest", "LinkTokenCreateResponse"),
        "/transactions/sync": _op("transactionsSync", "TransactionsSyncRequest", "TransactionsSyncResponse"),
        "/transactions/get": _op("transactionsGet", "TransactionsGetRequest", "TransactionsGetResponse"),
        "/accounts/get": _op("accountsGet", "AccountsGetRequest", "AccountsGetResponse"),
        "/item/public_token/exchange": _op("itemPublicTokenExchange", "ItemPublicTokenExchangeRequest", "ItemPublicTokenExchangeResponse"),
        "/payment_initiation/payment/create": _op("paymentInitiationPaymentCreate", "PaymentCreateRequest", "PaymentCreateResponse"),
    },
    "components": {
        "schemas": {
            "LinkTokenCreateRequest": _obj(
                ("client_name", "language", "country_codes"),
                client_id=STRING, secret=STRING, client_name=STRING, language=STRING,
                country_codes={"type": "array", "items": ref("CountryCode")},
                products={"type": "array", "items": ref("Products")},
                user=ref("LinkTokenCreateRequestUser"), webhook=STRING,
                account_filters={"type": "array", "items": ref("AccountFilter")},
            ),
            "LinkTokenCreateRequestUser": _obj(
                ("client_user_id",), client_user_id=STRING, legal_name=STRING, address=ref("UserAddress"),
            ),
            "UserAddress": _obj((), street=STRING, city=STRING, geo=ref("Geo")),
            "Geo": _obj((), lat=STRING, lon=STRING, source=ref("GeoSource")),
            "GeoSource": _obj((), provider=STRING, accuracy=STRING),
            "AccountFilter": _obj((), type=STRING, subtypes={"type": "array", "items": STRING}),
            "CountryCode": {"type": "string", "enum": ["US", "GB"]},
            "Products": {"type": "string", "enum": ["auth", "transactions", "identity"]},
            "LinkTokenCreateResponse": _obj((), link_token=STRING, expiration=STRING, request_id=STRING),
            "TransactionsSyncRequest": _obj(
                ("access_token",), client_id=STRING, secret=STRING, access_token=STRING, cursor=STRING,
                count={"type": "integer"}, options=ref("TransactionsSyncRequestOptions"),
            ),
            "TransactionsSyncRequestOptions": _obj((), include_original_description={"type": "boolean"}, days_requested={"type": "integer"}),
            "TransactionsSyncResponse": _obj(
                (), added={"type": "array", "items": ref("Transaction")}, next_cursor=STRING, has_more={"type": "boolean"},
            ),
            "TransactionsGetRequest": _obj(
                ("access_token", "start_date", "end_date"), access_token=STRING, start_date=STRING, end_date=STRING,
                options=ref("TransactionsGetRequestOptions"),
            ),
            "TransactionsGetRequestOptions": _obj((), count={"type": "integer"}, offset={"type": "integer"}),
            "TransactionsGetResponse": _obj((), transactions={"type": "array", "items": ref("Transaction")}, item=ref("Item")),
            "Transaction": _obj((), transaction_id=STRING, amount={"type": "number"}, merchant_name=STRING, category_id=STRING),
            "Item": _obj((), item_id=STRING, webhook=STRING),
            "AccountsGetRequest": _obj(("access_token",), access_token=STRING, options=ref("AccountsGetRequestOptions")),
            "AccountsGetRequestOptions": _obj((), account_ids={"type": "array", "items": STRING}),
            "AccountsGetResponse": _obj((), accounts={"type": "array", "items": ref("Account")}, item=ref("Item")),
            "Account": _obj((), account_id=STRING, mask=STRING, official_name=STRING),
            "ItemPublicTokenExchangeRequest": _obj(("public_token",), public_token=STRING),
            "ItemPublicTokenExchangeResponse": _obj((), access_token=STRING, item_id=STRING),
            "PaymentCreateRequest": _obj(("recipient_id", "reference", "amount"), recipient_id=STRING, reference=STRING, amount=ref("PaymentAmount")),
            "PaymentAmount": _obj(("currency", "value"), currency=ref("PaymentAmountCurrency"), value={"type": "number"}),
            "PaymentAmountCurrency": {"type": "string", "enum": ["GBP", "EUR"]},
            "PaymentCreateResponse": _obj((), payment_id=STRING, status=STRING),
        }
    },
}


def plaid_doc():
    return copy.deepcopy(_DOC)


def schemas(doc):
    return doc["components"]["schemas"]


def plaid_index(*docs):
    return PlaidIndex.build(*(load_spec(doc) for doc in (docs or (plaid_doc(),))))
