"""Find the Plaid calls in one file: which endpoint, which request fields, where.

JavaScript and TypeScript go through :mod:`plaidjs` (plaid-node), Python through :mod:`plaidpy`
(plaid-python). Every text file is also searched for literal ``*.plaid.com/<endpoint>`` URLs (code that
calls the API with its own HTTP client), which identify the endpoint but not the fields. Files in other
server languages that mention Plaid are counted, so a report can say they were not checked.

Plugged into :func:`scanner.scan_repo` through :data:`READER`; the index is a :class:`plaidmap.PlaidIndex`.
"""

from __future__ import annotations

import os
import re

from . import plaidjs, plaidpy, scanner
from .plaidmap import PlaidIndex
from .scanner import GENERIC_NOT_READS, LANGUAGES, Reader, Usage

HINT = re.compile(r"plaid", re.I)
URL_RE = re.compile(r"plaid\.com(/[A-Za-z0-9_{}$/-]+)")
# plaid-ruby, plaid-go and plaid-java are official SDKs that are not read yet; the rest reach Plaid through
# community clients or plain HTTP. Kotlin and Swift are left out: there Plaid is mostly the Link mobile SDK.
UNREAD_LANGUAGES = {
    ".rb": "Ruby", ".rake": "Ruby", ".go": "Go", ".java": "Java", ".php": "PHP", ".cs": "C#", ".rs": "Rust",
    ".ex": "Elixir", ".exs": "Elixir", ".scala": "Scala",
}
READ = {"python", "javascript", "typescript"}


def scan_text(text: str, rel: str, index: PlaidIndex) -> tuple[list[Usage], list[tuple[str, int, str]], str | None]:
    """Usages and other client calls of one file's text at repo-relative path ``rel``, and the language's
    name when the file is in one whose Plaid calls are not read (else None)."""
    suffix = os.path.splitext(rel)[1].lower()
    language = LANGUAGES.get(suffix)
    reads = scanner._reads(text, skip=GENERIC_NOT_READS, vendor="plaid")
    found: list[Usage] = []
    other: list[tuple[int, str]] = []
    if language == "python":
        calls, other = plaidpy.extract(text, index)
        found = [
            Usage(rel, c.line, c.resolved.method, c.resolved.path, c.call, frozenset(c.fields), c.complete, language, reads,
                  c.hidden, c.values)
            for c in calls
        ]
    elif language in READ:
        calls, other = plaidjs.extract(text, index)
        found = [
            Usage(rel, c.line, c.resolved.method, c.resolved.path, c.call, frozenset(c.fields.paths), c.fields.complete,
                  language, reads, frozenset(c.fields.hidden), c.values)
            for c in calls
        ]
    unread = None if language in READ else UNREAD_LANGUAGES.get(suffix)
    found += _urls(text, rel, index, reads, {u.line for u in found}, "doc" if scanner._doc_like(rel, suffix) else "http")
    return found, [(rel, line, call) for line, call in other], unread


def _urls(text: str, rel: str, index: PlaidIndex, reads: frozenset[str], taken: set[int], language: str) -> list[Usage]:
    """Literal endpoint URLs (``https://sandbox.plaid.com/link/token/create``). Links to the documentation and
    the dashboard name no endpoint and match nothing."""
    usages: list[Usage] = []
    for m in URL_RE.finditer(text):
        spec_path = index.match_path(m.group(1).rstrip("/"))
        line = text.count("\n", 0, m.start()) + 1
        if spec_path is None or line in taken:
            continue
        method = scanner._method_hint(text, m.start(), m.end(), spec_path, index)
        usages.append(Usage(rel, line, method, spec_path, "plaid.com" + m.group(1), frozenset(), False, language, reads))
    return usages


READER = Reader(
    vendor="Plaid", hint=HINT, scan_text=scan_text,
    others="the pre-2021 SDK's methods, SDK helpers", others_are="not matched to an endpoint",
)
