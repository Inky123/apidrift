"""The guidance table: keys shaped like the differ's changes, Stripe-sourced options, nothing the fixer already does."""

import re

import pytest

from apidrift import guidance, migrations
from apidrift.diff import diff_specs
from apidrift.guidance import GUIDANCE, PIN, Option
from apidrift.openapi import load_spec
from specs import _form, base_doc, charge_ref

PATH_RE = re.compile(r"/v[12](?:/[a-z0-9_{}]+)+")
TARGET_RE = re.compile(r"[a-z0-9_]+(?:\[\])?(?:\.[a-z0-9_]+(?:\[\])?)*")
TEXT_RE = re.compile(r"[A-Za-z0-9 .,;:'()\[\]{}/_`-]+")  # what an option may be written with
FIXED = (
    guidance.PIN, guidance.PIN_HOW, guidance.CHECK_HIDDEN, guidance.REMOVE_FIELD, guidance.SEND_FIELD,
    guidance.NEW_SHAPE, guidance.ACCEPTED_VALUE, guidance.STOP_READING, guidance.EVERY_SHAPE,
)


def _properties(parts: list[str], present: bool) -> dict:
    """A request-body schema holding the dotted field ``parts`` (``a[]`` is an array of objects) when ``present``."""
    name, rest = parts[0].removesuffix("[]"), parts[1:]
    if rest:
        inner = {"type": "object", "properties": {"kept": {"type": "string"}, **_properties(rest, present)}}
    elif present:
        inner = {"type": "string"}
    else:
        return {}
    return {name: {"type": "array", "items": inner} if parts[0].endswith("[]") else inner}


def _doc(entry: guidance.Guidance, present: bool) -> dict:
    doc = base_doc()
    doc["paths"][entry.path] = {entry.method: {
        "requestBody": _form({"kept": {"type": "string"}, **_properties(entry.target.split("."), present)}),
        "responses": {"200": {"content": {"application/json": {"schema": charge_ref()}}}},
    }}
    return doc


@pytest.mark.parametrize("entry", GUIDANCE, ids=lambda g: f"{g.path}:{g.target}")
def test_every_key_is_the_key_the_differ_gives_that_change(entry):
    assert entry.method in ("get", "post", "delete") and PATH_RE.fullmatch(entry.path) and TARGET_RE.fullmatch(entry.target)
    assert entry.kind == "body_removed", "add a spec shape for this kind to the test before curating it"
    changes = diff_specs(load_spec(_doc(entry, True)), load_spec(_doc(entry, False)))
    assert [(c.method, c.path, c.kind, c.target) for c in changes] == [(entry.method, entry.path, entry.kind, entry.target)]
    assert guidance.lookup(entry.method.upper(), entry.path, entry.kind, entry.target) is entry  # a finding's method is upper


def test_keys_are_unique_and_every_entry_links_stripes_docs():
    assert len({(g.method, g.path, g.kind, g.target) for g in GUIDANCE}) == len(GUIDANCE) >= 2
    for entry in GUIDANCE:
        assert 1 <= len(entry.options) < guidance.MAX_OPTIONS  # the version pin makes it two to four
        for doc in (entry.doc, *(o.doc for o in entry.options)):
            assert doc.startswith("https://docs.stripe.com/") and not re.search(r"[\s()<>\"]", doc)


def test_no_entry_repeats_a_migration_the_fixer_performs():
    replacements = {m.replacement for m in migrations.MIGRATIONS if m.replacement and m.replacement != m.target}
    for entry in GUIDANCE:
        change = {"kind": entry.kind, "method": entry.method, "path": entry.path, "target": entry.target}
        assert migrations.lookup_dict(change) is None and migrations.for_site(entry.path, entry.target) is None
        for option in entry.options:
            assert not any(f"`{name}`" in option.text for name in replacements)


def test_option_text_is_one_plain_sentence_with_balanced_code_marks():
    by_hand = [Option(guidance.by_hand(m)) for m in migrations.MIGRATIONS]
    for option in (*FIXED, *by_hand, *(o for g in GUIDANCE for o in g.options)):
        text = option.text
        assert TEXT_RE.fullmatch(text), text  # no @mention, #reference, link, HTML or newline can be written
        assert text.count("`") % 2 == 0 and "http" not in text and len(text) <= 360
        assert option.doc == "" or option.doc.startswith("https://docs.stripe.com/")
    for option in (*FIXED, *(o for g in GUIDANCE for o in g.options)):
        assert option.text.endswith(".")


