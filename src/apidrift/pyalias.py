"""Which SDK object a Python call goes through when the chain does not spell it out.

* stripe-python's async twins (``create_async``, ``list_payment_methods_async``, ``cancel_async``; >= 10.0) send
  the same request as the sync method, so they resolve as the name without ``_async``. The class-level pagers
  ``Customer.auto_paging_iter(**params)`` and ``Customer.search_auto_paging_iter(query=...)`` send ``list`` and
  ``search`` with the same parameters.
* A name bound exactly once, in the scope Python looks it up in, to an attribute chain into the SDK
  (``v1 = self._client.v1``, ``PI = stripe.PaymentIntent``) stands for that chain. Any other way of binding
  the name in that scope (a second assignment, a loop target, ``with ... as``, an import, ``del``) or a
  ``global`` / ``nonlocal`` statement naming it anywhere in the file leaves it alone.
* ``Model.stripe_class.create(...)``: a class defined in the file, bound once and undecorated, with no
  ``metaclass=``, whose body binds the attribute exactly once to such a chain (or, when the body does not bind
  it, whose single base is such a class). Nothing in the file may write that attribute (``x.attr = ...``,
  ``setattr``). ``self.stripe_class`` / ``cls.stripe_class`` need the whole repository (a subclass may rebind it)
  and are left to :mod:`pyrepo`.
"""

from __future__ import annotations

import ast

ASYNC = "_async"
PAGERS = {"auto_paging_iter": "list", "search_auto_paging_iter": "search"}
MAX_DEPTH = 4


def resolve(chain: list[str], call: ast.Call, resolve_chain):
    """``resolve_chain(chain)``, else the sync twin of an ``_async`` method, else a class-level pager's request;
    then the two account endpoints stripe-python tells apart by the id (:func:`_account`)."""
    name = chain[-1]
    if name.endswith(ASYNC) and len(name) > len(ASYNC) and not name.startswith("_") and resolve_chain(chain) is None:
        chain = chain[:-1] + [name[: -len(ASYNC)]]
    resolved = resolve_chain(chain)
    if resolved is None and chain[-1] in PAGERS and len(chain) >= 3 and _plain(call.func):
        resolved = resolve_chain(chain[:-1] + [PAGERS[chain[-1]]])  # stripe.Customer.auto_paging_iter: a class
    return _account(chain, call, resolved, resolve_chain)


def _account(chain: list[str], call: ast.Call, resolved, resolve_chain):
    """``stripe.Account.retrieve(id)`` / ``modify(id, ...)`` reach ``/v1/accounts/{account}`` (with no id,
    ``/v1/account``); StripeClient's ``accounts.retrieve_current()`` is ``GET /v1/account``."""
    if len(chain) >= 2 and chain[-2:] == ["accounts", "retrieve_current"] and resolved is None:
        current = resolve_chain(chain[:-2] + ["Account", "retrieve"])
        return current if current is not None and current.path == "/v1/account" else None
    if resolved is None or resolved.path != "/v1/account" or len(chain) < 2 or chain[-2] != "Account":
        return resolved
    first = call.args[0] if call.args else next((kw.value for kw in call.keywords if kw.arg == "id"), None)
    if first is None or (isinstance(first, ast.Constant) and first.value is None):
        return resolved
    by_id = resolve_chain(chain[:-2] + ["accounts", chain[-1] if chain[-1] != "modify" else "update"])
    if by_id is not None and by_id.path == "/v1/accounts/{account}" and by_id.method == resolved.method:
        return by_id
    return None  # an id, but no such endpoint by id: not this call


def _plain(func: ast.expr) -> bool:
    """An attribute chain on a name, with no call in it (``stripe.Customer.auto_paging_iter``)."""
    while isinstance(func, ast.Attribute):
        func = func.value
    return isinstance(func, ast.Name)


