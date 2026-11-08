"""A literal Stripe URL in code: the whole template it is part of and the HTTP method its call names.

``wp_remote_get('https://api.stripe.com/v1/tokens/' . $id, ['method' => 'GET'])`` calls
``GET /v1/tokens/{token}``: the joined ``$id`` fills the next path segment, and the method comes from the
call (``'method' => 'GET'``, ``wp_remote_get``, ``http.MethodGet``, ``requests.get``) before any guess
from the words around it. Works on plain text with a small string-aware scanner, for every language.
"""

from __future__ import annotations

import os
import re

from . import httpurl

QUOTES = "'\"`"
CONCAT = {"php": (".",), "python": ("+",), "go": ("+",), "ruby": ("+",), "javascript": ("+",), "typescript": ("+",)}
LANGUAGES = {
    ".php": "php", ".py": "python", ".go": "go", ".rb": "ruby", ".rake": "ruby",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
}
PHP_VAR = re.compile(r"\{\$[^}]*\}|\$\{[^}]*\}|\$[A-Za-z_]\w*(?:->[A-Za-z_]\w*|\[[^\]]*\])*")
JS_HOLE = re.compile(r"\$\{[^}]*\}")
RUBY_HOLE = re.compile(r"#\{[^}]*\}")
PY_HOLE = re.compile(r"\{[^{}]*\}")
GO_VERB = re.compile(r"%[-+# 0]*\d*(?:\.\d+)?[vsdqxX]")
CALLEE = re.compile(r"([A-Za-z_$][\w$]*(?:\s*(?:\.|->|::|\\)\s*[A-Za-z_$][\w$]*)*)\s*$")
METHOD_VARIABLE = re.compile(r"""\bmethod['"]?\s*(?:=>|:|=)\s*(?!['"])[$A-Za-z_]""", re.I)
OPEN, CLOSE = "([{", ")]}"
REACH = 600  # how far back the enclosing call's ``(`` is looked for
CLIENTISH = re.compile(  # receivers whose get/post/delete is an HTTP request
    r"requests|httpx|http|client|session|axios|faraday|restclient|httparty|guzzle|conn|urllib|request|api|net", re.I)


def language_of(rel: str) -> str | None:
    return LANGUAGES.get(os.path.splitext(rel)[1].lower())


def string_end(text: str, q: int) -> int:
    """Index just past the string literal whose opening quote is at ``q`` (end of text if unterminated)."""
    quote, j, n = text[q], q + 1, len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
        elif c == quote:
            return j + 1
        elif c == "\n" and quote != "`" and text[q - 1 : q] != "r":
            return j  # an unterminated one-line string: stop at the line end
        else:
            j += 1
    return n


def value(text: str, q: int, end: int, language: str) -> str:
    """A string literal's text with every interpolated expression replaced by a hole."""
    quote, raw = text[q], text[q + 1 : max(q + 1, end - 1)]
    prefix = re.search(r"[A-Za-z]{0,2}$", text[max(0, q - 2) : q]).group(0).lower()
    if language == "php" and quote == '"':
        raw = PHP_VAR.sub(httpurl.HOLE, raw)
    elif quote == "`" and language in ("javascript", "typescript"):
        raw = JS_HOLE.sub(httpurl.HOLE, raw)
    elif language == "ruby" and quote == '"':
        raw = RUBY_HOLE.sub(httpurl.HOLE, raw)
    elif language == "python" and "f" in prefix:
        raw = PY_HOLE.sub(httpurl.HOLE, raw)
    return raw


def skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def expression_end(text: str, i: int, ops: tuple[str, ...]) -> int:
    """End of one operand starting at ``i``: up to the next top-level join operator, separator or closer."""
    depth, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in QUOTES:
            i = string_end(text, i)
            continue
        if c in OPEN:
            depth += 1
        elif c in CLOSE:
            if depth == 0:
                return i
            depth -= 1
        elif depth == 0 and (c in ",;\n" or any(text.startswith(o, i) for o in ops)):
            if not (c == "." and text[i + 1 : i + 2] in ("=", ".")) and not text.startswith("->", i - 1):
                return i
        i += 1
    return n


def joined(text: str, i: int, language: str) -> tuple[list[str | None], int]:
    """The pieces joined after a string literal that ends at ``i`` (literal text, None for an expression)."""
    ops = CONCAT.get(language, ("+",))
    pieces: list[str | None] = []
    while True:
        j = skip_ws(text, i)
        op = next((o for o in ops if text.startswith(o, j)), None)
        if op is None or text[j + 1 : j + 2] in ("=", op):
            return pieces, i
        j = skip_ws(text, j + len(op))
        if j < len(text) and text[j] in QUOTES:
            end = string_end(text, j)
            pieces.append(value(text, j, end, language))
        else:
            end = expression_end(text, j, ops)
            if end == j:
                return pieces, i
            pieces.append(None)
        i = end


