"""Go wrappers followed across files: typed params (:mod:`crosstable` holds the passes; this is the Go half).

In stripe-go the params type names the request (``*stripe.CheckoutSessionParams``), so a wrapper is a
top-level function or method with a parameter of that type that it hands, unchanged, to one SDK call as
its params (monetr's ``NewCheckoutSession(ctx, params *stripe.CheckoutSessionParams)``; sealos's
``UpdateSubscription(id string, params *stripe.SubscriptionParams)``). Inside it the parameter may only be
that argument or the target of ``params.F = v`` for a field that is not part of the request (``Context``);
anything else (read, passed on, a method such as ``AddExpand``, rebound, used in a closure) and it is not
a candidate.

It is followed when its name is defined once (``func NAME(`` / ``func (r T) NAME(``) in the Go files the
scan reads, tests left out: Go has no overriding, so with one definition in the checkout a call that
passes a params value of exactly that type to a method of that name reaches it (an interface such as
monetr's ``stripe_helper.Stripe`` included: its one implementation here is the wrapper; mocks live in
tests). A use is a *visible caller* when:

- a method: ``x.NAME(`` with any receiver;
- a function: ``NAME(`` in a file of the same package directory and package clause, not shadowed by a
  local of that name, or ``pkg.NAME(`` where ``pkg`` imports the wrapper's package path (from the
  nearest ``go.mod``);

and the argument at the parameter's position is ``&stripe.T{...}`` of the wrapper's ``T``, or a variable
whose binding in force is one, read exactly as :mod:`govars` reads a direct call's params (the endpoint
and field names are the wrapper's call's). The wrapper's own site stays (:mod:`crosstable`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import crosstable, goparse, scanner
from .crosstable import Table, Wrapper
from .golex import ID, TYPE
from .govars import Params, Reader, chain_after

LANG = "go"
SUFFIXES = {".go"}
SHAPES = "go-shapes"
PACKAGE = re.compile(r"^package[ \t]+([A-Za-z_]\w*)", re.M)
IMPORT = re.compile(r"(?:^|[\s(])(?:([A-Za-z_]\w*|\.)[ \t]+)?\"([^\"\n]+)\"")


@dataclass(frozen=True)
class Shape:
    struct: str  # the params type: ``CheckoutSessionParams``
    package: str  # the wrapper file's package clause
    dir: str  # its directory (repo-relative)


# --- pass 1: candidates ------------------------------------------------------------------


def collect(calls: list[goparse.GoCall], f: goparse.GoFile, table: Table, rel: str, text: str) -> None:
    src = f.src
    pkg = PACKAGE.search(text)
    for c in calls:
        args = src.split(c.pos + 1, src.match[c.pos])
        if not args:
            continue
        a, b = src.trim(*args[-1])
        if b - a != 1 or src.kind[a] != ID:
            continue
        body = src.func_of(c.pos)
        func = src.funcs.get(body)
        if func is None or src.parent[func] != -1 or pkg is None:
            continue  # a function literal, or no package clause
        sig = _signature(f, func, body)
        if sig is None:
            continue
        name, receiver, params = sig
        k = next((i for i, (p, _) in enumerate(params) if p == src.tok[a]), None)
        if k is None or not _typed(f, params[k][1]) or not _unchanged(f, src.tok[a], a, body, c):
            continue
        struct = params[k][1][-1]
        w = Wrapper(LANG, name, rel, c.line, c.call, c.resolved, k, receiver is not None, receiver or "", rel.rpartition("/")[0])
        table.add(w)
        table.facts.setdefault(SHAPES, {})[(rel, c.line, name)] = Shape(struct, pkg.group(1), rel.rpartition("/")[0])


def _signature(f: goparse.GoFile, func: int, body: int):
    """(name, receiver type or None, [(parameter name, type tokens)]) of the declaration at ``func``."""
    src, j, receiver = f.src, func + 1, None
    if src.tok[j] == "(":
        ids = [src.tok[t] for t in range(j + 1, src.match[j]) if src.kind[t] == ID]
        receiver, j = (ids[-1] if ids else ""), src.match[j] + 1
    if src.kind[j] != ID or j + 1 >= body:
        return None
    name, j = src.tok[j], j + 1
    if src.tok[j] == "[":
        j = src.match[j] + 1  # type parameters
    if src.tok[j] != "(" or src.match[j] < 0:
        return None
    pieces = [[src.tok[t] for t in range(*src.trim(x, y))] for x, y in src.split(j + 1, src.match[j])]
    out: list[tuple[str, list[str]]] = []
    for i, piece in enumerate(pieces):
        if len(piece) >= 2:
            out.append((piece[0], piece[1:]))
        else:  # ``a, b T``: the type comes with the next named piece
            later = next((p for p in pieces[i + 1 :] if len(p) >= 2), None)
            out.append((piece[0], later[1:] if later else []))
    return name, receiver, out


def _typed(f: goparse.GoFile, type_: list[str]) -> bool:
    return len(type_) == 4 and type_[0] == "*" and type_[1] in f.roots and type_[2] == "." and type_[3].endswith("Params")


def _unchanged(f: goparse.GoFile, name: str, arg: int, body: int, c: goparse.GoCall) -> bool:
    """The parameter is only the SDK call's argument, or set at a field outside the request."""
    src = f.src
    reader = Reader(src, f.go, (c.resolved.method, c.resolved.path), f.roots)
    for t in src.ids(name):
        if not body < t < src.match[body] or t == arg or src.tok[t - 1] == ".":
            continue
        sel, k = chain_after(src, t)
        if (len(sel) == 1 and src.starts_stmt(t) and src.tok[k] == "=" and src.func_of(t) == body
                and reader.name("", src.tok[sel[0]])[0] is None):
            continue  # ``params.Context = ctx``
        return False
    return True


