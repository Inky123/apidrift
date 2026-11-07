"""Edit sites at a caller of a wrapper defined in another file (``[through NAME, FILE:LINE]``, :mod:`crosstable`).

The edit is made only at the literal the caller wrote, in the caller's file: the array literal it passes,
or the literal of the one local it passes. The caller is found again with the scan's own table (the same
code that made it a call site), never by searching for the key. Refusals, in the words of
:mod:`fixhelpers`:

- ``helper_shared``: the local the caller passes is also used elsewhere (passed on, read, or bound twice),
  so one edit could change another request too;
- ``not_found``: the table is gone (an oracle run without the scan) or two callers share the line.

The wrapper itself is never edited: its parameter goes only to the one SDK call (the reader's own rule),
so an edit at the caller changes that caller's request and nothing else. The oracle then re-scans the
caller's file with the same table.
"""

from __future__ import annotations

import re

from . import fixhelpers, phpcross, phpparse

THROUGH_FILE = re.compile(r" \[through ([A-Za-z_]\w*), (\S+):(\d+)\]$")


def is_cross(call: str) -> bool:
    return THROUGH_FILE.search(call) is not None


def php_sites(loc, usage, m):
    """The edit sites of a PHP caller of a wrapper defined elsewhere."""
    from .fixedit import Sites

    table = getattr(loc, "table", None)
    got = THROUGH_FILE.search(usage.call)
    if table is None or got is None or table.collecting:
        return Sites(found_call=False)
    rel = (getattr(loc, "context", None) or (None, ""))[1]
    helpers, _ = fixhelpers._php_reads(loc)
    ctx = phpcross.Context(loc.parsed, helpers)
    name, file, line = got.group(1), got.group(2), int(got.group(3))
    found = [
        (paren, arg) for w, at, paren, arg in phpcross.sites(ctx, table, rel)
        if at == usage.line and (w.name, w.file, w.line) == (name, file, line) and w.resolved.path == usage.path
        and w.resolved.method == usage.method
    ]
    if len(found) != 1:
        return Sites(found_call=False)
    (paren, arg), out, src = found[0], Sites(), loc.src
    if not src.balanced or src.uncertain or loc._unbounded_near(paren):
        out.why = "unbounded"
        return out
    i = src.skip_space(arg[0])
    opener = src.array_open(i)
    if opener is not None:
        loc._literal(opener, m, out)
        return out
    variable = phpparse.VARIABLE.match(src.text, i)
    loc._variable(variable.group(1), i, m, out)
    if _escapes(helpers.bindings, variable.group(1), i, m.target.split(".")[0]):
        out.why = out.why or fixhelpers.SHARED  # the caller's array is also used elsewhere
    return out


def _escapes(bindings, name: str, at: int, head: str) -> bool:
    """The literal the caller passes can also be seen by some other use of ``name``: an opaque use, or a
    use the same binding reaches, unless that use only reads another key (``$p['description']``)."""
    from .phpscope import INPLACE, OPAQUE, READS_ALL, USED

    lo, hi = bindings.scopes.extent(at)
    if READS_ALL.search(bindings.src.code, lo, hi):
        return True
    ours = {b.pos for b in bindings.reaching(name, at)}
    for u in bindings._in_scope(name, at):
        if u.pos == at or u.kind not in (USED, INPLACE, OPAQUE):
            continue
        if u.kind == OPAQUE:
            return True
        if u.kind == INPLACE and u.key is not None and u.key != head:
            continue
        reaching = bindings.reaching(name, u.pos)
        if any(b.value < 0 or b.pos in ours for b in reaching):
            return True
    return False


def js_sites(loc, usage, m):
    """The edit sites of a JS/TS caller of a wrapper defined elsewhere: its object literal, or the literal
    (and keyed writes) of the one local it passes, exactly as for a direct call's argument."""
    from types import SimpleNamespace

    from . import jscross
    from .fixedit import Sites

    table = getattr(loc, "table", None)
    got = THROUGH_FILE.search(usage.call)
    if table is None or got is None or table.collecting or loc.ruby:
        return Sites(found_call=False)
    rel = (getattr(loc, "context", None) or (None, ""))[1]
    ctx = jscross.Context(loc.text)
    name, file, line = got.group(1), got.group(2), int(got.group(3))
    found = [
        arg for w, at, _, (arg, _) in jscross.sites(ctx, table, rel)
        if at == usage.line and (w.name, w.file, w.line) == (name, file, line) and w.resolved.path == usage.path
        and w.resolved.method == usage.method
    ]
    if len(found) != 1:
        return Sites(found_call=False)
    (a, b), out, src = found[0], Sites(), ctx.src
    loc.bindings = loc.bindings or ctx.bindings  # value rewrites look up the names they wrap
    i = src.skip_space(a)
    if src.text.startswith("{", i):
        loc._js_literal(src, i + 1, m, out, literal=True)
        return out
    variable = src.text[i:b].strip()
    loc._js_variable(SimpleNamespace(bindings=ctx.bindings), variable, i, False, m, out)
    if ctx.bindings.escapes(variable, i):
        out.why = out.why or fixhelpers.SHARED  # the caller's object is also used elsewhere
    return out
