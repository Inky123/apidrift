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
from typing import NamedTuple

from . import httpgoform, httpurl, httpwrap
from .golex import ID, OP, SEMI, STR, GoSource

HTTP_CALLS = {"NewRequest": (0, 1, 2), "NewRequestWithContext": (1, 2, 3), "Get": (None, 0, None),
              "Post": (None, 0, 2), "PostForm": (None, 0, 1), "Head": (None, 0, None)}  # (method, url, body)
FIXED = {"Get": "get", "Post": "post", "PostForm": "post", "Head": None}
TRIMS = {"TrimRight", "TrimSuffix", "TrimSpace"}  # shape-keeping: only a "/" or space comes off
FUNC_NAME = re.compile(r"(?m)^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")
VERB = re.compile(r"%[-+# 0]*\d*(?:\.\d+)?([a-zA-Z%])")


def field_base(assigns, owner: str, field: str) -> str | None:
    values = {v for t, f, v in assigns if f == field and t in (owner, "")}
    if not any(t == owner for t, f, v in assigns if f == field) or None in values or len(values) != 1:
        return None
    return values.pop()


class GoFunc(NamedTuple):
    name: str
    method: bool
    params: tuple[str, ...]
    lo: int  # body ``{``
    hi: int  # its ``}``
    recv_var: str = ""
    recv_type: str = ""
    types: tuple[str, ...] = ()  # each parameter's type name (``*Config`` -> ``Config``), "" when unknown


