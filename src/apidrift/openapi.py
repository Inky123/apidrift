"""Normalize an OpenAPI 3 document into flat, comparable structures.

Only the parts that matter for client breakage are kept: operations, their
request parameters and body fields, and the component schemas they return.
Nested objects are flattened to dotted paths. In a component schema a ``$ref``
to another named object is kept as a leaf typed ``ref:Name``, because component
schemas are diffed on their own. In a request body it is followed, because a
request is diffed per operation and by the direction a client sends it in.
:mod:`refs` resolves everything else a vendor names (parameters, bodies,
responses, enums, ``allOf``).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .refs import HTTP_METHODS, Doc
from .refs import ref_name as _ref_name

MAX_DEPTH = 4
FINGERPRINT_DEPTH = 3
REF_RE = re.compile(r"ref:([^|<>]+)")


@dataclass(frozen=True)
class Param:
    location: str
    name: str
    required: bool
    type: str
    enum: tuple[str, ...] | None = None
    deprecated: bool = False
    open_enum: bool = False  # Stripe marks enums it will extend or not validate; they do not bind clients

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
    open_enum: bool = False


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


@dataclass(frozen=True)
class SdkOp:
    """How the official SDKs expose one operation, from Stripe's ``x-stripeOperations``."""

    class_name: str
    package: str
    method_name: str
    method_type: str
    method: str
    path: str


@dataclass
class Spec:
    version: str
    operations: dict[tuple[str, str], Operation] = field(default_factory=dict)
    schemas: dict[str, Schema] = field(default_factory=dict)
    sdk_ops: list[SdkOp] = field(default_factory=list)
    request_only: frozenset[str] = frozenset()  # schemas a client only sends; their changes are reported per operation

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


def _type_of(schema: Any, refs: Doc) -> str:
    schema = refs.inline(schema)
    if not isinstance(schema, dict):
        return "unknown"
    if "$ref" in schema:
        return "ref:" + _ref_name(schema["$ref"])
    for combo in ("anyOf", "oneOf", "allOf"):
        if combo in schema:
            return "|".join(sorted({_type_of(s, refs) for s in schema[combo]}))
    kind = schema.get("type")
    if kind == "array":
        return f"array<{_type_of(schema.get('items', {}), refs)}>"
    if kind is None:
        return "object" if "properties" in schema else "unknown"
    if isinstance(kind, list):
        return "|".join(sorted(str(k) for k in kind))
    return str(kind)


def _enum_of(schema: Any) -> tuple[str, ...] | None:
    if isinstance(schema, dict) and isinstance(schema.get("enum"), list):
        return tuple(str(v) for v in schema["enum"])
    return None


def _open_enum(schema: Any) -> bool:
    """True for an enum Stripe documents as open-ended (``x-stripeEnum.kind == "open"``) or does not validate."""
    if not isinstance(schema, dict):
        return False
    meta = schema.get("x-stripeEnum")
    kind = meta.get("kind") if isinstance(meta, dict) else None
    return kind == "open" or bool(schema.get("x-stripeBypassValidation"))


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


def flatten(schema: Any, refs: Doc, prefix: str = "", depth: int = 0, follow: bool = False) -> dict[str, Field]:
    """Flatten an object schema into dotted field paths, at most MAX_DEPTH levels deep.

    ``follow`` also descends into the named objects it refers to (request bodies); without it a
    reference to a named object is a leaf (component schemas).
    """
    out: dict[str, Field] = {}
    obj = _object_like(refs.resolved(schema))
    if obj is None or depth >= MAX_DEPTH:
        return out
    required = {str(name) for name in obj.get("required") or []}
    properties = obj.get("properties") or {}
    for name, sub in sorted(((str(key), value) for key, value in properties.items()), key=lambda pair: pair[0]):
        if not isinstance(sub, dict):
            continue
        path = f"{prefix}{name}"
        sub = refs.inline(sub)
        out[path] = Field(
            path=path,
            type=_type_of(sub, refs),
            required=name in required,
            enum=_enum_of(sub),
            deprecated=bool(sub.get("deprecated", False)),
            open_enum=_open_enum(sub),
        )
        child, child_prefix = sub, f"{path}."
        if sub.get("type") == "array" and isinstance(sub.get("items"), dict):
            child, child_prefix = refs.inline(sub["items"]), f"{path}[]."
        if "$ref" not in child or follow:
            out.update(flatten(child, refs, child_prefix, depth + 1, follow))
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


