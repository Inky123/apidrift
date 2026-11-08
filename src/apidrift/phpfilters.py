"""WordPress filters a checkout hooks itself: ``$x = apply_filters('hook', $x)`` returns ``$x`` unchanged unless
some callback is registered on ``hook``. A filter counts as pass-through when the checkout never calls
``add_filter`` on that literal name. ``add_filter('prefix_' . $x, ...)`` may be any hook starting with ``prefix_``
(recorded as ``prefix_*``); a name that is neither (a variable, an interpolated string) could be any hook, so
then no filter is pass-through. Registrations in test, fixture and documentation files do not count. Small and
free of the reader on purpose (it borrows only the scanner's test-path constants).
"""

from __future__ import annotations

import re

ADD = re.compile(
    r"(?<![\w$>:\\])add_filter\s*\(\s*(?:(['\"])([^'\"\\$]*)\1\s*(?:([,)])|(\.))|(.))", re.S
)
ANY = "*"  # in ``registered``: an add_filter whose name could be any hook


def registered(text_by_file: dict[str, str]) -> set[str]:
    """Hook names a callback is added to anywhere in these sources (keyed by repo-relative path): ``name``,
    ``prefix*`` or ``ANY``. Test, fixture and documentation files (the scanner's) register nothing."""
    out: set[str] = set()
    for path, text in text_by_file.items():
        if _test_like(path):
            continue
        for m in ADD.finditer(text):
            if m.group(3):
                out.add(m.group(2))
            elif m.group(4):
                out.add(m.group(2) + ANY)  # 'prefix_' . $x
            else:
                out.add(ANY)
    return out


def _test_like(path: str) -> bool:
    """A test, fixture or documentation file by the scanner's rules (TEST_DIRS, TEST_FILE, DOC_DIRS)."""
    from .scanner import DOC_DIRS, TEST_DIRS, TEST_FILE  # only the constants: no scanning here

    *dirs, name = path.replace("\\", "/").split("/")
    return any(d in TEST_DIRS or d in DOC_DIRS for d in dirs) or bool(TEST_FILE.match(name))


def pass_through(hook: str | None, hooks: set[str] | None) -> bool:
    """Whether ``apply_filters(hook, $x)`` returns ``$x`` as it is in this checkout: a literal hook name that
    nothing here registers on. Unknown hooks (``None``, outside a checkout scan) never pass through."""
    if hook is None or hooks is None or ANY in hooks or hook in hooks:
        return False
    return not any(h.endswith(ANY) and hook.startswith(h[:-1]) for h in hooks)
