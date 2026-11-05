"""Schema-level changes of a vendor that names everything, placed on operations and judged at call sites."""

import textwrap

from apidrift.diff import BREAKING, Change, collapse_removals, diff_specs
from apidrift.impact import AFFECTED, POSSIBLE, assess
from apidrift.localize import place, places, shared_leaves
from apidrift.openapi import load_spec
from apidrift.plaidmap import PlaidIndex
from apidrift.plaidscan import scan_text
from plaidspec import STRING, plaid_doc, ref, schemas

LINK = ("post", "/link/token/create")
NODE_HEAD = "import { PlaidApi } from 'plaid';\nconst client = new PlaidApi(config);\n"
PY_HEAD = textwrap.dedent("""\
    from plaid.api import plaid_api
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
    from plaid.model.user_address import UserAddress
    from plaid.model.geo import Geo
    from plaid.model.geo_source import GeoSource
    client = plaid_api.PlaidApi(api_client)
""")


def specs(mutate):
    old, new = plaid_doc(), plaid_doc()
    mutate(new)
    return load_spec(old), load_spec(new)


def impacts(mutate, source, rel="app.ts", called=None):
    old, new = specs(mutate)
    head = PY_HEAD if rel.endswith(".py") else NODE_HEAD
    usages, _, _ = scan_text(head + textwrap.dedent(source), rel, PlaidIndex.build(old, new))
    called = called if called is not None else {u.path for u in usages}
    changes = place(diff_specs(old, new), old, new, called)  # the steps ``apidrift impact --provider plaid`` takes
    lines = head.count("\n")
    found = assess(usages, changes, shared_leaves(old, new, called))
    return [(i.usage.line - lines, i.change.kind, i.change.target, i.certainty) for i in found]


def drop_accuracy(doc):
    schemas(doc)["GeoSource"]["properties"].pop("accuracy")


def test_the_diff_reports_a_deep_request_schema_once_and_place_gives_it_the_full_path_per_operation():
    old, new = specs(drop_accuracy)
    [change] = [c for c in diff_specs(old, new) if c.severity == BREAKING]
    assert (change.kind, change.schema, change.target, change.method, change.affects) == (
        "request_removed", "GeoSource", "accuracy", None, ("POST /link/token/create",)
    )
    [placed] = [c for c in place([change], old, new) if c.severity == BREAKING]
    assert (placed.kind, placed.method, placed.path, placed.target, placed.affects) == (
        "request_removed", "post", "/link/token/create", "user.address.geo.source.accuracy", ()
    )
    assert placed.message == "request field user.address.geo.source.accuracy was removed (on GeoSource)"
    assert place([change], old, new, called={"/accounts/get"}) == []  # no call reaches an operation that sends it


def test_a_deep_request_field_hits_the_call_that_sends_it_and_possibly_the_ones_that_may():
    found = impacts(drop_accuracy, """
        client.linkTokenCreate({ client_name: 'A', user: { address: { geo: { source: { accuracy: 'high' } } } } });
        client.linkTokenCreate({ client_name: 'A', user: { address: { geo: { source: { provider: 'gps' } } } } });
        client.linkTokenCreate({ client_name: 'A', user: { client_user_id: id } });
        client.linkTokenCreate({ client_name: 'A', user: { address: { geo } } });
        client.linkTokenCreate({ client_name: 'A', user: { address: { geo: { source: { ...src } } } } });
        client.linkTokenCreate({ ...base, client_name: 'A' });
        client.linkTokenCreate(request);
        client.accountsGet(request);
    """)
    target = "user.address.geo.source.accuracy"
    assert found == [
        (2, "request_removed", target, AFFECTED), (5, "request_removed", target, POSSIBLE),
        (6, "request_removed", target, POSSIBLE), (7, "request_removed", target, POSSIBLE),
        (8, "request_removed", target, POSSIBLE),
    ]


def test_the_same_change_is_judged_on_python_models():
    found = impacts(drop_accuracy, """
        def a(uid):
            user = LinkTokenCreateRequestUser(client_user_id=uid, address=UserAddress(geo=Geo(source=GeoSource(accuracy="high"))))
            return client.link_token_create(LinkTokenCreateRequest("A", "en", ["US"], user=user))
        def b(uid):
            return client.link_token_create(LinkTokenCreateRequest("A", "en", ["US"], user=LinkTokenCreateRequestUser(uid)))
        def c(geo):
            return client.link_token_create(LinkTokenCreateRequest("A", "en", ["US"], user={"address": {"geo": geo}}))
        def d(**kw):
            return client.link_token_create(LinkTokenCreateRequest(**kw))
    """, rel="bank/link.py")
    target = "user.address.geo.source.accuracy"
    assert found == [(4, "request_removed", target, AFFECTED), (8, "request_removed", target, POSSIBLE), (10, "request_removed", target, POSSIBLE)]


