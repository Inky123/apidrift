"""Filter and render a list of changes as text or JSON."""

from __future__ import annotations

import json

from .diff import BREAKING, INFO, SEVERITY_RANK, WARNING, Change


def filter_changes(changes: list[Change], min_severity: str = INFO, path_prefix: str | None = None) -> list[Change]:
    limit = SEVERITY_RANK[min_severity]
    out: list[Change] = []
    for change in changes:
        if SEVERITY_RANK[change.severity] > limit:
            continue
        if path_prefix and not _touches(change, path_prefix):
            continue
        out.append(change)
    return out


def _touches(change: Change, prefix: str) -> bool:
    if change.path and change.path.startswith(prefix):
        return True
    return any(label.split(" ", 1)[1].startswith(prefix) for label in change.affects)


def summary(changes: list[Change]) -> dict[str, int]:
    return {sev: sum(1 for c in changes if c.severity == sev) for sev in (BREAKING, WARNING, INFO)}


def render_text(changes: list[Change], old_label: str, new_label: str) -> str:
    counts = summary(changes)
    lines = [
        f"apidrift: {old_label} -> {new_label}",
        f"{counts[BREAKING]} breaking, {counts[WARNING]} warnings, {counts[INFO]} informational",
    ]
    for sev in (BREAKING, WARNING, INFO):
        group = [c for c in changes if c.severity == sev]
        if not group:
            continue
        lines += ["", f"== {sev.upper()} ({len(group)}) =="]
        for change in group:
            lines.append(f"- [{change.kind}] {change.where}: {change.message}")
            if change.affects:
                shown = ", ".join(change.affects[:4])
                if len(change.affects) > 4:
                    shown += f" (+{len(change.affects) - 4} more)"
                lines.append(f"    affects: {shown}")
    return "\n".join(lines)


def render_json(changes: list[Change], old_label: str, new_label: str) -> str:
    payload = {
        "old": old_label,
        "new": new_label,
        "summary": summary(changes),
        "changes": [c.to_dict() for c in changes],
    }
    return json.dumps(payload, indent=2)
