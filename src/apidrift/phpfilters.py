"""WordPress filters a checkout hooks itself: ``$x = apply_filters('hook', $x)`` returns ``$x`` unchanged unless
some callback is registered on ``hook``. A filter only this checkout could register counts as pass-through when
the checkout never calls ``add_filter('hook', ...)`` with that literal name; an ``add_filter`` whose name is not a
literal could be any hook, so then no filter is pass-through. Dependency-free on purpose.
"""

from __future__ import annotations

import re

ADD = re.compile(r"(?<![\w$>:\\])add_filter\s*\(\s*(?:(['\"])([^'\"\\$]*)\1\s*[,)]|(.))", re.S)
ANY = "*"  # in ``registered``: an add_filter with a name that is not a plain literal


def registered(text_by_file: dict[str, str]) -> set[str]:
    """Hook names a callback is added to anywhere in these sources (``ANY`` for a non-literal name)."""
    out: set[str] = set()
    for text in text_by_file.values():
        for m in ADD.finditer(text):
            out.add(m.group(2) if m.group(3) is None else ANY)
    return out


def pass_through(hook: str | None, hooks: set[str] | None) -> bool:
    """Whether ``apply_filters(hook, $x)`` returns ``$x`` as it is in this checkout: a literal hook name that
    nothing here registers on. Unknown hooks (``None``, outside a checkout scan) never pass through."""
    return hook is not None and hooks is not None and ANY not in hooks and hook not in hooks
