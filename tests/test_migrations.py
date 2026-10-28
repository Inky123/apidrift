from apidrift import migrations
from apidrift.diff import BREAKING, Change


def removed(path, target="payment_method_types", method="post", kind="body_removed"):
    return Change(BREAKING, kind, "x", method=method, path=path, target=target)


def test_the_six_payment_method_types_endpoints_rename_to_allowed_payment_method_types():
    paths = {
        "/v1/payment_intents", "/v1/payment_intents/{intent}", "/v1/payment_intents/{intent}/confirm",
        "/v1/setup_intents", "/v1/setup_intents/{intent}", "/v1/checkout/sessions",
    }
    assert {m.path for m in migrations.MIGRATIONS} == paths
    for path in paths:
        found = migrations.lookup(removed(path))
        assert found is not None and found.action == "rename"
        assert found.replacement == "allowed_payment_method_types" and found.introduced == "2026-09-30.endive"
        assert found.doc.startswith("https://docs.stripe.com/changelog/endive/2026-09-30/")
        assert "allowed_payment_method_types" in found.note


def test_only_documented_removals_match():
    assert migrations.lookup(removed("/v1/payment_links")) is None  # field still exists there
    assert migrations.lookup(removed("/v1/payment_intents", target="metadata")) is None
    assert migrations.lookup(removed("/v1/payment_intents", method="get")) is None
    assert migrations.lookup(removed("/v1/payment_intents", kind="body_type_changed")) is None
    assert migrations.lookup(Change(BREAKING, "body_removed", "x", schema="s", target="payment_method_types")) is None


def test_dict_and_method_case_variants():
    change = removed("/v1/payment_intents", method="POST")
    assert migrations.lookup(change) is migrations.lookup_dict(change.to_dict())
    assert migrations.lookup_dict({"kind": "body_removed"}) is None
    assert migrations.replacement_for("POST", "/v1/setup_intents", "payment_method_types") == "allowed_payment_method_types"
    assert migrations.replacement_for("post", "/v1/setup_intents", "other") is None
    assert migrations.MIGRATIONS[0].to_dict()["replacement"] == "allowed_payment_method_types"


def test_introduced_for_reads_only_the_table():
    assert migrations.introduced_for("/v1/checkout/sessions", "payment_method_types") == "2026-09-30.endive"
    assert migrations.introduced_for("/v1/charges", "description") is None