def _type_name(g, a: int, b: int) -> str:
    """The named type a type expression ends in: ``*Config`` / ``pay.Config`` -> ``Config``; "" otherwise."""
    toks = [g.tok[k] for k in range(a, b) if g.tok[k] not in ("*", "&")]
    if not toks or g.kind[a + (b - a) - 1] != ID or any(t in ("[", "]", "map", "func", "chan") for t in toks):
        return ""
    return toks[-1]


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
        self.assigns: list[tuple[str, str, str | None]] = []  # (struct type or "", field, the Stripe base it is set to)
        self.base_field: tuple[str, str] | None = None  # the last field :meth:`value` read a base from
        for i in range(g.n - 3):
            if g.kind[i] == ID and g.tok[i + 1] == "=" and g.kind[i + 2] == STR and g.func_of(i) < 0:
                if g.kind[i + 3] == SEMI or g.tok[i + 3] == ")":
                    if i == 0 or g.tok[i - 1] != ".":
                        self.consts.setdefault(g.tok[i], _str(g.tok[i + 2]))
        self.funcs: list[GoFunc] = []
        for brace, f in g.funcs.items():
            j = f + 1
            method = g.tok[j] == "("
            recv_var = recv_type = ""
            if method:
                close = g.match[j]
                if g.kind[j + 1] == ID and close - (j + 1) > 1:
                    recv_var = g.tok[j + 1]
                    recv_type = _type_name(g, j + 2, close)
                elif close - (j + 1) >= 1:
                    recv_type = _type_name(g, j + 1, close)
                j = close + 1
            if g.kind[j] != ID or g.tok[j + 1] != "(":
                continue
            paren = j + 1
            groups = g.split(paren + 1, g.match[paren])
            named = any(b - a > 1 and g.tok[a] != "..." for a, b in groups)
            params = tuple(g.tok[a] if named and g.kind[a] == ID else "" for a, b in groups)
            types, last = [], ""
            for a, b in reversed(groups):  # ``method, path string``: a name alone shares the next type
                if named and b - a > 1:
                    last = _type_name(g, a + 1, b)
                types.append(last if named else _type_name(g, a, b))
            self.funcs.append(GoFunc(g.tok[j], method, params, brace, g.match[brace], recv_var, recv_type,
                                     tuple(reversed(types))))
        for i in range(1, g.n - 2):
            if g.tok[i - 1] == "." and g.kind[i] == ID and g.tok[i + 1] == "=" and g.kind[i - 2] == ID:
                j = i + 2
                end = g.stmt_end(j)
                if any(g.tok[k] == g.tok[i] and g.tok[k - 1] == "." for k in range(j, end)):
                    continue  # ``c.URL = strings.TrimRight(c.URL, "/")`` only reshapes what it held
                fn = self.func_at(i)
                owner = self.type_of(g.tok[i - 2], fn, i) if fn else ""
                self.assigns.append((owner, g.tok[i], self._base(self.value(j, end, typed=False))))
            elif g.brace.get(i) == "lit" and g.kind[i - 1] == ID:  # ``&Paypal{BaseURL: u}``
                owner = g.tok[i - 1]
                for x, y in g.split(i + 1, g.match[i]):
                    if g.kind[x] == ID and g.tok[x + 1] == ":":
                        self.assigns.append((owner, g.tok[x], self._base(self.value(x + 2, y, typed=False))))

    @staticmethod
    def _base(value: str | None) -> str | None:
        return value if value is not None and httpurl.is_base(value.rstrip("/") + "/") else None

    def func_at(self, i: int) -> GoFunc | None:
        inside = [fn for fn in self.funcs if fn.lo < i < fn.hi]
        return max(inside, key=lambda fn: fn.lo) if inside else None

    def type_of(self, var: str, fn: GoFunc | None, at: int) -> str:
        """The named type of a receiver, parameter or local (``x := &T{}``, ``var x *T``); "" when unknown."""
        g = self.g
        if fn is None:
            return ""
        if var == fn.recv_var:
            return fn.recv_type
        if var in fn.params:
            return fn.types[fn.params.index(var)] if fn.params.index(var) < len(fn.types) else ""
        defs = self.assignments(var, fn, at)
        if len(defs) != 1:
            return ""
        i, op, span = defs[0]
        if span is None:
            if g.tok[i - 1] == "var":
                return _type_name(g, i + 1, g.stmt_end(i + 1))
            return ""
        a, b = span
        if g.tok[a] == "&":
            a += 1
        if g.tok[a] == "new" and g.tok[a + 1] == "(":
            return _type_name(g, a + 2, g.match[a + 1])
        k = a
        while k + 2 < b and g.tok[k + 1] == ".":
            k += 2
        return g.tok[k] if g.kind[k] == ID and g.brace.get(k + 1) == "lit" else ""

    def field_base(self, owner: str, field: str, evidence=None) -> str | None:
        """The one Stripe base every assignment of ``owner.field`` sets (this file's, or the package's
        when ``evidence`` is given); an assignment through a value of unknown type counts against it."""
        return field_base(self.assigns if evidence is None else evidence, owner, field)

    def value(self, a: int, b: int, depth: int = 0, typed: bool = True, fn=None) -> str | None:
        """The string an expression holds when it is a literal, a constant, a field of a struct type only
        ever set to a Stripe base (``typed``), or a ``strings.Trim*`` of one."""
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
                cut = "".join(g.tok[x] for x, y in args[1:2] for x in range(x, y)) if len(args) > 1 else '"/"'
                if args and (g.tok[opener - 1] == "TrimSpace" or cut in ('"/"', "`/`")):
                    inner = self.value(*args[0], depth + 1, typed, fn)
                    if inner is not None and httpurl.is_base(inner.rstrip("/") + "/"):
                        return inner.rstrip("/")
            return None
        if typed and b - a == 3 and g.tok[a + 1] == "." and g.kind[a] == ID and g.kind[b - 1] == ID:
            owner = self.type_of(g.tok[a], fn or self.func_at(a), a)
            got = self.field_base(owner, g.tok[b - 1]) if owner else None
            if got is not None:
                self.base_field = (owner, g.tok[b - 1])
            return got
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


def _direct(f: File, fn: GoFunc, rel: str):
    g, params, lo, hi = f.g, fn.params, fn.lo, fn.hi
    for k in range(lo, hi):
        if not (g.kind[k] == OP and g.tok[k] == "+" and g.kind[k + 1] == ID and g.tok[k + 1] in params):
            continue
        if g.tok[k + 2] in (".", "[", "(") or g.tok[k - 1] in ("+",):
            continue
        start = f.operand_start(k)
        f.base_field = None
        base = f.value(start, k, fn=fn)
        field = f.base_field
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
        adds, rewrites, maybe = set(), False, ()
        if body is not None:
            adds, rewrites, maybe = httpgoform.writes(f, params[body], lo, opener, fn, params[path])
        return httpwrap.Wrapper(
            "go", rel, fn.name, fn.recv_type if fn.method else "", base, path, method, method_param, body, body_seen,
            rewrites, frozenset(adds), tuple(None for _ in params), fn.method, g.line(lo), field=field, maybe=maybe,
        )
    return None


