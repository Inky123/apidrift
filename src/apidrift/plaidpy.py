"""Find plaid-python calls and read the request each one sends, with ``ast``.

A client is known by how it is made or typed: ``plaid_api.PlaidApi(api_client)`` (the pre-2021
``plaid.Client(...)`` too), a ``PlaidApi`` annotation, a function that returns one, or a name bound to
such a function's result; ``self.client = PlaidApi(...)`` makes ``self.client`` one for the file. A client
that comes from another file is not resolved across files: its calls are read when the receiver is named
after Plaid (``plaid_client.accounts_get``) or was imported from a module so named, and only when the
method is one of the SDK's generated names (``transactions_sync``).

The request is a generated model: ``TransactionsSyncRequest(access_token=t, options=...Options(...))``.
Its keyword arguments are the request body's field names and nested models nest the same way; a dict is
read alike. A request bound to a variable is looked up in the enclosing function, with later writes
(``request.cursor = c``, ``request['cursor'] = c``) folded in. ``**kwargs``, a parameter or a value built
elsewhere leave the fields under that object unknown. Built on the scopes of :mod:`pyparse`.
"""

from __future__ import annotations

import ast

from . import pyparse
from .plaidmap import PlaidIndex
from .pyparse import PyFound

CLIENT_CLASSES = ("PlaidApi", "Client")  # Client: the pre-2021 SDK


class _Imports:
    def __init__(self, tree: ast.AST) -> None:
        self.sdk: set[str] = set()  # names bound to the plaid package, one of its modules or one of its classes
        self.likely: set[str] = set()  # names imported from a module of the project that is named after Plaid
        self.star = False  # ``from plaid.models import *``
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "plaid":
                        self.sdk.add(alias.asname or "plaid")
                    elif "plaid" in alias.name.lower():
                        self.likely.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {alias.asname or alias.name for alias in node.names if alias.name != "*"}
                if node.level == 0 and node.module.split(".")[0] == "plaid":
                    self.sdk |= names
                    self.star = self.star or any(alias.name == "*" for alias in node.names)
                elif "plaid" in node.module.lower():
                    self.likely |= names


class _Clients:
    """Which names and ``self`` attributes hold a Plaid client, per function scope."""

    def __init__(self, tree: ast.Module, scopes: pyparse._PyScopes, imports: _Imports) -> None:
        self.scopes, self.imports = scopes, imports
        self.attrs: set[str] = set()
        self.factories: set[str] = set()
        self.shared = {name for node in ast.walk(tree) if isinstance(node, ast.Global) for name in node.names}
        for _ in range(3):
            before = (len(self.attrs), len(self.factories))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if any(isinstance(n, ast.Return) and self._client(n.value, node) for n in ast.walk(node)):
                        self.factories.add(node.name)
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    typed = isinstance(node, ast.AnnAssign) and _mentions_client(node.annotation)
                    for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                        if _self_attr(target) and (typed or self._client(node.value, None)):
                            self.attrs.add(target.attr)
            if before == (len(self.attrs), len(self.factories)):
                break

    def _made(self, node: ast.expr | None) -> bool:
        """``plaid_api.PlaidApi(...)``, ``PlaidApi(...)``, ``plaid.Client(...)`` or a call of a factory."""
        if not isinstance(node, ast.Call):
            return False
        chain = pyparse._chain(node.func)
        if not chain:
            return False
        if chain[-1] == "PlaidApi" or (chain[-1] == "Client" and chain[0] in self.imports.sdk):
            return True
        return chain[-1] in self.factories and len(chain) <= 2

    def _client(self, node: ast.expr | None, fn: ast.AST | None) -> bool:
        if isinstance(node, ast.Await):
            node = node.value
        if self._made(node):
            return True
        if _self_attr(node):
            return node.attr in self.attrs
        return isinstance(node, ast.Name) and self.name(node.id, fn)

    def name(self, name: str, fn: ast.AST | None) -> bool:
        """``name`` holds a client where ``fn`` can see it: bound to one in the nearest scope that binds it,
        or a parameter annotated as one."""
        if name in self.shared:  # ``global client`` in the function that builds it
            return any(self._made(value) for scope in self.scopes.bindings.values() for _, value, _ in scope.get(name, ()))
        scope = fn
        while True:
            if scope is not None and name in pyparse._param_names(scope):
                args = getattr(scope, "args", None)
                every = [*args.posonlyargs, *args.args, *args.kwonlyargs] if args else []
                return any(a.arg == name and _mentions_client(a.annotation) for a in every)
            bound = self.scopes.bindings[scope].get(name)
            if bound:
                return any(
                    self._made(value) or (isinstance(stmt, ast.AnnAssign) and _mentions_client(stmt.annotation))
                    for _, value, stmt in bound
                )
            if scope is None:
                return False
            scope = self.scopes.parent[scope]

    def holds(self, chain: list[str], fn: ast.AST | None) -> int:
        """How many leading parts of ``chain`` name a known client (0 when none do)."""
        if chain[0] in ("self", "cls") and len(chain) > 2 and chain[1] in self.attrs:
            return 2
        if chain[0] in self.factories or self.name(chain[0], fn):
            return 1
        return 0

    def may_hold(self, chain: list[str]) -> bool:
        """The receiver is named after Plaid or came from a module that is (the SDK's own modules aside)."""
        if chain[0] in self.imports.sdk:
            return False
        return chain[0] in self.imports.likely or any("plaid" in part.lower() for part in chain[:-1])


