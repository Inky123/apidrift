"""Spreads whose keys can be read: ``...(cond ? { a: 1 } : {})``, ``...(cond && { a: 1 })``, ``...{ a: 1 }``,
and ``...this.build(x)`` / ``...built`` where a same-file helper returns the object (:mod:`jshelpers`).

A spread like this is common for optional parameters, and it can only add the keys written in its
literal branches. Anything else (a variable, a call, a nested ternary, ``as T``, ``.x`` after it, an
arrow function) is left to the caller to treat as unknown.

Keys read from a literal branch keep their spans, with ``holder`` set to the branch body, so the fixer
edits them inside the branch they were written in. The same reading tells a request-options argument
written as a choice (``idempotencyKey ? { idempotencyKey } : undefined``) from parameters.

Keys read through a helper (:class:`Reader`, which the scanner installs on its source) keep no spans: the
fixer reads the call without it and never edits through a spread. A helper the reader cannot follow, a
helper already being read, or anything else leaves the spread hidden, as before.
"""

from __future__ import annotations

import re
from dataclasses import replace

from .jsroots import _statement_end

EMPTY = {"{}", "undefined", "null", "false", "''", '""', "0", "[]"}  # branches that spread no keys
RETURNED_EMPTY = {"{}", "undefined", "null"}  # branches of a returned choice that send no keys wherever it goes
OPERATORS = ("??", "&&", "||", "?", ":")
MAX_DEPTH = 4  # helpers read through spreads inside one another


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
    opens = [_branch(src, a, b, EMPTY) for a, b in branches]
    return None if -1 in opens else [k for k in opens if k is not None]


def _branch(src, a: int, b: int, empty: set[str]) -> int | None:
    """The ``{`` of the whole object literal spanning ``[a, b)``; None for an empty branch; -1 for anything else."""
    body = src.text[a:b].strip()
    if body in empty:
        return None
    s = src.skip_space(a)
    return s if s < b and src.text[s] == "{" and src.skip_balanced(s) == s + len(body) else -1


def returned_choice(src, j: int) -> tuple[int, int, int | None, int | None] | None:
    """``return cond ? { a } : {}`` with the expression at ``j``: (condition start, the ``?``, the ``{`` of the
    then-branch, the ``{`` of the else-branch; None for ``{}`` / ``undefined`` / ``null``). One choice between
    whole literals, parenthesised at most once, that ends the statement; None for anything else."""
    if src.ruby:
        return None
    end = asi_end(src, j, _statement_end(src, j))
    if j < src.n and src.text[j] == "(" and src.skip_space(src.skip_balanced(j)) == end:
        j, end = j + 1, src.skip_balanced(j) - 1
    ops = _operators(src, j, end)
    if [op for op, _ in ops] != ["?", ":"] or not src.text[j : ops[0][1]].strip():
        return None
    (_, q), (_, colon) = ops
    then, other = _branch(src, q + 1, colon, RETURNED_EMPTY), _branch(src, colon + 1, end, RETURNED_EMPTY)
    if -1 in (then, other) or then == other:  # then == other: both empty
        return None
    return j, q, then, other


CONTINUES = set("+-*/%.?:|&,[(`<>=!")  # a line starting (or one ending) with one of these goes on to the next


def asi_end(src, i: int, end: int) -> int:
    """Where the JavaScript expression at ``i`` ends, at most ``end`` (from ``jsroots._statement_end``, which
    stops at ``;`` or a closer only): a top-level newline ends it too when no semicolon is written, unless the line
    ends with an operator or the next line starts with one (``\n.then(..)``, ``\n? a : b``, ``\n+ x``)."""
    text = src.text
    j, last = i, ""
    while j < end:
        c = text[j]
        if c == "\n":
            k = src.skip_space(j)
            if k >= end or (last not in CONTINUES and text[k] not in CONTINUES):
                return j
            j = k
            continue
        if c in "\"'`":
            j = src.skip_string(j)
        elif c in "([{":
            j = src.skip_balanced(j)
        elif text.startswith("//", j):
            j = min(end, len(text) if text.find("\n", j) < 0 else text.find("\n", j))
            continue
        elif text.startswith("/*", j):
            j = min(end, len(text) if text.find("*/", j + 2) < 0 else text.find("*/", j + 2) + 2)
            continue
        else:
            j += 1
        if not c.isspace():
            last = text[j - 1]
    return end


def merged_argument(src, start: int, end: int) -> tuple[int, int] | None:
    """The object merged in by the statement at ``start``: the one argument of ``p.merge!(x)`` / ``p.update(x)``,
    or the second of ``Object.assign(p, x)`` (exactly two)."""
    text = src.text
    m = re.compile(r"[^(\n]*?(merge!|deep_merge!|update|Object\s*\.\s*assign)\s*\(").match(text, start)
    if m is None:
        return None
    args = src.args(m.end() - 1)
    if len(args) != (2 if m.group(1).startswith("Object") else 1):
        return None
    a = src.skip_space(args[-1][0])
    return a, args[-1][1]


