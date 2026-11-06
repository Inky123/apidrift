"""Tests that still name a field the fix pull request renames or removes.

A fix changes what the code sends; a test that builds or expects the old request (``expect(payload).to eq(...)``
with ``payment_method_types:``) then fails until it is updated in the same change. Tests are never edited: they
are listed, so the pull request can say which ones to update. Only code files in test locations are read
(the scanner's test-directory and test-file rules); recorded fixtures are Stripe's own payloads, not the
customer's expectations, and are left out.
"""

from __future__ import annotations

import re
from pathlib import Path

from .fixvalue import leaf
from .scanner import LANGUAGES, TEST_DIRS, TEST_FILE, _iter_files, _read

MAX_MENTIONS = 50
GONE_ACTIONS = ("rename", "rename_first", "delete")  # wrap_type keeps the field name: the key itself is not stale


def _spellings(field: str) -> set[str]:
    """``payment_method_types`` as code names it: the key, and the CamelCase a Go struct or a JS model uses."""
    parts = [p for p in field.split("_") if p]
    upper = "".join(p[:1].upper() + p[1:] for p in parts)
    return {field, upper, upper[:1].lower() + upper[1:]}


def _is_test(rel: str) -> bool:
    *dirs, name = rel.split("/")
    return any(d in TEST_DIRS for d in dirs) or bool(TEST_FILE.match(name))


def stale_tests(root: Path, sites: list[tuple[str, str]], edited: set[str]) -> list[tuple[str, int]]:
    """``(file, line)`` in test files outside ``edited`` that name a field one of ``sites`` renames or removes.

    ``sites`` holds ``(action, target)`` for every planned edit. Lines are 1-based; at most MAX_MENTIONS.
    """
    names: set[str] = set()
    for action, target in sites:
        if action in GONE_ACTIONS:
            names |= _spellings(leaf(target))
    if not names:
        return []
    pattern = re.compile(r"(?<![\w$])(?:" + "|".join(sorted(map(re.escape, names))) + r")(?![\w$])")
    root = Path(root).resolve()
    files, _, _ = _iter_files(root) if root.is_dir() else ([], 0, 0)
    out: list[tuple[str, int]] = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        if rel in edited or path.suffix.lower() not in LANGUAGES or not _is_test(rel):
            continue
        text = _read(path)
        if text is None or not pattern.search(text):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                out.append((rel, number))
                if len(out) >= MAX_MENTIONS:
                    return out
    return out