def _is_http(f: File, p: int) -> bool:
    """The call at ``(`` ``p`` is net/http's (``http.NewRequest``, ``http.Get``) or a client's
    (``client.Get``, ``c.httpClient.Post``), not ``cache.Get``."""
    g = f.g
    if f.callee(p) not in HTTP_CALLS or p < 3 or g.tok[p - 2] != ".":
        return False
    owner = g.tok[p - 3]
    return owner == "http" or (f.callee(p) not in ("NewRequest", "NewRequestWithContext")
                               and re.search(r"client|http", owner, re.I) is not None)


def _consumer(f: File, start: int, end: int, lo: int, hi: int) -> tuple[int, str] | None:
    """The HTTP call whose URL argument is the join at ``[start, end)``, directly or through a local."""
    g = f.g
    p = g.parent[start]
    if p >= 0 and g.tok[p] == "(" and _is_http(f, p):
        return p, f.callee(p)
    if start >= 2 and g.tok[start - 1] in (":=", "=") and g.kind[start - 2] == ID:
        var = g.tok[start - 2]
        for i in g.ids(var):
            if end < i < hi:
                q = g.parent[i]
                if q >= 0 and g.tok[q] == "(" and _is_http(f, q):
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


def survey(text: str, rel: str):
    """Wrappers of a Go file. Only a file that names a Stripe host is read: callers are found per package
    directory (:class:`httpwrap.Registry`), so no other file needs a survey."""
    if "stripe.com" not in text:
        return [], set(), set()
    f = File(text)
    return _wrappers(f, rel), set(), set()


def evidence(text: str) -> list[tuple[str, str, str | None]]:
    """Struct field assignments in a Go file: (type, field, the Stripe base it is set to or None)."""
    return File(text).assigns if "=" in text or ":" in text else []


def _wrappers(f: File, rel: str) -> list[httpwrap.Wrapper]:
    return [w for fn in f.funcs if (w := _direct(f, fn, rel)) is not None]


def sites(text: str, rel: str, index, reads: frozenset[str], language: str, registry) -> list:
    directory = rel.rsplit("/", 1)[0] if "/" in rel else ""
    known = [w for w in (registry.wrappers if registry is not None else []) if w.language == "go"
             and (w.rel.rsplit("/", 1)[0] if "/" in w.rel else "") == directory and w.rel != rel]
    if not known and "stripe.com" not in text:
        return []
    f = File(text)
    g = f.g
    here = _wrappers(f, rel)
    if registry is not None:  # a field base holds only if the whole package sets that field to it
        package = registry.go_evidence.get(directory)
        here = [w for w in here if w.field is None or package is None or field_base(package, *w.field) == w.base]
    names = {fn.name for fn in f.funcs if not fn.method}
    out = []
    for w in here + known:
        for i in g.ids(w.name):
            if g.tok[i + 1] != "(" or (i > 0 and g.tok[i - 1] == "func"):
                continue
            dotted = i > 0 and g.tok[i - 1] == "."
            if dotted != w.is_method:
                continue
            fn = f.func_at(i)
            if dotted:  # ``c.do(...)``: only on a value of the wrapper's own receiver type
                if g.kind[i - 2] != ID or (i > 2 and g.tok[i - 3] == ".") or not w.owner:
                    continue
                if f.type_of(g.tok[i - 2], fn, i) != w.owner:
                    continue
            elif w.rel != rel and w.name in names:
                continue  # shadowed by a function of this file
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
                fields, visible = httpgoform.body_fields(f, args[w.body], fn, i) if fn else (set(), False)
            start = i - 2 if dotted else i
            u = httpwrap.build(text, rel, index, w, g.line(start), w.name, pieces, method, fields,
                               set() if visible else {""}, reads, language)
            if u is not None:
                out.append(u)
    return out
