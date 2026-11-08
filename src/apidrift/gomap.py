"""stripe-go names: which operation a Go call reaches, and which request field a struct field sends.

stripe-go is generated from the same ``x-stripeOperations`` metadata as the other SDKs, in three styles:

* package functions: ``paymentintent.New(params)``, ``session.New(params)`` (``checkout/session``)
* ``client.API`` fields: ``sc.PaymentIntents.New(params)``, ``sc.CheckoutSessions.New(params)``
* ``stripe.Client`` services: ``sc.V1PaymentIntents.Create(ctx, params)``

The first two name the standard verbs ``New``/``Get``/``Update``/``Del``/``List``; the third
``Create``/``Retrieve``/``Update``/``Delete``/``List``. Custom methods are the SDK method name in
CamelCase everywhere. Names are compared lowercased, so Go's acronym spelling (``TaxIDs``) never has
to be reproduced. An alias that reaches more than one operation (``taxid.New`` posts to
``/v1/tax_ids`` or ``/v1/customers/{customer}/tax_ids`` depending on its params) resolves to nothing.

Struct fields are CamelCase forms of the request field (``PaymentMethodTypes`` sends
``payment_method_types``): a field is matched to the operation's own request fields, from every spec
the index was built with, by its lowercase letters and digits.
"""

from __future__ import annotations

import re
from typing import Iterable

from .openapi import SdkOp, Spec
from .sdkmap import Resolved, plural, singular

PACKAGE, API, SERVICE = "package", "api", "service"
VIA = "@"  # ``new@customer``: the verb of a call whose params name the parent (:meth:`GoIndex._split_parents`)
LEGACY_VERBS = {"create": "new", "retrieve": "get", "update": "update", "delete": "del", "list": "list"}
LEGACY_PACKAGES = {"sub": "subscription", "subitem": "subscriptionitem", "subschedule": "subscriptionschedule"}
ACRONYMS = ("ID", "URL", "IP", "API", "SEPA", "BACS", "ACH", "SKU", "IBAN", "BIC", "VAT", "PDF", "UI", "US", "TOS")
Op = tuple[str, str]


def _parent(top: Op, nested: Op) -> str | None:
    """``customer`` when ``nested`` is ``top`` under one parent's item (``/v1/customers/{customer}/tax_ids``
    and ``/v1/tax_ids``), with the same HTTP method."""
    a, b = top[1].strip("/").split("/"), nested[1].strip("/").split("/")
    if top[0] != nested[0] or len(b) != len(a) + 2 or b[:1] != a[:1] or b[3:] != a[1:] or not b[2].startswith("{"):
        return None
    return flat(singular(b[1]))


