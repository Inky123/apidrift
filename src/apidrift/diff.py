"""Compare two normalized specs and emit the changes a client would feel.

Severity follows the direction of data. Clients *send* request fields, so a
narrower request type or a removed field breaks them. Clients *receive*
response fields, so a removed field or a narrower type breaks them, while a
wider type or a new enum value is a warning. Schema renames compare equal
when the shape is unchanged; when the shape changed, the nested field-level
differences are reported instead of a vague "type changed".
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .openapi import Operation, Spec

BREAKING, WARNING, INFO = "breaking", "warning", "info"
SEVERITY_RANK = {BREAKING: 0, WARNING: 1, INFO: 2}
MAX_NESTED = 3
SINGLE_REF_RE = re.compile(r"^(array<)?ref:([^|<>]+)(>)?$")


@dataclass
class Change:
    severity: str
    kind: str
    message: str
    method: str | None = None
    path: str | None = None
    schema: str | None = None
    target: str | None = None
    old: Any = None
    new: Any = None
    affects: tuple[str, ...] = ()

    @property
    def where(self) -> str:
        if self.method and self.path:
            return f"{self.method.upper()} {self.path}"
        return f"schema {self.schema}" if self.schema else "spec"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["where"] = self.where
        data["affects"] = list(self.affects)
        return data


Maker = Callable[..., Change]


def diff_specs(old: Spec, new: Spec) -> list[Change]:
    changes = _diff_operations(old, new) + _diff_schemas(old, new)
    changes.sort(
        key=lambda c: (SEVERITY_RANK[c.severity], c.path or "", c.method or "", c.schema or "", c.target or "")
    )
    return changes


def _summary(item: Any) -> dict:
    return {"type": item.type, "required": item.required, "enum": list(item.enum) if item.enum else None}


def _top_level(keys: set[str]) -> list[str]:
    """Drop keys whose ancestor is also in the set, so a removed object is reported once."""
    kept: list[str] = []
    for key in sorted(keys):
        if not any(key.startswith(parent + ".") or key.startswith(parent + "[].") for parent in kept):
            kept.append(key)
    return kept


def _alts(spec: Spec, type_str: str) -> frozenset[str]:
    """Type alternatives with schema names replaced by shape fingerprints, so renames compare equal."""
    return frozenset(spec.fingerprint_type(alt) for alt in type_str.split("|"))


def _type_change(old_spec: Spec, a: str, new_spec: Spec, b: str, *, request: bool) -> tuple[str, str, str] | None:
    """Classify a type change as (severity, kind, verb), or None when only schema names differ."""
    old_alts, new_alts = _alts(old_spec, a), _alts(new_spec, b)
    if old_alts == new_alts:
        return None
    if old_alts < new_alts:
        return (INFO if request else WARNING), "type_widened", "widened"
    if new_alts < old_alts:
        return (BREAKING if request else INFO), "type_narrowed", "narrowed"
    return BREAKING, "type_changed", "changed"


def _diff_operations(old: Spec, new: Spec) -> list[Change]:
    out: list[Change] = []
    for key in sorted(old.operations.keys() - new.operations.keys()):
        op = old.operations[key]
        out.append(Change(BREAKING, "endpoint_removed", f"{op.label} was removed", op.method, op.path))
    for key in sorted(new.operations.keys() - old.operations.keys()):
        op = new.operations[key]
        out.append(Change(INFO, "endpoint_added", f"{op.label} was added", op.method, op.path))
    for key in sorted(old.operations.keys() & new.operations.keys()):
        a, b = old.operations[key], new.operations[key]
        if not a.deprecated and b.deprecated:
            out.append(Change(WARNING, "endpoint_deprecated", f"{b.label} is now deprecated", b.method, b.path))
        if a.response_schemas and b.response_schemas and a.response_schemas != b.response_schemas:
            out += _response_set_change(old, new, a, b)
        out += _diff_request_fields(old, new, b.method, b.path, "param", a.params, b.params)
        out += _diff_request_fields(old, new, b.method, b.path, "body", a.body, b.body)
    return out


def _response_set_change(old_spec: Spec, new_spec: Spec, a: Operation, b: Operation) -> list[Change]:
    old_alts = _alts(old_spec, "|".join(f"ref:{n}" for n in a.response_schemas))
    new_alts = _alts(new_spec, "|".join(f"ref:{n}" for n in b.response_schemas))
    if old_alts == new_alts:
        return []
    extra = sorted(set(b.response_schemas) - set(a.response_schemas))
    gone = sorted(set(a.response_schemas) - set(b.response_schemas))
    common = dict(method=b.method, path=b.path, old=list(a.response_schemas), new=list(b.response_schemas))
    if old_alts < new_alts:
        msg = f"{b.label} may now also return {', '.join(extra)}"
        return [Change(WARNING, "response_schema_widened", msg, **common)]
    if new_alts < old_alts:
        msg = f"{b.label} no longer returns {', '.join(gone)}"
        return [Change(INFO, "response_schema_narrowed", msg, **common)]
    msg = f"{b.label} now returns {', '.join(b.response_schemas)} instead of {', '.join(a.response_schemas)}"
    return [Change(BREAKING, "response_schema_changed", msg, **common)]


def _diff_request_fields(
    old_spec: Spec, new_spec: Spec, method: str, path: str, scope: str, old: dict, new: dict
) -> list[Change]:
    out: list[Change] = []
    noun = "parameter" if scope == "param" else "body field"

    def make(severity: str, kind: str, target: str, message: str, **extra: Any) -> Change:
        return Change(severity, f"{scope}_{kind}", message, method, path, target=target, **extra)

    for key in _top_level(old.keys() - new.keys()):
        out.append(make(BREAKING, "removed", key, f"{noun} {key} was removed", old=_summary(old[key])))
    for key in _top_level(new.keys() - old.keys()):
        item = new[key]
        if item.required:
            out.append(make(BREAKING, "added_required", key, f"new required {noun} {key}", new=_summary(item)))
        else:
            out.append(make(INFO, "added", key, f"new optional {noun} {key}", new=_summary(item)))
    for key in sorted(old.keys() & new.keys()):
        a, b = old[key], new[key]
        if not a.required and b.required:
            out.append(make(BREAKING, "now_required", key, f"{noun} {key} is now required"))
        elif a.required and not b.required:
            out.append(make(INFO, "now_optional", key, f"{noun} {key} is now optional"))
        if a.type != b.type:
            verdict = _type_change(old_spec, a.type, new_spec, b.type, request=True)
            if verdict:
                sev, kind, verb = verdict
                msg = f"{noun} {key} type {verb} from {a.type} to {b.type}"
                out.append(make(sev, kind, key, msg, old=a.type, new=b.type))
        out += _enum_changes(make, key, noun, a.enum, b.enum, request=True)
        if not a.deprecated and b.deprecated:
            out.append(make(WARNING, "deprecated", key, f"{noun} {key} is now deprecated"))
    return out


def _enum_changes(
    make: Maker, key: str, noun: str, old: tuple[str, ...] | None, new: tuple[str, ...] | None, *, request: bool
) -> list[Change]:
    if old is None and new is None:
        return []
    if old is not None and new is not None:
        removed, added = sorted(set(old) - set(new)), sorted(set(new) - set(old))
        out: list[Change] = []
        if request:
            if removed:
                out.append(make(BREAKING, "enum_removed", key, f"{noun} {key} no longer accepts {removed}", old=removed))
            if added:
                out.append(make(INFO, "enum_added", key, f"{noun} {key} now also accepts {added}", new=added))
        else:
            if added:
                out.append(make(WARNING, "enum_added", key, f"{noun} {key} may now return new values {added}", new=added))
            if removed:
                out.append(make(INFO, "enum_removed", key, f"{noun} {key} will no longer return {removed}", old=removed))
        return out
    if old is None:
        if request:
            return [make(BREAKING, "enum_restricted", key, f"{noun} {key} is now restricted to {list(new)}", new=list(new))]
        return [make(INFO, "enum_introduced", key, f"{noun} {key} is now limited to {list(new)}", new=list(new))]
    if request:
        return [make(INFO, "enum_relaxed", key, f"{noun} {key} is no longer restricted to {list(old)}", old=list(old))]
    return [make(WARNING, "enum_dropped", key, f"{noun} {key} is no longer limited to a fixed set of values", old=list(old))]


def _diff_schemas(old: Spec, new: Spec) -> list[Change]:
    out: list[Change] = []
    for name in sorted(old.schemas.keys() - new.schemas.keys()):
        affects = tuple(old.operations_returning(name))
        renamed_to = _rename_target(old, new, name) if affects else None
        if renamed_to:
            msg = f"schema {name} was renamed to {renamed_to} (same shape)"
            out.append(Change(INFO, "schema_renamed", msg, schema=name, new=renamed_to, affects=affects))
        elif affects:
            out.append(Change(BREAKING, "schema_removed", f"schema {name} was removed", schema=name, affects=affects))
        else:
            msg = f"schema {name} was removed (no endpoint returned it directly)"
            out.append(Change(INFO, "schema_removed", msg, schema=name))
    for name in sorted(new.schemas.keys() - old.schemas.keys()):
        out.append(Change(INFO, "schema_added", f"schema {name} was added", schema=name))
    for name in sorted(old.schemas.keys() & new.schemas.keys()):
        out += _diff_schema(old, new, name, name, name, "", tuple(new.operations_returning(name)), 0)
    return out


def _rename_target(old_spec: Spec, new_spec: Spec, name: str) -> str | None:
    """The new schema that takes ``name``'s place in every surviving operation that returned it."""
    shape = old_spec.fingerprint(name)
    candidate = None
    for op in old_spec.operations.values():
        if name not in op.response_schemas:
            continue
        new_op = new_spec.operations.get(op.key)
        if new_op is None:
            continue  # the endpoint itself is gone; reported as endpoint_removed
        matches = [n for n in new_op.response_schemas if n not in old_spec.schemas and new_spec.fingerprint(n) == shape]
        if not matches:
            return None
        candidate = candidate or matches[0]
    return candidate