# --- pass 2 ---------------------------------------------------------------------------------


@dataclass
class Facts:
    imports: dict[str, str]  # wrapper directory -> its import path ("" when there is no go.mod)


def finish(table: Table, base: Path, sources: list[Path], resolve) -> None:
    names = crosstable.single(table, LANG)
    index = getattr(table, "index", None)
    if not names or index is None or getattr(index, "go", None) is None:
        return
    pattern = "|".join(re.escape(w.name) for w in names.values())
    definition = re.compile(rf"^func[ \t]*(?:\([^()\n]*\)[ \t]*)?({pattern})[ \t]*[(\[]", re.M)
    mention = re.compile(rf"\b(?:{pattern})\b")
    defs: dict[str, int] = {}
    source = {str(p) for p in sources}
    mentioned: list[Path] = []
    test = lambda rel: scanner._doc_like(rel, ".go")  # noqa: E731
    for item in crosstable.walk(base, SUFFIXES, skip=test):
        if item is None:
            for w in names.values():
                table.note(w, "cap: too many Go files")
            return
        path, text = item
        for m in definition.finditer(text):
            defs[m.group(1)] = defs.get(m.group(1), 0) + 1
        if str(path) in source and mention.search(text):
            mentioned.append(path)
    facts = Facts({})
    for key, w in names.items():
        if defs.get(w.name, 0) != 1:
            table.note(w, f"{defs.get(w.name, 0)} definitions of the name")
            continue
        table.followed[key] = w
        facts.imports[w.scope] = _import_path(base, w.scope)
    table.facts[LANG] = facts
    followed = table.wrappers(LANG)
    if len(mentioned) > crosstable.MAX_PARSE_FILES:
        for w in followed:
            table.note(w, "cap: too many files name it")
            table.followed.pop(w.key)
        return
    for path in mentioned:
        rel, text = path.relative_to(base).as_posix(), scanner._read(path)
        f = goparse.calls(text, index.go)[2] if text is not None else None
        for w in followed:
            for line, _, visible in (uses(f, text, w, rel, table) if f is not None else [(0, -1, None)]):
                crosstable.record(table, w, rel, line, visible is not None)
    crosstable.settle(table, LANG)


def _import_path(base: Path, rel_dir: str) -> str:
    here = base / rel_dir
    while True:
        mod = here / "go.mod"
        if mod.is_file():
            m = re.search(r"^module[ \t]+(\S+)", mod.read_text(errors="replace"), re.M)
            if m is None:
                return ""
            tail = (base / rel_dir).relative_to(here).as_posix()
            return m.group(1) if tail == "." else f"{m.group(1)}/{tail}"
        if here == base or base not in here.parents:
            return ""
        here = here.parent


