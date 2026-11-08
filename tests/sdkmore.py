"""``specs.sdk_doc`` plus resources whose SDK names need care: a singleton with an id form (account), a
nested collection with a bare verb (login links), a DELETE named ``cancel`` (subscriptions), custom actions
on payment intents and a parent method named after its nested collection (customer payment methods)."""

from __future__ import annotations

from specs import _form, _op, sdk_doc


def _obj(name: str) -> dict:
    return {"content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{name}"}}}}


def _path(name: str) -> dict:
    return {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}


def _put(paths: dict, path: str, method: str, schema: str, fields: dict | None = None, query: bool = False) -> None:
    params = [_path(p[1:-1]) for p in path.split("/") if p.startswith("{")]
    op: dict = {"responses": {"200": _obj(schema)}}
    if fields is not None and not query:
        op["requestBody"] = _form(fields)
    if fields is not None and query:
        params += [{"name": k, "in": "query", "required": False, "schema": v} for k, v in fields.items()]
    if params:
        op["parameters"] = params
    paths.setdefault(path, {})[method] = op


def more_doc() -> dict:
    doc = sdk_doc()
    paths, schemas = doc["paths"], doc["components"]["schemas"]
    s = {"type": "string"}
    expand = {"expand": {"type": "array", "items": s}}
    _put(paths, "/v1/account", "get", "account", expand, query=True)
    _put(paths, "/v1/accounts", "post", "account", {"email": s, "country": s})
    _put(paths, "/v1/accounts/{account}", "get", "account", expand, query=True)
    _put(paths, "/v1/accounts/{account}", "post", "account", {"email": s, "metadata": {"type": "object"}})
    _put(paths, "/v1/accounts/{account}", "delete", "account")
    _put(paths, "/v1/accounts/{account}/login_links", "post", "login_link", {})
    _put(paths, "/v1/accounts/{account}/capabilities", "get", "account", {}, query=True)
    _put(paths, "/v1/balance", "get", "balance", {}, query=True)
    _put(paths, "/v1/subscriptions", "post", "subscription", {"customer": s, "billing_cycle_anchor": s})
    _put(paths, "/v1/subscriptions/{subscription_exposed_id}", "get", "subscription", expand, query=True)
    _put(paths, "/v1/subscriptions/{subscription_exposed_id}", "post", "subscription",
         {"billing_cycle_anchor": s, "metadata": {"type": "object"}, "cancel_at_period_end": {"type": "boolean"}})
    _put(paths, "/v1/subscriptions/{subscription_exposed_id}", "delete", "subscription", {"prorate": {"type": "boolean"}})
    pmt = {"type": "array", "items": s}
    _put(paths, "/v1/payment_intents", "post", "payment_intent", {"amount": {"type": "integer"}, "payment_method_types": pmt})
    _put(paths, "/v1/payment_intents/{intent}", "get", "payment_intent", expand, query=True)
    _put(paths, "/v1/payment_intents/{intent}", "post", "payment_intent", {"description": s, "metadata": {"type": "object"}})
    _put(paths, "/v1/payment_intents/{intent}/confirm", "post", "payment_intent", {"payment_method": s, "payment_method_types": pmt})
    _put(paths, "/v1/payment_intents/{intent}/cancel", "post", "payment_intent", {"cancellation_reason": s})
    _put(paths, "/v1/customers", "get", "customer", {"limit": {"type": "integer"}}, query=True)
    _put(paths, "/v1/customers/{customer}", "get", "customer", expand, query=True)
    _put(paths, "/v1/customers/{customer}/payment_methods", "get", "customer", {"type": s}, query=True)
    schemas["account"] = {
        "type": "object",
        "properties": {"id": s, "email": s, "capabilities": {"type": "object"}},
        "x-stripeResource": {"class_name": "Account", "in_package": ""},
        "x-stripeOperations": [
            _op("retrieve", "retrieve", "get", "/v1/account"),
            _op("create", "create", "post", "/v1/accounts"),
            _op("retrieve", "retrieve", "get", "/v1/accounts/{account}"),
            _op("update", "update", "post", "/v1/accounts/{account}"),
            _op("delete", "delete", "delete", "/v1/accounts/{account}"),
            _op("capabilities", "custom", "get", "/v1/accounts/{account}/capabilities"),
        ],
    }
    schemas["customer"] = {
        "type": "object", "properties": {"id": s},
        "x-stripeResource": {"class_name": "Customer", "in_package": ""},
        "x-stripeOperations": [
            _op("list", "list", "get", "/v1/customers"),
            _op("retrieve", "retrieve", "get", "/v1/customers/{customer}"),
            _op("list_payment_methods", "custom", "get", "/v1/customers/{customer}/payment_methods"),
        ],
    }
    schemas["login_link"] = {
        "type": "object", "properties": {"url": s},
        "x-stripeResource": {"class_name": "LoginLink", "in_package": ""},
        "x-stripeOperations": [_op("create", "custom", "post", "/v1/accounts/{account}/login_links")],
    }
    schemas["balance"] = {
        "type": "object", "properties": {"available": {"type": "array", "items": s}},
        "x-stripeResource": {"class_name": "Balance", "in_package": ""},
        "x-stripeOperations": [_op("retrieve", "retrieve", "get", "/v1/balance")],
    }
    schemas["subscription"] = {
        "type": "object", "properties": {"id": s, "status": s},
        "x-stripeResource": {"class_name": "Subscription", "in_package": ""},
        "x-stripeOperations": [
            _op("create", "create", "post", "/v1/subscriptions"),
            _op("retrieve", "retrieve", "get", "/v1/subscriptions/{subscription_exposed_id}"),
            _op("update", "update", "post", "/v1/subscriptions/{subscription_exposed_id}"),
            _op("cancel", "custom", "delete", "/v1/subscriptions/{subscription_exposed_id}"),
        ],
    }
    schemas["payment_intent"] = {
        "type": "object", "properties": {"id": s, "status": s, "payment_method_types": pmt},
        "x-stripeResource": {"class_name": "PaymentIntent", "in_package": ""},
        "x-stripeOperations": [
            _op("create", "create", "post", "/v1/payment_intents"),
            _op("retrieve", "retrieve", "get", "/v1/payment_intents/{intent}"),
            _op("update", "update", "post", "/v1/payment_intents/{intent}"),
            _op("confirm", "custom", "post", "/v1/payment_intents/{intent}/confirm"),
            _op("cancel", "custom", "post", "/v1/payment_intents/{intent}/cancel"),
        ],
    }
    return doc
