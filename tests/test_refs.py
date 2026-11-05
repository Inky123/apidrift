"""Parameters, request bodies and responses that a vendor names under ``components`` instead of inlining."""

import copy

from apidrift.diff import BREAKING, diff_specs
from apidrift.openapi import load_spec
from specs import base_doc

CHARGES, CHARGE = "/v1/charges", "/v1/charges/{charge}"


def named(doc=None):
    """``base_doc`` rewritten the way GitHub or Plaid would write it: nothing inline, everything a ``$ref``."""
    doc = doc or base_doc()
    paths = doc["paths"]
    components = doc["components"]
    get, post, one = paths[CHARGES]["get"], paths[CHARGES]["post"], paths[CHARGE]["get"]
    components["parameters"] = {"limit": get["parameters"][0], "status": get["parameters"][1], "charge": one["parameters"][0]}
    get["parameters"] = [{"$ref": "#/components/parameters/limit"}, {"$ref": "#/components/parameters/status"}]
    one["parameters"] = [{"$ref": "#/components/parameters/charge"}]
    components["requestBodies"] = {"new_charge": post["requestBody"]}
    post["requestBody"] = {"$ref": "#/components/requestBodies/new_charge"}
    components["responses"] = {"charge": one["responses"]["200"], "charge_list": get["responses"]["200"]}
    get["responses"]["200"] = {"$ref": "#/components/responses/charge_list"}
    post["responses"]["200"] = {"$ref": "#/components/responses/charge"}
    one["responses"]["200"] = {"$ref": "#/components/responses/charge"}
    return doc


def loaded(spec):
    return {
        key: (sorted(op.params.items()), sorted(op.body.items()), op.response_schemas) for key, op in spec.operations.items()
    }


def test_named_parameters_bodies_and_responses_load_like_inline_ones():
    inline, by_name = load_spec(base_doc()), load_spec(named())
    assert loaded(by_name) == loaded(inline)
    get = by_name.operations[("get", CHARGES)]
    assert set(get.params) == {"query:limit", "query:status"}
    assert get.params["query:status"].enum == ("pending", "succeeded", "failed")
    assert by_name.operations[("get", CHARGE)].params["path:charge"].required
    assert by_name.operations[("post", CHARGES)].body["shipping.address.city"].type == "string"
    assert by_name.operations_returning("charge") == ["GET /v1/charges", "GET /v1/charges/{charge}", "POST /v1/charges"]


def test_moving_inline_definitions_into_components_is_not_a_change():
    assert diff_specs(load_spec(base_doc()), load_spec(named())) == []
    assert diff_specs(load_spec(named()), load_spec(base_doc())) == []


def test_a_change_inside_a_named_parameter_reaches_every_operation_that_uses_it():
    old, new = named(), named()
    for doc in (old, new):
        doc["paths"]["/v1/refunds"] = {"get": {"parameters": [{"$ref": "#/components/parameters/status"}], "responses": {}}}
    new["components"]["parameters"]["status"]["schema"]["enum"].remove("failed")
    changes = diff_specs(load_spec(old), load_spec(new))
    assert [(c.kind, c.severity, c.where, c.target) for c in changes] == [
        ("param_enum_removed", BREAKING, "GET /v1/charges", "query:status"),
        ("param_enum_removed", BREAKING, "GET /v1/refunds", "query:status"),
    ]


def test_removing_a_named_parameter_from_one_operation_is_reported_there():
    old, new = named(), named()
    new["paths"][CHARGES]["get"]["parameters"].pop(0)
    (change,) = diff_specs(load_spec(old), load_spec(new))
    assert (change.kind, change.where, change.target) == ("param_removed", "GET /v1/charges", "query:limit")


def test_path_level_parameters_are_shared_and_an_operation_can_replace_one():
    doc = named()
    item = doc["paths"][CHARGES]
    item["parameters"] = [{"$ref": "#/components/parameters/limit"}, {"$ref": "#/components/parameters/status"}]
    item["get"]["parameters"] = [{"name": "limit", "in": "query", "required": True, "schema": {"type": "string"}}]
    spec = load_spec(doc)
    get, post = spec.operations[("get", CHARGES)], spec.operations[("post", CHARGES)]
    assert (get.params["query:limit"].type, get.params["query:limit"].required) == ("string", True)
    assert post.params["query:limit"].type == "integer" and set(post.params) == set(get.params)


def test_reference_chains_and_escaped_pointers_are_followed():
    doc = named()
    components = doc["components"]
    components["parameters"]["per/page~size"] = components["parameters"]["limit"]
    components["parameters"]["limit"] = {"$ref": "#/components/parameters/per~1page~0size"}
    components["responses"]["charge"] = {"$ref": "#/components/responses/the_charge"}
    components["responses"]["the_charge"] = {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/charge"}}}}
    assert loaded(load_spec(doc)) == loaded(load_spec(base_doc()))


def test_dangling_external_and_circular_references_are_skipped_not_fatal():
    doc = named()
    get = doc["paths"][CHARGES]["get"]
    doc["components"]["parameters"]["loop"] = {"$ref": "#/components/parameters/loop"}
    get["parameters"] += [
        {"$ref": "#/components/parameters/missing"},
        {"$ref": "other.yaml#/components/parameters/limit"},
        {"$ref": "#/components/parameters/loop"},
        "not an object",
    ]
    doc["paths"][CHARGES]["post"]["requestBody"] = {"$ref": "#/components/requestBodies/missing"}
    doc["paths"][CHARGE]["get"]["responses"]["200"] = {"$ref": "shared.yaml#/responses/charge"}
    spec = load_spec(doc)
    assert set(spec.operations[("get", CHARGES)].params) == {"query:limit", "query:status"}
    assert spec.operations[("post", CHARGES)].body == {}
    assert spec.operations[("get", CHARGE)].response_schemas == ()


def test_documents_with_empty_or_missing_sections_load():
    doc = base_doc()
    doc["paths"][CHARGES]["parameters"] = None
    doc["paths"][CHARGES]["get"]["parameters"] = None
    doc["paths"][CHARGES]["post"]["requestBody"]["content"] = None
    doc["paths"][CHARGE]["get"]["responses"]["200"]["content"] = None
    doc["components"]["schemas"]["address"]["properties"] = None
    doc["components"]["schemas"]["charge"]["required"] = None
    spec = load_spec(doc)
    assert spec.operations[("get", CHARGES)].params == {} and spec.schemas["address"].fields == {}
    assert not spec.schemas["charge"].fields["id"].required
    for empty in ({}, {"info": None, "paths": None, "components": None}, {"components": {"schemas": None}}):
        assert load_spec(copy.deepcopy(empty)).operations == {}