def _single_ref(type_str: str) -> tuple[str, str] | None:
    """(wrapper, schema name) for ``ref:X`` or ``array<ref:X>``; None for anything else."""
    match = SINGLE_REF_RE.match(type_str)
    return (match.group(1) or "", match.group(2)) if match else None


def _diff_schema(
    old_spec: Spec, new_spec: Spec, old_name: str, new_name: str, label: str, prefix: str, affects: tuple, depth: int
) -> list[Change]:
    a, b = old_spec.schemas[old_name], new_spec.schemas[new_name]
    out: list[Change] = []

    def make(severity: str, kind: str, target: str, message: str, **extra: Any) -> Change:
        return Change(severity, f"response_{kind}", message, schema=label, target=target, affects=affects, **extra)

    if depth == 0 and not a.deprecated and b.deprecated:
        out.append(Change(WARNING, "schema_deprecated", f"schema {label} is now deprecated", schema=label, affects=affects))
    for key in _top_level(a.fields.keys() - b.fields.keys()):
        target = prefix + key
        out.append(make(BREAKING, "field_removed", target, f"field {target} was removed from {label}", old=_summary(a.fields[key])))
    for key in _top_level(b.fields.keys() - a.fields.keys()):
        target = prefix + key
        out.append(make(INFO, "field_added", target, f"field {target} was added to {label}", new=_summary(b.fields[key])))
    for key in sorted(a.fields.keys() & b.fields.keys()):
        fa, fb = a.fields[key], b.fields[key]
        target = prefix + key
        if fa.type != fb.type:
            out += _field_type_change(old_spec, new_spec, fa, fb, label, target, affects, depth, make)
        if fa.required and not fb.required:
            out.append(make(WARNING, "field_now_optional", target, f"field {target} on {label} is no longer guaranteed to be present"))
        out += _enum_changes(make, target, "field", fa.enum, fb.enum, request=False)
        if not fa.deprecated and fb.deprecated:
            out.append(make(WARNING, "field_deprecated", target, f"field {target} on {label} is now deprecated"))
    return out


def _field_type_change(
    old_spec: Spec, new_spec: Spec, fa: Any, fb: Any, label: str, target: str, affects: tuple, depth: int, make: Maker
) -> list[Change]:
    verdict = _type_change(old_spec, fa.type, new_spec, fb.type, request=False)
    if verdict is None:
        return []
    old_ref, new_ref = _single_ref(fa.type), _single_ref(fb.type)
    if (
        old_ref and new_ref and old_ref[0] == new_ref[0] and depth < MAX_NESTED
        and old_ref[1] in old_spec.schemas and new_ref[1] in new_spec.schemas
    ):
        nested_prefix = f"{target}[]." if old_ref[0] else f"{target}."
        return _diff_schema(old_spec, new_spec, old_ref[1], new_ref[1], label, nested_prefix, affects, depth + 1)
    sev, kind, verb = verdict
    msg = f"field {target} on {label} type {verb} from {fa.type} to {fb.type}"
    return [make(sev, kind, target, msg, old=fa.type, new=fb.type)]
