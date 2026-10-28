"""Documented migrations: what to write instead of a removed request field.

The fixer only touches changes listed here. Each entry comes from a Stripe changelog entry that names
a like-for-like replacement, so the edit is a rename of the key and nothing else. A removed field
with no documented replacement is not fixed; it stays in the issue for a person to decide.
"""

from __future__ import annotations

from dataclasses import dataclass

from .diff import Change

PAYMENT_METHOD_TYPES_DOC = (
    "https://docs.stripe.com/changelog/endive/2026-09-30/"
    "removes-the-payment-method-types-parameter-from-payment-intents-and-setup-intents"
)
CHECKOUT_PAYMENT_METHOD_TYPES_DOC = (
    "https://docs.stripe.com/changelog/endive/2026-09-30/remove-payment-method-types-checkout-sessions"
)
PAYMENT_METHOD_TYPES_NOTE = (
    "allowed_payment_method_types keeps your explicit list; Stripe filters out incompatible types "
    "instead of returning an error."
)


@dataclass(frozen=True)
class Migration:
    method: str
    path: str
    target: str
    replacement: str
    introduced: str
    doc: str
    note: str
    action: str = "rename"

    def to_dict(self) -> dict:
        return {
            "method": self.method, "path": self.path, "target": self.target, "replacement": self.replacement,
            "introduced": self.introduced, "doc": self.doc, "note": self.note, "action": self.action,
        }


def _pmt(path: str, doc: str = PAYMENT_METHOD_TYPES_DOC) -> Migration:
    return Migration(
        method="post", path=path, target="payment_method_types", replacement="allowed_payment_method_types",
        introduced="2026-09-30.endive", doc=doc, note=PAYMENT_METHOD_TYPES_NOTE,
    )


MIGRATIONS: tuple[Migration, ...] = (
    _pmt("/v1/payment_intents"),
    _pmt("/v1/payment_intents/{intent}"),
    _pmt("/v1/payment_intents/{intent}/confirm"),
    _pmt("/v1/setup_intents"),
    _pmt("/v1/setup_intents/{intent}"),
    _pmt("/v1/checkout/sessions", CHECKOUT_PAYMENT_METHOD_TYPES_DOC),
)

_BY_KEY = {(m.method, m.path, m.target): m for m in MIGRATIONS}


def lookup(change: Change) -> Migration | None:
    """The documented migration for a removed request body field, or None when there is none."""
    if change.kind != "body_removed" or not change.method or not change.path or not change.target:
        return None
    return _BY_KEY.get((change.method.lower(), change.path, change.target))


def lookup_dict(change: dict) -> Migration | None:
    """Same as :func:`lookup` for a change already serialised with ``Change.to_dict()``."""
    if change.get("kind") != "body_removed":
        return None
    method, path, target = change.get("method"), change.get("path"), change.get("target")
    if not (method and path and target):
        return None
    return _BY_KEY.get((str(method).lower(), str(path), str(target)))


def replacement_for(method: str, path: str, target: str) -> str | None:
    found = _BY_KEY.get((method.lower(), path, target))
    return found.replacement if found else None