class Binders:
    """Every binding of every name, per scope (module, class, function, lambda), and the scopes each name use
    is looked up in."""

    def __init__(self, tree: ast.Module) -> None:
        self.by_scope: dict[ast.AST, dict[str, list[tuple[str, ast.AST]]]] = {}
        self.chains: dict[int, list[ast.AST]] = {}  # id(Name load) -> scopes searched, innermost first
        self.declared: set[str] = set()  # named by a global / nonlocal statement
        self.attr_stores: set[str] = set()  # attributes written or deleted anywhere (x.attr = v, setattr(x, "attr", v))
        self.setattr_any = False  # setattr / delattr with a computed name
        self._visit(tree, [tree])

    # --- walking ---------------------------------------------------------------------------

    def _bind(self, scope: ast.AST, name: str, kind: str, node: ast.AST) -> None:
        self.by_scope.setdefault(scope, {}).setdefault(name, []).append((kind, node))

    def _visit(self, node: ast.AST, chain: list[ast.AST]) -> None:
        scope = chain[0]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda):
                self._bind(scope, node.name, "def", node)
                for d in node.decorator_list:
                    self._visit(d, chain)
                if node.returns is not None:
                    self._visit(node.returns, chain)
            for default in [*node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]:
                self._visit(default, chain)
            inner = [node] + [s for s in chain if not isinstance(s, ast.ClassDef)]
            args = node.args
            for a in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if a is not None:
                    self._bind(node, a.arg, "param", a)
            for child in node.body if isinstance(node.body, list) else [node.body]:
                self._visit(child, inner)
            return
        if isinstance(node, ast.ClassDef):
            self._bind(scope, node.name, "class", node)
            for child in [*node.decorator_list, *node.bases, *node.keywords]:
                self._visit(child, chain)
            inner = [node] + [s for s in chain if not isinstance(s, ast.ClassDef)]
            for child in node.body:
                self._visit(child, inner)
            return
        if isinstance(node, ast.AnnAssign) and node.value is None and isinstance(node.target, ast.Name):
            self._visit(node.annotation, chain)  # ``params: dict``: declares, binds no value
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and self._simple_assign(node):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            self._bind(scope, target.id, "assign", node)
            self._visit(node.value, chain)
            if isinstance(node, ast.AnnAssign):
                self._visit(node.annotation, chain)
            return
        self._note(node, scope, chain)
        for child in ast.iter_child_nodes(node):
            self._visit(child, chain)

    @staticmethod
    def _simple_assign(node: ast.Assign | ast.AnnAssign) -> bool:
        if isinstance(node, ast.Assign):
            return len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
        return node.value is not None and isinstance(node.target, ast.Name)

    def _note(self, node: ast.AST, scope: ast.AST, chain: list[ast.AST]) -> None:
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                self.chains[id(node)] = chain
            else:
                self._bind(scope, node.id, "other", node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                self._bind(scope, (a.asname or a.name).split(".")[0], "import", node)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            self._bind(scope, node.name, "other", node)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            self.declared |= set(node.names)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            self._bind(scope, node.name, "other", node)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            self._bind(scope, node.rest, "other", node)
        elif isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            self.attr_stores.add(node.attr)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("setattr", "delattr"):
            name = node.args[1] if len(node.args) > 1 else None
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                self.attr_stores.add(name.value)
            else:
                self.setattr_any = True

    # --- reading ---------------------------------------------------------------------------

    def unmodelled(self, name: str, scopes: list[ast.AST]) -> bool:
        """``name`` is bound in one of ``scopes`` other than by plain assignment or as a parameter: a ``for`` /
        comprehension / ``with`` / ``except`` target, tuple unpacking, a walrus, an import, ``del``, a nested
        ``def`` / ``class``, or it is named by ``global`` / ``nonlocal``. The reader follows none of those."""
        if name in self.declared:
            return True
        return any(
            kind not in ("assign", "param") for scope in scopes for kind, _ in self.by_scope.get(scope, {}).get(name, ())
        )

    def binding(self, name: ast.Name) -> tuple[str, ast.AST] | None:
        """The one binding a loaded name can have, or None (several, none, or declared global/nonlocal)."""
        chain = self.chains.get(id(name))
        if chain is None or name.id in self.declared:
            return None
        for scope in chain:
            found = self.by_scope.get(scope, {}).get(name.id)
            if found:
                return found[0] if len(found) == 1 else None
        return None

    def sdk_chain(self, node: ast.expr, imports: dict[str, str], depth: int = 0) -> list[str] | None:
        """The SDK chain an expression names (``stripe.PaymentIntent``, ``self._client.v1``), else None."""
        if depth > MAX_DEPTH:
            return None
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        parts.reverse()
        root = self.root(node, parts, imports, depth)
        if root is None:
            return None
        chain = root + parts
        return chain if sdk_like(chain) else None

    def root(self, name: ast.Name, rest: list[str], imports: dict[str, str], depth: int) -> list[str] | None:
        """What a chain's first name stands for: an import of stripe, an alias, or a class attribute
        (``Model.stripe_class``, which consumes the first of ``rest``)."""
        found = self.binding(name)
        if found is None:
            return None
        kind, node = found
        if kind == "param":
            return [name.id] if name.id in ("self", "cls") else None  # self._client.v1: a StripeClient's namespace
        if kind == "import":
            return imports[name.id].split(".") if name.id in imports else None
        if kind == "assign":
            return self.sdk_chain(node.value, imports, depth + 1)
        if kind == "class" and rest:
            value = self.class_attr(node, rest[0], depth)
            if value is None:
                return None
            chain = self.sdk_chain(value, imports, depth + 1)
            if chain is None:
                return None
            rest.pop(0)
            return chain
        return None

    def class_attr(self, klass: ast.ClassDef, attr: str, depth: int) -> ast.expr | None:
        """The one value ``klass.attr`` can have when the file alone proves it."""
        if depth > MAX_DEPTH or self.setattr_any or attr in self.attr_stores or klass.decorator_list or klass.keywords:
            return None
        found = self.by_scope.get(klass, {}).get(attr)
        if found:
            if len(found) != 1 or found[0][0] != "assign":
                return None
            return found[0][1].value
        if len(klass.bases) != 1 or not isinstance(klass.bases[0], ast.Name):
            return None
        base = self.binding(klass.bases[0])
        if base is None or base[0] != "class":
            return None
        return self.class_attr(base[1], attr, depth + 1)


def expand(binders: Binders, func: ast.expr, chain: list[str], imports: dict[str, str]) -> list[str] | None:
    """The SDK chain behind a call whose first name is an alias or a class with an SDK attribute, else None."""
    node = func
    while isinstance(node, (ast.Attribute, ast.Call)):
        node = node.value if isinstance(node, ast.Attribute) else node.func
    if not isinstance(node, ast.Name) or node.id in imports or node.id in ("self", "cls"):
        return None
    found = binders.binding(node)
    if found is None or found[0] not in ("assign", "class"):
        return None
    rest = chain[1:]
    root = binders.root(node, rest, imports, 0)
    if root is None or not sdk_like(root):
        return None
    return root + rest


def sdk_like(chain: list[str]) -> bool:
    """The stripe module (or something under it), or a StripeClient namespace (``self._client.v1``)."""
    return chain[0] == "stripe" or any(part in ("v1", "v2") for part in chain[1:])
