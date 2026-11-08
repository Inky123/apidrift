"""Code inside strings: the interpolation holes of JS template literals (``${...}``), Ruby ``#{...}``,
PHP ``"{$p['k']}"`` / ``"$p[k]"`` / ``${...}`` in double quotes, backticks and heredocs, and Python f-strings.

The fixer's token count (:mod:`fixtokens`) and the escape checks of :mod:`jsroots` and :mod:`phpscope` read
code through :func:`masked_spans`: string text and comments stay opaque, while a read inside a hole
(```payid ${pm.payto.pay_id}```) is as visible as one outside. A string nested inside a hole is opaque again.
Outside the holes the spans are the ones the plain masks give (``jsparse.JS_MASK``, ``RUBY_MASK``,
``fixtokens.PY_MASK``, :func:`phplex.mask_spans`), so nothing that was code becomes text. Where the walk is
in doubt it reads code, never text: an unterminated string or hole is given back as code, as the masks do.
"""

from __future__ import annotations

import bisect
import re

from .phplex import HEREDOC, HEREDOC_KIND, OPEN_TAG, STRING, Lex

JS_STOP = re.compile(r"['\"`{}]|//|/\*")
RUBY_STOP = re.compile(r"['\"#{}]")
PY_STOP = re.compile(r"['\"#{}]")
PY_PREFIX = re.compile(r"(?<![\w])([A-Za-z]{1,2})$")
PHP_SIMPLE = re.compile(r"\$[A-Za-z_]\w*(?:\[(?:[A-Za-z_]\w*|-?\d+|\$[A-Za-z_]\w*)\]|->[A-Za-z_]\w*)?")
Spans = list[tuple[int, int]]


def masked_spans(text: str, language: str, lex: Lex | None = None) -> Spans:
    """Opaque spans of ``text`` (string text, comments, inline HTML) with the interpolation holes cut out,
    in order and never overlapping. ``lex`` is a PHP file's :class:`phplex.Lex` when already built."""
    if language == "php":
        return _php(text, lex)
    out: Spans = []
    if language == "python":
        _py_code(text, 0, len(text), out)
    elif language == "ruby":
        _rb_code(text, 0, len(text), out)
    else:
        _js_code(text, 0, len(text), out)
    return out


class Holes:
    """``covers(pos)`` over the spans of :func:`masked_spans`: True for string text, False for code (holes too)."""

    def __init__(self, text: str, language: str, lex: Lex | None = None) -> None:
        self.spans = masked_spans(text, language, lex)
        self.starts = [a for a, _ in self.spans]

    def covers(self, pos: int) -> bool:
        k = bisect.bisect_right(self.starts, pos) - 1
        return k >= 0 and self.spans[k][0] <= pos < self.spans[k][1]


# --- JavaScript / TypeScript -------------------------------------------------------------


def _js_code(text: str, i: int, end: int, out: Spans, closer: bool = False) -> int:
    """Read code from ``i``; when ``closer``, stop at the ``}`` that closes the hole and return its index."""
    depth = 0
    while i < end:
        m = JS_STOP.search(text, i, end)
        if m is None:
            break
        i, c = m.start(), m.group(0)
        if c == "//":
            nl = text.find("\n", i, end)
            i = _add(out, i, end if nl < 0 else nl)
        elif c == "/*":
            e = text.find("*/", i + 2, end)
            i = _add(out, i, e + 2) if e >= 0 else i + 2
        elif c in "'\"":
            i = _simple_string(text, i, end, out, newline_ends=True)
        elif c == "`":
            i = _js_template(text, i, end, out)
        elif c == "{":
            depth, i = depth + 1, i + 1
        else:  # "}"
            if closer and depth == 0:
                return i
            depth, i = max(depth - 1, 0), i + 1
    return end


def _js_template(text: str, i: int, end: int, out: Spans) -> int:
    a, j, mark = i, i + 1, len(out)
    while j < end:
        c = text[j]
        if c == "\\":
            j += 2
        elif c == "`":
            return _add(out, a, j + 1)
        elif text.startswith("${", j):
            _add(out, a, j + 2)
            k = _js_code(text, j + 2, end, out, closer=True)
            if k >= end:
                break
            a, j = k, k + 1
        else:
            j += 1
    del out[mark:]  # unterminated: the backtick is read as code, as JS_MASK reads it
    return i + 1


# --- Ruby -----------------------------------------------------------------------------------


def _rb_code(text: str, i: int, end: int, out: Spans, closer: bool = False) -> int:
    depth = 0
    while i < end:
        m = RUBY_STOP.search(text, i, end)
        if m is None:
            break
        i, c = m.start(), m.group(0)
        if c == "#":
            if closer:
                i += 1  # inside a hole: code
            elif text.startswith("#{", i):
                i = _rb_bare_hole(text, i, end, out)
            else:
                nl = text.find("\n", i, end)
                i = _add(out, i, end if nl < 0 else nl)
        elif c == "'":
            i = _simple_string(text, i, end, out, newline_ends=False)
        elif c == '"':
            i = _rb_string(text, i, end, out)
        elif c == "{":
            depth, i = depth + 1, i + 1
        else:
            if closer and depth == 0:
                return i
            depth, i = max(depth - 1, 0), i + 1
    return end


