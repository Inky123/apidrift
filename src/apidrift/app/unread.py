"""What a check could not read, as the parent tells it: the languages whose call sites were not parsed
(``payload["unread"]``), the cross-file caps the scan ran into (``payload["notes"]``), and the one-time
notice issue for a repository that is partly unreadable.

Everything here comes from the sandboxed child and is taken as data: names and notes are validated against
a short allow-list of shapes and rendered through the same escaping as the rest of an issue body
(:func:`issue.escape`, imported lazily because :mod:`issue` re-exports this module's functions).
"""

from __future__ import annotations

import re

from ..scanner import unread_sentence

UNREAD_NAME_RE = re.compile(r"[A-Za-z0-9 .+#-]{1,40}")  # a language name: "C#", "C++", ".NET", "Objective-C"
MAX_UNREAD = 50
MAX_NOTES, MAX_NOTE_CHARS = 20, 300
UNREAD_MARKER = "<!-- apidrift:unread -->"


def _escape(text: str) -> str:
    from .issue import escape  # issue imports this module at load time; the cycle is broken here

    return escape(text)


def unread_counts(payload: dict) -> dict[str, int]:
    """``payload["unread"]`` (language -> files not read) as the parent believes it: at most MAX_UNREAD short
    plain names (letters, digits, space, ``. + # -``; never ``#`` before a digit, which GitHub would link as an
    issue) with a positive count each. The child's text is never shown otherwise: a name that fails is dropped."""
    raw = payload.get("unread")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for name, count in list(raw.items())[:MAX_UNREAD]:
        if not isinstance(name, str) or not UNREAD_NAME_RE.fullmatch(name) or name != name.strip() or re.search(r"#\d", name):
            continue
        if isinstance(count, bool) or not isinstance(count, int) or not 0 < count <= 10_000_000:
            continue
        out[name] = count
    return out


def unread_note(payload: dict) -> str:
    """One sentence naming the languages whose call sites were not read, or "" when every file was. The names
    are the validated ones (:func:`unread_counts`), escaped like every other repository text in a body."""
    unread = unread_counts(payload)
    return unread_sentence({_escape(name): count for name, count in unread.items()}) if unread else ""


def notes_note(payload: dict) -> str:
    """One footer line for the cross-file caps the child's scan ran into (``payload["notes"]``), or "" when
    there were none or the list is not short printable text: output of the sandboxed child, taken as data."""
    notes = payload.get("notes")
    if not isinstance(notes, list) or not 0 < len(notes) <= MAX_NOTES:
        return ""
    if any(not isinstance(n, str) or not 0 < len(n) <= MAX_NOTE_CHARS or not n.isprintable() for n in notes):
        return ""
    return "Not every wrapper was followed across files: " + "; ".join(_escape(n) for n in notes) + "."


def render_unread_issue(full_name: str, sha: str, payload: dict) -> tuple[str, str]:
    """(title, body) for the one-time notice that part of this repository cannot be checked yet."""
    unread = unread_counts(payload)
    names = [_escape(name) for name in unread]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    title = f"apidrift cannot read {joined} call sites in this repository yet"
    body = [
        f"{unread_sentence(unread)} apidrift reads Python, JavaScript, TypeScript, Ruby, PHP and Go call sites and finds literal",
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
