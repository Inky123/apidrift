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


@dataclass
class PyCall:
    """One resolved SDK call with the scope data the fixer needs to walk its arguments."""

    node: ast.Call
    fn: ast.AST | None
    call: str
    resolved: Resolved
    scopes: "_PyScopes"


def extract(text: str, resolve: Callable[[list[str]], Resolved | None]) -> tuple[list[PyFound], list[tuple[int, str]]]:
    """Stripe SDK calls in a Python source, plus (line, call) pairs that matched no endpoint."""
    resolved_calls, unresolved = calls(text, resolve)
    found: list[PyFound] = []
    for c in resolved_calls:
        fields, complete = _py_fields(c.node, c.scopes.lookup(c.node.lineno, c.fn))
        found.append(PyFound(c.node.lineno, c.call, c.resolved, fields, complete))
    return found, unresolved


def calls(text: str, resolve: Callable[[list[str]], Resolved | None]) -> tuple[list[PyCall], list[tuple[int, str]]]:
    """Resolved SDK calls (arguments not yet read), plus the (line, call) pairs that matched no endpoint."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return [], []
    aliases = _import_aliases(tree)
    scopes = _PyScopes(tree)
    found: list[PyCall] = []
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
        found.append(PyCall(node, fn, call, resolved, scopes))
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


Binding = tuple[int, ast.expr, ast.stmt]  # (line, value, the assignment statement)
MutationRec = tuple[int, list[str], ast.AST]  # (line, keys written, the statement or call that writes them)


class _PyScopes:
    """Variables per function (module level as fallback): what they were bound to and what was written into them."""

    def __init__(self, tree: ast.Module) -> None:
        self.parent: dict[ast.AST | None, ast.AST | None] = {None: None}  # function -> enclosing function
        self.parents: dict[ast.AST, ast.AST] = {}  # node -> parent node
        self.lists: dict[ast.AST, tuple[ast.AST, str]] = {}  # statement -> the (parent, field) list holding it
        self.bindings: dict[ast.AST | None, dict[str, list[Binding]]] = {None: {}}
        self.mutations: dict[ast.AST | None, dict[str, list[MutationRec]]] = {None: {}}
        self.touches: dict[ast.AST | None, dict[str, list[tuple[int, str]]]] = {None: {}}  # reads/deletes of a key
        self.calls: list[tuple[ast.Call, ast.AST | None]] = []
        self._visit(tree, None)

    def _visit(self, node: ast.AST, fn: ast.AST | None) -> None:
        for name, value in ast.iter_fields(node):
            for child in value if isinstance(value, list) else [value]:
                if not isinstance(child, ast.AST):
                    continue
                self.parents[child] = node
                if isinstance(child, ast.stmt):
                    self.lists[child] = (node, name)
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    self.parent[child] = fn
                    self.bindings[child], self.mutations[child], self.touches[child] = {}, {}, {}
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
                    self.bindings[fn].setdefault(target.id, []).append((node.lineno, node.value, node))
                elif isinstance(target, (ast.Subscript, ast.Attribute)) and isinstance(target.value, ast.Name):
                    key = target.attr if isinstance(target, ast.Attribute) else _constant_str(target.slice)
                    self._mutate(fn, target.value.id, node, [key] if key else [])
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            self._mutate(fn, node.target.id, node, _constant_keys(node.value))
        elif isinstance(node, ast.AugAssign):
            self._touch_target(fn, node.target, node.lineno)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                self._touch_target(fn, target, node.lineno)
        elif isinstance(node, (ast.Subscript, ast.Attribute)) and not isinstance(node.ctx, ast.Store):
            self._touch_target(fn, node, node.lineno)
        elif (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.attr in DICT_MUTATORS
        ):
            keys = [kw.arg for kw in node.keywords if kw.arg] if node.func.attr == "update" else []
            if node.func.attr in ("update", "setdefault", "__setitem__") and node.args:
                keys += _constant_keys(node.args[0]) + ([_constant_str(node.args[0])] if _constant_str(node.args[0]) else [])
            if node.func.attr == "pop" and node.args and _constant_str(node.args[0]):
                self._touch(fn, node.func.value.id, node.lineno, _constant_str(node.args[0]))
            self._mutate(fn, node.func.value.id, node, keys)
        elif (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
            and node.func.attr in ("get", "__getitem__", "__contains__") and node.args and _constant_str(node.args[0])
        ):
            self._touch(fn, node.func.value.id, node.lineno, _constant_str(node.args[0]))

    def _mutate(self, fn: ast.AST | None, name: str, node: ast.AST, keys: list[str]) -> None:
        self.mutations[fn].setdefault(name, []).append((node.lineno, keys, node))

    def _touch_target(self, fn: ast.AST | None, target: ast.expr, line: int) -> None:
        """``params['k']`` / ``params.k`` read, deleted or operated on in place (not a plain write)."""
        if isinstance(target, (ast.Subscript, ast.Attribute)) and isinstance(target.value, ast.Name):
            key = target.attr if isinstance(target, ast.Attribute) else _constant_str(target.slice)
            if key:
                self._touch(fn, target.value.id, line, key)

    def _touch(self, fn: ast.AST | None, name: str, line: int, key: str) -> None:
        self.touches[fn].setdefault(name, []).append((line, key))

    def lookup(self, line: int, fn: ast.AST | None) -> "_PyLookup":
        return _PyLookup(self, line, fn)

    def reaching(self, name: str, call: ast.Call, fn: ast.AST | None) -> tuple[list[Binding], ast.AST | None, bool]:
        """Bindings of ``name`` that can be in force at ``call`` (nearest first), the scope they live in, and
        whether the walk ended at an unconditional binding.

        Same rule as :meth:`jsroots.Bindings.reaching`: walk back from the call and stop after a binding
        whose statement list also holds the call (an unconditional rebinding), keeping every
        conditional one before it.
        """
        scope, candidates = fn, []
        while True:
            candidates = [c for c in self.bindings[scope].get(name, []) if c[0] < call.lineno]
            if scope is None and not candidates:
                candidates = list(self.bindings[None].get(name, []))
            if candidates or scope is None:
                break
            scope = self.parent[scope]
        enclosing = self._lists_enclosing(call)
        out: list[Binding] = []
        dominated = False
        for binding in sorted(candidates, key=lambda c: c[0], reverse=True):
            out.append(binding)
            if self.lists.get(binding[2]) in enclosing:
                dominated = True
                break
        return out, scope, dominated

    def shadowed_by_parameter(self, name: str, fn: ast.AST | None, scope: ast.AST | None, dominated: bool) -> bool:
        """A function enclosing the call takes ``name`` as a parameter and no unconditional binding inside it
        rebinds the name before the call."""
        current = fn
        while current is not None:
            if current is scope and dominated:
                return False
            if name in _param_names(current):
                return True
            current = self.parent[current]
        return False

    def rebound_in_loop(self, name: str, call: ast.Call, scope: ast.AST | None) -> bool:
        """``name`` is bound again after the call inside a loop that also holds the call."""
        call_ancestors = self._ancestors(call)
        for line, _, stmt in self.bindings[scope].get(name, ()):
            if line <= call.lineno:
                continue
            for node in self._ancestors(stmt):
                if isinstance(node, (ast.For, ast.AsyncFor, ast.While)) and node in call_ancestors:
                    return True
        return False

    def mutation_nodes(self, name: str, scope: ast.AST | None, after: int, before: int) -> list[MutationRec]:
        """Writes into ``name`` recorded in ``scope`` on lines in ``(after, before)``."""
        return [m for m in self.mutations[scope].get(name, ()) if after < m[0] < before]

    def touched_keys(self, name: str, scope: ast.AST | None, after: int, before: int) -> set[str]:
        """Keys read, deleted or operated on in place through ``name`` on lines in ``(after, before)``."""
        return {k for line, k in self.touches[scope].get(name, ()) if after < line < before}

    def _ancestors(self, node: ast.AST) -> list[ast.AST]:
        out: list[ast.AST] = []
        current = self.parents.get(node)
        while current is not None:
            out.append(current)
            current = self.parents.get(current)
        return out

    def _lists_enclosing(self, node: ast.AST) -> set[tuple[ast.AST, str]]:
        found: set[tuple[ast.AST, str]] = set()
        current: ast.AST | None = node
        while current is not None:
            if current in self.lists:
                found.add(self.lists[current])
            current = self.parents.get(current)
        return found


class _PyLookup:
    """``get(name)``: the dict literal ``name`` holds at a call site, with later writes folded in; None otherwise."""

    def __init__(self, scopes: _PyScopes, line: int, fn: ast.AST | None) -> None:
        self.scopes, self.line, self.fn = scopes, line, fn

    def get(self, name: str) -> ast.expr | None:
        scope = self.fn
        while True:
            found = self._in(scope, name)
            if found is not None:
                bound_line, value, _ = found
                if _as_dict(value, _NOTHING) is None:
                    return None  # bound to something that is not a literal (a call, a parameter)
                mutations = [m for m in self.scopes.mutations[scope].get(name, ()) if m[0] > bound_line]
                return _with_mutations(value, mutations) if mutations else value
            if scope is None:
                return None
            scope = self.scopes.parent[scope]

    def _in(self, scope: ast.AST | None, name: str) -> Binding | None:
        candidates = self.scopes.bindings[scope].get(name, [])
        before = [c for c in candidates if c[0] < self.line]
        if scope is None and not before:
            before = candidates  # module-level constants may be defined below the function that uses them
        return max(before, key=lambda c: c[0]) if before else None


class _NoBindings:
    def get(self, name: str) -> None:
        return None


_NOTHING = _NoBindings()


def _param_names(fn: ast.AST) -> set[str]:
    args = getattr(fn, "args", None)
    if args is None:
        return set()
    names = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
    for extra in (args.vararg, args.kwarg):
        if extra is not None:
            names.add(extra.arg)
    return names


def _constant_str(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _constant_keys(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Dict):
        return [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
        return [kw.arg for kw in node.keywords if kw.arg]
    return []


def _with_mutations(value: ast.expr, mutations: list[MutationRec]) -> ast.expr:
    """The literal plus the keys later written into it, plus an unknown key so it reads as incomplete.

    The result is synthetic (no positions): it is read for field names only, never edited.
    """
    keys = [k for _, ks, _ in mutations for k in ks]
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
