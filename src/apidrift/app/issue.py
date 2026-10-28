"""Turn a check payload into the one issue apidrift opens: findings, fingerprint, title and body.

The body groups call sites by the Stripe change that hits them, names the release that introduced
each change, and says in one line what the change does to a request. Everything that comes from the
scanned repository (file paths, call text) is rendered inside code spans, so a hostile file name can
not mention a user, reference an issue or autolink.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import quote

from ..diff import BREAKING
from ..impact import AFFECTED, POSSIBLE
from ..scanner import unread_sentence

MARKER_RE = re.compile(r"<!-- apidrift:fp=([0-9a-f]{64}) -->")
MAX_FINDINGS = 50
MAX_INLINE_LINKS = 10  # a group with more call sites than this is folded into <details>
MAX_BODY_CHARS = 60_000  # GitHub rejects issue bodies over 65,536 characters
CHANGELOG = "https://docs.stripe.com/changelog"
FINDING_KEYS = (
    "file", "line", "url", "method", "path", "label", "call", "language",
    "change_kind", "change_message", "change_target", "introduced", "consequence", "reason", "certainty",
)
# Markdown punctuation that could open a link, code span, table cell, emphasis or URL; backslash-escaped.
_ESCAPES = {ch: "\\" + ch for ch in "\\<`|[]():*"}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_TICKS_RE = re.compile(r"`+")


def marker(fingerprint: str) -> str:
    return f"<!-- apidrift:fp={fingerprint} -->"


def marker_in(body: str | None) -> str | None:
    """The fingerprint in an issue body's marker, if it carries one."""
    match = MARKER_RE.search(body or "")
    return match.group(1) if match else None


def breaking(payload: dict) -> list[dict]:
    """The impacts that go in an issue: breaking changes, affected or possibly affected, in real code.

    Usages tagged ``language == "doc"`` (URL matches in READMEs and fixtures) are not call sites.
    """
    return [
        impact for impact in payload.get("impacts", [])
        if impact.get("change", {}).get("severity") == BREAKING
        and impact.get("certainty") in (AFFECTED, POSSIBLE)
        and impact.get("usage", {}).get("language") != "doc"
    ]


def _key(impact: dict) -> tuple[str, str, str, str, str]:
    usage, change = impact["usage"], impact["change"]
    return (str(usage["file"]), str(usage["method"]), str(usage["path"]), str(change["kind"]), str(change.get("target") or ""))


def finding_hashes(impacts: list[dict]) -> list[str]:
    """One short hash per distinct (method, path, change kind, target), sorted: what a later release can add to.

    The file is left out on purpose: a renamed file is not a new finding.
    """
    return sorted({hashlib.sha256(repr(_key(i)[1:]).encode()).hexdigest()[:16] for i in impacts})


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


def code(text: str) -> str:
    """``text`` as a Markdown code span whose fence is longer than any backtick run inside it.

    Inside a code span nothing renders: no @mention, no #123 reference, no www. autolink, no emphasis.
    """
    text = _CONTROL_RE.sub(lambda m: f"\\x{ord(m.group(0)):02x}", text)
    longest = max((len(run) for run in _TICKS_RE.findall(text)), default=0)
    fence = "`" * (longest + 1)
    if not text or text[0] == "`" or text[-1] == "`" or text != text.strip():
        text = f" {text} "  # the renderer strips one leading and one trailing space
    return f"{fence}{text}{fence}"


def changelog_url(version: str) -> str:
    """Stripe's per-codename changelog page: ``2026-09-30.endive`` -> ``.../changelog/endive``."""
    codename = version.rsplit(".", 1)[-1] if "." in version else ""
    return f"{CHANGELOG}/{quote(codename, safe='')}" if re.fullmatch(r"[A-Za-z0-9-]+", codename) else CHANGELOG


def _display_target(kind: str, target: str) -> str:
    if kind.startswith("param_") and ":" in target:
        return target.split(":", 1)[1]  # "query:limit" -> "limit"
    return target


