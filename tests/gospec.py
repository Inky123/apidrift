"""Stripe-shaped specs for the Go tests: the v2 fixer endpoints plus the resources the Go reader is tried on."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from apidrift.diff import BREAKING, diff_chain
from apidrift.fixer import FixPlan, plan_fixes
from apidrift.impact import Impact, assess
from apidrift.openapi import load_spec
from apidrift.report import filter_changes
from apidrift.scanner import Usage, scan_repo, scan_text
from apidrift.sdkmap import SdkIndex
from specs import _form, _op
from test_fixer_v2 import _resource, v2_docs

OLD, NEW = "payment_method_types", "allowed_payment_method_types"
HEAD = 'package billing\n\nimport (\n\t"context"\n\n\t"github.com/stripe/stripe-go/v86"\n%s)\n\n'


def go(body: str, *packages: str, root: str = "") -> str:
    """A Go file importing stripe-go (root optionally aliased) and the named sub-packages, then ``body``."""
    lines = "".join(f'\t"github.com/stripe/stripe-go/v86/{p}"\n' for p in packages)
    head = HEAD % lines
    if root:
        head = head.replace('\t"github.com/stripe/stripe-go/v86"\n', f'\t{root} "github.com/stripe/stripe-go/v86"\n')
    return head + body


def _body(doc: dict, path: str) -> dict:
    return doc["paths"][path]["post"]["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]["properties"]


def go_docs() -> tuple[dict, dict]:
    old, _ = v2_docs()
    paths, schemas = old["paths"], old["components"]["schemas"]
    ok = {"responses": {"200": {}}}
    query = lambda *names: [{"name": n, "in": "query", "schema": {"type": "string"}} for n in names]  # noqa: E731
    intent = {
        "amount": {"type": "integer"}, "currency": {"type": "string"}, "customer": {"type": "string"},
        OLD: {"type": "array", "items": {"type": "string"}}, "metadata": {"type": "object"},
        "expand": {"type": "array", "items": {"type": "string"}}, "return_url": {"type": "string"},
        "shipping": {"type": "object", "properties": {
            "name": {"type": "string"}, "address": {"type": "object", "properties": {"city": {"type": "string"}}},
        }},
        "payment_method_options": {"type": "object", "properties": {
            "card": {"type": "object", "properties": {"request_three_d_secure": {"type": "string"}}},
        }},
    }
    post = lambda props: {"requestBody": _form(json.loads(json.dumps(props))), **ok}  # noqa: E731
    paths["/v1/payment_intents"] = {"post": post(intent), "get": {"parameters": query("customer", "limit"), **ok}}
    paths["/v1/payment_intents/{intent}"] = {"post": post(intent), "get": {"parameters": query("expand"), **ok}}
    paths["/v1/payment_intents/{intent}/confirm"] = {"post": post({OLD: intent[OLD], "return_url": {"type": "string"}})}
    paths["/v1/payment_intents/{intent}/cancel"] = {"post": post({"cancellation_reason": {"type": "string"}})}
    paths["/v1/setup_intents"] = {"post": post({OLD: intent[OLD], "customer": {"type": "string"}})}
    customer = {
        "email": {"type": "string"}, "name": {"type": "string"}, "metadata": {"type": "object"},
        "expand": {"type": "array", "items": {"type": "string"}}, "tax_id_data": {"type": "array", "items": {
            "type": "object", "properties": {"type": {"type": "string"}, "value": {"type": "string"}},
        }},
    }
    paths["/v1/customers"] = {"post": post(customer), "get": {"parameters": query("email", "limit", "created"), **ok}}
    paths["/v1/customers/{customer}"] = {"post": post(customer), "get": {"parameters": query("expand"), **ok}, "delete": dict(ok)}
    paths["/v1/tax_ids"] = {"post": post({"type": {"type": "string"}, "value": {"type": "string"}})}
    paths["/v1/customers/{customer}/tax_ids"] = {"post": post({"type": {"type": "string"}, "value": {"type": "string"}})}
    paths["/v1/account"] = {"get": dict(ok)}
    paths["/v1/accounts/{account}"] = {"get": dict(ok)}
    paths["/v1/accounts"] = {"get": dict(ok)}
    paths["/v1/balance"] = {"get": dict(ok)}
    paths["/v1/invoices/{invoice}/lines"] = {"get": {"parameters": query("limit"), **ok}}
    paths["/v1/invoices"] = {"get": dict(ok)}
    paths["/v1/charges/{charge}/refunds"]["get"] = dict(ok)
    session = _body(old, "/v1/checkout/sessions")
    session[OLD] = intent[OLD]
    session["line_items"]["items"]["properties"]["price_data"] = {"type": "object", "properties": {
        "currency": {"type": "string"}, "product_data": {"type": "object", "properties": {"name": {"type": "string"}}},
    }}
    schemas["payment_intent"] = _resource("PaymentIntent", "", [
        _op("create", "create", "post", "/v1/payment_intents"), _op("list", "list", "get", "/v1/payment_intents"),
        _op("retrieve", "retrieve", "get", "/v1/payment_intents/{intent}"),
        _op("update", "update", "post", "/v1/payment_intents/{intent}"),
        _op("confirm", "custom", "post", "/v1/payment_intents/{intent}/confirm"),
        _op("cancel", "custom", "post", "/v1/payment_intents/{intent}/cancel"),
    ])
    schemas["setup_intent"] = _resource("SetupIntent", "", [_op("create", "create", "post", "/v1/setup_intents")])
    schemas["customer"] = _resource("Customer", "", [
        _op("create", "create", "post", "/v1/customers"), _op("list", "list", "get", "/v1/customers"),
        _op("retrieve", "retrieve", "get", "/v1/customers/{customer}"),
        _op("update", "update", "post", "/v1/customers/{customer}"),
        _op("delete", "delete", "delete", "/v1/customers/{customer}"),
    ])
    schemas["tax_id"] = _resource("TaxId", "", [
        _op("create", "create", "post", "/v1/tax_ids"), _op("create", "create", "post", "/v1/customers/{customer}/tax_ids"),
    ])
    schemas["account"] = _resource("Account", "", [
        _op("retrieve", "retrieve", "get", "/v1/account"), _op("retrieve", "retrieve", "get", "/v1/accounts/{account}"),
        _op("list", "list", "get", "/v1/accounts"),
    ])
    schemas["balance"] = _resource("Balance", "", [_op("retrieve", "retrieve", "get", "/v1/balance")])
    schemas["line_item"] = _resource("InvoiceLineItem", "", [_op("list", "list", "get", "/v1/invoices/{invoice}/lines")])
    schemas["invoice"]["x-stripeOperations"].append(_op("list", "list", "get", "/v1/invoices"))
    schemas["refund"]["x-stripeOperations"].append(
        {**_op("list", "list", "get", "/v1/charges/{charge}/refunds"), "method_on": "collection"}
    )
    new = json.loads(json.dumps(old))
    new["info"]["version"] = "2026-09-30.endive"
    for path in ("/v1/payment_intents", "/v1/payment_intents/{intent}", "/v1/payment_intents/{intent}/confirm",
                 "/v1/setup_intents", "/v1/checkout/sessions"):
        props = _body(new, path)
        props[NEW] = props.pop(OLD)
    _, v2_new = v2_docs()
    for path in v2_new["paths"]:
        if path in ("/v1/payment_methods/{payment_method}", "/v1/financial_connections/sessions",
                    "/v1/subscriptions/{subscription_exposed_id}", "/v1/subscriptions/{subscription}/resume",
                    "/v1/invoices/create_preview"):
            new["paths"][path] = v2_new["paths"][path]
    return old, new


@lru_cache(maxsize=1)
def specs():
    old, new = (load_spec(d) for d in go_docs())
    return SdkIndex.build(old, new), filter_changes(diff_chain([old, new])[0], min_severity=BREAKING)


def usages(text: str, name: str = "pay.go") -> list[Usage]:
    return scan_text(text, name, specs()[0])[0]


def one(text: str) -> Usage:
    found = usages(text)
    assert len(found) == 1, found
    return found[0]


def impacts(text: str) -> list[Impact]:
    return assess(usages(text), specs()[1])


def plan(root: Path, files: dict[str, str | bytes]) -> FixPlan:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    index, changes = specs()
    result = scan_repo(root, index)
    return plan_fixes(result, assess(result.usages, changes), index, root)
