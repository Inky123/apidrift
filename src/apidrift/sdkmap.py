"""Resolve SDK call chains such as ``stripe.checkout.Session.create`` to operations.

Stripe's SDK-flavored spec annotates each resource with ``x-stripeOperations``:
the method name every official SDK generates for an endpoint. Each operation
is indexed under the aliases the common SDK styles produce:

* Python legacy classes: ``checkout.Session.create``, ``PaymentIntent.modify``,
  ``Customer.create_tax_id``
* StripeClient / service style: ``checkout.sessions.create``,
  ``customers.tax_ids.create``, ``payment_intents.confirm``
* Node camelCase: ``paymentIntents.confirm``, ``customers.createTaxId``,
  ``customers.del``

Resolution tries the longest trailing part of a call chain first, so the
receiver (``stripe``, ``client.v1``, ``this.stripe``) never has to be known.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .openapi import SdkOp, Spec

STANDARD_TYPES = ("create", "retrieve", "update", "delete", "list")
MAX_CHAIN = 4
WILD = "__wild__"


@dataclass(frozen=True)
class Resolved:
    method: str
    path: str
    key: str

    @property
    def label(self) -> str:
        return f"{self.method.upper()} {self.path}"


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.capitalize() for part in rest)


def singular(name: str) -> str:
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith(("sses", "xes", "ches", "shes")):
        return name[:-2]
    if name.endswith("s") and not name.endswith("ss"):
        return name[:-1]
    return name


def plural(name: str) -> str:
    if name.endswith("y") and name[-2:-1] not in "aeiou":
        return name[:-1] + "ies"
    if name.endswith(("s", "x", "ch", "sh")):
        return name + "es"
    return name + "s"


def _is_param(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def _plural_like(segment: str) -> bool:
    return segment.endswith("s") and not segment.endswith("ss")


def _segments(path: str) -> list[str]:
    """Path segments after the version prefix (``/v1/customers/{customer}`` -> ``customers, {customer}``)."""
    return path.strip("/").split("/")[1:]


def _split(op: SdkOp) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(owning collection chain, sub-collection chain) for an operation's path.

    A trailing action segment (``confirm``, ``create_preview``) belongs to neither.
    """
    segments = _segments(op.path)
    first_param = next((i for i, s in enumerate(segments) if _is_param(s)), len(segments))
    owning = list(segments[:first_param])
    sub = [s for s in segments[first_param:] if not _is_param(s)]
    trailing_action = (
        op.method_type == "custom" and segments and not _is_param(segments[-1]) and not _plural_like(segments[-1])
    )
    if trailing_action:
        if sub:
            sub.pop()
        elif len(owning) > 1:
            owning.pop()
    return tuple(owning), tuple(sub)


