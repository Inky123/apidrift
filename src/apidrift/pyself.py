"""Calls and spreads through ``self`` / ``cls`` that the whole checkout (:class:`pyrepo.Repo`) proves.

* ``self.stripe_class.create(...)`` / ``cls.stripe_class.list(...)`` in a method of class ``C``: ``C`` (or its
  single in-file base) binds the attribute once to an SDK chain (:meth:`pyalias.Binders.class_attr`), no class
  anywhere that subclasses ``C`` binds it, and nothing outside tests writes ``.stripe_class``.
* ``self.api.Customer.create(...)`` where ``C.__init__`` does ``self.api = api`` unconditionally: ``C`` binds no
  ``api`` itself, no subclass defines ``__init__`` / ``__new__`` or binds ``api``, that is the only write of
  ``.api`` outside tests, and every construction of ``C`` or a subclass outside tests passes the stripe module
  as that argument (:meth:`pyrepo.Repo.injected_stripe`).
* ``create(..., **self.api_kwargs)`` where ``api_kwargs`` is a ``@property`` of ``C`` that no subclass rebinds and
  whose every return is a dict literal (or a local bound only to them, never written into) holding only
  request options (``api_key``, ``stripe_account``, ...): it sends no request field.

``self`` must be the first parameter of a plain method defined in ``C``'s body (``cls`` of a ``@classmethod``).
Without a :class:`pyrepo.Repo` (one file read alone) none of this is followed.
"""

from __future__ import annotations

import ast

from .pyalias import Binders


def _method(root: ast.Name, binders: Binders, parents) -> tuple[ast.FunctionDef, ast.ClassDef] | None:
    found = binders.binding(root)
    if found is None or found[0] != "param":
        return None
    arguments = parents.get(found[1])
    method = parents.get(arguments)
    klass = parents.get(method)
    if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or not isinstance(klass, ast.ClassDef):
        return None
    positional = [*method.args.posonlyargs, *method.args.args]
    if method not in klass.body or not positional or positional[0] is not found[1]:
        return None
    decorators = [getattr(d, "id", None) for d in method.decorator_list]
    if decorators != (["classmethod"] if root.id == "cls" else []):
        return None
    return method, klass


def expand(func: ast.expr, chain: list[str], binders: Binders, parents, imports: dict[str, str], repo, known) -> list[str] | None:
    """The SDK chain behind ``self.<attr>...`` / ``cls.<attr>...``; ``known(chain)`` says it names an endpoint.
    What one file shows is checked first, so the checkout is read only for a call that would resolve."""
    node = func
    while isinstance(node, (ast.Attribute, ast.Call)):
        node = node.value if isinstance(node, ast.Attribute) else node.func
    if repo is None or len(chain) < 3 or not isinstance(node, ast.Name) or node.id not in ("self", "cls"):
        return None
    found = _method(node, binders, parents)
    if found is None:
        return None
    _, klass = found
    attr = chain[1]
    value = binders.class_attr(klass, attr, 0)
    if value is not None:
        sdk = binders.sdk_chain(value, imports, 1)
        if sdk is None or not known(sdk + chain[2:]) or repo.stores(attr) != 0:
            return None
        family = repo.family(klass.name)
        return sdk + chain[2:] if family is not None and not repo.binds(family - {klass.name}, attr) else None
    if node.id != "self" or attr in binders.by_scope.get(klass, {}) or not known(["stripe"] + chain[2:]):
        return None
    slot = _injected(klass, attr, binders)
    if slot is None or repo.stores(attr) > 1:
        return None
    family = repo.family(klass.name)
    if family is None or repo.binds(family - {klass.name}, attr) or repo.binds(family - {klass.name}, "__init__"):
        return None
    if repo.binds(family, "__new__") or not repo.injected_stripe(family, *slot):
        return None
    return ["stripe"] + chain[2:]


def _injected(klass: ast.ClassDef, attr: str, binders: Binders) -> tuple[int, str] | None:
    """(position after ``self``, name) of the ``__init__`` parameter stored as ``self.<attr>``."""
    inits = binders.by_scope.get(klass, {}).get("__init__", [])
    if len(inits) != 1 or inits[0][0] != "def" or inits[0][1].decorator_list:
        return None
    init = inits[0][1]
    positional = [*init.args.posonlyargs, *init.args.args]
    if not positional:
        return None
    me = positional[0].arg
    for stmt in init.body:
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1):
            continue
        t = stmt.targets[0]
        if isinstance(t, ast.Attribute) and t.attr == attr and isinstance(t.value, ast.Name) and t.value.id == me:
            if not isinstance(stmt.value, ast.Name) or len(binders.by_scope.get(init, {}).get(stmt.value.id, [])) != 1:
                return None
            names = [a.arg for a in positional[1:]]
            if stmt.value.id in names:
                return names.index(stmt.value.id), stmt.value.id
            if stmt.value.id in [a.arg for a in init.args.kwonlyargs]:
                return 1 << 30, stmt.value.id
            return None
    return None


def options_only(value: ast.expr, binders: Binders, parents, repo, options: set[str]) -> bool:
    """``**self.<prop>`` where the property provably returns only request options."""
    if repo is None or not (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == "self"):
        return False
    found = _method(value.value, binders, parents)
    if found is None:
        return False
    _, klass = found
    attr = value.attr
    bound = binders.by_scope.get(klass, {}).get(attr, [])
    if len(bound) != 1 or bound[0][0] != "def" or attr in binders.attr_stores or binders.setattr_any:
        return False
    prop = bound[0][1]
    if [getattr(d, "id", None) for d in prop.decorator_list] != ["property"]:
        return False
    returns = list(_returns(prop))
    if not returns or any(r is None for r in returns):
        return False
    for r in returns:
        literals = [r.value] if not isinstance(r.value, ast.Name) else _locals(prop, r.value.id, binders)
        if not literals or not all(_keys(lit) is not None and _keys(lit) <= options for lit in literals):
            return False
    family = repo.family(klass.name)
    return family is not None and not repo.binds(family - {klass.name}, attr) and not repo.stores(attr)


def _returns(fn: ast.AST):
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            yield None
        if isinstance(node, ast.Return):
            yield node if node.value is not None else None
        stack.extend(ast.iter_child_nodes(node))


def _locals(fn: ast.AST, name: str, binders: Binders) -> list[ast.expr]:
    """The dict literals a local is bound to, when it is bound only to them and nothing else touches it."""
    bound = binders.by_scope.get(fn, {}).get(name, [])
    if not bound or any(kind != "assign" for kind, _ in bound):
        return []
    uses = [n for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Load)]
    if len(uses) != sum(isinstance(n, ast.Return) and isinstance(n.value, ast.Name) and n.value.id == name for n in ast.walk(fn)):
        return []  # read other than by ``return name``: it may be written into or passed on
    return [stmt.value for _, stmt in bound]


def _keys(node: ast.expr) -> set[str] | None:
    if isinstance(node, ast.Dict) and all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in node.keys):
        return {k.value for k in node.keys}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict" and not node.args:
        return {kw.arg for kw in node.keywords} if all(kw.arg for kw in node.keywords) else None
    return None
