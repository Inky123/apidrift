"""WordPress filters on a request array: ``$x = apply_filters('hook', $x, ...)``.

Without a callback the call returns its value unchanged, so the array is passed through as written. A
callback can be registered by the checkout itself (``add_filter('hook', ...)`` with the same literal
name, anywhere in it) and then may change any key: the array is no longer visible. A hook whose name is
not a literal can be any of them. Dependency-free on purpose: the PHP reader and the raw-HTTP reader
share it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

ADD_FILTER = re.compile(r"""(?<![\w$>:\\])add_filter\s*\(\s*(?:'([^'\\]+)'|"([^"\\$]+)")""")
APPLY = re.compile(
    r"""^\s*apply_filters(?P<deprecated>_deprecated)?\s*\(\s*(?:'(?P<sq>[^'\\]+)'|"(?P<dq>[^"\\$]+)"|(?P<other>[^,]+))\s*,\s*"""
    r"""(?P<rest>.*)\)\s*$""",
    re.S,
)


def registered(text_by_file: Mapping[str, str] | Iterable[str]) -> set[str]:
    """The literal hook names passed to ``add_filter(`` in the given files ({rel: text} or texts)."""
    texts = text_by_file.values() if isinstance(text_by_file, Mapping) else text_by_file
    out: set[str] = set()
    for text in texts:
        if "add_filter" in text:
            out |= {m.group(1) or m.group(2) for m in ADD_FILTER.finditer(text)}
    return out


def pass_through(hook: str | None, registered: set[str]) -> bool:
    """True when ``hook`` is a literal name no callback in the checkout is registered on."""
    return hook is not None and hook not in registered


def filtered(expr: str, name: str) -> tuple[bool, str | None]:
    """(``expr`` is ``apply_filters(<hook>, $name, ...)`` or ``apply_filters_deprecated(<hook>,
    [$name, ...], ...)``, its literal hook name or None)."""
    m = APPLY.match(expr)
    if m is None:
        return False, None
    rest = m.group("rest")
    pattern = rf"\[\s*\${name}\b(?!\s*\[|\s*->)" if m.group("deprecated") else rf"\${name}\b(?!\s*\[|\s*->)"
    if not re.match(pattern, rest):
        return False, None
    return True, m.group("sq") or m.group("dq")