def _rb_bare_hole(text: str, i: int, end: int, out: Spans) -> int:
    """``#{...}`` outside a known string (a heredoc, ``%Q()``): a hole when it closes on its line, else a comment."""
    mark = len(out)
    nl = text.find("\n", i, end)
    line_end = end if nl < 0 else nl
    k = _rb_code(text, i + 2, line_end, out, closer=True)
    if k < line_end:
        return k + 1
    del out[mark:]
    return _add(out, i, line_end)


def _rb_string(text: str, i: int, end: int, out: Spans) -> int:
    a, j, mark = i, i + 1, len(out)
    while j < end:
        c = text[j]
        if c == "\\":
            j += 2
        elif c == '"':
            return _add(out, a, j + 1)
        elif text.startswith("#{", j):
            _add(out, a, j + 2)
            k = _rb_code(text, j + 2, end, out, closer=True)
            if k >= end:
                break
            a, j = k, k + 1
        else:
            j += 1
    del out[mark:]
    return i + 1


# --- Python ----------------------------------------------------------------------------------


def _py_code(text: str, i: int, end: int, out: Spans, closer: bool = False) -> int:
    depth = 0
    while i < end:
        m = PY_STOP.search(text, i, end)
        if m is None:
            break
        i, c = m.start(), m.group(0)
        if c == "#":
            if closer:
                i += 1
            else:
                nl = text.find("\n", i, end)
                i = _add(out, i, end if nl < 0 else nl)
        elif c in "'\"":
            i = _py_string(text, i, end, out)
        elif c == "{":
            depth, i = depth + 1, i + 1
        else:
            if closer and depth == 0:
                return i
            depth, i = max(depth - 1, 0), i + 1
    return end


def _py_string(text: str, i: int, end: int, out: Spans) -> int:
    q = text[i]
    close = q * 3 if text.startswith(q * 3, i) else q
    prefix = PY_PREFIX.search(text, max(0, i - 3), i)
    interpolates = prefix is not None and any(ch in "fFtT" for ch in prefix.group(1))
    a, j, mark = i, i + len(close), len(out)
    while j < end:
        c = text[j]
        if c == "\\":
            j += 2
        elif text.startswith(close, j):
            return _add(out, a, j + len(close))
        elif c == "\n" and len(close) == 1:
            break
        elif interpolates and c in "{}":
            if text.startswith(c * 2, j):
                j += 2  # ``{{`` / ``}}``: a brace in the text
            elif c == "}":
                j += 1
            else:
                _add(out, a, j + 1)
                k = _py_code(text, j + 1, end, out, closer=True)
                if k >= end:
                    break
                a, j = k, k + 1
        else:
            j += 1
    del out[mark:]  # unterminated: the quote is code, as PY_MASK reads it
    return i + 1


# --- PHP --------------------------------------------------------------------------------------


def _php(text: str, lex: Lex | None) -> Spans:
    lex = lex or Lex(text, fragment=OPEN_TAG.search(text) is None)
    out: Spans = []
    for a, b, kind in lex.spans:
        heredoc = kind == HEREDOC_KIND and (m := HEREDOC.match(text, a)) is not None and m.group(2) is None
        if heredoc or (kind == STRING and text[a] in "\"`"):
            _php_string(text, a, b, out)
        else:
            out.append((a, b))
    return out


def _php_string(text: str, a: int, b: int, out: Spans) -> None:
    start, j = a, a + 1
    while j < b:
        c = text[j]
        if c == "\\":
            j += 2
        elif c == "{" and text.startswith("{$", j):
            _add(out, start, j + 1)
            start = j = _php_hole(text, j + 1, b, out)
        elif c == "$" and text.startswith("${", j):
            _add(out, start, j + 2)
            start = j = _php_hole(text, j + 2, b, out)
        elif c == "$" and (m := PHP_SIMPLE.match(text, j, b)) is not None:
            _add(out, start, j)
            start = j = m.end()
        else:
            j += 1
    _add(out, start, b)


def _php_hole(text: str, j: int, b: int, out: Spans) -> int:
    """From the first code character of a ``{$...}`` / ``${...}`` hole: the index of its closing ``}``
    (nested quoted strings are opaque)."""
    depth = 0
    while j < b:
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            if depth == 0:
                return j
            depth -= 1
        elif c in "'\"":
            k = j + 1
            while k < b and text[k] != c:
                k += 2 if text[k] == "\\" else 1
            _add(out, j, min(k + 1, b))
            j = k
        j += 1
    return b


# --- shared -------------------------------------------------------------------------------------


def _simple_string(text: str, i: int, end: int, out: Spans, *, newline_ends: bool) -> int:
    """A quoted string without interpolation; unterminated (or broken by a newline), the quote is code."""
    q, j = text[i], i + 1
    while j < end:
        c = text[j]
        if c == "\\":
            j += 2
        elif c == q:
            return _add(out, i, j + 1)
        elif c == "\n" and newline_ends:
            break
        else:
            j += 1
    return i + 1


def _add(out: Spans, a: int, b: int) -> int:
    if b > a:
        out.append((a, b))
    return b
