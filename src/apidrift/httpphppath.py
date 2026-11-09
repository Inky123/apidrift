"""Path arguments of PHP raw-HTTP calls that are not one literal, and URLs held in constants, for :mod:`httpphp`.

``"{$endpoint}/{$id}"`` where ``$endpoint = $this->intent_endpoint($id)`` returns ``'setup_intents'`` or
``'payment_intents'`` stands for two endpoints; ``'payment_intents' . $suffix`` with ``$suffix`` either
``''`` or ``'/' . $intent->id`` for two more. Each value is a call site of its own.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from . import httpurl, httpwrap

if TYPE_CHECKING:
    from .httpphp import File

MAX_PATHS = 4  # templates one path argument may stand for before it counts as not visible


def expand(f: File, pieces: list[tuple[str, str | None]], at: int, depth: int = 0) -> list[list[str | None]]:
    """The path templates ``pieces`` can stand for: a variable whose every reaching assignment is a
    string (``$id = ''`` / ``$id = '/' . $intent->id``), or a same-class method whose every ``return``
    is a literal or a ternary of two, gives one template per value. At most MAX_PATHS of them."""
    out: list[list[str | None]] = [[]]
    for kind, value in pieces:
        options: list[list[str | None]] = [[value if kind == "lit" else None]]
        if depth < 2 and kind == "var":
            options = _variable(f, value, at, depth) or options
        elif depth < 2 and kind == "expr" and value:
            options = _returns(f, *map(int, value.split(":"))) or options
        out = [a + b for a in out for b in options]
        if len(out) > MAX_PATHS:
            return [[None]]
    return out


def _variable(f: File, name: str, at: int, depth: int) -> list[list[str | None]] | None:
    reaching = f.parsed.bindings.reaching(name, at)
    if not reaching or any(b.value < 0 or b.literal is not None for b in reaching):
        return None
    out = []
    for b in reaching:
        end = f.src.skip_scalar(b.value, ";")
        got = f.pieces(b.value, end)
        if not got:
            return None
        out += expand(f, got, b.value, depth + 1)
    return out


def _returns(f: File, a: int, b: int) -> list[list[str | None]] | None:
    m = re.fullmatch(r"(?:\$this\s*->|(?:self|static)\s*::)\s*([A-Za-z_]\w*)\s*\(.*\)", f.code[a:b].strip(), re.S)
    if m is None:
        return None
    owner = f.class_at(a)
    fns = [fn for fn in f.funcs if fn.name == m.group(1) and fn.owner == owner]
    if len(fns) != 1:
        return None
    fn, out = fns[0], []
    for r in re.finditer(r"(?<![\w$>:\\])return\b", f.code[fn.open : fn.close]):
        start = f.src.skip_space(fn.open + r.end())
        end = f.src.skip_scalar(start, ";")
        if f.func_at(start) is not fn:
            continue  # a closure's return
        values = _literal_or_ternary(f, start, end)
        if values is None:
            return None
        out += [[v] for v in values]
    return out or None


def _literal_or_ternary(f: File, a: int, b: int) -> list[str] | None:
    code = f.code
    depth, q = 0, -1
    for j in range(a, b):
        if f.src.masked(j):
            continue
        c = code[j]
        depth += c in "([{"
        depth -= c in ")]}"
        if depth == 0 and c == "?" and code[j + 1 : j + 2] not in ("?", "-", ">") and code[j - 1 : j] != "?":
            q = j
            break
    spans = [(a, b)]
    if q >= 0:
        colon = next((j for j in range(q + 1, b) if code[j] == ":" and not f.src.masked(j)
                      and code[j - 1 : j] != ":" and code[j + 1 : j + 2] != ":"), -1)
        if colon < 0:
            return None
        spans = [(q + 1, colon), (colon + 1, b)]
    out = []
    for x, y in spans:
        got = f.pieces(x, y)
        if len(got) != 1 or got[0][0] != "lit":
            return None
        out.append(got[0][1])
    return out


def constant_sites(f: File, text: str, rel: str, index, reads, language) -> list:
    """HTTP calls whose URL is a constant holding a full Stripe URL (``curl_init(self::FILES_API_ENDPOINT)``,
    ``wp_remote_post(self::IMPORT_SETS_ENDPOINT . '/' . $id, ...)``); a literal URL is the URL scan's."""
    out = []
    for m in _http().finditer(f.code):
        callee, verb = m.group(1), m.group(2)
        args = f.src.args(m.end() - 1)
        url_at = 1 if verb == "request" else 0
        if len(args) <= url_at or "stripe.com" in text[args[url_at][0] : args[url_at][1]]:
            continue
        pieces = f.pieces(*args[url_at])
        if not pieces or pieces[0][0] != "lit":
            continue
        if not real_client(f, m):
            continue
        if callee == "curl_init":
            fn = f.func_at(m.start())
            rest = f.text[m.end() : fn.close if fn else len(text)]
            named = re.search(r"CURLOPT_CUSTOMREQUEST\s*(?:,|=>)\s*['\"](\w+)['\"]", rest)
            custom = "CURLOPT_CUSTOMREQUEST" in rest
            method = httpurl.method_word(named.group(1)) if named else None if custom else "post" if "CURLOPT_POST" in rest else "get"
        else:
            method = client_method(f, m, args)[0]
        w = httpwrap.Wrapper("php", rel, callee or verb, "", "", 0, method, body_seen=False)
        u = httpwrap.build(text, rel, index, w, f.line(m.start()), callee or verb,
                           [v if k == "lit" else None for k, v in pieces], method, set(), {""}, reads, language)
        if u is not None:
            out.append(u)
    return out


