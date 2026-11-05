"""Resolve the references inside one OpenAPI document.

Vendors differ in how much they name. Stripe inlines every request and names only the objects it
returns. Plaid, Square and GitHub also name parameters, request bodies, responses, enums and plain
strings, and compose objects with ``allOf``. This module turns those back into what they mean, so the
loader sees the same structure whichever way a vendor wrote it:

* a ``$ref`` to ``components/parameters``, ``requestBodies`` or ``responses`` is followed;
* a ``$ref`` to a named schema that has no fields of its own (an enum, a string, an array, a map) is
  replaced by that schema, because such a name is only an alias for a type;
* ``allOf`` is applied: a wrapper around one schema is that schema, anything else is one object
  holding every member's properties.

A ``$ref`` to a named object, or to a named union, stays a reference.
"""

from __future__ import annotations

from typing import Any

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "options", "head")
MAX_HOPS = 8
SCHEMA_PREFIX = "#/components/schemas/"
SHAPING = ("$ref", "properties", "required", "enum", "items", "allOf", "oneOf", "anyOf")
KEPT_ON_ALIAS = ("deprecated", "nullable")


def ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _is_ref(node: Any) -> bool:
    return isinstance(node, dict) and isinstance(node.get("$ref"), str)


def _shapes(member: dict) -> bool:
    """False for an ``allOf`` member that only annotates (``{"type": "object", "nullable": true}``)."""
    return any(key in member for key in SHAPING) or member.get("type") not in (None, "object")


class Doc:
    """A parsed OpenAPI 3 document and the lookups its references need."""

    def __init__(self, doc: dict):
        self.doc = doc
        components = doc.get("components")
        schemas = components.get("schemas") if isinstance(components, dict) else None
        self.schemas: dict = schemas if isinstance(schemas, dict) else {}

    def _pointer(self, ref: str) -> Any:
        """The node a local JSON pointer (``#/components/parameters/owner``) names; None when there is none."""
        if not ref.startswith("#/"):
            return None  # another file: multi-file specs are not read
        node: Any = self.doc
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def deref(self, node: Any) -> dict:
        """A parameter, request body or response with its ``$ref`` chain followed; {} when it dangles."""
        hops = 0
        while _is_ref(node) and hops < MAX_HOPS:
            node, hops = self._pointer(node["$ref"]), hops + 1
        return node if isinstance(node, dict) and "$ref" not in node else {}

    def view(self, node: Any, _depth: int = 0) -> Any:
        """``node`` with its ``allOf`` applied; any other node is returned as it is."""
        if not isinstance(node, dict) or not isinstance(node.get("allOf"), list) or _depth >= MAX_HOPS:
            return node
        outer = {key: value for key, value in node.items() if key != "allOf"}
        members = [m for m in node["allOf"] if isinstance(m, dict)]
        shaping = [m for m in members if _shapes(m)]
        if len(shaping) == 1 and "properties" not in outer and "required" not in outer:
            inner = self.view(shaping[0], _depth + 1)
            if _is_ref(inner):  # a named schema plus annotations: still that reference
                return {**{k: v for k, v in outer.items() if k in KEPT_ON_ALIAS}, **inner}
            return {**inner, **outer}
        merged, properties, required = dict(outer), {}, []
        for member in members + [outer]:
            member = self.resolved(member, _depth + 1)
            if isinstance(member.get("properties"), dict):
                properties.update(member["properties"])
            if isinstance(member.get("required"), list):
                required += member["required"]
            for key in ("type", "enum", "items"):
                if key in member:
                    merged.setdefault(key, member[key])
        if properties:
            merged["properties"] = properties
        merged["required"] = required
        return merged

    def resolved(self, node: Any, _depth: int = 0) -> dict:
        """The schema a node stands for: ``$ref`` chains followed and ``allOf`` applied. {} when it dangles."""
        node, hops = self.view(node, _depth), 0
        while _is_ref(node) and hops < MAX_HOPS:
            node, hops = self.view(self.schemas.get(ref_name(node["$ref"])), _depth + 1), hops + 1
        return node if isinstance(node, dict) and "$ref" not in node else {}

    def inline(self, node: Any) -> Any:
        """``node`` with ``allOf`` applied and, when it only refers to a named schema that has no fields of
        its own, that schema in its place. A reference to a named object or union comes back unchanged."""
        node = self.view(node)
        if not _is_ref(node):
            return node
        target = self.resolved(node)
        if not target or any(key in target for key in ("properties", "anyOf", "oneOf")):
            return node
        return {**target, **{key: node[key] for key in KEPT_ON_ALIAS if key in node}}

    def request_only(self) -> frozenset[str]:
        """Named schemas a client only ever sends: reachable from a parameter or request body and from no
        response, callback or webhook. A schema nothing refers to is not one of them."""
        sent: list = []
        received: list = [self.doc.get("webhooks"), self.doc.get("x-webhooks")]
        paths = self.doc.get("paths")
        for item in (paths.values() if isinstance(paths, dict) else ()):
            if not isinstance(item, dict):
                continue
            sent.append(item.get("parameters"))
            for method in HTTP_METHODS:
                op = item.get(method)
                if isinstance(op, dict):
                    sent += [op.get("parameters"), op.get("requestBody")]
                    received += [op.get("responses"), op.get("callbacks")]
        return frozenset(self._reach(sent) - self._reach(received))

    def _reach(self, roots: list) -> set[str]:
        """Every named schema the given nodes refer to, directly or through other references."""
        names: set[str] = set()
        seen: set[str] = set()
        stack = list(roots)
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                ref = node.get("$ref")
                if isinstance(ref, str) and ref not in seen:
                    seen.add(ref)
                    if ref.startswith(SCHEMA_PREFIX):
                        names.add(ref_name(ref))
                        stack.append(self.schemas.get(ref_name(ref)))
                    else:
                        stack.append(self._pointer(ref))
                stack.extend(node.values())
        return names
