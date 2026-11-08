"""Whether a Python dict variable escapes the SDK call that spreads it: the fixer's read check for
:meth:`pyparse._PyScopes.escapes`, kept out of the reader.

Every ``Load`` of the name in the scope counts except the use at the call, the target of a write into it
(``p['k'] = v``, ``del p['k']``, ``p.update(...)``), a use the caller answers for (the same dict spread into
another call of the same endpoint, which the oracle checks) and, when the edited key is known, a read of a
different literal key (``p['amount']``, ``p.get('amount')``): that value cannot change when another entry is
edited. A read inside an f-string is a read like any other (``ast`` walks into the holes).
"""

from __future__ import annotations

import ast
from typing import Iterable

DICT_MUTATORS = {"update", "setdefault", "__setitem__", "pop", "popitem", "clear"}


def escapes(scopes, name: str, scope: ast.AST | None, arg: ast.AST, allowed: Iterable[ast.AST], key: str | None) -> bool:
    others = set(map(id, allowed))
    for node in ast.walk(scope if scope is not None else scopes.tree):
        if not isinstance(node, ast.Name) or node.id != name or node is arg or not isinstance(node.ctx, ast.Load):
            continue
        if id(node) in others:
            continue
        parent = scopes.parents.get(node)
        if isinstance(parent, (ast.Subscript, ast.Attribute)) and parent.value is node:
            if isinstance(parent.ctx, (ast.Store, ast.Del)):
                continue
            call = scopes.parents.get(parent)
            if isinstance(parent, ast.Attribute) and isinstance(call, ast.Call) and call.func is parent and parent.attr in DICT_MUTATORS:
                continue
            if key is not None and _other_key_read(parent, call, key):
                continue
        return True
    return False


def _other_key_read(parent: ast.expr, call: ast.AST | None, key: str) -> bool:
    """``p['k']`` or ``p.get('k')`` with a literal ``k`` that is not ``key``: a read of another entry."""
    if isinstance(parent, ast.Subscript):
        read = _constant_str(parent.slice)
    elif isinstance(call, ast.Call) and call.func is parent and parent.attr == "get" and call.args:
        read = _constant_str(call.args[0])
    else:
        return False
    return read is not None and read != key


def _constant_str(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None
