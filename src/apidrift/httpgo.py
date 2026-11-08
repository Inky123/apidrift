"""Go raw-HTTP Stripe wrappers and their calls, for :mod:`httpwrap`.

``func stripeRequest(ctx context.Context, method, path string, form url.Values, ...)`` that builds
``http.NewRequestWithContext(ctx, method, "https://api.stripe.com/v1/"+path, strings.NewReader(form.Encode()))``
is a wrapper (path 2, method 1, body 3), and so is one whose base is a constant, or a struct field the
same file only ever sets to a Stripe base (``c.APIBaseURL = defaultAPIBaseURL``), trimmed or not
(``strings.TrimRight(cfg.APIBaseURL, "/") + path``). Calls in the same package directory are read:
the path from literals joined with ``+`` (or a ``fmt.Sprintf`` format, or a local bound once to one),
the fields from a ``url.Values{...}`` literal or a local built with ``.Set`` / ``.Add`` before the call.
"""

from __future__ import annotations

import json
import re

from . import httpurl, httpwrap
from .golex import ID, OP, SEMI, STR, GoSource

HTTP_CALLS = {"NewRequest": (0, 1, 2), "NewRequestWithContext": (1, 2, 3), "Get": (None, 0, None),
              "Post": (None, 0, 2), "PostForm": (None, 0, 1), "Head": (None, 0, None)}  # (method, url, body)
FIXED = {"Get": "get", "Post": "post", "PostForm": "post", "Head": None}
TRIMS = {"TrimRight", "TrimSuffix", "TrimSpace", "TrimPrefix", "TrimLeft", "Trim"}
FUNC_NAME = re.compile(r"(?m)^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")
VERB = re.compile(r"%[-+# 0]*\d*(?:\.\d+)?([a-zA-Z%])")


def _str(tok: str) -> str:
    if tok.startswith("`"):
        return tok[1:-1]
    try:
        return json.loads(tok) if tok.startswith('"') else tok
    except ValueError:
        return tok[1:-1]


