"""Normalize an OpenAPI 3 document into flat, comparable structures.

Only the parts that matter for client breakage are kept: operations, their
request parameters and body fields, and the component schemas they return.
Nested inline objects are flattened to dotted paths. A ``$ref`` is kept as a
leaf typed ``ref:Name`` because component schemas are diffed on their own.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

MAX_DEPTH = 4
FINGERPRINT_DEPTH = 3
REF_RE = re.compile(r"ref:([^|<>]+)")
HTTP_METHODS = ("get", "put", "post", "delete", "patch", "options", "head")


@dataclass(frozen=True)
class Param:
    location: str
    name: str
    required: bool
    type: str
    enum: tuple[str, ...] | None = None
    deprecated: bool = False

    @property
    def key(self) -> str:
        return f"{self.location}:{self.name}"


@dataclass(frozen=True)
class Field:
    path: str
    type: str
    required: bool
    enum: tuple[str, ...] | None = None
    deprecated: bool = False


@dataclass
class Operation:
    method: str
    path: str
    operation_id: str | None
    deprecated: bool
    params: dict[str, Param]
    body: dict[str, Field]
    response_schemas: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.path)

    @property
    def label(self) -> str:
        return f"{self.method.upper()} {self.path}"


@dataclass
class Schema:
    name: str
    deprecated: bool
    fields: dict[str, Field]


@dataclass
class Spec:
    version: str
    operations: dict[tuple[str, str], Operation] = field(default_factory=dict)
    schemas: dict[str, Schema] = field(default_factory=dict)

    def operations_returning(self, schema_name: str) -> list[str]:
        """Labels of operations whose 2xx response is this schema (directly or as list items)."""
        return sorted(
            op.label for op in self.operations.values() if schema_name in op.response_schemas
        )

    def fingerprint_type(self, type_str: str) -> str:
        """``type_str`` with every schema name replaced by a fingerprint of that schema's shape."""
        return REF_RE.sub(lambda m: "ref#" + self.fingerprint(m.group(1)), type_str)

    def fingerprint(self, name: str, depth: int = FINGERPRINT_DEPTH, _stack: tuple[str, ...] = ()) -> str:
        """Hash of a schema's field paths, types, required flags and enums, independent of its name."""
        schema = self.schemas.get(name)
        if schema is None:
            return f"missing:{name}"
        if name in _stack:
            return "cycle"
        if depth <= 0:
            return "deep"
        stack = _stack + (name,)
        rows = []
        for path, item in sorted(schema.fields.items()):
            kind = REF_RE.sub(lambda m: "ref#" + self.fingerprint(m.group(1), depth - 1, stack), item.type)
            rows.append((path, kind, item.required, item.enum))
        return hashlib.sha1(repr(rows).encode()).hexdigest()[:16]


def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _resolve(node: Any, components: dict) -> dict:
    """Follow a top-level ``$ref`` chain to its component definition."""
    hops = 0
    while isinstance(node, dict) and "$ref" in node and hops < 8:
        node = components.get(_ref_name(node["$ref"]), {})
        hops += 1
    return node if isinstance(node, dict) else {}


def _type_of(schema: Any) -> str:
    if not isinstance(schema, dict):
        return "unknown"
    if "$ref" in schema:
        return "ref:" + _ref_name(schema["$ref"])
    for combo in ("anyOf", "oneOf", "allOf"):
        if combo in schema:
            return "|".join(sorted({_type_of(s) for s in schema[combo]}))
    kind = schema.get("type")
    if kind == "array":
        return f"array<{_type_of(schema.get('items', {}))}>"
    if kind is None:
        return "object" if "properties" in schema else "unknown"
    if isinstance(kind, list):
        return "|".join(sorted(str(k) for k in kind))
    return str(kind)


def _enum_of(schema: Any) -> tuple[str, ...] | None:
    if isinstance(schema, dict) and isinstance(schema.get("enum"), list):
        return tuple(str(v) for v in schema["enum"])
    return None


