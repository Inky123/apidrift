"""Tests that still name a field the fix pull request renames or removes.

A fix changes what the code sends; a test that builds or expects the old request (``expect(payload).to eq(...)``
with ``payment_method_types:``) then fails until it is updated in the same change. :mod:`fixtestedit` updates
the ones it can prove expect the changed request; the rest are listed, so the pull request can say which ones
a person should check. Only code files in test locations are read (the scanner's test-directory and test-file
rules); recorded fixtures are Stripe's own payloads, not the customer's expectations, and are left out. A line
is listed only when it can be about the request the fix changed:

- the name is a code token (or a whole quoted key) or a form-encoded key in a string
  (``payment_method_types[0]=card``), not words in a comment or a test's description;
- the spelling is the request key's: ``payment_method_types`` everywhere, and Go's struct field
  ``PaymentMethodTypes`` in Go only (a camelCase ``paymentMethodTypes`` in JS is Stripe.js or the app's own
  model, never a server request key);
- the test is in the language of a file the fix changed (JS and TS count as one): a browser test cannot run
  the server's Ruby; unless the test sends to Stripe itself (a Stripe call in it, or an ``api.stripe.com``
  URL), when it is listed whatever its language;
- it is not inside a literal Stripe object (a hash with an ``id`` and an ``object`` string at its own level),
  which is a recorded response, not a request, nor an RSpec stub of a method of that name
  (``receive(:payment_method_types)``), which reads a response.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import jsparse, phplex
from .fixtokens import PY_MASK, Token, tokens
from .fixvalue import leaf
from .scanner import LANGUAGES, TEST_DIRS, TEST_FILE, _iter_files, _read

MAX_MENTIONS = 50
GONE_ACTIONS = ("rename", "rename_first", "delete")  # wrap_type keeps the field name: the key itself is not stale
FAMILY = {"typescript": "javascript"}
OBJECT_KEY = re.compile(r"""['"]?(id|object)['"]?\s*(?:=>|:|=)\s*(['"])[\w.]+\2""")
OPEN, CLOSE = "{[(", "}])"
STUBBED = re.compile(r"\b(?:receive|have_received|respond_to)\(\s*:\Z")


def _spellings(field: str, language: str) -> set[str]:
    """``payment_method_types`` as code names it: the key, and in Go the struct field ``PaymentMethodTypes``."""
    if language != "go":
        return {field}
    return {field, "".join(p[:1].upper() + p[1:] for p in field.split("_") if p)}


def _is_test(rel: str) -> bool:
    *dirs, name = rel.split("/")
    return any(d in TEST_DIRS for d in dirs) or bool(TEST_FILE.match(name))


def stale_tests(
    root: Path, sites: list[tuple[str, str]], edited: set[str], senders: set[str] = frozenset(),
) -> list[tuple[str, int]]:
    """``(file, line)`` in test files outside ``edited`` that name a field one of ``sites`` renames or removes.

    ``sites`` holds ``(action, target)`` for every planned edit; ``edited`` are the files the fix changes, whose
    languages are the only ones whose tests are read (all when empty), except a test that sends to Stripe
    itself: one of ``senders`` (files with a Stripe call) or one naming ``api.stripe.com``. Lines are 1-based;
    at most MAX_MENTIONS.
    """
    fields = {leaf(target) for action, target in sites if action in GONE_ACTIONS}
    if not fields:
        return []
    families = {FAMILY.get(lang, lang) for f in edited if (lang := LANGUAGES.get(Path(f).suffix.lower()))}
    loose = re.compile("|".join(re.escape(f.replace("_", "")) for f in sorted(fields)), re.I)
    root = Path(root).resolve()
    files, _, _ = _iter_files(root) if root.is_dir() else ([], 0, 0)
    out: list[tuple[str, int]] = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        language = LANGUAGES.get(path.suffix.lower())
        if rel in edited or language is None or not _is_test(rel):
            continue
        other = bool(families) and FAMILY.get(language, language) not in families and rel not in senders
        text = _read(path)
        if text is None or not loose.search(text.replace("_", "")) or (other and "api.stripe.com" not in text):
            continue  # a test in another language can only matter when it sends to Stripe itself
        found = [t for name in fields for spelling in _spellings(name, language) for t in tokens(text, language, spelling)]
        found += _form_encoded(text, language, fields)
        objects = _stripe_objects(text, language, {t.pos for t in found})
        if language == "ruby":  # ``receive(:payment_method_types)`` stubs the response's reader, not a request key
            objects |= {t.pos for t in found if STUBBED.search(text, max(0, t.pos - 40), t.pos)}
        for number in sorted({t.line for t in found if t.pos not in objects}):
            out.append((rel, number))
            if len(out) >= MAX_MENTIONS:
                return out
    return out


def _form_encoded(text: str, language: str, fields: set[str]) -> list[Token]:
    """The field as a form-encoded request key inside a string (``'amount=1&payment_method_types[0]=card'``,
    ``'payment_method_types[0]' => 'card'``): an HTTP stub or a recorded body that expects the old request."""
    names = "|".join(re.escape(f) for f in sorted(fields))
    form = re.compile(rf"(?:^|(?<=[&?\s'\"`]))(?:{names})(?=\[|=|%5B)", re.I)
    out = []
    for lo, hi in _spans(text, language):
        if text[lo] not in "'\"`":
            continue  # a comment
        for m in form.finditer(text, lo + 1, hi):
            out.append(Token(m.start(), text.count("\n", 0, m.start()) + 1, False))
    return out


def _spans(text: str, language: str) -> list[tuple[int, int]]:
    """Strings and comments, as :func:`fixtokens.tokens` reads them."""
    if language == "php":
        return phplex.mask_spans(text)
    mask = PY_MASK if language == "python" else (jsparse.RUBY_MASK if language == "ruby" else jsparse.JS_MASK)
    return [(m.start(), m.end()) for m in mask.finditer(text)]


def _stripe_objects(text: str, language: str, positions: set[int]) -> set[int]:
    """The ``positions`` that are keys of a literal with an ``id`` and an ``object`` string at its own level: a
    Stripe object as the API returns it (``{"id": "seti_1", "object": "setup_intent", ...}``)."""
    spans = _spans(text, language)
    openers = _openers(text, spans, sorted(positions))
    out: set[int] = set()
    checked: dict[int, bool] = {}
    for pos, opener in openers.items():
        if opener is not None:
            if opener not in checked:
                checked[opener] = _object_literal(text, spans, opener)
            if checked[opener]:
                out.add(pos)
    return out


def _openers(text: str, spans: list[tuple[int, int]], want: list[int]) -> dict[int, int | None]:
    """For each position in ``want`` (sorted), the innermost bracket open there; strings and comments skipped."""
    out: dict[int, int | None] = {}
    stack: list[int] = []
    i = k = w = 0
    while w < len(want):
        while k < len(spans) and spans[k][1] <= i:
            k += 1
        if k < len(spans) and spans[k][0] <= i:  # in a string or comment: a quoted key here has the same opener
            while w < len(want) and want[w] < spans[k][1]:
                out[want[w]] = stack[-1] if stack else None
                w += 1
            i = spans[k][1]
            continue
        nxt = min(want[w], spans[k][0] if k < len(spans) else len(text), len(text))
        while i < nxt:
            c = text[i]
            if c in OPEN:
                stack.append(i)
            elif c in CLOSE and stack:
                stack.pop()
            i += 1
        if i >= len(text) or i == want[w]:
            out[want[w]] = stack[-1] if stack else None
            w += 1
    return out


def _object_literal(text: str, spans: list[tuple[int, int]], opener: int) -> bool:
    """The literal opened at ``opener`` has ``id`` and ``object`` keys with string values at its own level."""
    found: set[str] = set()
    starts = {a: b for a, b in spans}
    depth, i = 0, opener + 1
    while i < len(text):
        c = text[i]
        if depth == 0 and (c in "io" or (c in "'\"" and text[i + 1 : i + 2] in ("i", "o"))):
            m = OBJECT_KEY.match(text, i)
            if m and not (text[i - 1].isalnum() or text[i - 1] in "_$"):
                found.add(m.group(1))
                if found == {"id", "object"}:
                    return True
        if i in starts:
            i = starts[i]
            continue
        if c in OPEN:
            depth += 1
        elif c in CLOSE:
            if depth == 0:
                return False
            depth -= 1
        i += 1
    return False
