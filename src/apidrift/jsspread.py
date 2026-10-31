"""Spreads whose keys can be read: ``...(cond ? { a: 1 } : {})``, ``...(cond && { a: 1 })``, ``...{ a: 1 }``.

A spread like this is common for optional parameters, and it can only add the keys written in its
literal branches. Anything else (a variable, a call, a nested ternary) is left to the caller to treat
as unknown.
"""

from __future__ import annotations

EMPTY = {"{}", "undefined", "null", "false", "''", '""', "0", "[]"}  # branches that spread no keys
OPERATORS = ("??", "&&", "||", "?", ":")


def literal_branches(src, i: int) -> list[int] | None:
    """Indices of the ``{`` of each literal branch of the spread expression at ``i`` (after the dots), or
    None when some branch is not a literal. JavaScript and TypeScript only."""
    if src.ruby:
        return None
    i = src.skip_space(i)
    if i >= src.n or src.text[i] not in "({":
        return None
    end = src.skip_balanced(i)
    if not _ends_expression(src, end):
        return None
    if src.text[i] == "{":
        return [i]
    ops = _operators(src, i + 1, end - 1)
    kinds = [op for op, _ in ops]
    if kinds == ["?", ":"]:
        (_, q), (_, colon) = ops
        branches = [(q + 1, colon), (colon + 1, end - 1)]
    elif kinds == ["&&"]:
        branches = [(ops[0][1] + 2, end - 1)]
    else:
        return None
    opens: list[int] = []
    for a, b in branches:
        body = src.text[a:b].strip()
        if body in EMPTY:
            continue
        s = src.skip_space(a)
        if src.text[s] != "{" or src.skip_balanced(s) != s + len(body):
            return None
        opens.append(s)
    return opens


def _ends_expression(src, end: int) -> bool:
    """The spread is the whole entry: what follows is the next entry or the closer, not ``.x`` or ``as T``."""
    j = src.skip_space(end)
    return j >= src.n or src.text[j] in ",}"


def _operators(src, i: int, end: int) -> list[tuple[str, int]]:
    """Top-level ternary and logical operators between ``i`` and ``end`` (strings and brackets skipped)."""
    text, found = src.text, []
    while i < end:
        i = src.skip_space(i)
        if i >= end:
            break
        c = text[i]
        if c in "\"'`":
            i = src.skip_string(i)
        elif c in "([{":
            i = src.skip_balanced(i)
        elif text.startswith("=>", i):
            return [("=>", i)]  # an arrow function: not a plain choice between literals
        else:
            op = next((o for o in OPERATORS if text.startswith(o, i)), None)
            if op is not None:
                found.append((op, i))
                i += len(op)
            else:
                i += 1
    return found
