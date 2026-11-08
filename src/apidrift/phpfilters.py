"""WordPress filters on a value: ``$x = apply_filters('hook', $x, ...)`` returns ``$x`` unchanged unless some
callback is registered on ``hook``. A filter counts as pass-through when the checkout never calls
``add_filter`` on that literal name. ``add_filter('prefix_' . $x, ...)`` may be any hook starting with ``prefix_``
(recorded as ``prefix_*``); a name that is neither (a variable, an interpolated string) could be any hook, so
then no filter is pass-through. A hook passed to ``apply_filters`` whose name is not a literal can be any of
them. Registrations in test, fixture and documentation files do not count. Small and free of the readers on
purpose: the PHP reader (phpflow) and the raw-HTTP reader (httpphp, httpphpbody) share it; it borrows only the
scanner's test-path rule.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

ADD = re.compile(
    r"""(?<![\w$>:\\])add_filter\s*\(\s*(?:(?:'([^'\\]*)'|"([^"\\$]*)")\s*(?:([,)])|(\.))|(.))""", re.S
)
ANY = "*"  # in ``registered``: an add_filter whose name could be any hook
APPLY = re.compile(
    r"""^\s*apply_filters(?P<deprecated>_deprecated)?\s*\(\s*(?:'(?P<sq>[^'\\]+)'|"(?P<dq>[^"\\$]+)"|(?P<other>[^,]+))\s*,\s*"""
    r"""(?P<rest>.*)\)\s*$""",
    re.S,
)


def registered(text_by_file: Mapping[str, str] | Iterable[str]) -> set[str]:
    """Hook names a callback is added to anywhere in these sources ({repo-relative path: text}, or bare texts):
    ``name``, ``prefix*`` or ``ANY``. Test, fixture and documentation files (the scanner's) register nothing."""
    if isinstance(text_by_file, Mapping):
        texts = [text for path, text in text_by_file.items() if not _test_like(path)]
    else:
        texts = list(text_by_file)
    out: set[str] = set()
    for text in texts:
        if "add_filter" not in text:
            continue
        for m in ADD.finditer(text):
            name = m.group(1) if m.group(1) is not None else m.group(2)
            if name is not None and m.group(3):
                out.add(name)
            elif name is not None and m.group(4):
                out.add(name + ANY)  # 'prefix_' . $x
            else:
                out.add(ANY)
    return out


def _test_like(path: str) -> bool:
    """A test, fixture or documentation file by the scanner's rules (TEST_DIRS, TEST_FILE, DOC_DIRS, DOC_EXT)."""
    import os

    from .scanner import _doc_like  # only the path rule: no scanning here

    rel = path.replace("\\", "/")
    return _doc_like(rel, os.path.splitext(rel)[1].lower())


def pass_through(hook: str | None, hooks: set[str] | None) -> bool:
    """Whether ``apply_filters(hook, $x)`` returns ``$x`` as it is in this checkout: a literal hook name that
    nothing here registers on. Unknown hooks (``None``, outside a checkout scan) never pass through."""
    if hook is None or hooks is None or ANY in hooks or hook in hooks:
        return False
    return not any(h.endswith(ANY) and hook.startswith(h[:-1]) for h in hooks)


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
