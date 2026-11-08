"""The fix pull request's merge advice: its opening sentence and the API-version pin section.

Only claims the evidence supports: a field that becomes an object (``wrap_type``) was a string enum before,
so an older API version rejects the new shape; a renamed field may or may not be accepted by an older version,
so the text says the pin predates the release that changed it and asks for a check or an upgrade.
"""

from __future__ import annotations

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