class File:
    def __init__(self, text: str) -> None:
        self.text = text
        self.g = g = GoSource(text)
        self.consts: dict[str, str] = {}
        self.fields: dict[str, set[str | None]] = {}
        for i in range(g.n - 3):
            if g.kind[i] == ID and g.tok[i + 1] == "=" and g.kind[i + 2] == STR and g.func_of(i) < 0:
                if g.kind[i + 3] == SEMI or g.tok[i + 3] == ")":
                    if i == 0 or g.tok[i - 1] != ".":
                        self.consts.setdefault(g.tok[i], _str(g.tok[i + 2]))
        for i in range(1, g.n - 2):
            if g.tok[i - 1] == "." and g.kind[i] == ID and g.tok[i + 1] == "=":
                j = i + 2
                end = g.stmt_end(j)
                if not any(g.tok[k] == g.tok[i] and g.tok[k - 1] == "." for k in range(j, end)):
                    # ``c.URL = strings.TrimRight(c.URL, "/")`` only reshapes what it held
                    self.fields.setdefault(g.tok[i], set()).add(self.value(j, end))
        self.funcs: list[tuple[str, bool, tuple[str, ...], int, int]] = []  # name, method, params, body {, }
        for brace, f in g.funcs.items():
            j = f + 1
            method = g.tok[j] == "("
            if method:
                j = g.match[j] + 1
            if g.kind[j] != ID or g.tok[j + 1] != "(":
                continue
            paren = j + 1
            groups = g.split(paren + 1, g.match[paren])
            named = any(b - a > 1 and g.tok[a] != "..." for a, b in groups)
            params = tuple(g.tok[a] if named and g.kind[a] == ID else "" for a, b in groups)
            self.funcs.append((g.tok[j], method, params, brace, g.match[brace]))

    def value(self, a: int, b: int, depth: int = 0) -> str | None:
        """The string an expression holds when it is a literal, a constant, a field always set to one
        Stripe base, or a ``strings.Trim*`` of one."""
        g = self.g
        if depth > 4:
            return None
        while a < b and g.kind[b - 1] == SEMI:
            b -= 1
        if b - a == 1 and g.kind[a] == STR:
            return _str(g.tok[a])
        if b - a == 1 and g.kind[a] == ID:
            return self.consts.get(g.tok[a])
        if g.tok[b - 1] == ")" and g.match[b - 1] >= 0:
            opener = g.match[b - 1]
            if opener - 1 >= a and g.tok[opener - 1] in TRIMS and opener - 3 >= a and g.tok[opener - 3] == "strings":
                args = g.split(opener + 1, b - 1)
                if args:
                    inner = self.value(*args[0], depth + 1)
                    if inner is not None and httpurl.is_base(inner.rstrip("/") + "/"):
                        return inner.rstrip("/")
            return None
        if b - a >= 3 and all(g.tok[k] == "." for k in range(a + 1, b, 2)) and g.kind[b - 1] == ID:
            values = self.fields.get(g.tok[b - 1], set())
            got = set(values)
            if values and None not in got and len(got) == 1:
                one = got.pop()
                return one if httpurl.is_base(one.rstrip("/") + "/") else None
        return None

    def operand_start(self, k: int) -> int:
        """First token of the operand that ends just before token ``k``."""
        g, j = self.g, k - 1
        while j >= 0:
            if g.tok[j] in (")", "]", "}") and g.kind[j] == OP and g.match[j] >= 0:
                j = g.match[j] - 1
                continue
            if g.kind[j] in (ID, STR):
                if j > 0 and g.tok[j - 1] == ".":
                    j -= 2
                    continue
                return j
            return j + 1
        return 0

    def callee(self, opener: int) -> str:
        g = self.g
        return g.tok[opener - 1] if opener > 0 and g.kind[opener - 1] == ID else ""

    def pieces(self, a: int, b: int, fn=None, before: int = 0, depth: int = 0) -> list[str | None]:
        """Literal pieces of a path expression joined with ``+`` (None for anything else)."""
        g = self.g
        out: list[str | None] = []
        for x, y in self.terms(a, b):
            if y - x == 1 and g.kind[x] == STR:
                out.append(_str(g.tok[x]))
            elif y - x >= 4 and g.tok[x] == "fmt" and g.tok[x + 2] == "Sprintf" and g.tok[x + 3] == "(":
                args = g.split(x + 4, g.match[x + 3])
                if args and args[0][1] - args[0][0] == 1 and g.kind[args[0][0]] == STR:
                    fmt_ = _str(g.tok[args[0][0]])
                    out.append(VERB.sub(lambda m: "%" if m.group(1) == "%" else httpurl.HOLE, fmt_))
                else:
                    out.append(None)
            elif y - x == 1 and g.kind[x] == ID and fn is not None and depth < 2:
                local = self.local(g.tok[x], fn, before)
                if local is None:
                    out.append(None)
                else:
                    for la, lb in local:
                        out += self.pieces(la, lb, fn, before, depth + 1)
            else:
                out.append(None)
        return out

    def terms(self, a: int, b: int) -> list[tuple[int, int]]:
        g, out, lo, j = self.g, [], a, a
        while j < b:
            if g.kind[j] == OP and g.tok[j] == "+":
                out.append((lo, j))
                lo = j + 1
            j = g._skip(j)
        out.append((lo, b))
        return [(x, y) for x, y in out if y > x]

    def local(self, name: str, fn, before: int) -> list[tuple[int, int]] | None:
        """Value spans of a local defined once (``:=`` / ``=``) before ``before``, plus its ``+=`` appends."""
        defs, appends = [], []
        for i, op, span in self.assignments(name, fn, before):
            (appends if op == "+=" else defs).append(span)
        return defs + appends if len(defs) == 1 and defs[0] is not None else None

    def assignments(self, name: str, fn, before: int) -> list[tuple[int, str, tuple[int, int] | None]]:
        """(token, operator, value span or None) of every statement in ``fn`` before ``before`` that sets the
        local ``name`` visible there: ``x := v``, ``x = v``, ``x += v``, ``var x T``, ``x, err := f()``."""
        g, out = self.g, []
        for i in g.ids(name):
            if not (fn[3] < i < before) or (i > 0 and g.tok[i - 1] == "."):
                continue
            op = g.tok[i + 1] if i + 1 < g.n else ""
            multi = self.multi_assign(i)
            if multi:
                op = multi
            block = g.block_of(i)
            declares = op == ":=" or g.tok[i - 1] == "var"
            if declares and block >= 0 and not (block < before < g.match[block]):
                continue  # declared in a block the call is not in: another variable
            if i > 0 and g.tok[i - 1] == "var":
                out.append((i, "=", (i + 3, g.stmt_end(i + 3)) if g.tok[i + 2] == "=" else None))
            elif multi:
                out.append((i, multi, None))  # one of several names a statement sets
            elif op in (":=", "=", "+=") and g.starts_stmt(i):
                out.append((i, op, (i + 2, g.stmt_end(i + 2))))
        return out

    def multi_assign(self, i: int) -> str | None:
        """``:=`` / ``=`` when identifier ``i`` is one of the names on the left of ``a, b := ...``."""
        g, a = self.g, i
        while a >= 2 and g.tok[a - 1] == "," and g.kind[a - 2] == ID:
            a -= 2
        b = i
        while b + 2 < g.n and g.tok[b + 1] == "," and g.kind[b + 2] == ID:
            b += 2
        if a == b or not g.starts_stmt(a) or b + 1 >= g.n:
            return None
        return g.tok[b + 1] if g.tok[b + 1] in (":=", "=") else None