def literal_template(text: str, start: int, language: str) -> tuple[str, int, int] | None:
    """(URL template, opening quote, end) for the URL literal around ``start`` and what is joined to it."""
    lo = max(0, start - 12)
    q = max(text.rfind(c, lo, start) for c in QUOTES)
    if q < 0 or "\n" in text[q:start]:
        return None
    end = string_end(text, q)
    url = value(text, q, end, language)
    if language == "go" and GO_VERB.search(url):
        url = GO_VERB.sub(httpurl.HOLE, url)
    pieces, stop = joined(text, end, language)
    url = url + httpurl.template(pieces)
    at = url.find("api.stripe.com")
    if at < 0:
        at = url.find("files.stripe.com")
    return ("https://" + url[at:], q, stop) if at >= 0 else None


def enclosing_call(text: str, pos: int) -> tuple[str, int, int] | None:
    """(callee, its ``(``, its ``)``) of the innermost call whose arguments hold ``pos``."""
    lo = text.rfind("\n", 0, max(0, pos - REACH)) + 1
    stack: list[int] = []
    i = lo
    while i < pos:
        c = text[i]
        if c in QUOTES:
            i = string_end(text, i)
            continue
        if c in OPEN:
            stack.append(i)
        elif c in CLOSE and stack:
            stack.pop()
        i += 1
    while stack:
        opener = stack.pop()
        if text[opener] != "(":
            continue
        m = CALLEE.search(text, max(0, opener - 120), opener)
        if m is None:
            return None
        close = _close(text, opener)
        return re.sub(r"\s+", "", m.group(1)), opener, close
    return None


def _close(text: str, opener: int) -> int:
    depth, i = 0, opener
    while i < len(text):
        c = text[i]
        if c in QUOTES:
            i = string_end(text, i)
            continue
        if c in OPEN:
            depth += 1
        elif c in CLOSE:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text)


def call_method(text: str, pos: int) -> str | None:
    """The method the call around ``pos`` names: an explicit option, its first argument
    (``http.NewRequest("GET", ...)``) or its own name; None when it names none or takes it from a variable."""
    call = enclosing_call(text, pos)
    if call is None:
        return None
    callee, opener, close = call
    args = text[opener + 1 : close]
    named = httpurl.explicit_method(args)
    if named:
        return named
    if METHOD_VARIABLE.search(args):
        return None
    last = re.split(r"[.:>\\]+", callee)[-1]
    if last in httpurl.NEW_REQUEST:
        for piece in args.split(",")[:2]:
            word = httpurl.method_word(piece)
            if word:
                return word
        return None
    if not default_holds(last, top_args(args)):
        return None
    receiver = callee[: len(callee) - len(last)]
    if receiver and not CLIENTISH.search(receiver):
        return None  # ``os.environ.get(url)``, ``$cache->get(url)``: not a request
    return httpurl.callee_method(callee)


def top_args(args: str) -> list[str]:
    """A call's argument text split at its top-level commas."""
    out, depth, start, i = [], 0, 0, 0
    while i < len(args):
        c = args[i]
        if c in QUOTES:
            i = string_end(args, i)
            continue
        if c in OPEN:
            depth += 1
        elif c in CLOSE:
            depth -= 1
        elif c == "," and depth == 0:
            out.append(args[start:i])
            start = i + 1
        i += 1
    out.append(args[start:])
    return [a.strip() for a in out if a.strip()]


def default_holds(last: str, args: list[str]) -> bool:
    """Whether a client's own method holds: ``fetch(url)`` and ``wp_remote_get(url, $args)`` take it from
    their options (a variable, a spread, or a ``method`` key), urllib's ``Request(url, data)`` posts."""
    if last in httpurl.OVERRIDABLE:
        if len(args) < 2:
            return True
        options = args[1]
        literal = re.match(r"(?:\{|\[|array\s*\()", options, re.I) is not None
        return literal and "..." not in options and not re.search(r"""['"]?\bmethod\b['"]?\s*(?::|=>|=)""", options)
    if last in httpurl.POSITIONAL_BODY:  # urllib Request / urlopen, PHP file_get_contents(url, false, $context)
        return len(args) == 1 and not any(re.match(r"(?:data|method)\s*=", a) for a in args)
    return True


def site(text: str, start: int, rel: str, index) -> tuple[str, str | None] | None:
    """(spec path, method the code names or None) of the literal URL found at ``start``; None: the
    template reader has nothing better than the URL as written."""
    language = language_of(rel)
    if language is None:
        return None
    got = literal_template(text, start, language)
    if got is None:
        return None
    url, quote, _ = got
    spec = httpurl.spec_path(index, url)
    if spec is None:
        return None
    return spec[0], call_method(text, quote)
