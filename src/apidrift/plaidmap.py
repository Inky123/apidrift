"""Resolve Plaid SDK method names to operations.

Plaid's official SDKs are generated from its OpenAPI document, one client method per operation, named
after the ``operationId``: ``transactionsSync`` in plaid-node, ``transactions_sync`` in plaid-python (and
plaid-ruby). Every method takes one request object whose keys are the request body's own field names,
so the spec needs no SDK metadata: the operations and their bodies are the map.

The pre-2021 plaid-python client (``client.Transactions.get``, ``client.Item.public_token.exchange``)
named its methods after the URL path; those chains resolve when their parts, joined, spell the same name.
"""

from __future__ import annotations

import re

from .openapi import Operation, Spec
from .sdkmap import WILD, Resolved, _path_pattern, snake

Op = tuple[str, str]


class PlaidIndex:
    """SDK method names, URL paths and request bodies of the operations in one or more specs."""

    def __init__(self) -> None:
        self._methods: dict[str, Op] = {}
        self._bodies: dict[Op, list[Operation]] = {}  # an operation as each spec describes it, in the order given
        self._patterns: list[tuple[str, re.Pattern[str]]] = []
        self.operations: set[Op] = set()

    @classmethod
    def build(cls, *specs: Spec) -> PlaidIndex:
        """Index the specs. A method name two specs give to different endpoints keeps the first spec's, so
        callers list the spec the scanned code was written against first."""
        index = cls()
        for spec in specs:
            for op in spec.operations.values():
                index.operations.add(op.key)
                index._bodies.setdefault(op.key, []).append(op)
                if op.operation_id:
                    for name in (op.operation_id, snake(op.operation_id)):
                        index._methods.setdefault(name, op.key)
        paths = sorted({path for _, path in index.operations})
        index._patterns = [(path, _path_pattern(path)) for path in sorted(paths, key=lambda p: (p.count("{"), p))]
        return index

    @property
    def empty(self) -> bool:
        return not self._methods

    def method(self, name: str) -> Resolved | None:
        """The operation behind an SDK method name (``transactionsSync`` or ``transactions_sync``)."""
        op = self._methods.get(name)
        return Resolved(op[0], op[1], name) if op else None

    def legacy(self, parts: list[str]) -> Resolved | None:
        """The operation behind a pre-2021 plaid-python chain: ``Item.public_token.exchange``."""
        if len(parts) < 2:
            return None
        return self.method("_".join(snake(part) for part in parts))

    def match_path(self, url_path: str) -> str | None:
        """Spec path for a concrete URL path such as ``/transactions/sync``."""
        segments = [WILD if re.search(r"[${}:%#+\"']", s) else s for s in url_path.strip("/").split("/")]
        candidate = "/" + "/".join(segments)
        for path, pattern in self._patterns:
            if pattern.match(candidate):
                return path
        return None

    def methods_for(self, path: str) -> list[str]:
        return sorted(m for m, p in self.operations if p == path)

    def has_children(self, op: Op, path: str) -> bool:
        """Some spec lists request fields under ``path`` (an object or a list of objects) for this operation."""
        prefixes = (path + ".", path + "[].")
        return any(name.startswith(prefixes) for body in self._bodies.get(op, ()) for name in body.body)

    def required(self, op: Op, prefix: str, count: int, given: set[str] | frozenset[str] = frozenset()) -> list[str] | None:
        """The request fields ``count`` positional arguments stand for under ``prefix`` ("" or ``a.b.``).

        plaid-python's generated models take their required properties as positional arguments, so when the
        count matches the required fields of a spec that are not also ``given`` by keyword, those are the
        fields sent. None when no spec agrees: the caller then treats them as fields it cannot name.
        """
        for body in self._bodies.get(op, ()):
            names = [
                name for name, item in body.body.items()
                if item.required and name.startswith(prefix) and "." not in name[len(prefix):] and name[len(prefix):] not in given
            ]
            if len(names) == count:
                return names
        return None
