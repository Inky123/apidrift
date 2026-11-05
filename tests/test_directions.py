"""A vendor that names everything (Plaid, Square): named enums and strings, ``allOf``, and schemas that are
only ever sent, which must be judged by the request rules and reported once."""

import copy

from apidrift.diff import BREAKING, INFO, WARNING, diff_specs
from apidrift.openapi import load_spec

ITEM_GET = ("post", "/item/get")


def ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def json_body(schema):
    return {"content": {"application/json": {"schema": schema}}}


def plaid_doc():
    return copy.deepcopy(_DOC)


_DOC = {
    "openapi": "3.0.0",
    "info": {"version": "2020-09-14_1.0.0"},
    "paths": {
        "/item/get": {
            "post": {
                "operationId": "itemGet",
                "requestBody": json_body(ref("ItemGetRequest")),
                "responses": {"200": json_body(ref("ItemGetResponse"))},
            }
        }
    },
    "components": {
        "schemas": {
            "ItemGetRequest": {
                "type": "object",
                "required": ["access_token"],
                "properties": {
                    "access_token": ref("AccessToken"),
                    "score_version": ref("ScoreVersion"),
                    "options": ref("ItemGetOptions"),
                },
            },
            "ItemGetOptions": {
                "type": "object",
                "properties": {"products": {"type": "array", "items": ref("Product")}, "user": ref("User")},
            },
            "User": {"type": "object", "properties": {"name": {"type": "string"}, "address": ref("Address")}},
            "Address": {"type": "object", "properties": {"city": {"type": "string"}, "region": {"type": "string"}}},
            "AccessToken": {"type": "string"},
            "ScoreVersion": {"type": "string", "enum": ["v1", "v2"]},
            "Product": {"type": "string", "enum": ["auth", "identity"]},
            "ItemGetResponse": {"type": "object", "properties": {"item": ref("Item"), "request_id": {"type": "string"}}},
            "Item": {"type": "object", "properties": {"status": ref("ItemStatus"), "address": ref("Address")}},
            "ItemStatus": {"type": "string", "enum": ["good", "bad"]},
            "ItemWebhook": {"type": "object", "properties": {"code": {"type": "string"}}},
        }
    },
}


def schemas(doc):
    return doc["components"]["schemas"]


def run(mutate, base=plaid_doc):
    old, new = base(), base()
    mutate(new)
    return diff_specs(load_spec(old), load_spec(new))


def rows(changes):
    return sorted((c.kind, c.severity, c.where, c.target or "") for c in changes)


def test_a_named_enum_or_string_is_read_as_the_type_it_names():
    spec = load_spec(plaid_doc())
    body = spec.operations[ITEM_GET].body
    assert (body["access_token"].type, body["access_token"].required, body["access_token"].enum) == ("string", True, None)
    assert (body["score_version"].type, body["score_version"].enum) == ("string", ("v1", "v2"))
    assert body["options.products"].type == "array<string>"
    status = spec.schemas["Item"].fields["status"]
    assert (status.type, status.enum) == ("string", ("good", "bad"))


def test_request_bodies_follow_named_objects_and_component_schemas_keep_them_as_leaves():
    spec = load_spec(plaid_doc())
    op = spec.operations[ITEM_GET]
    assert op.body["options"].type == "ref:ItemGetOptions" and op.body["options.user.address.city"].type == "string"
    assert op.listed == {"ItemGetRequest": 0, "ItemGetOptions": 1, "User": 2, "Address": 3}
    assert set(spec.schemas["Item"].fields) == {"status", "address"} and spec.schemas["Item"].fields["address"].type == "ref:Address"
    assert spec.operations_returning("ItemGetResponse") == ["POST /item/get"]


def test_schemas_only_requests_reach_are_request_only_and_shared_or_unreferenced_ones_are_not():
    spec = load_spec(plaid_doc())
    assert spec.request_only == {"ItemGetRequest", "ItemGetOptions", "User", "AccessToken", "ScoreVersion", "Product"}
    assert {"ItemGetRequest", "User", "Address", "Product"} <= spec.operations[ITEM_GET].sends
    assert "Item" not in spec.operations[ITEM_GET].sends


def test_removing_a_value_from_a_named_request_enum_is_breaking_where_it_is_sent():
    changes = run(lambda d: schemas(d)["ScoreVersion"]["enum"].remove("v1"))
    assert rows(changes) == [("body_enum_removed", BREAKING, "POST /item/get", "score_version")]
    assert changes[0].old == ["v1"]