def flat(name: str) -> str:
    """Letters and digits of a name, lowercased: ``PaymentMethodTypes`` and ``payment_method_types`` agree."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def snake_guess(name: str) -> str:
    """Best-effort snake_case of a Go field the spec does not list (shown in scans; never matched to a change)."""
    for acronym in ACRONYMS:
        name = re.sub(rf"{acronym}(?=[A-Z]|$|s\b)", acronym.capitalize(), name)
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


class GoIndex:
    """Go call aliases and per-operation request fields."""

    def __init__(self) -> None:
        self._aliases: dict[tuple[str, str, str], set[Op]] = {}
        self._owners: dict[str, set[str]] = {PACKAGE: set(), API: set(), SERVICE: set()}
        self._fields: dict[Op, set[str]] = {}
        self._children: dict[tuple[Op, str], dict[str, str]] = {}

    @classmethod
    def build(cls, specs: Iterable[Spec]) -> GoIndex:
        index = cls()
        ops: dict[tuple, SdkOp] = {}
        for spec in specs:
            for op in spec.sdk_ops:
                if op.on == "service":  # a nested collection's methods are not stripe-go functions
                    ops[(op.method, op.path, op.package, op.class_name, op.method_name)] = op
            for key, operation in spec.operations.items():
                names = index._fields.setdefault(key, set())
                names |= set(operation.body)
                names |= {p.name for p in operation.params.values() if p.location == "query"}
        by_class: dict[tuple[str, str], list[SdkOp]] = {}
        for op in ops.values():
            by_class.setdefault((op.package, op.class_name), []).append(op)
        owners: dict[str, tuple[str, str]] = {}  # collection path -> the class whose own resource it is
        for key, group in by_class.items():
            for op in group:
                if op.method_type == "list" and "{" not in op.path:
                    owners[op.path] = key
        for key, group in by_class.items():
            bare_get = any(op.method_type == "retrieve" and "{" not in op.path for op in group)
            singleton = bare_get and not any(op.method_type == "list" for op in group)  # Balance, TaxSettings
            for op in group:
                index._add(op, key, singleton, bare_get)
                parent, _, sub = op.path.rpartition("/")
                owner = owners.get(parent.rpartition("/")[0]) if parent.endswith("}") else None
                if op.method_type == "list" and owner and owner != key:  # invoice.ListLines
                    index._add(op, owner, False, False, verb="list" + flat(sub))
        index._split_parents()
        return index

    def _split_parents(self) -> None:
        """``taxid.New`` posts to ``/v1/customers/{customer}/tax_ids`` when its params set ``Customer`` and to
        ``/v1/tax_ids`` otherwise (stripe-go tests ``params.Customer != nil``). For an alias that reaches such a
        pair, ``verb@customer`` names the nested operation and ``verb@`` the other (:data:`VIA`)."""
        for (style, owner, verb), ops in list(self._aliases.items()):
            if len(ops) != 2 or VIA in verb:
                continue
            top, nested = sorted(ops, key=lambda op: op[1].count("/"))
            parent = _parent(top, nested)
            if parent:
                self._aliases[(style, owner, f"{verb}{VIA}{parent}")] = {nested}
                self._aliases[(style, owner, f"{verb}{VIA}")] = {top}

    def _add(self, op: SdkOp, key: tuple[str, str], singleton: bool, bare_get: bool, verb: str | None = None) -> None:
        package, class_name = key
        parts = [p.lower() for p in re.split(r"[._]", package) if p]
        if op.path.startswith("/v1/test_helpers/") and parts[:1] != ["testhelpers"]:
            parts.insert(0, "testhelpers")
        klass = class_name.lower()
        kind = op.method_type if op.method_type in LEGACY_VERBS else op.method_name
        legacy = LEGACY_VERBS.get(kind, flat(op.method_name))
        modern = kind if kind in LEGACY_VERBS else flat(op.method_name)
        if verb:
            legacy = modern = verb
        elif kind == "retrieve" and "{" in op.path:
            if not bare_get:  # account.Get() is /v1/account; account.GetByID(id, ...) the other
                self._add(op, key, singleton, bare_get, verb=legacy)
                self._add(op, key, singleton, bare_get, verb=modern)
            legacy = modern = "getbyid"
        field = "".join(parts) + (klass if singleton or klass.endswith("s") else plural(klass))
        target = (op.method, op.path)
        for style, owner, name in (
            (PACKAGE, "/".join(parts + [klass]), legacy), (API, field, legacy), (SERVICE, "v1" + field, modern),
        ):
            self._aliases.setdefault((style, owner, name), set()).add(target)
            self._owners[style].add(owner)

    def resolve(self, style: str, owner: str, verb: str) -> Resolved | None:
        """The one operation ``owner.verb`` reaches in ``style``; None when unknown or ambiguous."""
        owner = self._owner(style, owner)
        ops = self._aliases.get((style, owner, verb.lower()))
        if not ops or len(ops) != 1:
            return None
        method, path = next(iter(ops))
        return Resolved(method, path, f"{owner}.{verb}")

    def known(self, style: str, owner: str, verb: str) -> bool:
        """``owner.verb`` is a stripe-go method, even if it reaches more than one operation."""
        return (style, self._owner(style, owner), verb.lower()) in self._aliases

    def owns(self, style: str, owner: str) -> bool:
        """Whether ``owner`` names a stripe-go resource package or client field at all."""
        return self._owner(style, owner) in self._owners[style]

    @staticmethod
    def _owner(style: str, owner: str) -> str:
        owner = owner.lower()
        if style == PACKAGE:
            head, _, last = owner.rpartition("/")
            owner = (head + "/" if head else "") + LEGACY_PACKAGES.get(last, last)
        return owner

    def field(self, op: Op, prefix: str, go_name: str) -> str | None:
        """The request field path ``prefix + <name>`` that struct field ``go_name`` sends, if the spec lists it."""
        return self._names(op, prefix).get(flat(go_name))

    def children(self, op: Op, prefix: str) -> bool:
        """Whether the spec lists request fields under ``prefix`` (``line_items[].``) for the operation."""
        return bool(self._names(op, prefix))

    def path_param(self, op: Op, go_name: str) -> bool:
        """``go_name`` fills a ``{placeholder}`` of the operation's path (``Customer`` on a nested resource)."""
        want = flat(go_name)
        names = {flat(n) for n in re.findall(r"\{([^}]*)\}", op[1])}
        return want in names or want + "id" in names or want in {n + "id" for n in names}

    def _names(self, op: Op, prefix: str) -> dict[str, str]:
        cached = self._children.get((op, prefix))
        if cached is None:
            cached = {}
            clash: set[str] = set()
            for path in self._fields.get(op, ()):
                if not path.startswith(prefix):
                    continue
                leaf = path[len(prefix):]
                if "." in leaf or "[" in leaf:
                    continue
                key = flat(leaf)
                if key in cached and cached[key] != path:
                    clash.add(key)
                cached[key] = path
            for key in clash:  # two request fields one Go name could mean: say nothing
                del cached[key]
            self._children[(op, prefix)] = cached
        return cached
