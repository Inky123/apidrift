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

from .diff import BREAKING, INFO, SEVERITY_RANK, WARNING, Change
from .scanner import ScanResult, Usage, unread_sentence

AFFECTED, POSSIBLE = "affected", "possible"
SENT, OPAQUE, ABSENT = "sent", "opaque", "absent"
PARENT_RE = re.compile(r"^(.*?)(?:\[\])?\.[^.\[\]]+$")
RELAXATIONS = {"added", "now_optional", "enum_added", "enum_relaxed", "type_widened"}
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


def assess(usages: list[Usage], changes: list[Change]) -> list[Impact]:
    out: list[Impact] = []
    for change in changes:
        if change.kind in IGNORED:
            continue
        ops = _operations(change)
        if not ops:
            continue
        for usage in usages:
            if _calls(usage, ops):
                impact = _impact(change, usage)
                if impact is not None:
                    out.append(impact)
    out.sort(
        key=lambda i: (
            SEVERITY_RANK[i.severity], 0 if i.certainty == AFFECTED else 1,
            i.usage.file, i.usage.line, i.change.kind, i.change.target or "",
        )
    )
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


def _impact(change: Change, usage: Usage) -> Impact | None:
    kind = change.kind
    if kind in ("endpoint_removed", "endpoint_deprecated"):
        return Impact(usage, change, AFFECTED, "every call to this endpoint")
    if kind.startswith(("param_", "body_")):
        return _request_impact(change, usage)
    return _response_impact(change, usage)


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
    if use == SENT:
        return Impact(usage, change, AFFECTED, f"{target} is sent here")
    if action in RELAXATIONS:
        return None
    if use == OPAQUE:
        return Impact(usage, change, POSSIBLE, f"{parent} is sent here but its contents are not visible")
    if usage.complete:
        return None
    return Impact(usage, change, POSSIBLE, "arguments are not fully visible at this call")


def _required_impact(change: Change, usage: Usage, target: str, parent: str, use: str) -> Impact | None:
    """A field that became required binds callers that send its parent object (or everyone, at top level)."""
    if use == SENT:
        return None
    if not parent:
        if usage.complete or change.kind.endswith("added_required"):
            return Impact(usage, change, AFFECTED, f"every caller must now send {target}")
        return Impact(usage, change, POSSIBLE, f"{target} may not be sent here (arguments not fully visible)")
    parent_use = _uses(usage.fields, parent)
    if parent_use == SENT and use == ABSENT:
        return Impact(usage, change, AFFECTED, f"{parent} is sent here without {target}")
    if parent_use in (SENT, OPAQUE) or use == OPAQUE:
        return Impact(usage, change, POSSIBLE, f"{parent} is sent here but its contents are not visible")
    if usage.complete:
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


def _response_impact(change: Change, usage: Usage) -> Impact | None:
    kind = change.kind
    if kind in ("response_schema_changed", "schema_removed"):
        return Impact(usage, change, AFFECTED, "the response of this endpoint changed shape")
    if kind in ("response_schema_widened", "schema_deprecated"):
        return Impact(usage, change, POSSIBLE, "the response of this endpoint may change")
    name = _leaf(change.target)
    if name and name in usage.reads:
        if name in GENERIC_LEAVES:
            return Impact(usage, change, POSSIBLE, f"a field named {name} is read in this file (common name; may be another object)")
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
    lines = [
        f"apidrift impact: {result.root}",
        f"{old_label} -> {new_label}",
        f"{len(result.counted)} Stripe call sites in {result.files_with_usages()} files ({langs}); "
        f"{len(hit)} hit by this upgrade: {counts[BREAKING]} breaking, {counts[WARNING]} warnings, "
        f"{counts[INFO]} informational",
    ]
    if result.unread:
        lines.append("not read: " + unread_sentence(result.unread))
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
        lines += ["", f"in documentation / fixtures (not counted): {len(in_docs)}"]
        lines += _impact_lines(in_docs)
    if result.unresolved:
        lines.append(f"other stripe calls (Stripe.js, webhooks, SDK helpers): {len(result.unresolved)} (apidrift scan lists them)")
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