def test_a_named_response_enum_follows_the_response_rules():
    assert rows(run(lambda d: schemas(d)["ItemStatus"]["enum"].append("pending"))) == [
        ("response_enum_added", WARNING, "schema Item", "status")
    ]
    assert rows(run(lambda d: schemas(d)["ItemStatus"]["enum"].remove("bad"))) == [
        ("response_enum_removed", INFO, "schema Item", "status")
    ]


def test_naming_an_inline_type_or_inlining_a_named_one_is_not_a_change():
    def inline_everything(d):
        s = schemas(d)
        s["ItemGetRequest"]["properties"]["score_version"] = {"type": "string", "enum": ["v1", "v2"]}
        s["ItemGetRequest"]["properties"]["access_token"] = {"type": "string"}
        s["Item"]["properties"]["status"] = {"type": "string", "enum": ["good", "bad"]}
        options = copy.deepcopy(s["ItemGetOptions"])
        options["properties"]["user"] = copy.deepcopy(s["User"])
        s["ItemGetRequest"]["properties"]["options"] = options

    assert run(inline_everything) == []
    old, new = plaid_doc(), plaid_doc()
    inline_everything(old)
    assert diff_specs(load_spec(old), load_spec(new)) == []


def test_renaming_a_request_schema_is_not_a_breaking_change():
    def rename(d):
        s = schemas(d)
        s["Person"] = s.pop("User")
        s["Person"]["properties"]["email"] = {"type": "string"}
        s["ItemGetOptions"]["properties"]["user"] = ref("Person")

    assert rows(run(rename)) == [
        ("body_added", INFO, "POST /item/get", "options.user.email"),
        ("schema_added", INFO, "schema Person", ""),
        ("schema_removed", INFO, "schema User", ""),
    ]


def test_a_change_to_a_request_only_schema_is_reported_once_on_the_operation_by_the_request_rules():
    def tighten(d):
        user = schemas(d)["User"]
        user["properties"].pop("name")
        user["properties"]["id"] = {"type": "string"}
        user["required"] = ["id"]

    assert rows(run(tighten)) == [
        ("body_added_required", BREAKING, "POST /item/get", "options.user.id"),
        ("body_removed", BREAKING, "POST /item/get", "options.user.name"),
    ]


def test_a_schema_that_is_sent_and_returned_is_judged_in_both_directions():
    assert rows(run(lambda d: schemas(d)["Address"]["properties"].pop("region"))) == [
        ("body_removed", BREAKING, "POST /item/get", "options.user.address.region"),
        ("response_field_removed", BREAKING, "schema Address", "region"),
    ]
    changes = run(lambda d: schemas(d)["Address"].update(required=["city"]))
    assert rows(changes) == [("body_now_required", BREAKING, "POST /item/get", "options.user.address.city")]


def test_a_schema_no_operation_refers_to_keeps_the_response_rules():
    changes = run(lambda d: schemas(d)["ItemWebhook"]["properties"].pop("code"))
    assert rows(changes) == [("response_field_removed", BREAKING, "schema ItemWebhook", "code")]


def deep_doc():
    """A request nested deeper than the body is flattened: Deep4's fields are never listed."""
    doc = plaid_doc()
    s = schemas(doc)
    s["ItemGetRequest"]["properties"]["a"] = ref("Deep1")
    for level, field in enumerate("bcd", start=1):
        s[f"Deep{level}"] = {"type": "object", "properties": {field: ref(f"Deep{level + 1}")}}
    s["Deep4"] = {"type": "object", "properties": {"e": {"type": "string"}, "f": {"type": "string"}}}
    return doc


def test_a_request_schema_nested_too_deep_to_list_is_still_diffed_by_the_request_rules():
    spec = load_spec(deep_doc())
    assert "a.b.c.d" in spec.operations[ITEM_GET].body and "a.b.c.d.e" not in spec.operations[ITEM_GET].body
    assert "Deep4" in spec.operations[ITEM_GET].sends and "Deep4" not in spec.operations[ITEM_GET].listed

    def change(d):
        deep = schemas(d)["Deep4"]
        deep["properties"]["e"] = {"type": "integer"}
        deep["properties"].pop("f")
        deep["properties"]["g"] = {"type": "string"}
        deep["required"] = ["g"]

    changes = run(change, base=deep_doc)
    assert rows(changes) == [
        ("request_added_required", BREAKING, "schema Deep4", "g"),
        ("request_removed", BREAKING, "schema Deep4", "f"),
        ("request_type_changed", BREAKING, "schema Deep4", "e"),
    ]
    assert all(c.affects == ("POST /item/get",) and c.method is None for c in changes)
    assert "request field f was removed (on Deep4)" in [c.message for c in changes]
    # one level up the fields are listed, so the operation reports the change itself
    assert rows(run(lambda d: schemas(d)["Deep3"]["properties"].pop("d"), base=deep_doc)) == [
        ("body_removed", BREAKING, "POST /item/get", "a.b.c.d")
    ]


