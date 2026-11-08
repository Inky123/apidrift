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
        method = verb if verb in ("get", "post", "delete") else None
        if callee:
            fn = f.func_at(m.start())
            rest = f.text[m.end() : fn.close if fn else len(text)]
            if callee == "curl_init":
                named = re.search(r"CURLOPT_CUSTOMREQUEST\s*(?:,|=>)\s*['\"](\w+)['\"]", rest)
                method = httpurl.method_word(named.group(1)) if named else "post" if "CURLOPT_POST" in rest else "get"
            else:
                options = f.array_items(*args[1]) if len(args) > 1 else {}
                given = (options or {}).get("method")
                got = f.pieces(*given) if given else []
                if given:
                    method = httpurl.method_word(got[0][1]) if len(got) == 1 and got[0][0] == "lit" else None
                else:
                    method = "post" if callee.endswith("_post") else "get"
        w = httpwrap.Wrapper("php", rel, callee or verb, "", "", 0, method, body_seen=False)
        u = httpwrap.build(text, rel, index, w, f.line(m.start()), callee or verb,
                           [v if k == "lit" else None for k, v in pieces], method, set(), {""}, reads, language)
        if u is not None:
            out.append(u)
    return out


def _http():
    from .httpphp import HTTP

    return HTTP
