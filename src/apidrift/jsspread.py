"""Spreads whose keys can be read: ``...(cond ? { a: 1 } : {})``, ``...(cond && { a: 1 })``, ``...{ a: 1 }``.

A spread like this is common for optional parameters, and it can only add the keys written in its
literal branches. Anything else (a variable, a call, a nested ternary, ``as T``, ``.x`` after it, an
arrow function) is left to the caller to treat as unknown.

Keys read from a literal branch keep their spans, with ``holder`` set to the branch body, so the fixer
edits them inside the branch they were written in. The same reading tells a request-options argument
written as a choice (``idempotencyKey ? { idempotencyKey } : undefined``) from parameters.
"""

from __future__ import annotations

from dataclasses import replace

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
    return _branches(src, i + 1, end - 1)


def _branches(src, lo: int, hi: int) -> list[int] | None:
    """The literal branches of ``cond ? A : B`` or ``cond && A`` spanning ``[lo, hi)``: the ``{`` of each
    branch that is an object literal; None when the expression is anything else or a branch is not a
    whole literal (``{ a } as T``, ``{ a }.b``, a call, a variable, a nested choice)."""
    ops = _operators(src, lo, hi)
    kinds = [op for op, _ in ops]
    if kinds == ["?", ":"]:
        (_, q), (_, colon) = ops
        branches = [(q + 1, colon), (colon + 1, hi)]
    elif kinds == ["&&"]:
        branches = [(ops[0][1] + 2, hi)]
    else:
        return None
    if not src.text[lo : ops[0][1]].strip():
        return None  # no condition
    opens: list[int] = []
    for a, b in branches:
        body = src.text[a:b].strip()
        if body in EMPTY:
            continue
        s = src.skip_space(a)
        if s >= b or src.text[s] != "{" or src.skip_balanced(s) != s + len(body):
            return None
        opens.append(s)
    return opens


def absorb(out, sub, start: int) -> None:
    """Add the fields read from a literal spread branch whose body starts at ``start`` to ``out``; each key
    keeps its span and records the branch it sits in (``holder``), unless a deeper branch already did."""
    out.paths |= sub.paths
    out.hidden |= sub.hidden
    out.complete = out.complete and sub.complete
    for path, spans in sub.spans.items():
        out.spans.setdefault(path, []).extend(s if s.holder >= 0 else replace(s, holder=start) for s in spans)


def options_choice(src, a: int, b: int) -> bool:
    """The argument ``[a, b)`` is a choice between request options and nothing (``key ? { idempotencyKey: key }
    : undefined``, ``acct && { stripeAccount: acct }``): every literal branch holds only option keys and is
    read whole. Such an argument sends no request parameters."""
    from .jsparse import OPTION_KEYS, Fields  # jsparse imports this module

    if src.ruby:
        return False
    i = src.skip_space(a)
    if i >= b or src.text[i] in "({[":
        return False  # a parenthesised or literal argument is read by the caller as before
    opens = _branches(src, i, b)
    if not opens:
        return False
    for k in opens:
        sub = Fields()
        src.object_fields(k + 1, "", sub)
        top = {p for p in sub.paths if "." not in p and "[" not in p}
        if not sub.complete or not top or not top <= OPTION_KEYS:
            return False
    return True


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