def test_a_new_required_field_deep_in_a_request_binds_the_calls_that_send_its_object():
    def require(doc):
        schemas(doc)["GeoSource"]["required"] = ["provider"]

    found = impacts(require, """
        client.linkTokenCreate({ client_name: 'A', user: { address: { geo: { source: { accuracy: 'high' } } } } });
        client.linkTokenCreate({ client_name: 'A', user: { address: { geo: { source: { provider: 'gps' } } } } });
        client.linkTokenCreate({ client_name: 'A', user: { client_user_id: id } });
    """)
    assert found == [(2, "request_now_required", "user.address.geo.source.provider", AFFECTED)]


def test_a_schema_sent_in_a_list_or_at_several_places_gets_one_change_per_place():
    def nest(doc):
        s = schemas(doc)
        s["Geo"]["properties"]["sources"] = {"type": "array", "items": ref("GeoSource")}

    old, new = plaid_doc(), plaid_doc()
    nest(old), nest(new)
    drop_accuracy(new)
    old, new = load_spec(old), load_spec(new)
    assert places(new, new.operations[LINK], "GeoSource") == {"user.address.geo.source.", "user.address.geo.sources[]."}
    assert places(new, new.operations[LINK], "LinkTokenCreateRequestUser") == {"user."}
    assert places(new, new.operations[LINK], "LinkTokenCreateRequest") == {""}
    assert places(new, new.operations[("post", "/accounts/get")], "GeoSource") == set()
    targets = sorted(c.target for c in place(diff_specs(old, new), old, new) if c.kind == "request_removed")
    assert targets == ["user.address.geo.source.accuracy", "user.address.geo.sources[].accuracy"]


def test_a_request_schema_that_refers_to_itself_does_not_loop():
    doc = plaid_doc()
    schemas(doc)["GeoSource"]["properties"]["parent"] = ref("GeoSource")
    spec = load_spec(doc)
    found = places(spec, spec.operations[LINK], "GeoSource")
    assert "user.address.geo.source." in found and "user.address.geo.source.parent." in found and len(found) < 20


def test_a_request_change_that_cannot_be_placed_is_possible_wherever_its_operation_is_called():
    old, new = specs(lambda doc: None)
    usages, _, _ = scan_text(NODE_HEAD + "client.accountsGet({ access_token: t });\n", "a.js", PlaidIndex.build(old, new))
    lost = Change(BREAKING, "request_removed", "request field x was removed (on Ghost)", schema="Ghost", target="x", affects=("POST /accounts/get",))
    relaxed = Change("info", "request_added", "new optional request field y (on Ghost)", schema="Ghost", target="y", affects=("POST /accounts/get",))
    placed = place([lost, relaxed], old, new)
    assert [(c.method, c.affects) for c in placed] == [(None, ("POST /accounts/get",))] * 2
    [impact] = assess(usages, placed)
    assert (impact.certainty, impact.reason) == (POSSIBLE, "this request can carry Ghost; whether it does here is not visible")


def test_a_change_inside_a_returned_object_reaches_the_operations_whose_response_carries_it():
    def drop(doc):
        schemas(doc)["Transaction"]["properties"].pop("merchant_name")

    old, new = specs(drop)
    [change] = [c for c in diff_specs(old, new) if c.severity == BREAKING]
    assert (change.kind, change.schema, change.target, change.affects) == ("response_field_removed", "Transaction", "merchant_name", ())
    [placed] = [c for c in place([change], old, new) if c.severity == BREAKING]
    assert placed.affects == ("POST /transactions/get", "POST /transactions/sync")
    [narrowed] = [c for c in place([change], old, new, called={"/transactions/sync", "/accounts/get"}) if c.severity == BREAKING]
    assert narrowed.affects == ("POST /transactions/sync",)

    source = """
        const page = await client.transactionsSync({ access_token: t });
        const accounts = await client.accountsGet({ access_token: t });
    """
    assert impacts(drop, source + "show(page.data.added.map((tx) => tx.merchant_name));\n") == [
        (2, "response_field_removed", "merchant_name", AFFECTED)
    ]
    assert impacts(drop, source) == [(2, "response_field_removed", "merchant_name", POSSIBLE)]


def test_a_read_of_a_name_several_returned_objects_share_is_only_possible():
    def drop(doc):
        schemas(doc)["Account"]["properties"].pop("mask")

    old, new = plaid_doc(), plaid_doc()
    for doc in (old, new):
        schemas(doc)["Transaction"]["properties"]["mask"] = STRING
    drop(new)
    old, new = load_spec(old), load_spec(new)
    assert "mask" in shared_leaves(old, new, {"/accounts/get", "/transactions/get"})
    assert "mask" not in shared_leaves(old, new, {"/accounts/get"}) and "official_name" not in shared_leaves(old, new, {"/accounts/get", "/transactions/get"})

    source = NODE_HEAD + "const a = await client.accountsGet({ access_token: t });\nshow(a.data.accounts[0].mask);\n"
    usages, _, _ = scan_text(source, "a.js", PlaidIndex.build(old, new))
    changes = place(diff_specs(old, new), old, new)
    [alone] = assess(usages, changes, shared_leaves(old, new, {"/accounts/get"}))
    assert (alone.certainty, alone.reason) == (AFFECTED, "response field mask is read in this file")
    [among] = assess(usages, changes, shared_leaves(old, new, {"/accounts/get", "/transactions/get"}))
    assert (among.certainty, among.reason) == (POSSIBLE, "a field named mask is read in this file (several returned objects have one)")