def _direct(f: File, name: str, method_: bool, params: tuple[str, ...], lo: int, hi: int, rel: str):
    g = f.g
    for k in range(lo, hi):
        if not (g.kind[k] == OP and g.tok[k] == "+" and g.kind[k + 1] == ID and g.tok[k + 1] in params):
            continue
        if g.tok[k + 2] in (".", "[", "(") or g.tok[k - 1] in ("+",):
            continue
        start = f.operand_start(k)
        base = f.value(start, k)
        if base is None or not httpurl.is_base(base):
            continue
        path = params.index(g.tok[k + 1])
        call = _consumer(f, start, k + 2, lo, hi)
        if call is None:
            continue
        opener, kind = call
        args = g.split(opener + 1, g.match[opener])
        mi, _, bi = HTTP_CALLS[kind]
        method, method_param = FIXED.get(kind), None
        if mi is not None and mi < len(args):
            a, b = args[mi]
            word = " ".join(g.tok[a:b]).replace(" ", "")
            method = httpurl.method_word(_str(word) if g.kind[a] == STR else word)
            if b - a == 1 and g.tok[a] in params:
                method_param = params.index(g.tok[a])
        body, body_seen = None, True
        if bi is not None and bi < len(args):
            a, b = args[bi]
            used = _params_in(f, a, b, params, (lo, opener), {path, method_param})
            if not (b - a == 1 and g.tok[a] == "nil") and not (b - a == 3 and g.tok[a + 2] == "NoBody"):
                if len(used) == 1:
                    body = used.pop()
                else:
                    body_seen = False
        adds, rewrites = (_writes(f, params[body], lo, opener) if body is not None else (set(), False))
        return httpwrap.Wrapper(
            "go", rel, name, "", base, path, method, method_param, body, body_seen, rewrites, frozenset(adds),
            tuple(None for _ in params), method_, g.line(lo),
        )
    return None


def _consumer(f: File, start: int, end: int, lo: int, hi: int) -> tuple[int, str] | None:
    """The HTTP call whose URL argument is the join at ``[start, end)``, directly or through a local."""
    g = f.g
    p = g.parent[start]
    if p >= 0 and g.tok[p] == "(" and f.callee(p) in HTTP_CALLS:
        return p, f.callee(p)
    if start >= 2 and g.tok[start - 1] in (":=", "=") and g.kind[start - 2] == ID:
        var = g.tok[start - 2]
        for i in g.ids(var):
            if end < i < hi:
                q = g.parent[i]
                if q >= 0 and g.tok[q] == "(" and f.callee(q) in HTTP_CALLS:
                    args = g.split(q + 1, g.match[q])
                    ui = HTTP_CALLS[f.callee(q)][1]
                    if ui < len(args) and args[ui] == (i, i + 1):
                        return q, f.callee(q)
    return None


def _params_in(f: File, a: int, b: int, params, scope, skip) -> set[int]:
    """Parameters an argument is built from, looking through locals assigned in the function."""
    g, out, seen = f.g, set(), set()
    todo = [(a, b)]
    while todo:
        x, y = todo.pop()
        for j in range(x, y):
            if g.kind[j] != ID or (j > 0 and g.tok[j - 1] == "."):
                continue
            name = g.tok[j]
            if name in params and params.index(name) not in skip:
                out.add(params.index(name))
            elif name not in seen and name not in params:
                seen.add(name)
                for i in g.ids(name):
                    if scope[0] < i < scope[1] and g.tok[i + 1] in ("=", ":=") and g.starts_stmt(i):
                        todo.append((i + 2, g.stmt_end(i + 2)))
    return out


