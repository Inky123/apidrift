"""A small Stripe-shaped OpenAPI document that tests mutate."""

from __future__ import annotations

import copy


def charge_ref() -> dict:
    return {"$ref": "#/components/schemas/charge"}


def base_doc() -> dict:
    return copy.deepcopy(_BASE)


_BASE = {
    "openapi": "3.0.0",
    "info": {"version": "2026-01-01.test"},
    "paths": {
        "/v1/charges": {
            "get": {
                "operationId": "GetCharges",
                "parameters": [
                    {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer"}},
                    {
                        "name": "status",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string", "enum": ["pending", "succeeded", "failed"]},
                    },
                ],
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"data": {"type": "array", "items": charge_ref()}},
                                }
                            }
                        }
                    }
                },
            },
            "post": {
                "operationId": "PostCharges",
                "requestBody": {
                    "content": {
                        "application/x-www-form-urlencoded": {
                            "schema": {
                                "type": "object",
                                "required": ["amount", "currency"],
                                "properties": {
                                    "amount": {"type": "integer"},
                                    "currency": {"type": "string"},
                                    "description": {"type": "string"},
                                    "shipping": {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string"},
                                            "address": {
                                                "type": "object",
                                                "properties": {"city": {"type": "string"}},
                                            },
                                        },
                                    },
                                },
                            }
                        }
                    }
                },
                "responses": {"200": {"content": {"application/json": {"schema": charge_ref()}}}},
            },
        },
        "/v1/charges/{charge}": {
            "get": {
                "operationId": "GetCharge",
                "parameters": [{"name": "charge", "in": "path", "required": True, "schema": {"type": "string"}}],
                "responses": {"200": {"content": {"application/json": {"schema": charge_ref()}}}},
            }
        },
    },
    "components": {
        "schemas": {
            "charge": {
                "type": "object",
                "required": ["id", "amount"],
                "properties": {
                    "id": {"type": "string"},
                    "amount": {"type": "integer"},
                    "status": {"type": "string", "enum": ["pending", "succeeded", "failed"]},
                    "receipt_url": {"type": "string"},
                    "billing_details": {
                        "type": "object",
                        "properties": {
                            "email": {"type": "string"},
                            "address": {"$ref": "#/components/schemas/address"},
                        },
                    },
                },
            },
            "address": {
                "type": "object",
                "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
            },
        }
    },
}


def sdk_doc() -> dict:
    """``base_doc`` plus Stripe's SDK metadata, a custom action, a nested sub-resource and a namespaced resource."""
    doc = base_doc()
    paths, schemas = doc["paths"], doc["components"]["schemas"]
    charge_param = {"name": "charge", "in": "path", "required": True, "schema": {"type": "string"}}
    paths["/v1/charges/{charge}/capture"] = {
        "post": {
            "parameters": [charge_param],
            "requestBody": _form({"amount": {"type": "integer"}}),
            "responses": {"200": {"content": {"application/json": {"schema": charge_ref()}}}},
        }
    }
    paths["/v1/charges/{charge}/refunds"] = {
        "post": {
            "parameters": [charge_param],
            "requestBody": _form({"amount": {"type": "integer"}, "reason": {"type": "string", "enum": ["duplicate", "fraudulent"]}}),
            "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/refund"}}}}},
        }
    }
    paths["/v1/checkout/sessions"] = {
        "post": {
            "requestBody": _form(
                {
                    "mode": {"type": "string", "enum": ["payment", "subscription"]},
                    "success_url": {"type": "string"},
                    "line_items": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"price": {"type": "string"}, "quantity": {"type": "integer"}}},
                    },
                },
                required=["success_url"],
            ),
            "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/checkout.session"}}}}},
        }
    }
    schemas["charge"]["x-stripeResource"] = {"class_name": "Charge", "in_package": ""}
    schemas["charge"]["x-stripeOperations"] = [
        _op("list", "list", "get", "/v1/charges"),
        _op("create", "create", "post", "/v1/charges"),
        _op("retrieve", "retrieve", "get", "/v1/charges/{charge}"),
        _op("capture", "custom", "post", "/v1/charges/{charge}/capture"),
    ]
    schemas["refund"] = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "amount": {"type": "integer"}},
        "x-stripeResource": {"class_name": "Refund", "in_package": ""},
        "x-stripeOperations": [_op("create", "create", "post", "/v1/charges/{charge}/refunds")],
    }
    schemas["checkout.session"] = {
        "type": "object",
        "properties": {"id": {"type": "string"}, "url": {"type": "string"}},
        "x-stripeResource": {"class_name": "Session", "in_package": "Checkout"},
        "x-stripeOperations": [_op("create", "create", "post", "/v1/checkout/sessions")],
    }
    return doc


def _form(properties: dict, required: list | None = None) -> dict:
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return {"content": {"application/x-www-form-urlencoded": {"schema": schema}}}


def _op(name: str, kind: str, method: str, path: str) -> dict:
    return {"method_name": name, "method_type": kind, "method_on": "service", "operation": method, "path": path}