def _object_like(schema: Any) -> dict | None:
    """Return the schema whose properties should be flattened, if there is exactly one."""
    if not isinstance(schema, dict):
        return None
    if "properties" in schema:
        return schema
    for combo in ("anyOf", "oneOf", "allOf"):
        objs = [s for s in schema.get(combo, []) if isinstance(s, dict) and "properties" in s]
        if len(objs) == 1:
            return objs[0]
    return None


def flatten(schema: Any, prefix: str = "", depth: int = 0) -> dict[str, Field]:
    """Flatten an object schema into dotted field paths, at most MAX_DEPTH levels deep."""
    out: dict[str, Field] = {}
    obj = _object_like(schema)
    if obj is None or depth >= MAX_DEPTH:
        return out
    required = set(obj.get("required", []))
    for name, sub in sorted(obj.get("properties", {}).items()):
        if not isinstance(sub, dict):
            continue
        path = f"{prefix}{name}"
        out[path] = Field(
            path=path,
            type=_type_of(sub),
            required=name in required,
            enum=_enum_of(sub),
            deprecated=bool(sub.get("deprecated", False)),
        )
        child, child_prefix = sub, f"{path}."
        if sub.get("type") == "array" and isinstance(sub.get("items"), dict):
            child, child_prefix = sub["items"], f"{path}[]."
        if "$ref" not in child:
            out.update(flatten(child, child_prefix, depth + 1))
    return out


def _schema_refs(schema: Any, depth: int = 0) -> set[str]:
    """Component names a response schema points at: direct refs, list ``data`` items, unions."""
    if not isinstance(schema, dict) or depth > 3:
        return set()
    if "$ref" in schema:
        return {_ref_name(schema["$ref"])}
    found: set[str] = set()
    for combo in ("anyOf", "oneOf", "allOf"):
        for sub in schema.get(combo, []):
            found |= _schema_refs(sub, depth + 1)
    if schema.get("type") == "array":
        found |= _schema_refs(schema.get("items"), depth + 1)
    data = schema.get("properties", {}).get("data")
    if data is not None:
        found |= _schema_refs(data, depth + 1)
    return found


def _response_schemas(responses: Any) -> tuple[str, ...]:
    names: set[str] = set()
    if not isinstance(responses, dict):
        return ()
    for code, resp in responses.items():
        if not str(code).startswith("2") or not isinstance(resp, dict):
            continue
        for media in resp.get("content", {}).values():
            if isinstance(media, dict):
                names |= _schema_refs(media.get("schema"))
    return tuple(sorted(names))


def _params(raw: list, components: dict) -> dict[str, Param]:
    out: dict[str, Param] = {}
    for item in raw:
        if not isinstance(item, dict) or "name" not in item:
            continue
        schema = _resolve(item.get("schema", {}), components)
        location = item.get("in", "query")
        param = Param(
            location=location,
            name=item["name"],
            required=bool(item.get("required", False)) or location == "path",
            type=_type_of(schema),
            enum=_enum_of(schema),
            deprecated=bool(item.get("deprecated", False)),
        )
        out[param.key] = param
    return out


def _body(request_body: Any, components: dict) -> dict[str, Field]:
    if not isinstance(request_body, dict):
        return {}
    for media in request_body.get("content", {}).values():
        if isinstance(media, dict) and "schema" in media:
            return flatten(_resolve(media["schema"], components))
    return {}


def load_spec(doc: dict) -> Spec:
    """Build a Spec from a parsed OpenAPI 3 document."""
    components = doc.get("components", {}).get("schemas", {})
    spec = Spec(version=str(doc.get("info", {}).get("version", "unknown")))
    for path, item in sorted(doc.get("paths", {}).items()):
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters", [])
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            operation = Operation(
                method=method,
                path=path,
                operation_id=op.get("operationId"),
                deprecated=bool(op.get("deprecated", False)),
                params=_params(list(shared) + list(op.get("parameters", [])), components),
                body=_body(op.get("requestBody"), components),
                response_schemas=_response_schemas(op.get("responses")),
            )
            spec.operations[operation.key] = operation
    for name, schema in sorted(components.items()):
        if isinstance(schema, dict):
            spec.schemas[name] = Schema(
                name=name,
                deprecated=bool(schema.get("deprecated", False)),
                fields=flatten(schema),
            )
    return spec