# --- uses -------------------------------------------------------------------------------------


def uses(f: goparse.GoFile, text: str, w: Wrapper, rel: str, table: Table):
    """(line, ``(`` token, (fields, hidden) or None) for every mention of the wrapper's name in the file."""
    src = f.src
    shape = table.facts.get(SHAPES, {}).get((w.file, w.line, w.name))
    facts = table.facts.get(LANG)
    if shape is None or facts is None:
        return
    here_pkg = PACKAGE.search(text)
    aliases = _aliases(text, facts.imports.get(w.scope, ""))
    for t in src.ids(w.name):
        line = src.line(t)
        if src.tok[t - 1] == "func" or (src.tok[t - 1] == ")" and src.tok[src.match[t - 1] - 1] == "func"):
            continue  # its definition
        if src.brace.get(src.parent[t]) == TYPE:
            continue  # an interface's method list
        if t + 1 >= src.n or src.tok[t + 1] != "(" or src.match[t + 1] < 0:
            yield line, -1, None  # a method value or another reference
            continue
        paren = t + 1
        dotted = src.tok[t - 1] == "."
        if w.method:
            reach = dotted
        elif dotted:
            receiver = src.tok[t - 2] if src.kind[t - 2] == ID and src.tok[t - 3] != "." else ""
            reach = receiver in aliases and not f.vars.shadowed(receiver, t - 2)
        else:
            reach = (rel.rpartition("/")[0] == shape.dir and here_pkg is not None and here_pkg.group(1) == shape.package
                     and not f.vars.shadowed(w.name, t))
        yield line, paren, (argument(f, paren, w, shape) if reach else None)


def _aliases(text: str, path: str) -> set[str]:
    """Local names under which the file imports the package at ``path``."""
    if not path:
        return set()
    out = set()
    for m in IMPORT.finditer(text):
        if m.group(2) == path and m.group(1) not in (".", "_"):
            out.add(m.group(1) or path.rsplit("/", 1)[-1])
    return out


def argument(f: goparse.GoFile, paren: int, w: Wrapper, shape: Shape) -> tuple[set[str], set[str]] | None:
    """What the caller's argument sends, read as a direct call's params: only when it is ``&stripe.T{...}``
    of the wrapper's type, or a variable whose binding in force is one."""
    src = f.src
    args = src.split(paren + 1, src.match[paren])
    if w.index >= len(args) or any(src.tok[src.trim(*x)[1] - 1] == "..." for x in args):
        return None
    params = read(f, paren, w)
    a, b = src.trim(*args[w.index])
    j = a + 1 if src.tok[a] == "&" else a
    found = Reader(src, f.go, (w.resolved.method, w.resolved.path), f.roots).typed(j, b)
    struct = found[0] if found and src.tok[a] == "&" else (params.struct if b - a == 1 and src.kind[a] == ID else "")
    if struct != shape.struct:
        return None
    return set(params.fields), set(params.hidden)


def read(f: goparse.GoFile, paren: int, w: Wrapper) -> Params:
    return f.read(paren, w.resolved, w.index)


# --- pass 3 ---------------------------------------------------------------------------------


def extend(text: str, table: Table | None, rel: str, found: list, calls, f) -> list:
    """``found`` with each visible caller of a followed wrapper added (the wrapper's own site stays)."""
    if table is None or f is None:
        return found
    if table.collecting:
        collect(calls, f, table, rel, text)
        return found
    wrappers = [w for w in table.wrappers(LANG) if w.name in text]
    if not wrappers or scanner._doc_like(rel, ".go"):
        return found
    out = list(found)
    for w in wrappers:
        for line, _, visible in uses(f, text, w, rel, table):
            if visible is not None:
                fields, hidden = visible
                out.append(goparse.GoFound(line, w.call + w.through(), w.resolved, fields, not hidden, frozenset(hidden)))
    return out


def sites(f: goparse.GoFile, text: str, table: Table, rel: str):
    """(wrapper, line, ``(``, Params) of every visible caller in the file, for the fixer."""
    for w in table.wrappers(LANG):
        if w.name in text:
            for line, paren, visible in uses(f, text, w, rel, table):
                if visible is not None:
                    yield w, line, paren, read(f, paren, w)
