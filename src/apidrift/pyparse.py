"""Pull Stripe calls and their request fields out of Python source with ``ast``.

Imports are resolved (``import stripe as s``, ``from stripe import Charge``),
keyword arguments and dict literals are flattened into dotted field paths, and
a variable passed as ``**params`` or ``params=`` is looked up in the enclosing
function (module level as fallback), with later writes into it folded in.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Callable

from .sdkmap import Resolved

OPTION_KWARGS = {
    "api_key", "stripe_account", "stripe_version", "idempotency_key", "api_version", "options", "timeout",
    "max_network_retries", "stripe_context",
}
DICT_MUTATORS = {"update", "setdefault", "__setitem__", "pop", "popitem", "clear"}


@dataclass
class PyFound:
    line: int
    call: str
    resolved: Resolved
    fields: set[str]
    complete: bool


def extract(text: str, resolve: Callable[[list[str]], Resolved | None]) -> tuple[list[PyFound], list[tuple[int, str]]]:
    """Stripe SDK calls in a Python source, plus (line, call) pairs that matched no endpoint."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return [], []
    aliases = _import_aliases(tree)
    scopes = _PyScopes(tree)
    found: list[PyFound] = []
    unresolved: list[tuple[int, str]] = []
    for node, fn in scopes.calls:
        chain = _chain(node.func)
        if not chain:
            continue
        if chain[0] in aliases:
            chain = aliases[chain[0]].split(".") + chain[1:]
        if not stripe_like(chain):
            continue
        call = ".".join(chain)
        resolved = resolve(chain)
        if resolved is None:
            unresolved.append((node.lineno, call))
            continue
        fields, complete = _py_fields(node, scopes.lookup(node.lineno, fn))
        found.append(PyFound(node.lineno, call, resolved, fields, complete))
    return found, unresolved


def stripe_like(chain: list[str]) -> bool:
    return any("stripe" in part.lower() for part in chain) or any(part in ("v1", "v2") for part in chain[1:-1])


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "stripe" or alias.name.startswith("stripe."):
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else "stripe"
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "stripe":
            for alias in node.names:
                if alias.name != "*":
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