class SdkIndex:
    """Alias table from SDK call chains to (method, path) operations."""

    def __init__(self) -> None:
        self._aliases: dict[str, set[tuple[str, str]]] = {}
        self._owners: dict[tuple[str, ...], tuple[str, str]] = {}
        self._patterns: list[tuple[str, int, re.Pattern[str]]] = []
        self.operations: set[tuple[str, str]] = set()

    @classmethod
    def build(cls, *specs: Spec) -> SdkIndex:
        index = cls()
        ops: dict[tuple, SdkOp] = {}
        for spec in specs:
            index.operations |= set(spec.operations)
            for op in spec.sdk_ops:
                ops[(op.method, op.path, op.package, op.class_name, op.method_name)] = op
        index._learn_owners(ops.values())
        for op in ops.values():
            index._add(op)
        paths = sorted({path for _, path in index.operations})
        index._patterns = sorted(((p, p.count("{"), _path_pattern(p)) for p in paths), key=lambda t: (t[1], t[0]))
        return index

    @property
    def empty(self) -> bool:
        return not self._aliases

    def resolve(self, chain: Iterable[str]) -> Resolved | None:
        parts = [p for p in chain if p]
        for size in range(min(MAX_CHAIN, len(parts)), 1, -1):
            key = ".".join(parts[-size:])
            ops = self._aliases.get(key)
            if ops:
                method, path = _pick(key, ops)
                return Resolved(method, path, key)
        return None

    def match_path(self, url_path: str) -> str | None:
        """Spec path template for a concrete URL path such as ``/v1/customers/cus_123``."""
        segments = [WILD if re.search(r"[${}:%#+\"']", s) else s for s in url_path.strip("/").split("/")]
        candidate = "/" + "/".join(segments)
        for path, _, pattern in self._patterns:
            if pattern.match(candidate):
                return path
        return None

    def methods_for(self, path: str) -> list[str]:
        return sorted(m for m, p in self.operations if p == path)

    def _learn_owners(self, ops: Iterable[SdkOp]) -> None:
        for op in ops:
            segments = _segments(op.path)
            params = [i for i, s in enumerate(segments) if _is_param(s)]
            if op.method_type in STANDARD_TYPES and (not params or params == [len(segments) - 1]):
                owning = tuple(s for s in segments if not _is_param(s))
                self._owners.setdefault(_strip_helpers(owning), (op.package, op.class_name))

    def _add(self, op: SdkOp) -> None:
        owning, sub = _split(op)
        package, class_name = self._owners.get(_strip_helpers(owning), (op.package, op.class_name))
        namespace = snake(package) if package else ""

        chain_names = {op.method_name}
        if op.method_type in STANDARD_TYPES:
            chain_names.add(op.method_type)
        if op.method_type == "update":
            chain_names.add("modify")
        if sub and op.method_type == "custom" and op.method_name.split("_", 1)[0] in STANDARD_TYPES:
            chain_names.add(op.method_name.split("_", 1)[0])
        if sub and op.method_type in STANDARD_TYPES:
            noun = sub[-1] if op.method_type == "list" else singular(sub[-1])
            parent_names = {f"{op.method_type}_{noun}"}
        else:
            parent_names = set(chain_names)

        class_chains = {owning}
        service = plural(snake(class_name)) if not owning or owning[-1] != snake(class_name) else snake(class_name)
        class_chains.add(((namespace,) if namespace else ()) + (service,))
        for chain in class_chains:
            for name in parent_names:
                self._alias(chain + (name,), op)
            for name in chain_names:
                self._alias(chain + sub + (name,), op)
        for name in parent_names:
            self._alias(((namespace,) if namespace else ()) + (class_name, name), op)
            if namespace:
                self._alias((package, class_name, name), op)
            if owning[:1] == ("test_helpers",):  # Python legacy: stripe.Customer.TestHelpers.fund_cash_balance
                self._alias((class_name, "TestHelpers", name), op)

    def _alias(self, chain: tuple[str, ...], op: SdkOp) -> None:
        for variant in (chain, tuple(camel(part) if i < len(chain) - 1 else part for i, part in enumerate(chain))):
            self._aliases.setdefault(".".join(variant), set()).add((op.method, op.path))
            name = variant[-1]
            camel_name = camel(name)
            if camel_name != name:
                self._aliases.setdefault(".".join(variant[:-1] + (camel_name,)), set()).add((op.method, op.path))
            if name == "delete":
                self._aliases.setdefault(".".join(variant[:-1] + ("del",)), set()).add((op.method, op.path))


def _strip_helpers(chain: tuple[str, ...]) -> tuple[str, ...]:
    return chain[1:] if chain[:1] == ("test_helpers",) else chain


def _path_pattern(path: str) -> re.Pattern[str]:
    parts = [r"[^/]+" if _is_param(s) else re.escape(s) for s in path.strip("/").split("/")]
    return re.compile("^/" + "/".join(parts) + "$")


def _pick(key: str, ops: set[tuple[str, str]]) -> tuple[str, str]:
    """Choose among operations sharing an alias: prefer the resource's own path, then the shortest."""
    first = snake(key.split(".", 1)[0])

    def rank(op: tuple[str, str]) -> tuple[int, int, str, str]:
        segments = _segments(op[1])
        own = 0 if segments and segments[0] == first else 1
        return (own, len(segments), op[1], op[0])

    return min(ops, key=rank)
