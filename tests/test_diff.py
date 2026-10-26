from apidrift.diff import BREAKING, INFO, WARNING, diff_specs
from apidrift.openapi import load_spec
from specs import base_doc


def run(mutate):
    old, new = base_doc(), base_doc()
    mutate(new)
    return diff_specs(load_spec(old), load_spec(new))


def only(changes, kind):
    found = [c for c in changes if c.kind == kind]
    assert len(found) == 1, [(c.kind, c.target) for c in changes]
    return found[0]


def post_body(doc):
    return doc["paths"]["/v1/charges"]["post"]["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]


def charge(doc):
    return doc["components"]["schemas"]["charge"]


def test_identical_specs_produce_no_changes():
    assert run(lambda d: None) == []


def test_removed_endpoint_is_breaking():
    changes = run(lambda d: d["paths"].pop("/v1/charges/{charge}"))
    change = only(changes, "endpoint_removed")
    assert change.severity == BREAKING and change.where == "GET /v1/charges/{charge}"


def test_added_endpoint_is_info():
    def mutate(d):
        d["paths"]["/v1/refunds"] = {"get": {"responses": {"200": {}}}}

    assert only(run(mutate), "endpoint_added").severity == INFO


def test_removed_query_param_is_breaking():
    changes = run(lambda d: d["paths"]["/v1/charges"]["get"]["parameters"].pop(0))
    change = only(changes, "param_removed")
    assert change.severity == BREAKING and change.target == "query:limit"


def test_new_param_severity_depends_on_required():
    def add(required):
        def mutate(d):
            d["paths"]["/v1/charges"]["get"]["parameters"].append(
                {"name": "customer", "in": "query", "required": required, "schema": {"type": "string"}}
            )

        return mutate

    assert only(run(add(True)), "param_added_required").severity == BREAKING
    assert only(run(add(False)), "param_added").severity == INFO


def test_param_becoming_required_is_breaking():
    def mutate(d):
        d["paths"]["/v1/charges"]["get"]["parameters"][0]["required"] = True

    assert only(run(mutate), "param_now_required").severity == BREAKING


def test_request_enum_value_removed_is_breaking_and_added_is_info():
    def mutate(d):
        d["paths"]["/v1/charges"]["get"]["parameters"][1]["schema"]["enum"] = ["pending", "succeeded", "refunded"]

    changes = run(mutate)
    assert only(changes, "param_enum_removed").old == ["failed"]
    assert only(changes, "param_enum_added").new == ["refunded"]


def test_removed_body_field_is_breaking():
    changes = run(lambda d: post_body(d)["properties"].pop("description"))
    assert only(changes, "body_removed").target == "description"


def test_new_required_body_field_is_breaking():
    def mutate(d):
        post_body(d)["properties"]["customer"] = {"type": "string"}
        post_body(d)["required"].append("customer")

    change = only(run(mutate), "body_added_required")
    assert change.severity == BREAKING and change.target == "customer"


def test_nested_body_field_removal_is_reported_at_its_path():
    changes = run(lambda d: post_body(d)["properties"]["shipping"]["properties"]["address"]["properties"].pop("city"))
    assert only(changes, "body_removed").target == "shipping.address.city"


def test_removed_parent_object_is_reported_once():
    changes = run(lambda d: post_body(d)["properties"].pop("shipping"))
    removed = [c for c in changes if c.kind == "body_removed"]
    assert [c.target for c in removed] == ["shipping"]


def test_response_field_removed_lists_affected_endpoints():
    change = only(run(lambda d: charge(d)["properties"].pop("receipt_url")), "response_field_removed")
    assert change.severity == BREAKING and change.schema == "charge"
    assert change.affects == ("GET /v1/charges", "GET /v1/charges/{charge}", "POST /v1/charges")


def test_response_field_type_change_is_breaking():
    def mutate(d):
        charge(d)["properties"]["amount"]["type"] = "string"

    change = only(run(mutate), "response_type_changed")
    assert (change.old, change.new) == ("integer", "string")


def test_new_response_enum_value_is_warning():
    def mutate(d):
        charge(d)["properties"]["status"]["enum"].append("refunded")

    change = only(run(mutate), "response_enum_added")
    assert change.severity == WARNING and change.new == ["refunded"]


def test_response_field_no_longer_required_is_warning():
    def mutate(d):
        charge(d)["required"].remove("amount")

    assert only(run(mutate), "response_field_now_optional").severity == WARNING


def test_deprecations_are_warnings():
    def mutate(d):
        d["paths"]["/v1/charges"]["get"]["deprecated"] = True
        charge(d)["properties"]["receipt_url"]["deprecated"] = True

    changes = run(mutate)
    assert only(changes, "endpoint_deprecated").severity == WARNING
    assert only(changes, "response_field_deprecated").target == "receipt_url"


def test_removed_schema_is_breaking():
    changes = run(lambda d: d["components"]["schemas"].pop("address"))
    assert only(changes, "schema_removed").schema == "address"


def test_changed_response_schema_is_breaking():
    def mutate(d):
        d["paths"]["/v1/charges/{charge}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
            "$ref": "#/components/schemas/address"
        }

    assert only(run(mutate), "response_schema_changed").severity == BREAKING


def test_breaking_changes_sort_first():
    def mutate(d):
        d["paths"]["/v1/refunds"] = {"get": {"responses": {"200": {}}}}
        d["paths"]["/v1/charges"]["get"]["deprecated"] = True
        d["paths"]["/v1/charges"]["get"]["parameters"].pop(0)

    severities = [c.severity for c in run(mutate)]
    assert severities == [BREAKING, WARNING, INFO]


def run2(mutate_old, mutate_new):
    old, new = base_doc(), base_doc()
    mutate_old(old)
    mutate_new(new)
    return diff_specs(load_spec(old), load_spec(new))


def rename_address(d, new_name="postal_address"):
    d["components"]["schemas"][new_name] = d["components"]["schemas"].pop("address")
    charge(d)["properties"]["billing_details"]["properties"]["address"] = {"$ref": f"#/components/schemas/{new_name}"}


def test_renamed_schema_with_same_shape_is_not_breaking():
    changes = run(rename_address)
    assert [c.severity for c in changes] == [INFO, INFO]
    assert {c.kind for c in changes} == {"schema_removed", "schema_added"}


def test_renamed_schema_with_changed_shape_reports_nested_fields():
    def mutate(d):
        rename_address(d)
        d["components"]["schemas"]["postal_address"]["properties"].pop("country")

    change = only(run(mutate), "response_field_removed")
    assert change.schema == "charge"
    assert change.target == "billing_details.address.country"
    assert change.affects == ("GET /v1/charges", "GET /v1/charges/{charge}", "POST /v1/charges")


def test_unreferenced_schema_removal_is_info_but_returned_schema_removal_is_breaking():
    assert only(run(lambda d: d["components"]["schemas"].pop("address")), "schema_removed").severity == INFO
    changes = run(lambda d: d["components"]["schemas"].pop("charge"))
    change = only(changes, "schema_removed")
    assert change.severity == BREAKING and "GET /v1/charges" in change.affects


def test_response_type_widened_is_warning_and_narrowed_is_info():
    def widen(d):
        charge(d)["properties"]["receipt_url"] = {"anyOf": [{"type": "string"}, {"type": "integer"}]}

    assert only(run(widen), "response_type_widened").severity == WARNING
    assert only(run2(widen, lambda d: None), "response_type_narrowed").severity == INFO


def test_request_type_widened_is_info_and_narrowed_is_breaking():
    def widen(d):
        post_body(d)["properties"]["amount"] = {"anyOf": [{"type": "integer"}, {"type": "string"}]}

    assert only(run(widen), "body_type_widened").severity == INFO
    assert only(run2(widen, lambda d: None), "body_type_narrowed").severity == BREAKING


def test_response_type_changed_outright_is_breaking():
    def mutate(d):
        charge(d)["properties"]["amount"]["type"] = "string"

    assert only(run(mutate), "response_type_changed").severity == BREAKING


def test_response_schema_set_widened_is_warning():
    def mutate(d):
        d["paths"]["/v1/charges/{charge}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
            "anyOf": [{"$ref": "#/components/schemas/charge"}, {"$ref": "#/components/schemas/address"}]
        }

    change = only(run(mutate), "response_schema_widened")
    assert change.severity == WARNING and "address" in change.message


def test_response_schema_renamed_is_silent():
    def mutate(d):
        d["components"]["schemas"]["charge_v2"] = d["components"]["schemas"].pop("charge")
        for item in d["paths"].values():
            for op in item.values():
                schema = op["responses"]["200"]["content"]["application/json"]["schema"]
                if "$ref" in schema:
                    schema["$ref"] = "#/components/schemas/charge_v2"
                else:
                    schema["properties"]["data"]["items"]["$ref"] = "#/components/schemas/charge_v2"

    changes = run(mutate)
    assert all(c.severity == INFO for c in changes), [(c.kind, c.severity) for c in changes]