def test_a_curated_change_gets_its_options_then_the_pin():
    entry = guidance.lookup("post", "/v1/checkout/sessions", "body_removed", "line_items[].dynamic_tax_rates")
    assert entry is not None and entry.doc == "https://docs.stripe.com/payments/checkout/use-manual-tax-rates"
    found = guidance.options("POST", entry.path, entry.kind, entry.target, certain=True, possible=False)
    assert found == (*entry.options, PIN)
    assert "`automatic_tax.enabled`" in found[0].text and found[0].doc == "https://docs.stripe.com/payments/checkout/taxes"
    assert "`line_items[].tax_rates`" in found[1].text
    with_hidden = guidance.options("POST", entry.path, entry.kind, entry.target, certain=True, possible=True)
    assert with_hidden == (*entry.options, guidance.CHECK_HIDDEN, PIN)
    accounts = guidance.options("post", "/v1/accounts", "body_removed", "documents.proof_of_registration", certain=True, possible=False)
    assert len(accounts) == 2 and accounts[0].doc == "https://docs.stripe.com/api/accounts/update"
    assert guidance.lookup("post", "/v1/accounts/{account}", "body_removed", "documents.proof_of_registration") is None


@pytest.mark.parametrize("kind,first", [
    ("body_removed", guidance.REMOVE_FIELD), ("param_removed", guidance.REMOVE_FIELD),
    ("body_added_required", guidance.SEND_FIELD), ("param_now_required", guidance.SEND_FIELD),
    ("body_type_changed", guidance.NEW_SHAPE), ("body_enum_removed", guidance.ACCEPTED_VALUE),
    ("param_enum_restricted", guidance.ACCEPTED_VALUE), ("response_field_removed", guidance.STOP_READING),
    ("response_schema_changed", guidance.EVERY_SHAPE),
])
def test_generic_options_by_kind(kind, first):
    assert guidance.options("post", "/v1/charges", kind, "description", certain=True, possible=False) == (first, PIN)
    assert guidance.options("post", "/v1/charges", kind, "description", certain=False, possible=True) == (first, guidance.CHECK_HIDDEN, PIN)


def test_kinds_without_a_generic_action_get_the_pin_only_and_a_deprecation_gets_nothing():
    for kind in ("endpoint_removed", "schema_removed", "response_type_changed", "response_enum_added"):
        assert guidance.options("get", "/v1/charges", kind, "", certain=True, possible=False) == (PIN,)
    assert guidance.options("get", "/v1/charges", "endpoint_deprecated", "", certain=True, possible=False) == ()
    assert guidance.options("post", "/v1/charges", "body_removed", "description", certain=False, possible=False) == ()


def test_a_documented_migration_is_offered_by_hand_from_the_migration_table():
    pmt = migrations.for_site("/v1/setup_intents", "payment_method_types")
    certain, pin = guidance.options("post", pmt.path, "body_removed", pmt.target, certain=True, possible=False)
    assert pin is PIN and certain.doc == pmt.doc
    assert certain.text == ("Make Stripe's documented change by hand at the lines apidrift did not rewrite: "
                            "rename `payment_method_types` to `allowed_payment_method_types`.")
    both = guidance.options("post", pmt.path, "body_removed", pmt.target, certain=True, possible=True)
    assert len(both) == 3 and both[0] == certain and both[1].doc == pmt.doc
    assert both[1].text.startswith("For a possibly affected site, find where the arguments apidrift cannot see are built")
    assert both[1].text.endswith("if they carry the field, rename `payment_method_types` to `allowed_payment_method_types` there.")
    # the required ``.type`` of a wrapped value is answered by the same migration
    anchor = guidance.options("post", "/v1/subscriptions/{subscription_exposed_id}", "body_added_required",
                              "billing_cycle_anchor.type", certain=True, possible=False)
    assert "wrap the `billing_cycle_anchor` value in an object with `type`" in anchor[0].text


def test_by_hand_names_each_action():
    said = {m.action: guidance.by_hand(m) for m in migrations.MIGRATIONS}
    assert set(said) == set(migrations.ACTIONS)
    assert said["delete"] == "remove `payto`"
    assert said["rename_first"] == "send `filters.country` with the first element of the `filters.countries` list"
    assert said["wrap_type"].startswith("wrap the `") and said["rename"].startswith("rename `payment_method_types` to")
    preview = migrations.for_site("/v1/invoices/create_preview", "subscription_details.billing_cycle_anchor")
    assert guidance.by_hand(preview) == (
        "wrap the `subscription_details.billing_cycle_anchor` value in an object with `type` "
        "(an integer timestamp goes in `timestamp`, with `type` set to `timestamp`)"
    )
    resume = migrations.for_site("/v1/subscriptions/{subscription}/resume", "billing_cycle_anchor")
    assert guidance.by_hand(resume) == "wrap the `billing_cycle_anchor` value in an object with `type`"


def test_options_never_exceed_the_cap():
    kinds = ("body_removed", "body_type_changed", "body_added_required", "response_schema_changed", "endpoint_removed")
    keys = [(g.method, g.path, g.kind, g.target) for g in GUIDANCE]
    keys += [(m.method, m.path, m.kind, m.target) for m in migrations.MIGRATIONS]
    keys += [("post", "/v1/charges", kind, "description") for kind in kinds]
    for key in keys:
        for certain, possible in ((True, False), (False, True), (True, True)):
            found = guidance.options(*key, certain=certain, possible=possible)
            assert 1 <= len(found) <= guidance.MAX_OPTIONS and found[-1] is PIN and len(set(found)) == len(found)