def _http():
    from .httpphp import HTTP

    return HTTP


def real_client(f: File, m: re.Match) -> bool:
    """A WordPress / file function, or ``->request|get|post|delete`` on something named as an HTTP client
    (``$this->client``, ``$http``, ``$guzzle``), not ``$this->cache->get(...)``."""
    if m.group(1):
        return True
    owner = re.search(r"(\$?[A-Za-z_]\w*)\s*(?:\(\s*\))?\s*$", f.code[max(0, m.start() - 80) : m.start()])
    return owner is not None and re.search(r"http|client|guzzle", owner.group(1), re.I) is not None


def client_method(f: File, m: re.Match, args) -> tuple[str | None, str | None, dict | None, tuple[str, ...]]:
    """(method the call names or None, the variable it takes the method from, its options, the option keys
    that carry a body). A WordPress default holds only when the options are a visible array without
    ``method``; options that are not visible leave the method unknown."""
    callee, verb = m.group(1), m.group(2)
    if verb == "request":  # Guzzle: request($method, $uri, $options)
        got = f.pieces(*args[0]) if args else []
        word = httpurl.method_word(got[0][1]) if len(got) == 1 and got[0][0] == "lit" else None
        var = got[0][1] if len(got) == 1 and got[0][0] == "var" else None
        return word, var, (f.array_items(*args[2]) if len(args) > 2 else {}), ("form_params", "json", "body", "multipart", "query")
    if verb:
        return verb, None, (f.array_items(*args[1]) if len(args) > 1 else {}), ("form_params", "json", "body", "query")
    options = f.array_items(*args[1]) if len(args) > 1 else {}
    if callee == "file_get_contents":
        return ("get" if len(args) == 1 else None), None, {}, ()
    if options is None:
        return None, None, None, ("body",)
    if "method" in options:
        got = f.pieces(*options["method"])
        word = httpurl.method_word(got[0][1]) if len(got) == 1 and got[0][0] == "lit" else None
        return word, (got[0][1] if len(got) == 1 and got[0][0] == "var" else None), options, ("body",)
    return ("post" if callee.endswith("_post") else "get"), None, options, ("body",)


def form_array(f, opener: int, path: str, out) -> None:
    """:func:`phpparse.read_array` that also reads form-encoded keys (``'payment_method_types[0]'``,
    ``'metadata[order]'``) through :func:`httpurl.form_paths` instead of hiding them."""
    from . import phpparse
    from .phplex import STRING

    src, text = f.src, f.text
    prefix = path + "." if path else ""
    for el in phpparse.elements(src, opener):
        span = src.span_at(el.start)
        raw = None
        if el.arrow >= 0 and span and span[0] == el.start and span[2] == STRING and src.back_space(el.arrow) == span[1]:
            raw = text[el.start + 1 : span[1] - 1]
            raw = None if "$" in raw and text[el.start] == '"' else raw
        if el.arrow < 0 and path and not text.startswith("...", el.start):  # a list element
            inner = src.array_open(el.value)
            if inner is not None:
                form_array(f, inner, path + "[]", out)
            continue
        if raw is None or not re.fullmatch(r"[A-Za-z_][\w\[\]]*", raw):
            phpparse._hide(out, path)
            continue
        if re.fullmatch(r"[A-Za-z_]\w*", raw):
            child = prefix + raw
            out.add(child)
            inner = src.array_open(el.value)
            if inner is not None:
                form_array(f, inner, child, out)
                if src.pairs[inner] + 1 != el.end:
                    phpparse._hide(out, child)
            continue
        got = httpurl.form_paths(raw)
        if got is None or src.array_open(el.value) is not None:
            phpparse._hide(out, path)
            continue
        out.paths.update(prefix + p for p in got)
