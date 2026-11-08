"""stripe-php copied into a checkout under a renamed namespace (Strauss, PHP-Scoper): ``\\EDD\\Vendor\\Stripe\\...``.

WordPress plugins often ship their own copy of stripe-php with every namespace prefixed, so two plugins can
load different SDK versions side by side. Their calls read ``\\EDD\\Vendor\\Stripe\\PaymentIntent::create(...)``.
A name like ``Foo\\Stripe\\Charge`` is just as often an application class (``App\\PaymentDrivers\\Stripe\\...``),
so a prefix counts only when the checkout holds the copy itself: a file ``ApiResource.php`` that declares
``namespace <Prefix>\\Stripe;`` and stripe-php's ``abstract class ApiResource``. That file is looked for, once
per scan and only when a prefixed name turns up, anywhere in the checkout but ``.git``-style and
``node_modules`` directories (the copy usually sits in ``libraries/``, ``vendor-prefixed/`` or ``vendor/``).

The scan of a checkout (:func:`scanner.scan_repo`) and the fixer's planning (:func:`fixer.plan_fixes`) run
inside :func:`within_repo`, so the per-file reader and the fixer's re-scan see the same prefixes. A file read on
its own (``scan_text``) knows no prefixes.
"""

from __future__ import annotations

import functools
import inspect
import os
import re
from contextvars import ContextVar
from pathlib import Path

MARKER = "ApiResource.php"
NAMESPACE = re.compile(r"^[ \t]*namespace\s+((?:[A-Za-z_]\w*\\)+)Stripe\s*;", re.M)
RESOURCE_CLASS = re.compile(r"\babstract\s+class\s+ApiResource\b")
READ_LIMIT = 200_000
NOT_WALKED = {"node_modules"}


class _Repo:
    def __init__(self, root: Path | None) -> None:
        self.root = root
        self._prefixes: frozenset[tuple[str, ...]] | None = None
        self.cache: dict[str, object] = {}

    def prefixes(self) -> frozenset[tuple[str, ...]]:
        if self._prefixes is None:
            self._prefixes = _find(self.root) if self.root is not None else frozenset()
        return self._prefixes


def per_repo(key: str, compute):
    """``compute(root)`` once per checkout scan (None outside one): facts about the checkout as a whole."""
    repo = _REPO.get()
    if repo is None or repo.root is None:
        return None
    if key not in repo.cache:
        repo.cache[key] = compute(repo.root)
    return repo.cache[key]


_REPO: ContextVar[_Repo | None] = ContextVar("apidrift_php_repo", default=None)


def within_repo(fn):
    """Run ``fn`` (which takes a ``root`` argument: a checkout directory or one file) with that checkout's
    vendored prefixes available to :func:`unprefix`."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def run(*args, **kwargs):
        root = sig.bind(*args, **kwargs).arguments.get("root")
        try:
            path = Path(root).resolve() if root is not None else None
        except (OSError, TypeError):
            path = None
        token = _REPO.set(_Repo(path if path is not None and path.is_dir() else None))
        try:
            return fn(*args, **kwargs)
        finally:
            _REPO.reset(token)

    return run


def unprefix(parts: list[str]) -> list[str]:
    """``EDD\\Vendor\\Stripe\\PaymentIntent`` -> ``Stripe\\PaymentIntent`` when this checkout vendors stripe-php
    under ``EDD\\Vendor``; any other name unchanged."""
    if len(parts) < 3 or parts[0] == "Stripe" or "Stripe" not in parts[1:-1]:
        return parts
    repo = _REPO.get()
    if repo is None:
        return parts
    prefixes = repo.prefixes()
    k = next((k for k in range(1, len(parts) - 1) if parts[k] == "Stripe" and tuple(parts[:k]) in prefixes), 0)
    return parts[k:] if k else parts


def _find(root: Path) -> frozenset[tuple[str, ...]]:
    found: set[tuple[str, ...]] = set()
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in NOT_WALKED]
        if MARKER not in names:
            continue
        path = Path(dirpath) / MARKER
        try:
            if path.is_symlink():
                continue
            with path.open("rb") as fh:
                text = fh.read(READ_LIMIT).decode("utf-8", errors="replace")
        except OSError:
            continue
        m = NAMESPACE.search(text)
        if m and RESOURCE_CLASS.search(text):
            found.add(tuple(p for p in m.group(1).split("\\") if p))
    return frozenset(found)
