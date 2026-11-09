"""Small questions about Python syntax nodes that :mod:`pyrepo` asks while collecting a module's facts."""

from __future__ import annotations

import ast


def _builds_own_type(func: ast.expr) -> bool:
    if isinstance(func, ast.Call) and isinstance(func.func, ast.Name) and func.func.id == "type":
        return True
    return isinstance(func, ast.Attribute) and func.attr == "__class__"


def _all_strings(node: ast.Assign | ast.AugAssign) -> set[int]:
    """ids of the constants :func:`_in_all` is true for under this statement: those reached from its value
    through list and tuple displays only, when a target is the name ``__all__``."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
        return set()
    out, todo = set(), [node.value]
    while todo:
        n = todo.pop()
        if isinstance(n, (ast.List, ast.Tuple)):
            todo += n.elts
        elif isinstance(n, ast.Constant):
            out.add(id(n))
    return out


def _in_all(node: ast.AST, parents) -> bool:
    """A string in ``__all__ = [...]``: an export list, not a lookup."""
    parent = parents.get(node)
    while isinstance(parent, (ast.List, ast.Tuple)):
        parent = parents.get(parent)
    return isinstance(parent, (ast.Assign, ast.AugAssign)) and any(
        isinstance(t, ast.Name) and t.id == "__all__" for t in (parent.targets if isinstance(parent, ast.Assign) else [parent.target])
    )


def _dict_of(node: ast.expr) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "__dict__":
        return True
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "vars"
