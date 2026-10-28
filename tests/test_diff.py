import pytest

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


# --- release attribution, collapsed removals, open enums ----------------------------------------

from apidrift.diff import collapse_removals, diff_chain  # noqa: E402


def versioned(version, *mutations):
    doc = base_doc()
    doc["info"]["version"] = version
    for mutate in mutations:
        mutate(doc)
    return load_spec(doc)


def drop_description(d):
    post_body(d)["properties"].pop("description")


def drop_receipt(d):
    charge(d)["properties"].pop("receipt_url")


def test_diff_chain_stamps_each_change_with_the_version_that_introduced_it():
    specs = [
        versioned("2026-01-01.a"),
        versioned("2026-02-01.b", drop_description),
        versioned("2026-02-01.b", drop_description),  # same version string: a re-published spec
        versioned("2026-03-01.c", drop_description, drop_receipt),
    ]
    changes, versions = diff_chain(specs)
    assert versions == ["2026-01-01.a", "2026-02-01.b", "2026-03-01.c"]
    by_kind = {c.kind: c.introduced for c in changes}
    assert by_kind == {"body_removed": "2026-02-01.b", "response_field_removed": "2026-03-01.c"}
    assert changes[0].to_dict()["introduced"] in ("2026-02-01.b", "2026-03-01.c")
    assert changes[0].key() == (changes[0].kind, changes[0].method, changes[0].path, changes[0].schema, changes[0].target)


def test_diff_chain_with_two_specs_is_a_single_diff_stamped_with_the_new_version():
    changes, versions = diff_chain([versioned("2026-01-01.a"), versioned("2026-02-01.b", drop_description)])
    assert versions == ["2026-01-01.a", "2026-02-01.b"]
    assert [(c.kind, c.introduced) for c in changes] == [("body_removed", "2026-02-01.b")]
    with pytest.raises(ValueError):
        diff_chain([versioned("2026-01-01.a")])


def test_change_seen_only_in_the_full_diff_counts_as_introduced_by_the_new_version():
    # Removed in b, re-added in c, removed again in d: the pairwise diffs attribute it to b (the first time).
    # A change that no pairwise step reports at all (here: injected by hand) falls back to the last version.
    specs = [versioned("1.a"), versioned("2.b", drop_description), versioned("3.c"), versioned("4.d", drop_description)]
    changes, _ = diff_chain(specs)
    assert [(c.kind, c.introduced) for c in changes] == [("body_removed", "2.b")]
    changes, _ = diff_chain([versioned("1.a"), versioned("2.b"), versioned("3.c", drop_description)])
    assert [(c.kind, c.introduced) for c in changes] == [("body_removed", "3.c")]


def add_country_options(d, codes=("at", "be", "de")):
    options = {}
    for code in codes:
        options[code] = {"type": "object", "properties": {"igic": {"type": "object", "properties": {"rate": {"type": "integer"}}}, "standard": {"type": "string"}}}
    post_body(d)["properties"]["country_options"] = {"type": "object", "properties": options}


def drop_igic(d, codes=("at", "be", "de")):
    for code in codes:
        post_body(d)["properties"]["country_options"]["properties"][code]["properties"].pop("igic")


def test_sibling_leaf_removals_collapse_into_one_wildcard_change_when_asked():
    old, new = base_doc(), base_doc()
    add_country_options(old)
    add_country_options(new)
    drop_igic(new)
    exact = diff_specs(load_spec(old), load_spec(new))
    assert [c.target for c in exact] == ["country_options.at.igic", "country_options.be.igic", "country_options.de.igic"]
    collapsed = diff_specs(load_spec(old), load_spec(new), collapse=True)
    assert len(collapsed) == 1
    change = collapsed[0]
    assert (change.kind, change.severity, change.target) == ("body_removed", BREAKING, "country_options.*.igic")
    assert change.message == "body field country_options.*.igic was removed (3 keys: at, be, de)"
    assert change.old == {"type": "object", "required": False, "enum": None}
    assert change.key() == ("body_removed", "post", "/v1/charges", None, "country_options.*.igic")


def test_fewer_than_three_siblings_or_different_shapes_do_not_collapse():
    old, new = base_doc(), base_doc()
    add_country_options(old, ("at", "be"))
    add_country_options(new, ("at", "be"))
    drop_igic(new, ("at", "be"))
    assert len(diff_specs(load_spec(old), load_spec(new), collapse=True)) == 2
    old, new = base_doc(), base_doc()
    add_country_options(old)
    post_body(old)["properties"]["country_options"]["properties"]["de"]["properties"]["igic"] = {"type": "string"}
    add_country_options(new)
    drop_igic(new)
    targets = sorted(c.target for c in diff_specs(load_spec(old), load_spec(new), collapse=True))
    assert targets == ["country_options.at.igic", "country_options.be.igic", "country_options.de.igic"]


def test_collapse_applies_to_response_fields_and_leaves_other_kinds_alone():
    old, new = base_doc(), base_doc()
    for d in (old, new):
        charge(d)["properties"]["options"] = {
            "type": "object",
            "properties": {cc: {"type": "object", "properties": {"igic": {"type": "integer"}}} for cc in ("at", "be", "de")},
        }
    for cc in ("at", "be", "de"):
        charge(new)["properties"]["options"]["properties"][cc]["properties"].pop("igic")
    new["paths"].pop("/v1/charges/{charge}")
    changes = diff_specs(load_spec(old), load_spec(new), collapse=True)
    assert sorted(c.kind for c in changes) == ["endpoint_removed", "response_field_removed"]
    field = only(changes, "response_field_removed")
    assert field.target == "options.*.igic" and field.schema == "charge"
    assert field.message == "field options.*.igic was removed from charge (3 keys: at, be, de)"
    assert field.affects == ("GET /v1/charges", "POST /v1/charges")
    assert collapse_removals([]) == []


def restrict_description(d, **marks):
    post_body(d)["properties"]["description"] = {"type": "string", "enum": ["a", "b"], **marks}


def test_restricting_a_string_to_an_open_or_unvalidated_enum_is_a_warning():
    for marks in ({"x-stripeEnum": {"kind": "open"}}, {"x-stripeBypassValidation": True}):
        change = only(run(lambda d, m=marks: restrict_description(d, **m)), "body_enum_restricted")
        assert change.severity == WARNING, marks
        assert change.new == ["a", "b"] and "other values are still accepted" in change.message
    for marks in ({}, {"x-stripeEnum": {"kind": "closed"}}, {"x-stripeBypassValidation": False}):
        change = only(run(lambda d, m=marks: restrict_description(d, **m)), "body_enum_restricted")
        assert change.severity == BREAKING, marks


def test_open_enum_does_not_soften_other_enum_changes():
    def mutate(d):
        d["paths"]["/v1/charges"]["get"]["parameters"][1]["schema"]["enum"] = ["pending"]
        d["paths"]["/v1/charges"]["get"]["parameters"][1]["schema"]["x-stripeEnum"] = {"kind": "open"}

    assert only(run(mutate), "param_enum_removed").severity == BREAKING