def absorb(out, sub, start: int) -> None:
    """Add the fields read from a literal spread branch whose body starts at ``start`` to ``out``; each key
    keeps its span and records the branch it sits in (``holder``), unless a deeper branch already did."""
    out.paths |= sub.paths
    out.hidden |= sub.hidden
    out.switched |= sub.switched
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


class Reader:
    """Reads spreads the scanner can follow in the same file, adding the keys without spans (the fixer reads the
    call without this reader and never edits through a spread):

    - ``...this.build(x)``, ``...build(x)``, ``...built`` (a local bound only to such a call): what a same-file
      helper returns (:class:`jshelpers.Helpers`);
    - ``...urls`` where every binding of ``urls`` that reaches the spread, in the same function, is an object
      literal (with the writes into it before the spread, :func:`jswrites.add_writes`) or a choice of two
      literals (``const p = c ? { a } : { b }``); ``params = { ...params, mode }`` reads the earlier ``params``.
      ``urls`` must not be used in between other than spread, rebound or written by a plain statement: passed
      to a function, aliased, or touched in a callback, it may have gained keys.

    Returns False (the caller hides the spread) for anything else, for a helper or binding already being read
    through a spread, and beyond MAX_DEPTH."""

    def __init__(self, helpers) -> None:
        self.helpers, self.active = helpers, []

    def __call__(self, src, i: int, prefix: str, out) -> bool:
        from . import jsswitch
        from .jsroots import NAME

        helpers = self.helpers
        if src.ruby or src is not helpers.src:
            return False
        i = src.skip_space(i)
        b = src.skip_scalar(i)
        got = helpers.argument(i, b)
        if got is None:
            name = src.text[i:b].strip()
            if not re.fullmatch(NAME, name):
                return False
            sub = self._local(src, name, i)
            if sub is None:
                return not prefix and jsswitch.rest_excludes(helpers, name, i)
        else:
            sub = self._enter(got.helper, lambda: (helpers.read(i, b) or (None, None))[1])
        if sub is None:
            return False
        out.paths |= {prefix + p for p in sub.paths}
        out.hidden |= {prefix + h for h in sub.hidden}
        out.switched |= {(prefix + old, prefix + new, since) for old, new, since in sub.switched}
        out.complete = out.complete and sub.complete
        return True

    def _enter(self, key, read):
        if key in self.active or len(self.active) >= MAX_DEPTH:
            return None
        self.active.append(key)
        try:
            return read()
        finally:
            self.active.pop()

    def _local(self, src, name: str, at: int):
        """The fields of the local ``name`` spread at ``at``, or None when they are not all visible."""
        from .jsparse import Fields, merge, parse_object, variable_fields

        bindings, scopes, text = self.helpers.bindings, self.helpers.scopes, src.text
        fn = scopes.innermost(at)
        reaching = bindings.reaching(name, at)
        inside = [b for b in reaching if b.literal is not None and b.literal < at < src.skip_balanced(b.literal)]
        if inside:  # ``p = { ...p }`` spreads the value p had before this assignment
            reaching = [b for b in bindings.reaching(name, inside[0].pos) if b not in inside]
        if not reaching or any(b.value < 0 or scopes.innermost(b.pos) != fn for b in reaching):
            return None  # a parameter, a rebinding in a loop, or a binding outside this function
        if not self._only_spread(src, name, min(b.pos for b in reaching), at, fn):
            return None
        out = Fields()
        for b in reaching:
            if b.literal is not None:
                sub = self._enter(("binding", b.pos), lambda b=b: variable_fields(src, bindings, b, at))
            else:
                opens = _branches(src, b.value, asi_end(src, b.value, _statement_end(src, b.value)))
                if opens is None or bindings.mutations(name, b.pos, at):
                    return None
                sub = Fields()
                for k in opens:
                    merge(sub, parse_object(src, k + 1))
            if sub is None:
                return None
            merge(out, sub)
        return out

    def _only_spread(self, src, name: str, after: int, at: int, fn) -> bool:
        """Between ``after`` and ``at``, ``name`` is only rebound, spread, or written by a statement in ``fn``."""
        from .jsroots import _mutation_re

        helpers, text = self.helpers, src.text
        own = {b.pos for b in helpers.bindings.by_name.get(name, ())}
        writes = {m.start() for m in _mutation_re(name).finditer(text, after, at)}
        for m in re.compile(rf"(?<![\w$.]){re.escape(name)}(?![\w$])").finditer(text, after, at):
            p = m.start()
            if helpers.mask.covers(p) or p in own or text[max(0, p - 3):p] == "...":
                continue
            if p in writes and helpers.scopes.innermost(p) == fn:
                continue
            if re.match(r"\s*:(?!:)", text[m.end():m.end() + 3]) and text[:p].rstrip()[-1:] in "{,":
                continue  # a key that shares the name
            return False
        return True
