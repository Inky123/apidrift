"""Cross a checkout's Stripe call sites with spec changes: which calls does an upgrade hit?

A request-side change hits a call when the call sends that field (or a parent
or child of it). When a call's arguments are not fully visible (``**params``,
``...spread``, a variable), the verdict is *possible* instead of *affected*.
Response-side changes hit every caller of the endpoint; they are *affected*
when the file reads the changed field and *possible* otherwise.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import migrations
from .diff import BREAKING, INFO, SEVERITY_RANK, WARNING, Change
from .jsswitch import holds as switched_holds
from .scanner import ScanResult, Usage, unread_sentence

AFFECTED, POSSIBLE = "affected", "possible"
SENT, OPAQUE, ABSENT = "sent", "opaque", "absent"
PARENT_RE = re.compile(r"^(.*?)(?:\[\])?\.[^.\[\]]+$")
RELAXATIONS = {"added", "now_optional", "enum_added", "enum_relaxed", "type_widened"}
VALUE_ACTIONS = {"enum_removed", "enum_restricted"}  # judged by the value sent, where the reader collected values
IGNORED = {"endpoint_added", "schema_added", "schema_renamed", "response_schema_narrowed", "response_field_added"}
GENERIC_LEAVES = {  # member names so common that a read in the file need not be of this response
    "type", "id", "status", "object", "data", "amount", "currency", "created", "livemode", "metadata", "url", "name",
    "description",
}
MARK = {AFFECTED: "!", POSSIBLE: "?"}
DOC = "doc"


@dataclass(frozen=True)
class Impact:
    usage: Usage
    change: Change
    certainty: str
    reason: str

    @property
    def severity(self) -> str:
        return self.change.severity

    def to_dict(self) -> dict:
        return {
            "certainty": self.certainty,
            "reason": self.reason,
            "usage": self.usage.to_dict(),
            "change": self.change.to_dict(),
        }


def assess(usages: list[Usage], changes: list[Change], shared: frozenset[str] | None = None) -> list[Impact]:
    """``shared`` is given for a vendor whose responses nest many named objects: the field names several
    returned objects have (see :func:`localize.shared_leaves`). A read of one of those in a file is not
    pinned on the object that changed, and neither is a read in a file where calls to different
    operations return the changed object; both are *possible*."""
    out: list[Impact] = []
    for change in changes:
        if change.kind in IGNORED:
            continue
        ops = _operations(change)
        if not ops:
            continue
        for usage in usages:
            if _calls(usage, ops):
                impact = _impact(change, usage, shared or frozenset())
                if impact is not None:
                    out.append(impact)
    if shared is not None:
        out = _one_source(out)
    out.sort(
        key=lambda i: (
            SEVERITY_RANK[i.severity], 0 if i.certainty == AFFECTED else 1,
            i.usage.file, i.usage.line, i.change.kind, i.change.target or "",
        )
    )
    return out


def _one_source(impacts: list[Impact]) -> list[Impact]:
    """A response field read in a file is certain only when one operation called there returns it."""
    sources: dict[tuple[str, int], set[str]] = {}
    read = [i for i in impacts if i.certainty == AFFECTED and i.change.kind.startswith("response_") and i.change.target]
    for impact in read:
        sources.setdefault((impact.usage.file, id(impact.change)), set()).add(impact.usage.label)
    read_ids = {id(impact) for impact in read}
    out: list[Impact] = []
    for impact in impacts:
        labels = sources.get((impact.usage.file, id(impact.change)), ()) if id(impact) in read_ids else ()
        if len(labels) > 1:
            reason = f"{impact.reason} ({len(labels)} operations called here return it)"
            impact = Impact(impact.usage, impact.change, POSSIBLE, reason)
        out.append(impact)
    return out


def _operations(change: Change) -> set[tuple[str, str]]:
    if change.method and change.path:
        return {(change.method, change.path)}
    ops: set[tuple[str, str]] = set()
    for label in change.affects:
        method, _, path = label.partition(" ")
        if path:
            ops.add((method.lower(), path))
    return ops


def _calls(usage: Usage, ops: set[tuple[str, str]]) -> bool:
    if usage.method == "?":
        return any(path == usage.path for _, path in ops)
    return (usage.method, usage.path) in ops


def _impact(change: Change, usage: Usage, shared: frozenset[str] = frozenset()) -> Impact | None:
    kind = change.kind
    if kind in ("endpoint_removed", "endpoint_deprecated"):
        return Impact(usage, change, AFFECTED, "every call to this endpoint")
    if kind.startswith(("param_", "body_")) or (kind.startswith("request_") and change.method):
        return _request_impact(change, usage)
    if kind.startswith("request_"):
        return _unplaced_impact(change, usage)
    return _response_impact(change, usage, shared)


def _request_impact(change: Change, usage: Usage) -> Impact | None:
    scope, action = change.kind.split("_", 1)
    target = change.target or ""
    if scope == "param":
        location, _, target = target.partition(":")
        if location == "path":
            return None
    use = _uses(usage.fields, target)
    parent = _parent(target)
    if action in ("added_required", "now_required"):
        return _required_impact(change, usage, target, parent, use)
    if _migrated(change, usage, action):
        return None
    switch = _switched(change, usage, action)
    if switch is not None:
        return switch[0]
    if use == SENT and usage.values is not None and action in VALUE_ACTIONS:
        return _value_impact(change, usage, target, action)
    if use == SENT:
        return Impact(usage, change, AFFECTED, f"{target} is sent here")
    if action in RELAXATIONS:
        return None
    if use == OPAQUE:
        return Impact(usage, change, POSSIBLE, f"{parent} is sent here but its contents are not visible")
    if not usage.hides(target):
        return None
    return Impact(usage, change, POSSIBLE, "arguments are not fully visible at this call")


def _value_impact(change: Change, usage: Usage, target: str, action: str) -> Impact | None:
    """A change to the values a field accepts, at a call whose reader collected the values it sends: it
    hits when a value sent is one no longer accepted, cannot be ruled out when a value is not a visible
    literal, and misses when every value sent is still accepted. The reason names the value only when the
    change lists it (a withdrawn value) or it is an SDK enum member: the App's index keeps other values as
    hashes it cannot turn back into text, and its replay must word the finding as the full check does."""
    gone = action == "enum_removed"
    listed = [str(v) for v in (change.old if gone else change.new) or ()]
    sent = [value for path, value in usage.values if path == target]
    for value in sorted(v for v in sent if v is not None):
        among = _flat(value) in {_flat(v) for v in listed} if value.startswith("~") else value in listed
        if among == gone:
            if gone or value.startswith("~"):
                return Impact(usage, change, AFFECTED, f"{target} is sent here as {value.lstrip('~')}")
            since = change.introduced or "the new version"
            return Impact(usage, change, AFFECTED, f"{target} is sent here with a value {since} does not accept")
    if not sent or None in sent:
        return Impact(usage, change, POSSIBLE, f"{target} is sent here; its value is not visible")
    return None


def _flat(value: str) -> str:
    """Letters and digits, lowercased: a generated enum member (``ProcessorEnum.ModernTreasury``) against its value."""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _unplaced_impact(change: Change, usage: Usage) -> Impact | None:
    """A change to a schema this call's request can carry, when where it sits in the request is not known
    (:func:`localize.place` found no path to it): nothing here says whether the call sends it."""
    if change.kind.split("_", 1)[1] in RELAXATIONS:
        return None
    return Impact(usage, change, POSSIBLE, f"this request can carry {change.schema}; whether it does here is not visible")


def _migrated(change: Change, usage: Usage, action: str) -> bool:
    """The call already has the shape a documented migration asks for (the replacement is sent and the old
    key is not; the value is wrapped in an object with ``type``), so the change does not hit it and the
    fields that are not visible are not assumed to carry the old shape. A deleted key at a call whose
    arguments are not fully visible is not assumed gone: a spread may still send it."""
    if action not in ("removed", "type_changed"):
        return False
    migration = migrations.lookup(change)
    if migration is None or not migrations.migrated(migration, usage.fields):
        return False
    return migration.action != "delete" or not usage.hides(migration.target)


def _switched(change: Change, usage: Usage, action: str) -> tuple[Impact | None] | None:
    """A removed key that the call sends only through a helper choosing by API version between it and its
    documented replacement (:mod:`jsswitch`): None when that does not apply; otherwise ``(impact,)``. On the release
    that removes the key the helper sends the replacement when its threshold is not later: then nothing is hit,
    unless an argument that is not visible could still send the old key. A later threshold means the release
    still gets the old key."""
    if action != "removed" or not usage.switched:
        return None
    m = migrations.lookup(change)
    if m is None or m.action not in ("rename", "rename_first"):
        return None
    for old, new, since, hidden in sorted(usage.switched):
        if (old, new) != (m.target, m.replacement):
            continue
        if not switched_holds(since, m.introduced):
            before = f"before {since[2:]}" if since.startswith(">=") else f"up to {since[1:]}"
            return (Impact(usage, change, AFFECTED, f"{old} is sent here on API versions {before}, {m.introduced} among them"),)
        return (Impact(usage, change, POSSIBLE, "arguments are not fully visible at this call") if hidden else None,)
    return None


def _required_impact(change: Change, usage: Usage, target: str, parent: str, use: str) -> Impact | None:
    """A field that became required binds callers that send its parent object (or everyone, at top level)."""
    if use == SENT:
        return None
    if not parent:
        if not usage.hides(target) or change.kind.endswith("added_required"):
            return Impact(usage, change, AFFECTED, f"every caller must now send {target}")
        return Impact(usage, change, POSSIBLE, f"{target} may not be sent here (arguments not fully visible)")
    parent_use = _uses(usage.fields, parent)
    if parent_use == SENT and use == ABSENT:
        return Impact(usage, change, AFFECTED, f"{parent} is sent here without {target}")
    if parent_use in (SENT, OPAQUE) or use == OPAQUE:
        return Impact(usage, change, POSSIBLE, f"{parent} is sent here but its contents are not visible")
    if not usage.hides(parent):
        return None
    return Impact(usage, change, POSSIBLE, f"{parent} may be sent here (arguments not fully visible)")


def _uses(fields: frozenset[str], target: str) -> str:
    """SENT when the call sends the field (or a child of it); OPAQUE when it sends an ancestor whose
    contents are not visible; ABSENT otherwise."""
    for sent in fields:
        if sent == target or sent.startswith((target + ".", target + "[].")):
            return SENT
    ancestors = [sent for sent in fields if target.startswith((sent + ".", sent + "[]."))]
    if not ancestors:
        return ABSENT
    deepest = max(ancestors, key=len)
    visible = any(f != deepest and f.startswith((deepest + ".", deepest + "[].")) for f in fields)
    return ABSENT if visible else OPAQUE


def _parent(target: str) -> str:
    m = PARENT_RE.match(target)
    return m.group(1) if m else ""


def _response_impact(change: Change, usage: Usage, shared: frozenset[str] = frozenset()) -> Impact | None:
    kind = change.kind
    if kind in ("response_schema_changed", "schema_removed"):
        return Impact(usage, change, AFFECTED, "the response of this endpoint changed shape")
    if kind in ("response_schema_widened", "schema_deprecated"):
        return Impact(usage, change, POSSIBLE, "the response of this endpoint may change")
    name = _leaf(change.target)
    if name and name in usage.reads:
        if name in GENERIC_LEAVES:
            return Impact(usage, change, POSSIBLE, f"a field named {name} is read in this file (common name; may be another object)")
        if name in shared:
            return Impact(usage, change, POSSIBLE, f"a field named {name} is read in this file (several returned objects have one)")
        return Impact(usage, change, AFFECTED, f"response field {name} is read in this file")
    if name and change.severity != BREAKING:
        return None  # a warning about a response field this file never reads is noise
    if name:
        return Impact(usage, change, POSSIBLE, f"endpoint is called here; no read of {name} seen in this file")
    return Impact(usage, change, POSSIBLE, "endpoint is called here")


def _leaf(target: str | None) -> str:
    if not target:
        return ""
    return target.rsplit(".", 1)[-1].replace("[]", "")


# --- Rendering ---------------------------------------------------------------


def summary(impacts: list[Impact]) -> dict[str, int]:
    counts = {sev: 0 for sev in (BREAKING, WARNING, INFO)}
    counts.update({AFFECTED: 0, POSSIBLE: 0})
    for impact in impacts:
        counts[impact.severity] += 1
        counts[impact.certainty] += 1
    return counts


def render_impact_text(
    result: ScanResult, impacts: list[Impact], old_label: str, new_label: str, show_all: bool = False
) -> str:
    counted = [i for i in impacts if i.usage.language != DOC]
    in_docs = [i for i in impacts if i.usage.language == DOC]
    counts = summary(counted)
    hit = {(i.usage.file, i.usage.line, i.usage.path) for i in counted}
    langs = ", ".join(f"{lang} {n}" for lang, n in result.by_language().items()) or "none"
    words, vendor = result.reader, result.reader.vendor.lower()
    lines = [
        f"apidrift impact: {result.root}",
        f"{old_label} -> {new_label}",
        f"{len(result.counted)} {words.vendor} call sites in {result.files_with_usages()} files ({langs}); "
        f"{len(hit)} hit by this upgrade: {counts[BREAKING]} breaking, {counts[WARNING]} warnings, "
        f"{counts[INFO]} informational",
    ]
    if result.unread:
        lines.append("not read: " + unread_sentence(result.unread, words.vendor))
    for sev in (BREAKING, WARNING, INFO):
        group = [i for i in counted if i.severity == sev]
        if not group:
            continue
        lines += ["", f"== {sev.upper()} ({len(group)}) =="]
        lines += _impact_lines(group)
    if counted:
        lines += ["", "! affected   ? possibly affected (arguments or response reads not visible)"]
    untouched = [u for u in result.counted if (u.file, u.line, u.path) not in hit]
    if untouched:
        lines.append(f"not hit: {len(untouched)} call sites" + ("" if show_all else " (--all lists them)"))
        if show_all:
            lines += [f"  {u.where}  {u.label}  via {u.call}" for u in untouched]
    if in_docs:
        lines += ["", f"in documentation, fixtures and tests (not counted): {len(in_docs)}"]
        lines += _impact_lines(in_docs)
    if result.unresolved:
        lines.append(f"other {vendor} calls ({words.others}): {len(result.unresolved)} (apidrift scan lists them)")
    return "\n".join(lines)


def _impact_lines(group: list[Impact]) -> list[str]:
    lines: list[str] = []
    last: Usage | None = None
    for impact in group:
        u = impact.usage
        if u is not last:
            lines.append(f"{MARK[impact.certainty]} {u.where}  {u.label}  via {u.call}")
            last = u
        lines.append(f"    [{impact.change.kind}] {impact.change.message}")
        lines.append(f"    why: {impact.reason}")
    return lines


def render_impact_json(result: ScanResult, impacts: list[Impact], old_label: str, new_label: str) -> str:
    counted = [i for i in impacts if i.usage.language != DOC]
    payload = {
        "root": result.root,
        "old": old_label,
        "new": new_label,
        "call_sites": len(result.counted),
        "documentation_matches": len(result.docs),
        "unread": result.unread,
        "summary": summary(counted),
        "impacts": [i.to_dict() for i in impacts],  # documentation hits included, tagged language "doc"
    }
    return json.dumps(payload, indent=2)
