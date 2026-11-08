"""The caller side of :mod:`pycross`: which calls in a module reach a followed wrapper, and what they send."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from . import pyparse, pysyntax


def bindings(tree: ast.AST) -> Counter[str]:
    """How many times each name is bound anywhere in the module (any scope): assignments and other
    targets, parameters, definitions, imports, ``except ... as``, ``global``/``nonlocal``, match captures."""
    out: Counter[str] = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            out[node.id] += 1
        elif isinstance(node, ast.arg):
            out[node.arg] += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound = a.asname or (a.name.split(".")[0] if isinstance(node, ast.Import) else a.name)
                out[bound] += 1
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out[node.name] += 1
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            out.update(node.names)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            out[node.name] += 1
        elif isinstance(node, ast.MatchMapping) and node.rest:
            out[node.rest] += 1
    return out


class Module:
    """One parsed caller module: its imports, its bindings, and the scope data the request is read with."""

    def __init__(self, text: str, rel: str) -> None:
        self.rel = rel
        self.tree = pysyntax.parse(text)
        if self.tree is None:
            return
        self.scopes = pyparse._PyScopes(self.tree)
        self.fn = {id(call): fn for call, fn in self.scopes.calls}
        self.counts = bindings(self.tree)
        self.imports: dict[str, tuple[str, int, str]] = {}  # local -> (module, level, imported name)
        self.star = False
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name == "*":
                        self.star = True
                    else:
                        self.imports[a.asname or a.name] = (node.module or "", node.level, a.name)


def resolves(base: Path, rel: str, module: str, level: int, target: str, files: set[str]) -> bool:
    """``from <level dots><module> import ...`` in ``rel`` reads the module file ``target``."""
    tail = module.replace(".", "/") if module else ""
    if level:
        parts = rel.split("/")[:-1]
        if level - 1 > len(parts):
            return False
        here = "/".join(parts[: len(parts) - (level - 1)])
        path = "/".join(p for p in (here, tail) if p)
        return target in (path + ".py", path + "/__init__.py")
    if not tail:
        return False
    found = []
    for f in files:
        for option in (tail + ".py", tail + "/__init__.py"):
            if f == option or f.endswith("/" + option):
                root = f[: len(f) - len(option)].rstrip("/")
                if not root or not (base / root / "__init__.py").exists():
                    found.append(f)  # under a top-level directory that is not itself a package
    return found == [target]


def uses(ctx: Module, w, table):
    """(line, call node or None, (fields, hidden) or None) for every reference of the wrapper's name in the
    module: None when the reference is not a visible caller."""
    from .pycross import LANG, SHAPES

    facts = table.facts.get(LANG)
    shape = table.facts.get(SHAPES, {}).get((w.file, w.line, w.name))
    if facts is None or shape is None:
        return
    aliases = _aliases(ctx, w, facts, table.base)
    parents = ctx.scopes.parents
    for node in ast.walk(ctx.tree):
        named = (isinstance(node, ast.Name) and not w.method and (node.id in aliases or node.id == w.name)) or (
            isinstance(node, ast.Attribute) and node.attr == w.name)
        if not named:
            continue
        call = parents.get(node)
        line = getattr(node, "lineno", 0)
        if not (isinstance(call, ast.Call) and call.func is node):
            yield line, None, None  # named, not called: a reference that may call it later, or a rebinding
            continue
        if not _reaches(ctx, w, node, aliases, facts):
            yield call.lineno, call, None
            continue
        yield call.lineno, call, request(ctx, call, shape)


def _aliases(ctx: Module, w, facts, base) -> set[str]:
    """Local names that are the wrapper function, or the wrapper's module-level instance, here."""
    if ctx.star:
        return set()
    target = w.name if not w.method else facts.instances.get((w.file, w.owner))
    if target is None:
        return set()
    out = set()
    if ctx.rel == w.file and ctx.counts.get(target, 0) == 1 and target not in ctx.imports:
        out.add(target)
    for local, (module, level, name) in ctx.imports.items():
        if name == target and ctx.counts.get(local, 0) == 1 and resolves(base, ctx.rel, module, level, w.file, facts.files):
            out.add(local)
    return out


