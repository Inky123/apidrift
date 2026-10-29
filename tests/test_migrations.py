from apidrift import migrations
from apidrift.diff import BREAKING, Change


def removed(path, target="payment_method_types", method="post", kind="body_removed"):
    return Change(BREAKING, kind, "x", method=method, path=path, target=target)


def test_the_six_payment_method_types_endpoints_rename_to_allowed_payment_method_types():
    paths = {
        "/v1/payment_intents", "/v1/payment_intents/{intent}", "/v1/payment_intents/{intent}/confirm",
        "/v1/setup_intents", "/v1/setup_intents/{intent}", "/v1/checkout/sessions",
    }
    assert {m.path for m in migrations.MIGRATIONS if m.target == "payment_method_types"} == paths
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


def test_v2_entries_actions_and_lookup():
    payto = migrations.lookup(removed("/v1/payment_methods/{payment_method}", target="payto"))
    assert payto.action == "delete" and payto.replacement == ""
    countries = migrations.lookup(removed("/v1/financial_connections/sessions", target="filters.countries"))
    assert countries.action == "rename_first" and countries.replacement == "filters.country"
    anchor = migrations.lookup(removed("/v1/subscriptions/{subscription_exposed_id}", target="billing_cycle_anchor",
                                       kind="body_type_changed"))
    assert anchor.action == "wrap_type" and anchor.wrap_any and not anchor.timestamps
    same = migrations.lookup(removed("/v1/subscriptions/{subscription_exposed_id}", target="billing_cycle_anchor.type",
                                     kind="body_added_required"))
    assert same is anchor  # the required .type is answered by the same wrap
    preview = migrations.for_site("/v1/invoices/create_preview", "subscription_details.billing_cycle_anchor")
    assert preview.timestamps and not preview.wrap_any and preview.wrap_values == ("now", "unchanged")
    assert migrations.lookup(removed("/v1/subscriptions/{subscription_exposed_id}", target="billing_cycle_anchor")) is None
    assert migrations.lookup(removed("/v1/tax/registrations", target="country_options.at.igic")) is None  # left out
    assert {m.action for m in migrations.MIGRATIONS} == set(migrations.ACTIONS)


def test_expected_delta_and_migrated():
    payto = migrations.for_site("/v1/payment_methods/{payment_method}", "payto")
    assert migrations.expected_delta(payto, {"payto", "payto.pay_id", "metadata"}) == ({"payto", "payto.pay_id"}, set())
    assert migrations.migrated(payto, {"metadata"}) and not migrations.migrated(payto, {"payto.pay_id"})
    countries = migrations.for_site("/v1/financial_connections/sessions", "filters.countries")
    assert migrations.expected_delta(countries, {"filters", "filters.countries"}) == ({"filters.countries"}, {"filters.country"})
    assert migrations.migrated(countries, {"filters", "filters.country"})
    anchor = migrations.for_site("/v1/subscriptions/{subscription}/resume", "billing_cycle_anchor")
    assert migrations.expected_delta(anchor, {"billing_cycle_anchor"}) == (set(), {"billing_cycle_anchor.type"})
    preview = migrations.for_site("/v1/invoices/create_preview", "subscription_details.billing_cycle_anchor")
    t = "subscription_details.billing_cycle_anchor"
    assert migrations.expected_delta(preview, {t}, timestamp=True) == (set(), {t + ".type", t + ".timestamp"})
    assert migrations.migrated(anchor, {"billing_cycle_anchor", "billing_cycle_anchor.type"})
    assert not migrations.migrated(anchor, {"billing_cycle_anchor"})
    pmt = migrations.for_site("/v1/payment_intents", "payment_method_types")
    assert migrations.migrated(pmt, {"allowed_payment_method_types"}) and not migrations.migrated(pmt, {"payment_method_types"})
