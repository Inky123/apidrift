"""Turn a check payload into the one issue apidrift opens: findings, fingerprint, title and body."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import quote

from ..diff import BREAKING
from ..impact import AFFECTED, POSSIBLE

MARKER_RE = re.compile(r"<!-- apidrift:fp=([0-9a-f]{64}) -->")
MAX_FINDINGS = 50
MAX_BODY_CHARS = 60_000  # GitHub rejects issue bodies over 65,536 characters
FINDING_KEYS = (
    "file", "line", "url", "method", "path", "label", "call", "language",
    "change_kind", "change_message", "reason", "certainty",
)
# Markdown punctuation that could open a link, code span, table cell, emphasis or URL; backslash-escaped.
_ESCAPES = {ch: "\\" + ch for ch in "\\<`|[]():*"}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def marker(fingerprint: str) -> str:
    return f"<!-- apidrift:fp={fingerprint} -->"


def marker_in(body: str | None) -> str | None:
    """The fingerprint in an issue body's marker, if it carries one."""
    match = MARKER_RE.search(body or "")
    return match.group(1) if match else None


def breaking(payload: dict) -> list[dict]:
    """The impacts that go in an issue: breaking changes, affected or possibly affected."""
    return [
        impact for impact in payload.get("impacts", [])
        if impact.get("change", {}).get("severity") == BREAKING and impact.get("certainty") in (AFFECTED, POSSIBLE)
    ]


def _key(impact: dict) -> tuple[str, str, str, str, str]:
    usage, change = impact["usage"], impact["change"]
    return (str(usage["file"]), str(usage["method"]), str(usage["path"]), str(change["kind"]), str(change.get("target") or ""))


def finding_hashes(impacts: list[dict]) -> list[str]:
    """One short hash per distinct finding (no line numbers), sorted; stored so a later gain can be detected."""
    return sorted({hashlib.sha256(repr(_key(i)).encode()).hexdigest()[:16] for i in impacts})


def fingerprint(impacts: list[dict]) -> str:
    """sha256 over the sorted set of (file, method, path, change kind, change target); stable across line shifts."""
    keys = sorted({_key(i) for i in impacts})
    return hashlib.sha256("\n".join(repr(k) for k in keys).encode()).hexdigest()


def valid_path(path: str) -> bool:
    if not path or path.startswith("/") or ".." in path.split("/") or "\\" in path:
        return False
    return _CONTROL_RE.search(path) is None


def permalink(full_name: str, sha: str, path: str, line: int) -> str | None:
    if not valid_path(path):
        return None
    encoded = "/".join(quote(segment, safe="") for segment in path.split("/"))
    return f"https://github.com/{full_name}/blob/{sha}/{encoded}#L{line}"


def escape(text: str) -> str:
    """Neutralise Markdown: escape link/code/table/emphasis/URL punctuation, spell out control characters."""
    text = _CONTROL_RE.sub(lambda m: f"\\x{ord(m.group(0)):02x}", text)
    return "".join(_ESCAPES.get(ch, ch) for ch in text)


def findings(payload: dict, full_name: str, sha: str) -> list[dict]:
    """Flat finding dicts with exactly FINDING_KEYS, the shape ``demo.json`` and the issue body share."""
    out: list[dict] = []
    for impact in breaking(payload):
        usage, change = impact["usage"], impact["change"]
        method = str(usage["method"]).upper()
        out.append({
            "file": usage["file"],
            "line": int(usage["line"]),
            "url": permalink(full_name, sha, usage["file"], int(usage["line"])),
            "method": method,
            "path": usage["path"],
            "label": f"{method} {usage['path']}",
            "call": usage["call"],
            "language": usage["language"],
            "change_kind": change["kind"],
            "change_message": change["message"],
            "reason": impact["reason"],
            "certainty": impact["certainty"],
        })
    out.sort(key=lambda f: (f["file"], f["line"], f["path"], f["change_kind"]))
    return out


def title_for(new_version: str, count: int) -> str:
    noun = "call site" if count == 1 else "call sites"
    return f"Stripe {new_version} breaks {count} Stripe {noun}"


def render_issue(
    items: list[dict],
    *,
    full_name: str,
    sha: str,
    old_version: str,
    new_version: str,
    call_sites: int,
    fingerprint: str,
    supersedes: int | None = None,
) -> tuple[str, str]:
    """(title, body) for an issue listing ``items`` (as produced by :func:`findings`)."""
    count = len(items)
    possible = sum(1 for f in items if f["certainty"] == POSSIBLE)
    lines: list[str] = []
    if supersedes is not None:
        lines.append(f"Supersedes #{supersedes}, which was closed before Stripe {new_version} added new findings.")
        lines.append("")
    verdict = f"Stripe {new_version} affects {count} Stripe {'call site' if count == 1 else 'call sites'} in {full_name} at {sha[:7]}"
    lines.append(verdict + (f"; {possible} of them possibly." if possible else "."))
    footer = [
        "",
        "---",
        f"Scanned {full_name} at {sha} against Stripe {old_version} -> {new_version}: {call_sites} Stripe call sites; "
        "unaffected call sites are not listed.",
        marker(fingerprint),
    ]
    budget = MAX_BODY_CHARS - sum(len(ln) + 1 for ln in lines + footer) - 64  # room for the "and N more." line
    used = shown = 0
    current_file: str | None = None
    for f in items[:MAX_FINDINGS]:
        chunk: list[str] = []
        if f["file"] != current_file:
            chunk += ["", f"**{escape(f['file'])}**"]
        link = f["url"] or f"{escape(f['file'])}:{f['line']}"
        prefix = "possibly affected: " if f["certainty"] == POSSIBLE else ""
        chunk.append(f"- {link}")
        chunk.append(f"  {prefix}`{f['label']}` via {escape(f['call'])}: {f['change_message']} ({f['reason']})")
        size = sum(len(ln) + 1 for ln in chunk)
        if used + size > budget:
            break
        lines += chunk
        used += size
        shown += 1
        current_file = f["file"]
    if count > shown:
        lines += ["", f"and {count - shown} more."]
    return title_for(new_version, count), "\n".join(lines + footer) + "\n"