def _reaches(ctx: Module, w, func: ast.expr, aliases: set[str], facts) -> bool:
    if isinstance(func, ast.Name):
        return not w.method and func.id in aliases
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return False
    receiver = func.value.id
    if receiver in aliases:
        return w.method
    if not w.method or ctx.rel != w.file or w.owner in facts.subclassed or w.file in facts.library:
        return False
    return _self_of_owner(ctx, func.value, w.owner)


def _self_of_owner(ctx: Module, name: ast.Name, owner: str) -> bool:
    """``name`` is the first parameter of an undecorated method written directly in the module-level class
    ``owner`` (nested functions inside it included), never rebound there."""
    parents, node = ctx.scopes.parents, name
    method = None
    while node is not None:
        node = parents.get(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            holder = parents.get(node)
            if isinstance(holder, ast.ClassDef):
                method = node
                break
        elif isinstance(node, ast.ClassDef):
            return False
    if method is None or method.decorator_list:
        return False
    holder = parents.get(method)
    if holder.name != owner or not isinstance(parents.get(holder), ast.Module):
        return False
    first = [*method.args.posonlyargs, *method.args.args]
    if not first or first[0].arg != name.id:
        return False
    return bindings(method).get(name.id, 0) == 1  # the parameter itself only


def request(ctx: Module, call: ast.Call, shape) -> tuple[set[str], set[str]] | None:
    """The request a visible caller sends through the wrapper (fields, hidden prefixes), or None."""
    if any(isinstance(a, ast.Starred) for a in call.args) or len(call.args) > len(shape.params):
        return None
    fn = ctx.fn.get(id(call))
    lookup = ctx.scopes.lookup(call.lineno, fn)
    fields, hidden = set(), set()
    if shape.kwargs:
        bound = set(shape.params[: len(call.args)])
        for kw in call.keywords:
            if kw.arg is None:
                if not _spread(ctx, call, fn, kw.value, lookup, fields, hidden):
                    return None
            elif kw.arg in bound or kw.arg in shape.params or kw.arg in pyparse.OPTION_KWARGS:
                continue
            else:
                fields.add(kw.arg)
                pyparse._nested(kw.value, kw.arg, fields, lookup, hidden)
    else:
        if any(kw.arg is None for kw in call.keywords):
            return None  # ``**more`` may carry the parameter
        k = shape.params.index(shape.forwarded)
        given = [kw.value for kw in call.keywords if kw.arg == shape.forwarded]
        value = call.args[k] if k < len(call.args) else (given[0] if given else None)
        if value is None or not _spread(ctx, call, fn, value, lookup, fields, hidden):
            return None
    return fields | shape.added, hidden


def _spread(ctx: Module, call: ast.Call, fn, value: ast.expr, lookup, fields: set[str], hidden: set[str]) -> bool:
    """Read a dict the caller hands over: a literal, or a local every reaching binding of which is one,
    written into only by literal keys before the call. False when it is not visible."""
    literal = pyparse._as_dict(value, pyparse._NOTHING)
    if literal is not None:
        pyparse._dict_fields(literal, "", fields, lookup, hidden)
        return True
    if not isinstance(value, ast.Name):
        return False
    scopes, name = ctx.scopes, value.id
    reaching, scope, dominated = scopes.reaching(name, call, fn)
    if not reaching or scopes.shadowed_by_parameter(name, fn, scope, dominated) or scopes.rebound_in_loop(name, call, scope):
        return False
    if any(pyparse._as_dict(bound, pyparse._NOTHING) is None for _, bound, _ in reaching):
        return False  # a call, a parameter, an expression: not visible
    for _, bound, _ in reaching:
        pyparse._dict_fields(bound, "", fields, lookup, hidden)
    for _, keys, _ in scopes.mutation_nodes(name, scope, min(b[0] for b in reaching), call.lineno):
        if not keys:
            return False  # ``d.update(other)``, ``d.pop(k)``: not visible
        fields.update(keys)
        hidden.add("")  # read as the direct reader reads a written ``**params`` local
    return True
