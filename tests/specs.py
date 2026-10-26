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
