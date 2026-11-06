"""Same-file helpers for the Python reader: ``create(**self._build(basket))``, ``params=build()``, or a variable
bound only to such a call.

The helper is a module-level function (bare name) or a method of the class around the call (``self.`` /
``cls.``), defined exactly once, undecorated (``staticmethod`` / ``classmethod`` aside), not a generator,
and not shadowed by a parameter, a variable or an import of the same name. Every ``return`` in it must
give a dict literal, ``dict(...)``, or a local bound only to one; the writes into that local before the
return are read key by key (``p['k'] = v``, ``p.update(k=v)``), and anything else written into it, or the
local being passed on, hides the top level as a spread would. Otherwise the argument stays hidden.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from . import pyparse
from .jshelpers import MAX_DEPTH, SUFFIX

REMOVERS = {"pop", "popitem", "clear"}
DICT_METHODS = {"get", "items", "keys", "values", "copy", "update", "setdefault", "pop", "popitem", "clear"}
DECORATORS = {"staticmethod", "classmethod"}


@dataclass
class PyRead:
    helper: ast.FunctionDef | ast.AsyncFunctionDef
    call: ast.Call  # the helper call
    local: ast.Name | None = None  # the variable passed at the SDK call, when the helper result was bound first
    binding: ast.stmt | None = None
    seen: list = field(default_factory=list)  # every helper whose dict was read


class PyHelpers:
    def __init__(self, scopes: pyparse._PyScopes) -> None:
        self.scopes = scopes
        self._names: tuple[dict[str, list[ast.AST]], set[str]] | None = None

    @property
    def names(self) -> tuple[dict[str, list[ast.AST]], set[str]]:
        """Module-level definitions by name, and every imported name (found on first use)."""
        if self._names is None:
            defs: dict[str, list[ast.AST]] = {}
            for node in self.scopes.tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    defs.setdefault(node.name, []).append(node)
            imported = {
                (a.asname or a.name).split(".")[0] for node in ast.walk(self.scopes.tree)
                if isinstance(node, (ast.Import, ast.ImportFrom)) for a in node.names
            }
            self._names = defs, imported
        return self._names

    # --- which helper ----------------------------------------------------------------------

    def callee(self, node: ast.expr, fn: ast.AST | None) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        if not isinstance(node, ast.Call):
            return None
        func, found = node.func, None
        if isinstance(func, ast.Name):
            name = func.id
            defs, imported = self.names
            if name not in defs or name in imported or self._shadowed(name, fn):
                return None
            found = defs[name]
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in ("self", "cls"):
            klass = self._class_of(fn)
            if klass is None:
                return None
            found = [n for n in klass.body if getattr(n, "name", None) == func.attr or _assigns(n, func.attr)]
        if not found or len(found) != 1 or not isinstance(found[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None
        d = found[0]
        if any(not (isinstance(x, ast.Name) and x.id in DECORATORS) for x in d.decorator_list):
            return None
        if self._inside(node, d):
            return None  # the helper calling itself
        return d

    def _shadowed(self, name: str, fn: ast.AST | None) -> bool:
        scope = fn
        while True:
            if name in self.scopes.bindings[scope] or (scope is not None and name in pyparse._param_names(scope)):
                return True
            if scope is None:
                return False
            scope = self.scopes.parent[scope]

    def _class_of(self, fn: ast.AST | None) -> ast.ClassDef | None:
        current = fn
        while current is not None:
            parent = self.scopes.parents.get(current)
            if isinstance(parent, ast.ClassDef):
                return parent
            current = self.scopes.parent[current]
        return None

    def _inside(self, node: ast.AST, d: ast.AST) -> bool:
        current = self.scopes.parents.get(node)
        while current is not None:
            if current is d:
                return True
            current = self.scopes.parents.get(current)
        return False

    def argument(self, value: ast.expr, fn: ast.AST | None, at: ast.AST) -> PyRead | None:
        """A helper call, or a variable whose only binding reaching ``at`` is one."""
        d = self.callee(value, fn)
        if d is not None:
            return PyRead(d, value)
        if not isinstance(value, ast.Name):
            return None
        bindings, scope, dominated = self.scopes.reaching(value.id, at, fn)
        if len(bindings) != 1 or not dominated or self.scopes.shadowed_by_parameter(value.id, fn, scope, dominated):
            return None
        if scope is not fn or self.scopes.rebound_in_loop(value.id, at, scope):
            return None
        _, bound, stmt = bindings[0]
        d = self.callee(bound, fn)
        return PyRead(d, bound, value, stmt) if d is not None else None

    # --- what it returns -----------------------------------------------------------------

    def returns(self, d: ast.AST) -> list[ast.Return] | None:
        out: list[ast.Return] = []
        stack = list(ast.iter_child_nodes(d))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            if isinstance(node, (ast.Yield, ast.YieldFrom)):
                return None
            if isinstance(node, ast.Return):
                if node.value is None:
                    return None
                out.append(node)
            stack.extend(ast.iter_child_nodes(node))
        return sorted(out, key=lambda r: (r.lineno, r.col_offset)) or None

    def returned(self, ret: ast.Return, d: ast.AST) -> tuple[ast.expr | None, str | None]:
        """(the dict literal returned, or None) and (the local returned, or None)."""
        if pyparse._as_dict(ret.value, pyparse._NOTHING) is not None:
            return ret.value, None
        if isinstance(ret.value, ast.Name):
            return None, ret.value.id
        return None, None

    # --- reading ---------------------------------------------------------------------------

    def read(self, value: ast.expr, fn: ast.AST | None, at: ast.AST) -> tuple[PyRead, set[str], set[str]] | None:
        got = self.argument(value, fn, at)
        if got is None:
            return None
        fields: set[str] = set()
        hidden: set[str] = set()
        if not self.helper_fields(got.helper, fields, hidden, [], got.seen):
            return None
        if got.local is not None:
            self._writes(got.local.id, fn, got.binding.lineno, at.lineno, fields, hidden)
            if self.scopes.escapes(got.local.id, fn, got.local):
                hidden.add("")
        return got, fields, hidden

    def helper_fields(self, d: ast.AST, fields: set[str], hidden: set[str], stack: list, seen: list) -> bool:
        if d in stack or len(stack) >= MAX_DEPTH:
            return False
        rets = self.returns(d)
        if rets is None:
            return False
        seen.append(d)
        for ret in rets:
            literal, local = self.returned(ret, d)
            lookup = self.scopes.lookup(ret.lineno, d)
            if literal is not None:
                pyparse._dict_fields(literal, "", fields, lookup, hidden)
            elif local is None or not self._local(local, ret, d, fields, hidden):
                return False
        return True

    def _local(self, name: str, ret: ast.Return, d: ast.AST, fields: set[str], hidden: set[str]) -> bool:
        bindings, scope, dominated = self.scopes.reaching(name, ret, d)
        if not bindings or scope is not d or self.scopes.shadowed_by_parameter(name, d, scope, dominated):
            return False
        if self.scopes.rebound_in_loop(name, ret, scope):
            return False
        for line, bound, _ in bindings:
            if pyparse._as_dict(bound, pyparse._NOTHING) is None:
                return False
            pyparse._dict_fields(bound, "", fields, self.scopes.lookup(line + 1, d), hidden)
        self._writes(name, d, min(b[0] for b in bindings), ret.lineno, fields, hidden)
        if self.scopes.escapes(name, d, ret.value):
            hidden.add("")  # passed on or aliased before it is returned: another function may add keys
        return True

    def _writes(self, name: str, scope, after: int, before: int, fields: set[str], hidden: set[str]) -> None:
        """Keys written into ``name`` on lines in ``(after, before)``: each visible key is added (with a dict
        literal written under it); a write that names no key may add any."""
        lookup = self.scopes.lookup(before, scope)
        for _, keys, node in self.scopes.mutation_nodes(name, scope, after, before):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in REMOVERS:
                continue
            if not keys:
                hidden.add("")
                continue
            for key in keys:
                fields.add(key)
            if isinstance(node, ast.Assign) and len(keys) == 1:
                pyparse._nested(node.value, keys[0], fields, lookup, hidden)
        for key in self.scopes.touched_keys(name, scope, after, before) - DICT_METHODS:
            hidden.add(key + ".")  # p['k'].update(...), p['k']['x'] = v: keys below it may be added

    def suffix(self, read: PyRead | None) -> str:
        return "" if read is None else SUFFIX.format(name=read.helper.name, line=read.helper.lineno)


class Via:
    """The hook :func:`pyparse._py_fields` calls for a ``**`` / ``params=`` value it cannot read."""

    def __init__(self, helpers: PyHelpers | None, call: pyparse.PyCall) -> None:
        self.helpers, self.c, self.first, self.reads = helpers, call, None, []

    def __call__(self, value: ast.expr, fields: set[str], hidden: set[str]) -> bool:
        got = self.helpers.read(value, self.c.fn, self.c.node) if self.helpers is not None else None
        if got is None:
            return False
        fields |= got[1]
        hidden |= got[2]
        self.first = self.first or got[0]
        self.reads.append(got[0])
        return True

    @property
    def suffix(self) -> str:
        return self.helpers.suffix(self.first) if self.helpers is not None else ""


def _assigns(node: ast.AST, name: str) -> bool:
    return isinstance(node, (ast.Assign, ast.AnnAssign)) and any(
        isinstance(t, ast.Name) and t.id == name for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
    )