def heading(kind: str, target: str | None, label: str) -> str:
    """One phrase naming the change, e.g. ``payment_method_types removed from POST /v1/payment_intents``."""
    target = escape(_display_target(kind, target or ""))
    label = escape(label)
    if kind == "endpoint_removed":
        return f"{label} removed"
    if kind == "endpoint_deprecated":
        return f"{label} deprecated"
    if kind.endswith("enum_removed") and kind.startswith(("param_", "body_")):
        return f"{target} values removed on {label}"
    if kind in ("param_removed", "body_removed"):
        return f"{target} removed from {label}"
    if kind.endswith(("added_required", "now_required")):
        return f"{target} now required on {label}"
    if kind.endswith(("type_changed", "type_narrowed")) and kind.startswith(("param_", "body_")):
        return f"{target} type changed on {label}"
    if kind.endswith("enum_restricted"):
        return f"{target} restricted on {label}"
    if kind == "response_field_removed":
        return f"{target} removed from the {label} response"
    if kind.startswith("response_") and target:
        return f"{target} changed in the {label} response"
    if kind in ("schema_removed", "response_schema_changed"):
        return f"{label} response changed shape"
    return f"{kind} on {label}" + (f": {target}" if target else "")


def _values(value: object) -> str:
    items = value if isinstance(value, (list, tuple)) else []
    return ", ".join(escape(str(v)) for v in items)


def consequence(change: dict) -> str:
    """One sentence saying what the change does to a call, derived from its kind."""
    kind = str(change.get("kind") or "")
    old, new = change.get("old"), change.get("new")
    if kind in ("param_removed", "body_removed"):
        return "Requests that still send it are rejected."
    if kind.endswith(("added_required", "now_required")):
        return "Requests without it are rejected."
    if kind.endswith(("type_changed", "type_narrowed")):
        if isinstance(old, str) and isinstance(new, str):
            return f"The value shape changed from {escape(old)} to {escape(new)}."
        return "The value shape changed."
    if kind.endswith("enum_removed") and kind.startswith(("param_", "body_")):
        return f"These values are no longer accepted: {_values(old)}."
    if kind.endswith("enum_restricted"):
        return f"Only these values are accepted now: {_values(new)}."
    if kind == "endpoint_removed":
        return "The endpoint no longer exists."
    if kind == "endpoint_deprecated":
        return "The endpoint is deprecated and will be removed."
    if kind == "response_field_removed":
        return "Responses no longer include it."
    if kind in ("schema_removed", "response_schema_changed"):
        return "The response changed shape."
    if kind.startswith("response_"):
        return "Responses may carry values this code does not expect."
    return escape(str(change.get("message") or ""))


def findings(payload: dict, full_name: str, sha: str) -> list[dict]:
    """Flat finding dicts with exactly FINDING_KEYS, the shape ``demo.json`` and the issue body share."""
    out: list[dict] = []
    fallback = str(payload.get("new_version") or "")
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
            "change_target": change.get("target") or "",
            "introduced": str(change.get("introduced") or fallback),
            "consequence": consequence(change),
            "reason": impact["reason"],
            "certainty": impact["certainty"],
        })
    out.sort(key=lambda f: (f["file"], f["line"], f["path"], f["change_kind"]))
    return out


def distinct_sites(items: list[dict]) -> int:
    """Distinct (file, line, path) call sites among the findings; the number the title reports."""
    return len({(f["file"], f["line"], f["path"]) for f in items})


def versions_of(items: list[dict], fallback: str) -> list[str]:
    """The Stripe versions the findings were introduced in, oldest first (date-prefixed, so sorted as text)."""
    found = sorted({str(f.get("introduced") or fallback) for f in items})
    return found or [fallback]


def title_for(version: str, count: int) -> str:
    noun = "call site" if count == 1 else "call sites"
    return f"Upgrading to Stripe {version} breaks {count} {noun}"


def group_findings(items: list[dict], fallback: str) -> list[dict]:
    """Findings grouped by the change that hits them: ``{heading, introduced, changelog, consequence, items}``."""
    groups: dict[tuple, dict] = {}
    for f in items:
        introduced = str(f.get("introduced") or fallback)
        key = (f["path"], f["change_kind"], f.get("change_target") or "", introduced)
        group = groups.setdefault(key, {
            "heading": heading(f["change_kind"], f.get("change_target"), f["label"]),
            "introduced": introduced,
            "changelog": changelog_url(introduced),
            "consequence": f.get("consequence") or "",
            "items": [],
        })
        group["items"].append(f)
    return [groups[key] for key in sorted(groups)]


