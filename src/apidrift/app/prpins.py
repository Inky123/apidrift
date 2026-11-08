"""The fix pull request's merge advice: its opening sentence and the API-version pin section.

Only claims the evidence supports: a field that becomes an object (``wrap_type``) was a string enum before,
so an older API version rejects the new shape; a renamed field may or may not be accepted by an older version,
so the text says the pin predates the release that changed it and asks for a check or an upgrade.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .issue import code

MAX_LINKS = 50
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
DELETES_ONLY = "Removing these fields is safe on your current API version too, so this can merge any time."
RENAMES = ("rename", "rename_first")


def lede(actions: set[str], introduced: str) -> str:
    """The sentence after "Ready for Stripe X." for a pull request whose sites have ``actions``."""
    if actions and all(a == "delete" for a in actions):
        return DELETES_ONLY
    wraps, renames = "wrap_type" in actions, any(a in RENAMES for a in actions)
    if wraps and not renames:
        return ("Merge this together with your API-version and SDK upgrade; on your current pinned version Stripe "
                "rejects the new shape.")
    text = (f"Merge this together with your API-version and SDK upgrade: Stripe changed these fields in {introduced}, "
            "so an older pinned version may not accept the new names; check it, or upgrade.")
    if wraps:
        text += " The fields that become an object are rejected on your current pinned version."
    return text


Where = Callable[[str, int], str]  # (file, line) -> a permalink or a code span


def section(pins: list[dict], actions: set[str], introduced: str, shown: str, where: Where) -> list[str]:
    """The pin section. ``pins``: checked by :func:`fixcheck.pin_list`; ``introduced``: the earliest release that
    changed a non-delete field here (from the parent's table), ``shown`` the same rendered for prose."""
    if actions and all(a == "delete" for a in actions):  # no merge-together claim: accepted on every version
        lines = ["", "**API-version pins found**"]
        server, other = _split(pins)
        if server:
            lines.append("These files pin an API version; this pull request does not depend on it:")
            lines += _items(server, where)
        elif not other:
            lines.append("No API-version pin was found in the scanned files.")
        return lines + _others(other, where)
    dated = DATE_RE.match(introduced) is not None  # else nothing to compare with: every pin needs a look
    older = [p for p in pins if not dated or p["version"][:10] < introduced[:10]]
    current = [p for p in pins if p not in older]
    server, other = _split(older)
    lines = ["", "**Merge together with your upgrade**"]
    if server:
        if any(a in RENAMES for a in actions):
            lines.append(f"These files pin an API version older than {shown}, the release that changed these fields: "
                         "check that the pinned version accepts the new field, or bump them and the Stripe SDK when "
                         "you merge it:")
        else:
            lines.append(f"These files pin an API version older than {shown}, which rejects the new shape; bump them "
                         "and the Stripe SDK when you merge it:")
        lines += _items(server, where)
    else:
        lines.append(f"No server-side API-version pin older than {shown} was found in the scanned files.")
    lines += _others(other, where)
    if current:
        lines.append(f"Already pinned to {shown} or later: " + _inline(current, where) + ".")
    return lines


def _split(pins: list[dict]) -> tuple[list[dict], list[dict]]:
    """Server pins first; test-file and browser Stripe.js pins apart (they do not set the server's version)."""
    server = [p for p in pins if p.get("kind") not in ("test", "browser")]
    return server, [p for p in pins if p not in server]


def _others(pins: list[dict], where: Where) -> list[str]:
    if not pins:
        return []
    kinds = sorted({p["kind"] for p in pins})
    what = {("test",): "tests", ("browser",): "browser Stripe.js code"}.get(tuple(kinds), "tests and browser Stripe.js code")
    return [f"Also pinned in {what}, which does not set your server's API version ({len(pins)}): "
            + _inline(pins, where) + "."]


def _items(pins: list[dict], where: Where) -> list[str]:
    lines = [f"- {where(p['file'], p['line'])} pins {code(p['version'])}" for p in pins[:MAX_LINKS]]
    if len(pins) > MAX_LINKS:
        lines.append(f"- and {len(pins) - MAX_LINKS} more.")
    return lines


def _inline(pins: list[dict], where: Where) -> str:
    shown = [f"{where(p['file'], p['line'])} ({code(p['version'])})" for p in pins[:MAX_LINKS]]
    more = len(pins) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")