def test_allof_around_one_schema_is_that_schema():
    doc = plaid_doc()
    s = schemas(doc)
    s["ItemGetRequest"]["properties"]["score_version"] = {"allOf": [ref("ScoreVersion")], "description": "which score", "deprecated": True}
    s["Item"]["properties"]["status"] = {"allOf": [ref("ItemStatus"), {}], "x-hidden-from-docs": True}
    s["Item"]["properties"]["address"] = {"allOf": [ref("AddressNullable")]}
    s["AddressNullable"] = {"allOf": [ref("Address"), {"type": "object", "additionalProperties": True}], "nullable": True}
    spec = load_spec(doc)
    version = spec.operations[ITEM_GET].body["score_version"]
    assert (version.type, version.enum, version.deprecated) == ("string", ("v1", "v2"), True)
    assert spec.schemas["Item"].fields["status"].enum == ("good", "bad")
    assert spec.schemas["Item"].fields["address"].type == "ref:AddressNullable"
    assert set(spec.schemas["AddressNullable"].fields) == {"city", "region"}
    plain = load_spec(plaid_doc())
    assert [c for c in diff_specs(plain, spec) if c.kind not in ("schema_added", "body_deprecated")] == []


def test_allof_composition_is_one_object_with_every_members_fields():
    doc = plaid_doc()
    s = schemas(doc)
    s["Item"] = {
        "allOf": [ref("ItemBase"), {"type": "object", "required": ["address"], "properties": {"address": ref("Address")}}]
    }
    s["ItemBase"] = {"type": "object", "required": ["status"], "properties": {"status": ref("ItemStatus")}}
    s["ItemGetRequest"]["properties"]["options"] = {
        "allOf": [ref("ItemGetOptions")], "type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]
    }
    spec = load_spec(doc)
    item = spec.schemas["Item"].fields
    assert {name: (f.type, f.required) for name, f in item.items()} == {"status": ("string", True), "address": ("ref:Address", True)}
    body = spec.operations[ITEM_GET].body
    assert body["options"].type == "object" and body["options.count"].required
    assert body["options.user.name"].type == "string"

    def move_field_between_members(d):
        base, extension = schemas(d)["ItemBase"], schemas(d)["Item"]["allOf"][1]
        extension["properties"]["status"] = base["properties"].pop("status")
        extension["required"].append(base["required"].pop())

    changes = diff_specs(spec, load_spec((move_field_between_members(doc), doc)[1]))
    assert rows(changes) == [("response_field_removed", BREAKING, "schema ItemBase", "status")]  # Item itself is unchanged


def test_a_named_union_stays_a_reference():
    doc = plaid_doc()
    schemas(doc)["Source"] = {"anyOf": [ref("Item"), ref("Address")]}
    schemas(doc)["Item"]["properties"]["source"] = ref("Source")
    assert load_spec(doc).schemas["Item"].fields["source"].type == "ref:Source"


def test_dropping_the_type_keyword_widens_and_adding_one_narrows():
    def untyped(d, name, field):
        schemas(d)[name]["properties"][field] = {"description": "a string or a number"}

    assert rows(run(lambda d: untyped(d, "ItemGetResponse", "request_id"))) == [
        ("response_type_widened", WARNING, "schema ItemGetResponse", "request_id")
    ]
    assert rows(run(lambda d: untyped(d, "User", "name"))) == [("body_type_widened", INFO, "POST /item/get", "options.user.name")]
    old, new = plaid_doc(), plaid_doc()
    untyped(old, "User", "name")
    assert rows(diff_specs(load_spec(old), load_spec(new))) == [
        ("body_type_narrowed", BREAKING, "POST /item/get", "options.user.name")
    ]


def test_defining_a_schema_the_old_spec_only_referred_to_is_not_a_change():
    old, new = plaid_doc(), plaid_doc()
    for doc in (old, new):
        schemas(doc)["User"]["properties"]["tier"] = ref("Tier")
        schemas(doc)["Item"]["properties"]["tier"] = ref("Tier")
    schemas(new)["Tier"] = {"type": "string", "enum": ["free", "paid"]}
    assert rows(diff_specs(load_spec(old), load_spec(new))) == [
        ("response_enum_introduced", INFO, "schema Item", "tier"),
        ("schema_added", INFO, "schema Tier", ""),
    ]
    assert rows(diff_specs(load_spec(new), load_spec(old))) == [
        ("response_enum_dropped", WARNING, "schema Item", "tier"),
        ("schema_removed", INFO, "schema Tier", ""),
    ]