def _writes(f: File, name: str, lo: int, hi: int) -> tuple[set[str], bool]:
    """(field paths added with literal keys, changed in a way that is not visible) for a form ``name``."""
    g, adds, hidden = f.g, set(), False
    for i in g.ids(name):
        if not (lo < i < hi) or (i > 0 and g.tok[i - 1] == "."):
            continue
        nxt = g.tok[i + 1] if i + 1 < g.n else ""
        if nxt == "." and g.tok[i + 2] in ("Set", "Add") and g.tok[i + 3] == "(":
            args = g.split(i + 4, g.match[i + 3])
            key = _key(f, args[0]) if args else None
            if key is None:
                hidden = True
            else:
                adds |= set(key)
        elif nxt == "[" and g.tok[g.match[i + 1] + 1] == "=":
            key = _key(f, (i + 2, g.match[i + 1]))
            hidden = hidden or key is None
            adds |= set(key or ())
        elif nxt in ("=", ":=") and g.starts_stmt(i):
            hidden = True
        elif nxt == "." and g.tok[i + 2] in ("Encode", "Get", "Has", "Del"):
            continue
        elif nxt in (",", ")") and g.parent[i] >= 0 and g.tok[g.parent[i]] == "(":
            if f.callee(g.parent[i]) not in ("NewReader", "Encode", "len") and "Encode" not in g.tok[i + 1 : i + 3]:
                hidden = True  # handed to another function, which may add keys
    return adds, hidden


def _key(f: File, span: tuple[int, int]) -> list[str] | None:
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


def _values_literal(f: File, a: int) -> tuple[set[str], bool] | None:
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


def _body_fields(f: File, span: tuple[int, int], fn, at: int) -> tuple[set[str], bool]:
    g = f.g
    a, b = span
    if b - a == 1 and g.tok[a] == "nil":
        return set(), True
    got = _values_literal(f, a)
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
                got = _values_literal(f, span[0])
                if got is not None and g.match[span[0] + 3 if g.tok[span[0]] == "url" else span[0] + 1] + 1 != span[1]:
                    got = None
            if got is not None:
                adds, hidden = _writes(f, name, i + 1, at)
                return got[0] | adds, got[1] and not hidden
    return set(), False


def survey(text: str, rel: str):
    defined = set(FUNC_NAME.findall(text))
    called = set(re.findall(r"([A-Za-z_]\w*)\s*\(", text)) if defined or "(" in text else set()
    if "stripe.com" not in text:
        return [], defined, called
    return _wrappers(File(text), rel), defined, called


def _wrappers(f: File, rel: str) -> list[httpwrap.Wrapper]:
    return [w for name, m, params, lo, hi in f.funcs if (w := _direct(f, name, m, params, lo, hi, rel)) is not None]


def sites(text: str, rel: str, index, reads: frozenset[str], language: str, registry) -> list:
    directory = rel.rsplit("/", 1)[0] if "/" in rel else ""
    known = [w for w in (registry.wrappers if registry is not None else []) if w.language == "go"
             and (w.rel.rsplit("/", 1)[0] if "/" in w.rel else "") == directory and w.rel != rel]
    if not known and "stripe.com" not in text:
        return []
    f = File(text)
    g = f.g
    here = _wrappers(f, rel)
    names = {fn[0] for fn in f.funcs}
    out = []
    for w in here + known:
        for i in g.ids(w.name):
            if g.tok[i + 1] != "(" or (i > 0 and g.tok[i - 1] == "func"):
                continue
            dotted = i > 0 and g.tok[i - 1] == "."
            if dotted != w.is_method:
                continue
            if dotted and not (registry.unique_in(directory, w.name) if registry is not None else
                               sum(1 for fn in f.funcs if fn[0] == w.name) == 1):
                continue
            if not dotted and w.rel != rel and w.name in names:
                continue  # shadowed by a function of this file
            fn = next((x for x in f.funcs if x[3] < i < x[4]), None)
            paren = i + 1
            args = g.split(paren + 1, g.match[paren])
            if w.path >= len(args) or any(g.tok[a] == "..." or g.tok[b - 1] == "..." for a, b in args):
                continue
            pieces = f.pieces(*args[w.path], fn=fn, before=i)
            method = w.method
            if w.method_param is not None and w.method_param < len(args):
                a, b = args[w.method_param]
                word = "".join(g.tok[a:b])
                method = httpurl.method_word(_str(word) if g.kind[a] == STR else word)
            fields, visible = set(), True
            if w.body is not None and w.body < len(args):
                fields, visible = _body_fields(f, args[w.body], fn, i) if fn else (set(), False)
            start = i - 2 if dotted else i
            u = httpwrap.build(text, rel, index, w, g.line(start), w.name, pieces, method, fields,
                               set() if visible else {""}, reads, language)
            if u is not None:
                out.append(u)
    return out