class _PyScopes:
    """Variables per function (module level as fallback): what they were bound to and what was written into them."""

    def __init__(self, tree: ast.Module) -> None:
        self.parent: dict[ast.AST | None, ast.AST | None] = {None: None}
        self.bindings: dict[ast.AST | None, dict[str, list[tuple[int, ast.expr]]]] = {None: {}}
        self.mutations: dict[ast.AST | None, dict[str, list[tuple[int, list[str]]]]] = {None: {}}
        self.calls: list[tuple[ast.Call, ast.AST | None]] = []
        self._visit(tree, None)

    def _visit(self, node: ast.AST, fn: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                self.parent[child] = fn
                self.bindings[child], self.mutations[child] = {}, {}
                self._visit(child, child)
                continue
            if isinstance(child, ast.Call):
                self.calls.append((child, fn))
            self._record(child, fn)
            self._visit(child, fn)

    def _record(self, node: ast.AST, fn: ast.AST | None) -> None:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                if isinstance(target, ast.Name):
                    self.bindings[fn].setdefault(target.id, []).append((node.lineno, node.value))
                elif isinstance(target, (ast.Subscript, ast.Attribute)) and isinstance(target.value, ast.Name):
                    key = target.attr if isinstance(target, ast.Attribute) else _constant_str(target.slice)
                    self._mutate(fn, target.value.id, node.lineno, [key] if key else [])
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            self._mutate(fn, node.target.id, node.lineno, _constant_keys(node.value))
        elif (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.attr in DICT_MUTATORS
        ):
            keys = [kw.arg for kw in node.keywords if kw.arg] if node.func.attr == "update" else []
            if node.func.attr in ("update", "setdefault", "__setitem__") and node.args:
                keys += _constant_keys(node.args[0]) + ([_constant_str(node.args[0])] if _constant_str(node.args[0]) else [])
            self._mutate(fn, node.func.value.id, node.lineno, keys)

    def _mutate(self, fn: ast.AST | None, name: str, line: int, keys: list[str]) -> None:
        self.mutations[fn].setdefault(name, []).append((line, keys))

    def lookup(self, line: int, fn: ast.AST | None) -> "_PyLookup":
        return _PyLookup(self, line, fn)


class _PyLookup:
    """``get(name)``: the dict literal ``name`` holds at a call site, with later writes folded in; None otherwise."""

    def __init__(self, scopes: _PyScopes, line: int, fn: ast.AST | None) -> None:
        self.scopes, self.line, self.fn = scopes, line, fn

    def get(self, name: str) -> ast.expr | None:
        scope = self.fn
        while True:
            found = self._in(scope, name)
            if found is not None:
                bound_line, value = found
                if _as_dict(value, _NOTHING) is None:
                    return None  # bound to something that is not a literal (a call, a parameter)
                mutations = [m for m in self.scopes.mutations[scope].get(name, ()) if m[0] > bound_line]
                return _with_mutations(value, mutations) if mutations else value
            if scope is None:
                return None
            scope = self.scopes.parent[scope]

    def _in(self, scope: ast.AST | None, name: str) -> tuple[int, ast.expr] | None:
        candidates = self.scopes.bindings[scope].get(name, [])
        before = [c for c in candidates if c[0] < self.line]
        if scope is None and not before:
            before = candidates  # module-level constants may be defined below the function that uses them
        return max(before, key=lambda c: c[0]) if before else None


class _NoBindings:
    def get(self, name: str) -> None:
        return None


_NOTHING = _NoBindings()


def _constant_str(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _constant_keys(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Dict):
        return [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
        return [kw.arg for kw in node.keywords if kw.arg]
    return []


def _with_mutations(value: ast.expr, mutations: list[tuple[int, list[str]]]) -> ast.expr:
    """The literal plus the keys later written into it, plus an unknown key so it reads as incomplete."""
    keys = [k for _, ks in mutations for k in ks]
    if isinstance(value, ast.Dict):
        extra = [ast.Constant(k) for k in keys] + [None]
        return ast.Dict(keys=list(value.keys) + extra, values=list(value.values) + [ast.Constant(None)] * len(extra))
    extra_kw = [ast.keyword(arg=k, value=ast.Constant(None)) for k in keys] + [ast.keyword(arg=None, value=ast.Constant(None))]
    return ast.Call(func=value.func, args=[], keywords=list(value.keywords) + extra_kw)


def _chain(node: ast.expr) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return [node.id] + parts[::-1]
    if isinstance(node, ast.Call):
        inner = _chain(node.func)
        return inner + parts[::-1] if inner else None
    return None


def _as_dict(node: ast.expr | None, assigned):
    """Return a node whose keys we can read: a dict literal, ``dict(k=v)`` call, or a variable bound to one."""
    if isinstance(node, ast.Dict):
        return node
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict" and not node.args:
        return node
    if isinstance(node, ast.Name):
        return assigned.get(node.id)
    return None


def _py_fields(call: ast.Call, assigned) -> tuple[set[str], bool]:
    fields: set[str] = set()
    complete = True
    for kw in call.keywords:
        if kw.arg is None or kw.arg == "params":
            node = _as_dict(kw.value, assigned)
            complete &= _dict_fields(node, "", fields, assigned) if node is not None else False
            continue
        if kw.arg in OPTION_KWARGS:
            continue
        fields.add(kw.arg)
        complete &= _nested(kw.value, kw.arg, fields, assigned)
    for arg in call.args:
        if isinstance(arg, ast.Starred):
            complete = False
        elif isinstance(arg, ast.Dict):
            complete &= _dict_fields(arg, "", fields, assigned)
    return fields, complete


def _dict_fields(node: ast.expr, prefix: str, fields: set[str], assigned) -> bool:
    complete = True
    if isinstance(node, ast.Call):  # dict(amount=1, currency="usd")
        pairs = [(kw.arg, kw.value) for kw in node.keywords]
    else:
        pairs = [(k.value if isinstance(k, ast.Constant) else None, v) for k, v in zip(node.keys, node.values)]
    for key, value in pairs:
        if not isinstance(key, str):
            complete = False
            continue
        path = prefix + key
        fields.add(path)
        complete &= _nested(value, path, fields, assigned)
    return complete


def _nested(value: ast.expr, path: str, fields: set[str], assigned) -> bool:
    node = _as_dict(value, assigned)
    if node is not None:
        return _dict_fields(node, path + ".", fields, assigned)
    if isinstance(value, (ast.List, ast.Tuple)):
        complete = True
        for item in value.elts:
            inner = _as_dict(item, assigned)
            if inner is not None:
                complete &= _dict_fields(inner, path + "[].", fields, assigned)
            elif isinstance(item, ast.Starred):
                complete = False
        return complete
    return True
