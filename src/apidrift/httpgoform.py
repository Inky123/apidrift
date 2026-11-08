"""Form bodies of Go raw-HTTP Stripe calls, for :mod:`httpgo`: ``url.Values{...}`` literals, and keys set
with ``.Set`` / ``.Add`` / ``form[k] =``. Form keys become field paths through :func:`httpurl.form_paths`.

A key the wrapper itself sets on every path is sent by every caller. One it sets only under an ``if``
is sent only when the condition holds: when the condition compares the path parameter with a literal
(``if path == "payment_intents"``), it is decided from each caller's literal path; otherwise the key may
or may not be sent (hidden there, never claimed).
"""

from __future__ import annotations

import json
import re

from . import httpurl
from .golex import ID, STR


def _str(tok: str) -> str:
    if tok.startswith("`"):
        return tok[1:-1]
    try:
        return json.loads(tok) if tok.startswith('"') else tok
    except ValueError:
        return tok[1:-1]


def writes(f, name: str, lo: int, hi: int, fn=None, path_name: str | None = None):
    """(field paths set with literal keys, changed in a way that is not visible, conditional keys) for a
    form ``name`` between tokens ``lo`` and ``hi``. With ``fn`` (a wrapper's own body), keys set inside
    a nested block are conditional: (path, literal the path parameter must equal, or None)."""
    g, adds, hidden, maybe = f.g, set(), False, []
    for i in g.ids(name):
        if not (lo < i < hi) or (i > 0 and g.tok[i - 1] == "."):
            continue
        nxt = g.tok[i + 1] if i + 1 < g.n else ""
        key = None
        if nxt == "." and g.tok[i + 2] in ("Set", "Add") and g.tok[i + 3] == "(":
            args = g.split(i + 4, g.match[i + 3])
            key = _key(f, args[0]) if args else None
            hidden = hidden or key is None
        elif nxt == "[" and g.tok[g.match[i + 1] + 1] == "=":
            key = _key(f, (i + 2, g.match[i + 1]))
            hidden = hidden or key is None
        elif nxt in ("=", ":=") and g.starts_stmt(i):
            hidden = True
        elif nxt == "." and g.tok[i + 2] in ("Encode", "Get", "Has", "Del"):
            continue
        elif nxt in (",", ")") and g.parent[i] >= 0 and g.tok[g.parent[i]] == "(":
            if f.callee(g.parent[i]) not in ("NewReader", "Encode", "len") and "Encode" not in g.tok[i + 1 : i + 3]:
                hidden = True  # handed to another function, which may add keys
        if not key:
            continue
        block = g.block_of(i)
        if fn is not None and block != fn.lo:
            cond = _path_equals(f, block, path_name)
            maybe += [(p, cond) for p in key]
        else:
            adds |= set(key)
    return adds, hidden, tuple(maybe)


def _path_equals(f, block: int, path_name: str | None) -> str | None:
    """The literal of ``if <path> == "lit" {`` when ``block`` is that if's body (and not an ``else``)."""
    g = f.g
    if path_name is None or g.head.get(block) != "if":
        return None
    j = block - 1
    while j > 0 and g.tok[j] != "if" and block - j < 12:
        j -= 1
    cond = g.tok[j + 1 : block]
    if len(cond) == 3 and cond[1] == "==":
        a, b = cond[0], cond[2]
        if a == path_name and g.kind[block - 1] == STR:
            return _str(b)
        if b == path_name and g.kind[j + 1] == STR:
            return _str(a)
    return None


def _key(f, span: tuple[int, int]) -> list[str] | None:
    g = f.g
    a, b = span
    if b - a == 1 and g.kind[a] == STR:
        return httpurl.form_paths(_str(g.tok[a]))
    if b - a >= 5 and g.tok[a] == "fmt" and g.tok[a + 2] == "Sprintf":
        args = g.split(a + 4, g.match[a + 3])
        if args and g.kind[args[0][0]] == STR and args[0][1] - args[0][0] == 1:
            text = _str(g.tok[args[0][0]])
            if re.search(r"%(?!d)", text):
                return None
            return httpurl.form_paths(text.replace("%d", "0"))
    return None


def values_literal(f, a: int) -> tuple[set[str], bool] | None:
    """Keys of ``url.Values{...}`` starting at ``a`` (or ``make(url.Values)``): (paths, all visible)."""
    g = f.g
    if g.tok[a : a + 4] == ["url", ".", "Values", "{"]:
        brace = a + 3
        paths, visible = set(), True
        for x, y in g.split(brace + 1, g.match[brace]):
            if g.kind[x] == STR and g.tok[x + 1] == ":":
                got = httpurl.form_paths(_str(g.tok[x]))
                if got is None:
                    visible = False
                else:
                    paths |= set(got)
            else:
                visible = False
        return paths, visible
    if g.tok[a : a + 6] == ["make", "(", "url", ".", "Values", ")"]:
        return set(), True
    return None


def body_fields(f, span: tuple[int, int], fn, at: int) -> tuple[set[str], bool]:
    """(field paths, all visible) of the form a caller passes: ``nil``, a literal, or a local built
    from one before the call."""
    g = f.g
    a, b = span
    if b - a == 1 and g.tok[a] == "nil":
        return set(), True
    got = values_literal(f, a)
    if got is not None and g.match[a + 3 if g.tok[a] == "url" else a + 1] + 1 == b:
        return got
    if b - a == 1 and g.kind[a] == ID:
        name = g.tok[a]
        defs = f.assignments(name, fn, at)
        if len(defs) == 1 and defs[0][1] != "+=":
            i, _, span = defs[0]
            if span is None:
                declared = g.tok[i - 1] == "var" and g.tok[i + 1 : i + 4] == ["url", ".", "Values"]
                got = (set(), True) if declared else None
            else:
                got = values_literal(f, span[0])
                if got is not None and g.match[span[0] + 3 if g.tok[span[0]] == "url" else span[0] + 1] + 1 != span[1]:
                    got = None
            if got is not None:
                adds, hidden, _ = writes(f, name, i + 1, at)
                return got[0] | adds, got[1] and not hidden
    return set(), False
