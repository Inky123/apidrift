"""What reaches a call's parameters through variables: the array literals a variable can hold, the keys
written into it before the call, ``apply_filters`` and ``array_merge``.

- Every binding that can reach the call counts (phpscope's :meth:`Bindings.reaching`), so a literal in each
  branch of an ``if`` is read; a binding that is not a literal hides everything.
- A write whose subscripts are all plain string literals (``$d['k'] = v``, ``$d['a']['b'] = v``,
  ``$d['k'] .= v``) sends that key path and hides nothing else: an array literal assigned there is read too. Any
  other write (``$d[$k] = v``, ``$d[] = v``, ``$d['k'][] = v``, ``$d .= x``) hides what else is sent.
- ``$d = apply_filters('hook', $d, ...)`` (WordPress) returns ``$d`` unless a plugin hooks in: what ``$d`` held is
  still sent, and anything else may be.
- ``array_merge(A, B, ...)`` / ``array_replace(...)`` sends the keys of every argument; it is fully visible only
  when every argument is an array literal or a local variable whose fields are fully visible.
"""

from __future__ import annotations

import re

from .jsparse import Fields
from .phplex import STRING
from .phpscope import WRITE

FILTER = re.compile(r"apply_filters\s*\(")
MERGE_ONLY = re.compile(r"(?:array_merge|array_replace|array_merge_recursive|array_replace_recursive)\s*\(")
VARIABLE = re.compile(r"\$([A-Za-z_]\w*)")


def variable_fields(parsed, name: str, at: int, out: Fields, depth: int = 0) -> None:
    """Fields of the array literals a variable can hold at the call, plus keys written into it before it."""
    from .phpparse import elements, read_array  # phpparse builds on this module

    src, bindings = parsed.src, parsed.bindings
    reaching = bindings.reaching(name, at)
    if not reaching:
        out.hide("")
        return
    for binding in reaching:
        if binding.literal is None:
            if depth < 8 and filtered(parsed, name, binding):
                variable_fields(parsed, name, binding.pos, out, depth + 1)
            out.hide("")
            continue
        before = len(out.paths)
        read_array(src, binding.literal, "", out)
        if len(out.paths) == before and not elements(src, binding.literal):
            out.hide("")  # ``$params = [];`` is filled in elsewhere
    for write in writes(parsed, name, min(b.pos for b in reaching), at):
        written(parsed, write, out)


def writes(parsed, name: str, after: int, at: int) -> list:
    """Writes into ``name`` that reach the call at ``at``: between its first reaching binding and the call, and
    after the call in a loop around both (they reach it on the next pass)."""
    bindings = parsed.bindings
    out = bindings.mutations(name, after, at)
    loops = [(o, c) for o, c in _loops(bindings) if o < at < c]
    if loops:
        lo, hi = min(o for o, _ in loops), max(c for _, c in loops)
        out += [u for u in bindings._in_scope(name, at) if at < u.pos < hi and u.pos > lo and u.kind == WRITE]
    return out


def _loops(bindings) -> list[tuple[int, int]]:
    bindings._same_loop(0, 0)  # fills the loop table
    return bindings._loops or []


def written(parsed, write, out: Fields) -> None:
    """What one write into the parameters variable adds to the fields sent."""
    from .phpparse import read_array  # phpparse builds on this module

    src = parsed.src
    if write.path:
        path = ""
        for part in write.path:  # ("line_items", "[]", "price") sends line_items and line_items[].price
            path = path + "[]" if part == "[]" else (path + "." if path else "") + part
            if part != "[]":
                out.add(path)
        opener = src.array_open(write.value) if write.value >= 0 else None
        if opener is not None:
            read_array(src, opener, path, out)
            if not _whole_statement(src, opener):
                _hide(out, path)  # ``['a' => 1] + $more``: the literal is only part of the value
        return
    if write.literal is not None and _whole_statement(src, write.literal):  # ``$p += [...]``: adds missing keys
        read_array(src, write.literal, "", out)
        return
    out.hide("")
    if write.key:
        out.add(write.key)
        _hide(out, write.key)


def add_path(out: Fields, path: str) -> None:
    """``a.b.c`` and every parent of it are sent."""
    parts = path.split(".")
    for k in range(1, len(parts) + 1):
        out.add(".".join(parts[:k]))


def _whole_statement(src, opener: int) -> bool:
    after = src.skip_space(src.pairs[opener] + 1)
    return after < src.n and src.text[after] == ";"


def _hide(out: Fields, path: str) -> None:
    out.hide(path + ".")
    out.hide(path + "[].")


def filtered(parsed, name: str, binding) -> bool:
    """``$name = apply_filters('hook', $name, ...);``: a WordPress filter of the variable's own value."""
    src, text = parsed.src, parsed.src.text
    if binding.value < 0:
        return False
    m = FILTER.match(src.code, binding.value)
    if m is None or (m.end() - 1) not in src.pairs:
        return False
    args = src.args(m.end() - 1)
    if len(args) < 2:
        return False
    first = src.skip_space(args[0][0])
    hook = src.span_at(first)
    if hook is None or hook[0] != first or hook[2] != STRING or src.skip_space(hook[1]) < args[0][1]:
        return False  # the hook's name is one string literal
    a, b = args[1]
    v = VARIABLE.fullmatch(text, src.skip_space(a), src.back_space(b))
    after = src.skip_space(src.pairs[m.end() - 1] + 1)
    return v is not None and v.group(1) == name and after < src.n and text[after] == ";"


def merge_parts(parsed, at: int, end: int) -> list[tuple[str, int, str]] | None:
    """The arguments of an ``array_merge(...)`` / ``array_replace(...)`` that is the whole parameters argument
    (``at``..``end``): ("literal", opener, "") or ("variable", position, name) each; None when an argument is
    anything else, or the merge is followed by more."""
    src, text = parsed.src, parsed.src.text
    m = MERGE_ONLY.match(text, at)
    if m is None or (m.end() - 1) not in src.pairs or src.skip_space(src.pairs[m.end() - 1] + 1) < end:
        return None
    out = []
    for a, b in src.args(m.end() - 1):
        i = src.skip_space(a)
        if text.startswith("...", i):
            return None
        opener = src.array_open(i)
        if opener is not None and src.skip_space(src.pairs[opener] + 1) >= b:
            out.append(("literal", opener, ""))
            continue
        v = VARIABLE.match(text, i)
        if v is None or v.group(1) == "this" or src.skip_space(v.end()) < b:
            return None
        out.append(("variable", i, v.group(1)))
    return out or None


def merge_fields(parsed, at: int, end: int, out: Fields) -> None:
    """Fields of ``array_merge(...)`` as the parameters argument: the keys of every argument; anything an
    argument hides (or an argument that is neither a literal nor a variable) stays hidden."""
    from .phpparse import read_array  # phpparse builds on this module

    src = parsed.src
    parts = merge_parts(parsed, at, end)
    if parts is None:  # array_filter(...), a call inside, more after it: read the literals, hide the rest
        open_ = src.text.index("(", at)
        for x, _ in src.args(open_):
            inner = src.array_open(src.skip_space(x))
            if inner is not None:
                sub = Fields()
                read_array(src, inner, "", sub)
                out.paths |= sub.paths
                out.hidden |= sub.hidden
        out.hide("")
        return
    for kind, pos, name in parts:
        sub = Fields()
        if kind == "literal":
            read_array(src, pos, "", sub)
        else:
            variable_fields(parsed, name, pos, sub)
        out.paths |= sub.paths  # read, not edited through these spans: phpfix walks the merge itself
        for prefix in sub.hidden:
            out.hide(prefix)