def _response_schemas(responses: Any, refs: Doc) -> tuple[str, ...]:
    names: set[str] = set()
    if not isinstance(responses, dict):
        return ()
    for code, resp in responses.items():
        if not str(code).startswith("2"):
            continue
        for media in (refs.deref(resp).get("content") or {}).values():
            if isinstance(media, dict):
                names |= _schema_refs(media.get("schema"))
    return tuple(sorted(names))


def _params(raw: list, refs: Doc) -> dict[str, Param]:
    """Parameters by ``location:name``; later entries (the operation's own) replace the path's shared ones."""
    out: dict[str, Param] = {}
    for item in raw:
        item = refs.deref(item)
        if "name" not in item:
            continue
        schema = refs.resolved(item.get("schema", {}))
        location = item.get("in", "query")
        param = Param(
            location=location,
            name=item["name"],
            required=bool(item.get("required", False)) or location == "path",
            type=_type_of(schema, refs),
            enum=_enum_of(schema),
            deprecated=bool(item.get("deprecated", False)),
            open_enum=_open_enum(schema),
        )
        out[param.key] = param
    return out


def _body(request_body: Any, refs: Doc) -> dict[str, Field]:
    for media in (refs.deref(request_body).get("content") or {}).values():
        if isinstance(media, dict) and "schema" in media:
            return flatten(media["schema"], refs, follow=True)
    return {}


def load_spec(doc: dict) -> Spec:
    """Build a Spec from a parsed OpenAPI 3 document."""
    refs = Doc(doc)
    info, paths = doc.get("info"), doc.get("paths")
    version = info.get("version", "unknown") if isinstance(info, dict) else "unknown"
    spec = Spec(version=str(version), request_only=refs.request_only())
    for path, item in sorted(paths.items() if isinstance(paths, dict) else ()):
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") or []
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            operation = Operation(
                method=method,
                path=path,
                operation_id=op.get("operationId"),
                deprecated=bool(op.get("deprecated", False)),
                params=_params(list(shared) + list(op.get("parameters") or []), refs),
                body=_body(op.get("requestBody"), refs),
                response_schemas=_response_schemas(op.get("responses"), refs),
            )
            spec.operations[operation.key] = operation
    for name, schema in sorted(refs.schemas.items()):
        if isinstance(schema, dict):
            spec.schemas[name] = Schema(
                name=name,
                deprecated=bool(schema.get("deprecated", False)),
                fields=flatten(schema, refs),
            )
            spec.sdk_ops += _sdk_ops(name, schema, spec)
    return spec


def _sdk_ops(name: str, schema: dict, spec: Spec) -> list[SdkOp]:
    """SDK method metadata for a resource schema, when the spec carries it."""
    raw_ops = schema.get("x-stripeOperations")
    if not isinstance(raw_ops, list):
        return []
    resource = schema.get("x-stripeResource") or {}
    fallback = "".join(part.capitalize() for part in re.split(r"[._]", name))
    class_name = str(resource.get("class_name") or fallback)
    package = str(resource.get("in_package") or "")
    out: list[SdkOp] = []
    for raw in raw_ops:
        if not isinstance(raw, dict):
            continue
        method, path = str(raw.get("operation", "")).lower(), str(raw.get("path", ""))
        if (method, path) not in spec.operations or not raw.get("method_name"):
            continue
        out.append(SdkOp(class_name, package, str(raw["method_name"]), str(raw.get("method_type", "custom")), method, path))
    return out