def _self_attr(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in ("self", "cls")


def _mentions_client(annotation: ast.expr | None) -> bool:
    if annotation is None:
        return False
    return any(
        (isinstance(n, ast.Name) and n.id == "PlaidApi") or (isinstance(n, ast.Attribute) and n.attr == "PlaidApi")
        or (isinstance(n, ast.Constant) and isinstance(n.value, str) and "PlaidApi" in n.value)
        for n in ast.walk(annotation)
    )


def extract(text: str, index: PlaidIndex) -> tuple[list[PyFound], list[tuple[int, str]]]:
    """plaid-python calls in a source, plus (line, call) pairs for other methods called on a known client."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return [], []
    imports = _Imports(tree)
    scopes = pyparse._PyScopes(tree)
    clients = _Clients(tree, scopes, imports)
    found: list[PyFound] = []
    other: list[tuple[int, str]] = []
    for node, fn in scopes.calls:
        chain = pyparse._chain(node.func)
        if not chain or len(chain) < 2:
            continue
        held = clients.holds(chain, fn)
        if not (held or clients.may_hold(chain)):
            continue
        name = chain[-1]
        resolved = index.method(name) if name == name.lower() else None
        legacy = False
        if resolved is None and held and len(chain) > held + 1 and chain[held][:1].isupper():
            resolved, legacy = index.legacy(chain[held:]), True  # client.Transactions.get(access_token, start, end)
        if resolved is None:
            if held and (len(chain) == held + 1 or chain[held][:1].isupper()):
                other.append((node.lineno, ".".join(chain)))
            continue
        reader = _Request(index, (resolved.method, resolved.path), scopes, imports, node, fn)
        if legacy:
            reader.hidden.add("")  # positional arguments under the old client's own names
        else:
            reader.argument(name)
        found.append(PyFound(node.lineno, ".".join(chain), resolved, reader.fields, not reader.hidden, frozenset(reader.hidden)))
    return found, other


class _Request:
    """Reads the request of one call into dotted field paths and the prefixes where unseen fields may be sent."""

    def __init__(self, index: PlaidIndex, op: tuple[str, str], scopes, imports: _Imports, call: ast.Call, fn) -> None:
        self.index, self.op, self.scopes, self.imports, self.call, self.fn = index, op, scopes, imports, call, fn
        self.fields: set[str] = set()
        self.hidden: set[str] = set()
        self._active: set[str] = set()  # variables being read, so a self-referring binding ends

    def argument(self, method: str) -> None:
        """The request object: the first positional argument, or the ``<method>_request`` keyword."""
        args, keywords = self.call.args, {kw.arg: kw.value for kw in self.call.keywords}
        if args:
            self.object(args[0], "")
        elif f"{method}_request" in keywords:
            self.object(keywords[f"{method}_request"], "")
        elif None in keywords:
            self.hidden.add("")

    def object(self, node: ast.expr, prefix: str) -> None:
        if isinstance(node, ast.Starred):
            self.hidden.add(prefix)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if key is None:
                    self.object(value, prefix)  # {**base, 'cursor': c}
                elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                    self.field(prefix + key.value, value)
                else:
                    self.hidden.add(prefix)
        elif isinstance(node, ast.Call) and self._model(node):
            self.positional(node, prefix)
            for kw in node.keywords:
                if kw.arg is None:
                    self.object(kw.value, prefix)  # Request(**params)
                elif not kw.arg.startswith("_"):  # _check_type, _configuration: model options
                    self.field(prefix + kw.arg, kw.value)
        elif isinstance(node, ast.IfExp):
            self.object(node.body, prefix)
            self.object(node.orelse, prefix)
        elif isinstance(node, ast.Name):
            self.variable(node, prefix)
        else:
            self.hidden.add(prefix)

    def field(self, path: str, value: ast.expr) -> None:
        """A field and, when the spec lists fields under it, the object or list of objects it is given."""
        self.fields.add(path)
        if not self.index.has_children(self.op, path):
            return  # a plain value, an enum model (Products('auth')) or a free-form object
        if not isinstance(value, ast.Name):
            self.nested(path, value)
            return
        values, scope, first = self._reaching(value)
        if values and self.scopes.mutation_nodes(value.id, scope, first, self.call.lineno):
            self.hidden.add(path + ".")  # written into after it was built: not followed for a nested object
        for bound in values:  # none: the object is sent with nothing of it visible, which impact reports as possible
            self.nested(path, bound)

    def nested(self, path: str, value: ast.expr) -> None:
        if isinstance(value, (ast.List, ast.Tuple)):
            for item in value.elts:
                if isinstance(item, (ast.Dict, ast.Call, ast.IfExp)):
                    self.object(item, path + "[].")
                elif isinstance(item, (ast.Name, ast.Starred)):
                    self.hidden.add(path + "[].")
        elif isinstance(value, (ast.Dict, ast.Call, ast.IfExp)):
            self.object(value, path + ".")

    def positional(self, node: ast.Call, prefix: str) -> None:
        """A generated model takes exactly its required properties positionally; ``dict(mapping)`` is not read."""
        if not node.args:
            return
        names = None
        if not (isinstance(node.func, ast.Name) and node.func.id == "dict") and not any(isinstance(a, ast.Starred) for a in node.args):
            names = self.index.required(self.op, prefix, len(node.args), {kw.arg for kw in node.keywords if kw.arg})
        if names is None:
            self.hidden.add(prefix)
        else:
            self.fields.update(names)

    def _model(self, node: ast.Call) -> bool:
        """A call that builds a request object: a class imported from the plaid package, or ``dict(...)``."""
        func = node.func
        if isinstance(func, ast.Name):
            if func.id == "dict":
                return True
            return func.id[:1].isupper() and (func.id in self.imports.sdk or self.imports.star)
        chain = pyparse._chain(func)
        return bool(chain) and chain[0] in self.imports.sdk and chain[-1][:1].isupper()

    def _reaching(self, node: ast.Name):
        """(values the variable can hold at the call, the scope that binds it, the first line any is bound on);
        no values when it is a parameter, is rebound in a loop around the call, or is not bound in this file."""
        name = node.id
        bindings, scope, dominated = self.scopes.reaching(name, self.call, self.fn)
        if (
            not bindings or name in self._active
            or self.scopes.shadowed_by_parameter(name, self.fn, scope, dominated)
            or self.scopes.rebound_in_loop(name, self.call, scope)
        ):
            return [], scope, 0
        return [value for _, value, _ in bindings], scope, min(line for line, _, _ in bindings)

    def variable(self, node: ast.Name, prefix: str) -> None:
        values, scope, first = self._reaching(node)
        if not values:
            self.hidden.add(prefix)
            return
        self._active.add(node.id)
        for value in values:
            if isinstance(value, ast.Name):
                self.hidden.add(prefix)  # an alias of another variable: not followed
            else:
                self.object(value, prefix)
        for _, keys, stmt in self.scopes.mutation_nodes(node.id, scope, first, self.call.lineno):
            written = stmt.value if isinstance(stmt, ast.Assign) and len(keys) == 1 else None
            for key in keys:
                if written is not None:
                    self.field(prefix + key, written)
                else:
                    self.fields.add(prefix + key)
            if not keys:
                self.hidden.add(prefix)  # request.update(other), request[name] = value
        self._active.discard(node.id)
