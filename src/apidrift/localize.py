"""Place schema-level changes on the operations a checkout calls.

A vendor that names every object it sends and returns (Plaid) has changes the diff reports on a schema, not
on an operation, because no single operation owns the schema:

* ``request_*``: a schema that is only sent, nested too deep for an operation's body to list its fields.
  The target is relative to the schema. Here the change is repeated for each operation that sends the
  schema and each place it sits in that operation's request, with the full dotted path
  (``options.user.address.geo.source``) that impact matches against the fields a call site sends.
* ``response_*`` on a schema no operation returns directly (``Transaction`` inside
  ``TransactionsSyncResponse.added[]``): its ``affects`` is filled with every operation whose response
  can carry the schema.

Stripe returns its resources directly and inlines its requests, so its changes need neither step.
"""

from __future__ import annotations

from dataclasses import replace

from .diff import Change
from .openapi import REF_RE, Operation, Spec

MAX_SEGMENTS = 10  # how deep a request path is followed through named schemas
MAX_STEPS = 20000


def place(changes: list[Change], old: Spec, new: Spec, called: set[str] | None = None) -> list[Change]:
    """``changes`` with schema-level ones placed on operations. ``called`` limits the operations to the URL
    paths a checkout calls (None: all), which keeps a widely shared schema from listing hundreds."""
    listed = {(c.method, c.path, c.kind.split("_", 1)[1], c.target) for c in changes if c.kind.startswith("body_")}
    carriers: dict[str, set[str]] | None = None
    out: list[Change] = []
    for change in changes:
        if change.method or not change.schema or not change.target:
            out.append(change)
        elif change.kind.startswith("request_"):
            out += _place_request(change, old, new, called, listed)
        elif change.kind.startswith("response_"):
            if carriers is None:
                carriers = _carriers(old, new, called)
            labels = set(change.affects) | carriers.get(change.schema, set())
            out.append(replace(change, affects=tuple(sorted(labels))))
        else:
            out.append(change)
    return out


def _carriers(old: Spec, new: Spec, called: set[str] | None) -> dict[str, set[str]]:
    """Schema name -> labels of the operations whose response carried it before the change and that still
    exist. An operation the old spec does not have, or one that only now returns the object, lost nothing."""
    out: dict[str, set[str]] = {}
    for key, before in old.operations.items():
        if key in new.operations and (called is None or before.path in called):
            for name in before.receives:
                out.setdefault(name, set()).add(before.label)
    return out


def _place_request(change: Change, old: Spec, new: Spec, called: set[str] | None, listed: set[tuple]) -> list[Change]:
    out: list[Change] = []
    unplaced: list[str] = []
    action = change.kind.split("_", 1)[1]
    for label in change.affects:
        method, _, path = label.partition(" ")
        key = (method.lower(), path)
        if called is not None and path not in called:
            continue
        prefixes = sorted({p for spec in (new, old) if key in spec.operations for p in places(spec, spec.operations[key], change.schema)})
        if not prefixes:
            unplaced.append(label)
        for prefix in prefixes:
            target = prefix + change.target
            if (key[0], path, action, target) in listed:
                continue  # the operation's own body lists this field: already reported there
            message = change.message.replace(f"request field {change.target}", f"request field {target}", 1)
            out.append(replace(change, method=key[0], path=path, target=target, message=message, affects=()))
    if unplaced:
        out.append(replace(change, affects=tuple(unplaced)))
    return out


def places(spec: Spec, op: Operation, name: str) -> set[str]:
    """The path prefixes (``a.b.``, ``a.b[].``; "" for the body itself) at which ``op``'s request holds schema ``name``."""
    found = {""} if op.listed.get(name) == 0 else set()
    seen: set[tuple[str, str]] = set()
    stack = [(path, item.type) for path, item in op.body.items()]
    steps = 0
    while stack and steps < MAX_STEPS:
        steps += 1
        path, type_str = stack.pop()
        prefix = path + ("[]." if type_str.startswith("array<") else ".")
        for ref in REF_RE.findall(type_str):
            if ref == name:
                found.add(prefix)
            if (ref, prefix) not in seen and prefix.count(".") < MAX_SEGMENTS and ref in spec.schemas:
                seen.add((ref, prefix))
                if not any(other.startswith(prefix) for other in op.body):  # else the body lists those fields itself
                    stack += [(prefix + sub, item.type) for sub, item in spec.schemas[ref].fields.items()]
    return found


def shared_leaves(old: Spec, new: Spec, called: set[str]) -> frozenset[str]:
    """Field names found on more than one of the schemas the called operations can return. Plaid's responses
    nest many objects (an account, a transaction and a security each have a ``type``), so a file that reads
    such a name need not be reading the object a change is on."""
    carried: set[str] = set()
    for spec in (old, new):
        for op in spec.operations.values():
            if op.path in called:
                carried |= op.receives
    owners: dict[str, set[str]] = {}
    for spec in (old, new):
        for name in carried:
            schema = spec.schemas.get(name)
            for path in schema.fields if schema else ():
                owners.setdefault(path.rsplit(".", 1)[-1].replace("[]", ""), set()).add(name)
    return frozenset(leaf for leaf, names in owners.items() if len(names) > 1)
