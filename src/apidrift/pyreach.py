"""The dict literal a name holds at a Python SDK call when several bindings can reach it.

``if a: p = {...} else: p = {...}`` then ``create(**p)``: every reaching literal is read (their union), and a
binding after the call inside a loop that holds the call reaches it too. When some binding is not a literal,
what can be read is kept and the top level is marked unseen. Nothing is read when none is a literal, when a
parameter of an enclosing function shadows the name, or when the name is also bound by a form the reader does
not follow (a loop or comprehension target, ``with ... as``, tuple unpacking: :meth:`pyparse._PyScopes.unmodelled`).
"""

from __future__ import annotations

import ast


def reaching_literal(scopes, name: str, at: ast.AST, fn: ast.AST | None) -> ast.expr | None:
    from .pyparse import _NOTHING, _as_dict, _with_mutations  # pyparse calls this module

    bindings, scope, dominated = scopes.reaching(name, at, fn)
    if not bindings or scopes.shadowed_by_parameter(name, fn, scope, dominated):
        return None
    values = [value for _, value, _ in bindings] + scopes.loop_rebindings(name, at, scope)
    readable = [v for v in values if _as_dict(v, _NOTHING) is not None]
    if not readable:
        return None
    if len(readable) < len(values):  # read what can be read; the rest may send anything (a ``**`` entry)
        readable.append(ast.Dict(keys=[None], values=[ast.Constant(None)]))
    value = readable[0] if len(readable) == 1 else union(readable)
    first = min(line for line, _, _ in bindings)
    mutations = [m for m in scopes.mutations[scope].get(name, ()) if m[0] > first]
    return _with_mutations(value, mutations) if mutations else value


def union(values: list[ast.expr]) -> ast.Dict:
    """One synthetic dict holding every entry of several literals (read for field names only, never edited)."""
    keys: list[ast.expr | None] = []
    vals: list[ast.expr] = []
    for v in values:
        if isinstance(v, ast.Dict):
            keys += v.keys
            vals += v.values
        else:  # dict(k=v, **rest)
            keys += [ast.Constant(kw.arg) if kw.arg else None for kw in v.keywords]
            vals += [kw.value for kw in v.keywords]
    return ast.Dict(keys=keys, values=vals)
