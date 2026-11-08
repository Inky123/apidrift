"""Stripe calls made through a string-dispatch wrapper: ``edds_api_request('PaymentIntent', 'create', $args)``.

Some plugins never name a stripe-php class at the call. They pass the class and method as strings to one
wrapper that builds the class name and calls it::

    function request($object, $method, ...$args) {
        $classname = 'EDD\\\\Vendor\\\\Stripe\\\\' . $object;
        return call_user_func_array(array($classname, $method), $args);
    }

A call ``wrapper('PaymentIntent', 'create', $p)`` is then ``\\Stripe\\PaymentIntent::create($p)``. The rule, checked
over the whole checkout (once per scan, only when a file has a call whose first two arguments are a class-like
and a method-like string):

- the wrapper takes exactly ``($class, $method, ...$args)`` (in either order of the first two), binds one local
  once to ``'<namespace>\\\\' . $class`` where the namespace is ``Stripe`` or a vendored copy of it
  (:mod:`phpvendor`), and makes exactly one ``call_user_func_array([$local, $method], $args)`` (or
  ``call_user_func([$local, $method], ...$args)``); neither ``$class`` nor ``$method`` is assigned in it;
- or it is a function whose body is only ``return w($class, $method, ...$args);`` for such a wrapper ``w`` (a
  function, ``$this->w``, ``self::w``, or ``$v->w`` after ``$v = new C();``), possibly through several;
- its name (a function's, or a class's method) is defined once in the checkout;
- a call counts only when both strings are literals at the call.

A wrapper that writes into ``$args`` (``$args[1]['expand'] = ...``) hides what it sends at that argument
position (every position for any other write), and those calls are never edited.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import phpinstance, phpvendor
from .phplex import STRING, Lex
from .phpscope import BIND, OPAQUE, WRITE, Bindings, Scopes

NAMED_FUNC = re.compile(r"(?<![\w$>:\\])function\s*&?\s*([A-Za-z_]\w*)\s*\(")
CALLERS = re.compile(r"(?<![\w$>:\\])(call_user_func_array|call_user_func)\s*\(")
NAMESPACE = re.compile(r"(?<![\w$>:\\])namespace\s+([A-Za-z_][\w\\]*)\s*[;{]")
CLASS_NAME = re.compile(r"(?<![\w$>:\\])(?:class|trait)\s+([A-Za-z_]\w*)[^{;]*\{")
PAIR = re.compile(r"\(\s*(['\"])[A-Z][\w\\]*\1\s*,\s*(['\"])[A-Za-z_]\w*\2")  # f('PaymentIntent', 'create'
FREE_CALL = re.compile(r"(?<![\w$>:\\])\\?([A-Za-z_]\w*)\s*\(")
PARAM = re.compile(r"^\s*(?:\??[\\\w|]+\s+)?(&\s*)?(\.\.\.\s*)?\$([A-Za-z_]\w*)\s*(?:=.*)?$", re.S)
FORWARD = re.compile(
    r"\s*(?:\$([A-Za-z_]\w*)\s*=\s*new\s+(\\?[A-Za-z_][\w\\]*)\s*(?:\(\s*\))?\s*;\s*)?"
    r"return\s+(\\?[A-Za-z_][\w\\]*|\$[A-Za-z_]\w*\s*->\s*[A-Za-z_]\w*|(?:self|static)\s*::\s*[A-Za-z_]\w*)"
    r"\s*\(\s*\$([A-Za-z_]\w*)\s*,\s*\$([A-Za-z_]\w*)\s*,\s*\.\.\.\s*\$([A-Za-z_]\w*)\s*\)\s*;\s*",
)
CONCAT = re.compile(r"\s*(['\"])((?:\\\\)?(?:[A-Za-z_]\w*\\\\)+)\1\s*\.\s*\$([A-Za-z_]\w*)\s*;")
ARG_WRITE = re.compile(r"\s*\[\s*(\d+)\s*\]((?:\s*\[\s*(?:'[A-Za-z_]\w*'|\"[A-Za-z_]\w*\")\s*\])*)\s*=(?![=>])")
EMPTY = re.compile(r"\s*(?:array\s*\(\s*\)|\[\s*\])\s*;")
ANY = -1  # in Dispatcher.changes: the wrapper may change any forwarded argument
MAX_FORWARDS = 4


@dataclass(frozen=True)
class Dispatcher:
    cls: int  # which argument names the class
    method: int  # which argument names the method (the forwarded arguments follow both)
    changes: frozenset[int]  # forwarded argument positions the wrapper writes into; ANY: any of them
    adds: frozenset[tuple[int, str]] = frozenset()  # (position, key path) written by a literal key path


def dispatchers() -> dict[str, Dispatcher]:
    """Free functions (lowercased) of this checkout that dispatch to stripe-php by class-name string."""
    return phpvendor.per_repo("php-dispatch", _find) or {}


def calls(src: Lex) -> list[tuple[int, int, str, list[str], str, Dispatcher]]:
    """(start, ``(`` position, wrapper name, class parts, method, dispatcher) for every dispatch call in a file."""
    if not PAIR.search(src.text):
        return []
    table = dispatchers()
    if not table:
        return []
    out = []
    text = src.text
    for m in FREE_CALL.finditer(src.code):
        d = table.get(m.group(1).lower())
        if d is None or (m.end() - 1) not in src.pairs:
            continue
        before = text[max(0, m.start() - 12) : m.start()].rstrip()
        if before.endswith(("function", "new")):
            continue
        args = src.args(m.end() - 1)
        if len(args) < 2:
            continue
        cls, method = _string(src, args[d.cls]), _string(src, args[d.method])
        if cls is None or method is None or not re.fullmatch(r"[A-Za-z_]\w*(?:\\[A-Za-z_]\w*)*", cls):
            continue
        if not re.fullmatch(r"[A-Za-z_]\w*", method):
            continue
        out.append((m.start(), m.end() - 1, m.group(1), cls.split("\\"), method, d))
    return out


def add(parsed, item, resolve, resolved_at: dict, other: dict) -> None:
    """Record one dispatch call as the static stripe-php call it makes (phpparse's ``Call``), or as a
    Stripe-looking call that matches no endpoint."""
    from .phpparse import Call, _params, _static  # phpparse builds on this module

    src = parsed.src
    start, open_, name, cls, method, d = item
    shown = "\\".join(cls)
    call = f"{name}('{shown}', '{method}')"
    spans = src.args(open_)
    first = max(d.cls, d.method) + 1  # the forwarded arguments
    resolved = phpinstance.account(_static(["Stripe"] + cls, method, resolve), src, spans[first:], method, resolve)
    if resolved is None:
        other[start] = (src.line(start), call)
        return
    ids = resolved.path.count("{")
    params = _params(src, spans[first:], ids, id_only=method == "retrieve" and ids == 1)
    hidden = "" if params is None or not (ANY in d.changes or params in d.changes) else "the wrapper writes into it"
    extra = tuple(sorted(path for k, path in d.adds if k == params))
    c = Call(src.line(start), call, resolved, open_, spans, None if params is None else params + first, start, None, hidden, extra)
    resolved_at[start] = c
    parsed.calls.append(c)


def _string(src: Lex, span: tuple[int, int]) -> str | None:
    """The value of a plain string literal that is the whole argument (no interpolation)."""
    a, b = span
    i = src.skip_space(a)
    s = src.span_at(i)
    if s is None or s[0] != i or s[2] != STRING or src.skip_space(s[1]) < b:
        return None
    raw = src.text[i : s[1]]
    if raw[0] not in "'\"" or (raw[0] == '"' and re.search(r"[$]|\\[^\\]", raw[1:-1])):
        return None
    return raw[1:-1].replace("\\\\", "\\")


# --- the checkout's wrappers -------------------------------------------------------------


@dataclass
class _Fn:
    key: tuple[str, ...]  # ("func", name) or ("method", class, name), lowercased
    params: list[tuple[str, bool, bool]]  # (name, variadic, by reference)
    lo: int
    hi: int
    file: "_File"


@dataclass
class _File:
    src: Lex
    scopes: Scopes
    bindings: Bindings
    namespace: str | None
    uses: dict[str, list[str]]


def _find(root) -> dict[str, Dispatcher]:
    from .scanner import _iter_files  # the scanner builds on this module

    fns: list[_Fn] = []
    for path in _iter_files(root)[0]:
        if path.suffix.lower() != ".php":
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"call_user_func" not in raw and b"...$" not in raw:
            continue
        fns += _functions(raw.decode("utf-8", errors="replace"))
    counts: dict[tuple, int] = {}
    for fn in fns:
        counts[fn.key] = counts.get(fn.key, 0) + 1
    known: dict[tuple, Dispatcher] = {}
    for fn in fns:
        d = _core(fn) if counts[fn.key] == 1 else None
        if d is not None:
            known[fn.key] = d
    for _ in range(MAX_FORWARDS):
        added = False
        for fn in fns:
            if fn.key in known or counts[fn.key] != 1:
                continue
            d = _forward(fn, known)
            if d is not None:
                known[fn.key], added = d, True
        if not added:
            break
    return {k[1]: d for k, d in known.items() if k[0] == "func"}


def _functions(text: str) -> list[_Fn]:
    from .phpparse import _uses  # phpparse builds on this module

    src = Lex(text)
    if not src.balanced or src.uncertain:
        return []
    scopes = Scopes(src)
    spaces = NAMESPACE.findall(src.code)
    f = _File(src, scopes, Bindings(src, scopes), spaces[0] if len(spaces) == 1 else None, _uses(src))
    classes = {m.end() - 1: m.group(1) for m in CLASS_NAME.finditer(src.code)}
    out = []
    for m in NAMED_FUNC.finditer(src.code):
        close = src.pairs.get(m.end() - 1)
        if close is None:
            continue
        j = src.skip_scalar(src.skip_space(close + 1), "{;")
        if j >= src.n or src.text[j] != "{" or j not in src.pairs:
            continue
        params = []
        for a, b in src.args(m.end() - 1):
            p = PARAM.match(src.code[a:b])
            if p is None:
                params = None
                break
            params.append((p.group(3), bool(p.group(2)), bool(p.group(1))))
        if params is None:
            continue
        holder = src.enclosing(m.start())
        name = m.group(1).lower()
        if holder in classes:
            if f.namespace is None and spaces:
                continue
            key = ("method", _fq(f.namespace, classes[holder]), name)
        elif holder < 0:
            key = ("func", name)
        else:
            continue  # a function declared inside another: not reachable by name for certain
        out.append(_Fn(key, params, j, src.pairs[j], f))
    return out


def _fq(namespace: str | None, name: str) -> str:
    return (namespace + "\\" + name if namespace else name).lower()


def _shape(fn: _Fn) -> tuple[str, str, str] | None:
    """The (first, second, variadic) parameter names of a ``($a, $b, ...$c)`` function."""
    p = fn.params
    if len(p) != 3 or not p[2][1] or p[0][1] or p[1][1] or any(ref for _, _, ref in p):
        return None
    return p[0][0], p[1][0], p[2][0]


def _uses_in(fn: _Fn, name: str) -> list:
    b = fn.file.bindings
    return [u for u in b.uses.get(name, ()) if fn.lo < u.pos < fn.hi]


def _core(fn: _Fn) -> Dispatcher | None:
    shape = _shape(fn)
    if shape is None:
        return None
    src, code = fn.file.src, fn.file.src.code
    found = list(CALLERS.finditer(code, fn.lo, fn.hi))
    if len(found) != 1 or (found[0].end() - 1) not in src.pairs:
        return None
    m = found[0]
    args = src.args(m.end() - 1)
    if len(args) != 2:
        return None
    opener = src.array_open(src.skip_space(args[0][0]))
    if opener is None or src.skip_space(src.pairs[opener] + 1) < args[0][1]:
        return None
    pair = [x.strip() for x in code[opener + 1 : src.pairs[opener]].split(",")]
    rest = code[args[1][0] : args[1][1]].strip()
    variadic = shape[2]
    want = f"${variadic}" if m.group(1) == "call_user_func_array" else f"...${variadic}"
    if len(pair) != 2 or re.sub(r"\s+", "", rest) != want:
        return None
    holder, method = pair[0].lstrip("$"), pair[1].lstrip("$")
    if not (pair[0].startswith("$") and pair[1].startswith("$")) or method not in shape[:2]:
        return None
    binds = [b for b in fn.file.bindings.by_name.get(holder, ()) if fn.lo < b.pos < fn.hi]
    if len(binds) != 1 or any(u.kind in (WRITE, OPAQUE) for u in _uses_in(fn, holder)):
        return None
    c = CONCAT.match(src.text, binds[0].value)
    if c is None or c.group(3) not in shape[:2] or c.group(3) == method:
        return None
    parts = [p for p in c.group(2).replace("\\\\", "\\").split("\\") if p]
    if parts[-1] != "Stripe" or (len(parts) > 1 and phpvendor.unprefix(parts + ["X"]) != ["Stripe", "X"]):
        return None
    for name in (c.group(3), method):
        if any(u.kind in (BIND, WRITE, OPAQUE) for u in _uses_in(fn, name)):
            return None
    changes: set[int] = set()
    adds: set[tuple[int, str]] = set()
    for u in _uses_in(fn, variadic):
        if u.kind in (BIND, OPAQUE):
            changes.add(ANY)
        elif u.kind == WRITE:
            w = ARG_WRITE.match(src.text, u.pos + 1 + len(variadic))
            if w is None:
                at = re.match(r"\s*\[\s*(\d+)\s*\]", src.text[u.pos + 1 + len(variadic) :])
                changes.add(int(at.group(1)) if at else ANY)  # ``$args[1][$k] = v``: that position only
                continue
            k, keys = int(w.group(1)), re.findall(r"['\"]([A-Za-z_]\w*)['\"]", w.group(2))
            if keys:  # ``$args[1]['expand'] = ...``: that key path, nothing else (as a caller's own write)
                adds.add((k, ".".join(keys)))
            elif not (EMPTY.match(src.text, w.end()) and src.enclosing(u.pos) != fn.lo):
                changes.add(k)  # only a conditional ``$args[1] = array();`` adds nothing
    return Dispatcher(shape.index(c.group(3)), shape.index(method), frozenset(changes), frozenset(adds))


def _forward(fn: _Fn, known: dict[tuple, Dispatcher]) -> Dispatcher | None:
    shape = _shape(fn)
    if shape is None:
        return None
    f = fn.file
    m = FORWARD.fullmatch(f.src.code, fn.lo + 1, fn.hi)
    if m is None or (m.group(4), m.group(5), m.group(6)) != shape:
        return None
    target = re.sub(r"\s+", "", m.group(3))
    if target.startswith("$"):
        var, _, name = target.partition("->")
        if var == "$this":
            key = _own_class(fn, name)
        elif m.group(1) and var == "$" + m.group(1):
            key = ("method", _qualify(f, m.group(2)), name.lower())
        else:
            return None
    elif "::" in target:
        key = _own_class(fn, target.partition("::")[2])
    else:
        key = ("func", target.lstrip("\\").rsplit("\\", 1)[-1].lower())
    return known.get(key) if key else None


def _own_class(fn: _Fn, name: str) -> tuple[str, ...] | None:
    return ("method", fn.key[1], name.lower()) if fn.key[0] == "method" else None


def _qualify(f: _File, name: str) -> str:
    parts = [p for p in name.split("\\") if p]
    if not name.startswith("\\"):
        if parts and parts[0] in f.uses:
            parts = f.uses[parts[0]] + parts[1:]
        elif f.namespace:
            parts = f.namespace.split("\\") + parts
    return "\\".join(parts).lower()
