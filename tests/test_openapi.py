from apidrift.openapi import load_spec
from specs import base_doc


def test_list_response_resolves_to_item_schema():
    spec = load_spec(base_doc())
    assert spec.operations[("get", "/v1/charges")].response_schemas == ("charge",)
    assert spec.operations[("post", "/v1/charges")].response_schemas == ("charge",)


def test_path_params_are_always_required():
    doc = base_doc()
    del doc["paths"]["/v1/charges/{charge}"]["get"]["parameters"][0]["required"]
    spec = load_spec(doc)
    assert spec.operations[("get", "/v1/charges/{charge}")].params["path:charge"].required


def test_body_is_flattened_to_dotted_paths():
    body = load_spec(base_doc()).operations[("post", "/v1/charges")].body
    assert body["amount"].required and body["amount"].type == "integer"
    assert not body["description"].required
    assert body["shipping.address.city"].type == "string"


def test_refs_stay_leaves_and_components_are_flattened():
    spec = load_spec(base_doc())
    charge = spec.schemas["charge"].fields
    assert charge["billing_details.address"].type == "ref:address"
    assert "billing_details.address.city" not in charge
    assert spec.schemas["address"].fields["city"].type == "string"


def test_operations_returning_lists_every_endpoint():
    spec = load_spec(base_doc())
    assert spec.operations_returning("charge") == ["GET /v1/charges", "GET /v1/charges/{charge}", "POST /v1/charges"]
    assert spec.operations_returning("address") == []