def _site_line(f: dict) -> str:
    where = f["url"] or f"{code(f['file'])}:{f['line']}"
    line = f"- {where} via {code(f['call'])}"
    if f["certainty"] == POSSIBLE:
        line += f" (possibly affected: {escape(str(f['reason']))})"
    return line


def unread_note(payload: dict) -> str:
    """One sentence naming the languages whose call sites were not read, or "" when every file was."""
    unread = payload.get("unread") or {}
    return unread_sentence({str(k): int(v) for k, v in unread.items()}) if isinstance(unread, dict) else ""


UNREAD_MARKER = "<!-- apidrift:unread -->"


def render_unread_issue(full_name: str, sha: str, payload: dict) -> tuple[str, str]:
    """(title, body) for the one-time notice that part of this repository cannot be checked yet."""
    unread = {str(k): int(v) for k, v in (payload.get("unread") or {}).items()}
    names = list(unread)
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    title = f"apidrift cannot read {joined} call sites in this repository yet"
    body = [
        f"{unread_sentence(unread)} apidrift reads Python, JavaScript, TypeScript and Ruby call sites and finds literal",
        "api.stripe.com URLs in any file; Stripe calls made through the SDK in other languages are invisible to it today.",
        "",
        "What this means: a Stripe release can break code in those files without apidrift opening an issue here.",
        "The rest of the repository is still checked on every push and every Stripe release, and this notice will",
        "be closed when apidrift learns the language.",
        "",
        "---",
        f"Scanned {full_name} at {sha}: {int(payload.get('call_sites', 0))} Stripe call sites read.",
        UNREAD_MARKER,
    ]
    return title, "\n".join(body) + "\n"


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
    unread: str = "",
) -> tuple[str, str]:
    """(title, body) for an issue listing ``items`` (as produced by :func:`findings`)."""
    sites = distinct_sites(items)
    versions = versions_of(items, new_version)
    newest = versions[-1]
    possible = len({(f["file"], f["line"], f["path"]) for f in items if f["certainty"] == POSSIBLE})
    noun = "call site" if sites == 1 else "call sites"
    lines: list[str] = []
    if supersedes is not None:
        lines += [f"Supersedes #{supersedes}, which was closed before Stripe {new_version} added new findings.", ""]
    verdict = f"Upgrading to Stripe {newest} breaks {sites} {noun} in {full_name} at {sha[:7]}"
    verdict += f"; {possible} of them possibly." if possible else "."
    if len(versions) > 1:
        verdict += f" The changes arrived between Stripe {versions[0]} and {newest}."
    lines.append(verdict)
    footer = [
        "",
        "---",
        f"Scanned {full_name} at {sha} against Stripe {old_version} -> {new_version}: {call_sites} Stripe call sites; "
        "unaffected call sites are not listed.",
    ]
    if unread:
        footer.append(unread)
    footer.append(marker(fingerprint))
    budget = MAX_BODY_CHARS - sum(len(ln) + 1 for ln in lines + footer) - 64  # room for the "and N more." line
    shown = _render_groups(lines, group_findings(items, new_version), budget)
    if len(items) > shown:
        lines += ["", f"and {len(items) - shown} more."]
    return title_for(newest, sites), "\n".join(lines + footer) + "\n"


def _render_groups(lines: list[str], groups: list[dict], budget: int) -> int:
    """Append the group blocks to ``lines`` within the character budget and MAX_FINDINGS; returns links shown."""
    used = shown = 0
    for group in groups:
        head = [
            "",
            f"**{group['heading']}** (introduced in Stripe {escape(group['introduced'])}, [changelog]({group['changelog']}))",
            group["consequence"],
        ]
        folded = len(group["items"]) > MAX_INLINE_LINKS
        if folded:
            head += [f"<details><summary>{len(group['items'])} call sites</summary>", ""]
        tail = ["", "</details>"] if folded else []
        reserve = sum(len(ln) + 1 for ln in head + tail)
        first = _site_line(group["items"][0])
        if shown >= MAX_FINDINGS or used + reserve + len(first) + 1 > budget:
            break
        lines += head
        used += reserve
        for f in group["items"]:
            site = _site_line(f)
            if shown >= MAX_FINDINGS or used + len(site) + 1 > budget:
                break
            lines.append(site)
            used += len(site) + 1
            shown += 1
        lines += tail
    return shown
