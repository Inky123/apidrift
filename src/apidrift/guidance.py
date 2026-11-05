"""What a person can do about a break apidrift does not fix by itself: the realistic choices, per change.

The issue lists these under every change the fix pull request does not fully cover. They come from three places:

- ``GUIDANCE``: curated entries, keyed the way a finding is keyed (method, path, change kind, target). Every option
  restates what a Stripe page says about that exact change and carries that page's link. A change Stripe documents
  no path for has no entry here: nothing is invented, and it gets the generic options only.
- :mod:`migrations`: when Stripe documents the replacement but apidrift did not apply it at a site (the arguments
  are hidden behind a spread or a variable, or the fixer refused the edit), the option is that migration by hand.
- ``GENERIC``: options that hold for every Stripe change of a kind, plus staying on the pinned API version.

Option text is written here and nowhere else; ``backticks`` mark code. Nothing in an option comes from a scanned
repository, and a caller selects options by keys it derives itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import migrations

DOCS = "https://docs.stripe.com/"
MAX_OPTIONS = 4
VERSIONING_DOC = DOCS + "api/versioning"
ERROR_CODES_DOC = DOCS + "error-codes"
MANUAL_TAX_RATES_DOC = DOCS + "payments/checkout/use-manual-tax-rates"
STRIPE_TAX_DOC = DOCS + "payments/checkout/taxes"
ACCOUNT_UPDATE_DOC = DOCS + "api/accounts/update"


@dataclass(frozen=True)
class Option:
    text: str  # one sentence a developer can act on
    doc: str = ""  # the Stripe page that says so; "" for a statement that needs no source


@dataclass(frozen=True)
class Guidance:
    method: str
    path: str
    kind: str
    target: str
    doc: str  # where Stripe documents the change itself
    options: tuple[Option, ...]


# Stripe pins the version per request: the SDK release, an explicit setting, or the account default. Every list of
# options ends with PIN; the issue explains how once, with PIN_HOW, instead of under every change.
PIN = Option("Stay on your current API version until this code is changed (how: the note at the end of this issue).")
PIN_HOW = Option(
    "Staying on your current API version: each Stripe SDK release sends the API version that was current when it "
    "shipped, so hold the SDK upgrade or set the version yourself (`apiVersion`, `api_version` or the "
    "`Stripe-Version` header; not possible in Java, Go or .NET); requests with no pinned version use the account "
    "default in Workbench.",
    VERSIONING_DOC,
)
HIDDEN = "find where the arguments apidrift cannot see are built (the spread or variable in that call)"
CHECK_HIDDEN = Option(f"For a possibly affected site, {HIDDEN} and check whether they carry the field.")

GUIDANCE: tuple[Guidance, ...] = (
    Guidance(
        method="post", path="/v1/checkout/sessions", kind="body_removed", target="line_items[].dynamic_tax_rates",
        doc=MANUAL_TAX_RATES_DOC,
        options=(
            Option(
                "Let Stripe Tax work out the rate: set `automatic_tax.enabled` to true on the session and drop the "
                "list of rates; Checkout then calculates tax from the customer's address (Stripe Tax has to be set "
                "up on the account).",
                STRIPE_TAX_DOC,
            ),
            Option(
                "Pass fixed rates instead: put the Tax Rate IDs in `line_items[].tax_rates` (for subscriptions, "
                "`subscription_data.default_tax_rates`); this fits when you know the rate before checkout starts.",
                MANUAL_TAX_RATES_DOC,
            ),
        ),
    ),
    Guidance(
        method="post", path="/v1/accounts", kind="body_removed", target="documents.proof_of_registration",
        doc=ACCOUNT_UPDATE_DOC,
        options=(
            Option(
                "Send the document once the account exists: Stripe's API reference still lists "
                "`documents.proof_of_registration` on account update (`POST /v1/accounts/{account}`); only account "
                "creation no longer takes it.",
                ACCOUNT_UPDATE_DOC,
            ),
        ),
    ),
)

_BY_KEY = {(g.method, g.path, g.kind, g.target): g for g in GUIDANCE}

REMOVE_FIELD = Option(
    "Remove the field from the request at the listed lines: Stripe answers a parameter it does not expect with "
    "`parameter_unknown`, and the documented remedy is to remove it.",
    ERROR_CODES_DOC,
)
SEND_FIELD = Option(
    "Send the field at the listed lines: Stripe answers a request that lacks a required value with `parameter_missing`.",
    ERROR_CODES_DOC,
)
NEW_SHAPE = Option("Send the value in the new shape at the listed lines.")
ACCEPTED_VALUE = Option("Send one of the values Stripe still accepts at the listed lines.")
STOP_READING = Option("Stop reading the field from the result of the listed calls.")
EVERY_SHAPE = Option("Where the result of the listed calls is read, handle each shape the response can now take.")


def generic(kind: str) -> tuple[Option, ...]:
    """The options that hold for every Stripe change of ``kind`` (without the version pin, which :func:`options` adds)."""
    request = kind.startswith(("param_", "body_"))
    if kind in ("param_removed", "body_removed"):
        return (REMOVE_FIELD,)
    if request and kind.endswith(("added_required", "now_required")):
        return (SEND_FIELD,)
    if request and kind.endswith(("type_changed", "type_narrowed")):
        return (NEW_SHAPE,)
    if request and kind.endswith(("enum_removed", "enum_restricted")):
        return (ACCEPTED_VALUE,)
    if kind == "response_field_removed":
        return (STOP_READING,)
    if kind == "response_schema_changed":
        return (EVERY_SHAPE,)
    return ()


def lookup(method: str, path: str, kind: str, target: str) -> Guidance | None:
    """The curated entry for a finding's (method, path, change kind, target), if Stripe documents a path for it."""
    return _BY_KEY.get((method.lower(), path, kind, target))


def by_hand(migration: migrations.Migration) -> str:
    """The documented migration as an instruction, from the migration table's own names."""
    old, new = f"`{migration.target}`", f"`{migration.replacement}`"
    if migration.action == "delete":
        return f"remove {old}"
    if migration.action == "rename_first":
        return f"send {new} with the first element of the {old} list"
    if migration.action == "wrap_type":
        stamp = " (an integer timestamp goes in `timestamp`, with `type` set to `timestamp`)" if migration.timestamps else ""
        return f"wrap the {old} value in an object with `type`{stamp}"
    return f"rename {old} to {new}"


def options(method: str, path: str, kind: str, target: str, *, certain: bool, possible: bool) -> tuple[Option, ...]:
    """The choices for one change at the call sites apidrift leaves alone, at most MAX_OPTIONS.

    ``certain`` and ``possible`` say which kinds of site are left: ones that send the field, and ones that may send
    it through arguments apidrift cannot see. A deprecated endpoint still works, so it has no options.
    """
    if kind == "endpoint_deprecated" or not (certain or possible):
        return ()
    found: list[Option] = []
    migration = migrations.lookup_dict({"kind": kind, "method": method, "path": path, "target": target})
    if migration is not None:
        change = by_hand(migration)
        if certain:
            found.append(Option(
                f"Make Stripe's documented change by hand at the lines apidrift did not rewrite: {change}.", migration.doc
            ))
        if possible:
            found.append(Option(
                f"For a possibly affected site, {HIDDEN} and, if they carry the field, {change} there.", migration.doc
            ))
    else:
        curated = lookup(method, path, kind, target)
        found += curated.options if curated is not None else generic(kind)
        if possible:
            found.append(CHECK_HIDDEN)
    return (*found[: MAX_OPTIONS - 1], PIN)