def test_a_read_is_certain_only_when_one_operation_called_in_the_file_returns_the_object():
    def drop(doc):
        schemas(doc)["Item"]["properties"].pop("webhook")

    one = """
        const a = await client.accountsGet({ access_token: t });
        const b = await client.accountsGet({ access_token: u });
        show(a.data.item.webhook);
    """
    assert impacts(drop, one) == [(2, "response_field_removed", "webhook", AFFECTED), (3, "response_field_removed", "webhook", AFFECTED)]
    two = one + "const c = await client.transactionsGet({ access_token: t, start_date: s, end_date: e });\n"
    assert impacts(drop, two) == [
        (2, "response_field_removed", "webhook", POSSIBLE), (3, "response_field_removed", "webhook", POSSIBLE),
        (5, "response_field_removed", "webhook", POSSIBLE),
    ]


def test_a_schema_returned_directly_keeps_its_operations_and_gains_the_ones_that_nest_it():
    def drop(doc):
        schemas(doc)["Item"]["properties"].pop("webhook")
        doc["paths"]["/item/get"] = {"post": {"operationId": "itemGet", "responses": {"200": {"content": {"application/json": {"schema": ref("Item")}}}}}}

    old, new = plaid_doc(), plaid_doc()
    old["paths"]["/item/get"] = {"post": {"operationId": "itemGet", "responses": {"200": {"content": {"application/json": {"schema": ref("Item")}}}}}}
    drop(new)
    old, new = load_spec(old), load_spec(new)
    [change] = [c for c in diff_specs(old, new) if c.severity == BREAKING]
    assert change.affects == ("POST /item/get",)
    [placed] = [c for c in place([change], old, new) if c.severity == BREAKING]
    assert placed.affects == ("POST /accounts/get", "POST /item/get", "POST /transactions/get")


def test_receives_follows_maps_and_unions_that_field_types_do_not_show():
    doc = plaid_doc()
    schemas(doc)["AccountsGetResponse"]["properties"]["by_id"] = {"type": "object", "additionalProperties": ref("Geo")}
    spec = load_spec(doc)
    op = spec.operations[("post", "/accounts/get")]
    assert {"AccountsGetResponse", "Account", "Item", "Geo", "GeoSource"} <= op.receives
    assert "AccountsGetRequest" not in op.receives and "AccountsGetRequest" in op.sends


def test_collapsed_request_removals_keep_the_schema_they_are_on():
    removed = [
        Change(BREAKING, "request_removed", f"request field options.{cc}.tax was removed (on Opts)", schema="Opts", target=f"options.{cc}.tax", affects=("POST /x",))
        for cc in ("at", "be", "de")
    ]
    [merged] = collapse_removals(removed)
    assert merged.target == "options.*.tax" and merged.affects == ("POST /x",)
    assert merged.message == "request field options.*.tax was removed (3 keys: at, be, de) (on Opts)"


def test_stripe_shaped_changes_pass_through_place_untouched():
    old, new = specs(lambda doc: schemas(doc)["TransactionsSyncRequest"]["properties"].pop("count"))
    changes = diff_specs(old, new)
    body = [c for c in changes if c.kind == "body_removed"]
    assert [(c.method, c.target) for c in body] == [("post", "count")]
    assert [c for c in place(changes, old, new) if c.kind == "body_removed"] == body


def test_a_field_the_operation_already_lists_is_not_reported_twice_after_placing():
    # UserAddress.extra is an inline object the operation's body lists down to extra.inner and no further
    def inner():
        return {"type": "object", "properties": {"deep": STRING, "deeper": {"type": "object", "properties": {"x": STRING}}}}

    old, new = plaid_doc(), plaid_doc()
    for doc in (old, new):
        schemas(doc)["UserAddress"]["properties"]["extra"] = {"type": "object", "properties": {"inner": inner(), "kept": STRING}}
    extra = schemas(new)["UserAddress"]["properties"]["extra"]["properties"]
    extra.pop("kept")
    extra["inner"]["properties"].pop("deep")
    old, new = load_spec(old), load_spec(new)
    raw = diff_specs(old, new)
    assert sorted((c.kind, c.target) for c in raw if c.severity == BREAKING) == [
        ("body_removed", "user.address.extra.kept"), ("request_removed", "extra.inner.deep"), ("request_removed", "extra.kept"),
    ]
    assert sorted((c.kind, c.target) for c in place(raw, old, new) if c.severity == BREAKING) == [
        ("body_removed", "user.address.extra.kept"), ("request_removed", "user.address.extra.inner.deep"),
    ]
