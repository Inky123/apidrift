import pytest

from apidrift.openapi import load_spec
from apidrift.sdkmap import SdkIndex
from specs import base_doc, sdk_doc


@pytest.fixture(scope="module")
def index() -> SdkIndex:
    return SdkIndex.build(load_spec(sdk_doc()))


@pytest.mark.parametrize(
    "chain,label",
    [
        # Python legacy classes
        ("stripe.Charge.create", "POST /v1/charges"),
        ("stripe.Charge.list", "GET /v1/charges"),
        ("stripe.Charge.retrieve", "GET /v1/charges/{charge}"),
        ("stripe.Charge.capture", "POST /v1/charges/{charge}/capture"),
        ("stripe.Charge.create_refund", "POST /v1/charges/{charge}/refunds"),
        ("stripe.checkout.Session.create", "POST /v1/checkout/sessions"),
        # StripeClient / service style
        ("client.v1.charges.create", "POST /v1/charges"),
        ("client.v1.charges.capture", "POST /v1/charges/{charge}/capture"),
        ("client.v1.charges.refunds.create", "POST /v1/charges/{charge}/refunds"),
        ("client.v1.checkout.sessions.create", "POST /v1/checkout/sessions"),
        # Node camelCase
        ("stripe.charges.createRefund", "POST /v1/charges/{charge}/refunds"),
        ("this.stripe.checkout.sessions.create", "POST /v1/checkout/sessions"),
        # Ruby constants
        ("Stripe.Checkout.Session.create", "POST /v1/checkout/sessions"),
        ("Stripe.Charge.capture", "POST /v1/charges/{charge}/capture"),
    ],
)
def test_resolves_sdk_naming_styles(index, chain, label):
    resolved = index.resolve(chain.split("."))
    assert resolved is not None and resolved.label == label


def test_unknown_chains_resolve_to_nothing(index):
    assert index.resolve(["stripe", "Webhook", "construct_event"]) is None
    assert index.resolve(["console", "log"]) is None
    assert index.resolve(["create"]) is None


def test_bare_create_on_parent_is_not_the_nested_resource(index):
    assert index.resolve(["stripe", "charges", "create"]).path == "/v1/charges"
    assert index.resolve(["stripe", "Charge", "create"]).path == "/v1/charges"


def test_match_path_maps_concrete_urls_to_templates(index):
    assert index.match_path("/v1/charges/ch_123") == "/v1/charges/{charge}"
    assert index.match_path("/v1/charges/${id}/capture") == "/v1/charges/{charge}/capture"
    assert index.match_path("/v1/charges") == "/v1/charges"
    assert index.match_path("/v1/nothing") is None
    assert index.methods_for("/v1/charges") == ["get", "post"]


def test_spec_without_sdk_metadata_gives_empty_index():
    assert SdkIndex.build(load_spec(base_doc())).empty
    assert not SdkIndex.build(load_spec(sdk_doc())).empty


def test_union_of_specs_keeps_operations_removed_in_the_newer_one():
    old, new = sdk_doc(), sdk_doc()
    new["paths"].pop("/v1/charges/{charge}/capture")
    new["components"]["schemas"]["charge"]["x-stripeOperations"].pop()
    index = SdkIndex.build(load_spec(old), load_spec(new))
    assert index.resolve(["stripe", "Charge", "capture"]).path == "/v1/charges/{charge}/capture"
