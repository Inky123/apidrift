"""Documented migrations: what to write instead of a broken request field.

The fixer only touches changes listed here. Every entry comes from a Stripe changelog entry that says
exactly what to send instead, so the edit is mechanical:

- ``rename``: the key gets a new name, the value stays (``payment_method_types`` →
  ``allowed_payment_method_types``).
- ``rename_first``: the key gets a new name and the array value becomes its first element, because the
  old field only ever used the first element (``filters.countries`` → ``filters.country``).
- ``delete``: the key is removed, because Stripe documents removal with nothing in its place and never
  acted on the value (``payto`` on payment method updates).
- ``wrap_type``: the value is wrapped in an object with ``type`` (``billing_cycle_anchor: 'now'`` →
  ``{type: 'now'}``); ``timestamps`` says an integer literal becomes ``{type: 'timestamp', timestamp: N}``.

A change with no documented replacement is not fixed; it stays in the issue for a person to decide.
"""

from __future__ import annotations

from dataclasses import dataclass

from .diff import Change

ACTIONS = ("rename", "rename_first", "delete", "wrap_type")
CHANGELOG = "https://docs.stripe.com/changelog/endive/2026-09-30/"
PAYMENT_METHOD_TYPES_DOC = CHANGELOG + "removes-the-payment-method-types-parameter-from-payment-intents-and-setup-intents"
CHECKOUT_PAYMENT_METHOD_TYPES_DOC = CHANGELOG + "remove-payment-method-types-checkout-sessions"
PAYTO_DOC = CHANGELOG + "removed-payto-fields"
COUNTRIES_DOC = CHANGELOG + "rename-financial-connections-countries-filter-to-country"
ANCHOR_DOC = CHANGELOG + "polymorphic-billing-cycle-anchor"
PAYMENT_METHOD_TYPES_NOTE = (
    "allowed_payment_method_types keeps your explicit list; Stripe filters out incompatible types "
    "instead of returning an error."
)
ENDIVE = "2026-09-30.endive"


@dataclass(frozen=True)
class Migration:
    method: str
    path: str
    target: str
    replacement: str  # "" for delete; the same key for wrap_type
    introduced: str
    doc: str
    note: str
    action: str = "rename"
    kind: str = "body_removed"  # the diff kind this entry answers
    timestamps: bool = False  # wrap_type: an integer literal becomes {type: 'timestamp', timestamp: N}
    wrap_values: tuple[str, ...] = ()  # wrap_type: string literals that may be wrapped
    wrap_any: bool = False  # wrap_type: a non-literal value may be wrapped (the old type was the enum only)

    def to_dict(self) -> dict:
        return {
            "method": self.method, "path": self.path, "target": self.target, "replacement": self.replacement,
            "introduced": self.introduced, "doc": self.doc, "note": self.note, "action": self.action,
        }


def _pmt(path: str, doc: str = PAYMENT_METHOD_TYPES_DOC) -> Migration:
    return Migration(
        method="post", path=path, target="payment_method_types", replacement="allowed_payment_method_types",
        introduced=ENDIVE, doc=doc, note=PAYMENT_METHOD_TYPES_NOTE,
    )


def _anchor(path: str, target: str, *, timestamps: bool) -> Migration:
    note = "Stripe now takes an object with a type instead of a string"
    note += (", and a timestamp becomes {type: 'timestamp', timestamp: …}." if timestamps else ".")
    return Migration(
        method="post", path=path, target=target, replacement=target, introduced=ENDIVE, doc=ANCHOR_DOC, note=note,
        action="wrap_type", kind="body_type_changed", timestamps=timestamps, wrap_values=("now", "unchanged"),
        wrap_any=not timestamps,
    )


MIGRATIONS: tuple[Migration, ...] = (
    _pmt("/v1/payment_intents"),
    _pmt("/v1/payment_intents/{intent}"),
    _pmt("/v1/payment_intents/{intent}/confirm"),
    _pmt("/v1/setup_intents"),
    _pmt("/v1/setup_intents/{intent}"),
    _pmt("/v1/checkout/sessions", CHECKOUT_PAYMENT_METHOD_TYPES_DOC),
    Migration(
        method="post", path="/v1/payment_methods/{payment_method}", target="payto", replacement="", introduced=ENDIVE,
        doc=PAYTO_DOC, note="Stripe accepted payto here but never stored it, so removing it changes nothing.",
        action="delete",
    ),
    Migration(
        method="post", path="/v1/financial_connections/sessions", target="filters.countries",
        replacement="filters.country", introduced=ENDIVE, doc=COUNTRIES_DOC,
        note="The old filter used only the first country in the list, so the new one takes that country.",
        action="rename_first",
    ),
    _anchor("/v1/subscriptions/{subscription_exposed_id}", "billing_cycle_anchor", timestamps=False),
    _anchor("/v1/subscriptions/{subscription}/resume", "billing_cycle_anchor", timestamps=False),
    _anchor("/v1/invoices/create_preview", "subscription_details.billing_cycle_anchor", timestamps=True),
)

_BY_KEY = {(m.method, m.path, m.kind, m.target): m for m in MIGRATIONS}
_BY_SITE = {(m.path, m.target): m for m in MIGRATIONS}


def _find(kind: str | None, method: str | None, path: str | None, target: str | None) -> Migration | None:
    if not (kind and method and path and target):
        return None
    method = method.lower()
    found = _BY_KEY.get((method, path, kind, target))
    if found is None and kind == "body_added_required" and target.endswith(".type"):
        parent = _BY_KEY.get((method, path, "body_type_changed", target[: -len(".type")]))
        found = parent if parent is not None and parent.action == "wrap_type" else None
    return found


def lookup(change: Change) -> Migration | None:
    """The documented migration answering ``change`` (a wrap_type entry also answers the required ``.type``)."""
    return _find(change.kind, change.method, change.path, change.target)


def lookup_dict(change: dict) -> Migration | None:
    """Same as :func:`lookup` for a change already serialised with ``Change.to_dict()``."""
    get = change.get
    return _find(get("kind"), get("method"), get("path"), get("target"))


def for_site(path: str, target: str) -> Migration | None:
    """The migration for a fix site, by the endpoint path and the field it edits (what a fix payload names)."""
    return _BY_SITE.get((path, target))


def replacement_for(method: str, path: str, target: str) -> str | None:
    found = _BY_SITE.get((path, target))
    return found.replacement if found is not None and found.method == method.lower() else None


def introduced_for(path: str, target: str) -> str | None:
    """The release that introduced the documented migration for (path, target), from this table only."""
    found = _BY_SITE.get((path, target))
    return found.introduced if found is not None else None


def _below(field: str, target: str) -> bool:
    return field == target or field.startswith((target + ".", target + "[]"))


def expected_delta(migration: Migration, fields: frozenset[str] | set[str], *, timestamp: bool = False) -> tuple[set[str], set[str]]:
    """(removed, added) field paths a correct fix produces at a call that sent ``fields``."""
    t = migration.target
    if migration.action in ("rename", "rename_first"):
        return {t}, {migration.replacement}
    if migration.action == "delete":
        return {f for f in fields if _below(f, t)}, set()
    added = {t + ".type"} | ({t + ".timestamp"} if timestamp else set())
    return set(), added - set(fields)


def migrated(migration: Migration, fields: frozenset[str] | set[str]) -> bool:
    """True when a call that sends ``fields`` already has the new shape for ``migration``."""
    t = migration.target
    if migration.action in ("rename", "rename_first"):
        return t not in fields and migration.replacement in fields
    if migration.action == "delete":
        return not any(_below(f, t) for f in fields)
    return t + ".type" in fields
