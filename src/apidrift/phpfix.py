"""Edit sites for documented migrations in PHP: the PHP half of :mod:`fixedit`.

Only ``'key' => value`` entries of an array literal are edited, and only when that literal is the call's
parameters argument or a same-scope binding of the variable passed there. The walk is the reader's own
(:mod:`phpparse`), so nothing is found by searching for the key name.

There is no PHP binary where this runs, so nothing here can be checked by ``php -l``. Instead a site is
refused (``unbounded``) whenever the file cannot be read with certainty or the code around the call can
change variables without naming them: an unbalanced or uncertain file, a heredoc or a ``?>`` block in the
call's function, ``extract()``, ``eval``, an include or a variable variable before the call. After the
edit, :func:`phplex.same_structure` must hold and the fixer's usual re-scan oracle and leftover-token
count run on the result.

Value rewrites are an allow-list, as in :mod:`fixvalue`:

- ``rename_first``: an array literal becomes its first element; ``$v`` or ``$a->b`` becomes ``$v[0] ?? null``.
- ``wrap_type``: ``V`` becomes ``['type' => V]`` for a documented string, a variable or a property, and
  for a ternary or ``?:`` / ``??`` whose every branch is one of those.
"""

from __future__ import annotations

import re

from . import fixhelpers, fixvalue, phpflow, phpparse
from .fixedit import Sites, _checked
from .fixvalue import UNSUPPORTED, Container, Entry
from .jsparse import Fields, Span
from .migrations import Migration
from .phplex import HEREDOC_KIND, HTML, Lex
from .scanner import Usage, _resolve
from .sdkmap import SdkIndex

UNBOUNDED = "unbounded"
STRING_VALUE = re.compile(r"'([^'\\\n]*)'|\"([^\"\\\n${]*)\"")
INTEGER = re.compile(r"\d+")
REFERENCE = re.compile(r"\$(?!this\b)[A-Za-z_]\w*(?:->[A-Za-z_]\w*)*|\$this(?:->[A-Za-z_]\w*)+")
BARE_VARIABLE = re.compile(r"\$([A-Za-z_]\w*)")
BAD_HEAD = re.compile(r"-?\.?\d|(?:null|true|false|new)\b", re.I)


class Locator:
    """Edit sites for the usages of one PHP file, parsing it once."""

    def __init__(self, text: str, language: str, index: SdkIndex) -> None:
        self.text = text
        self.parsed = phpparse.parse(text, lambda chain: _resolve(index, chain))
        self.src: Lex = self.parsed.src
        self.bindings = self.parsed.bindings

    def sites(self, usage: Usage, m: Migration) -> Sites:
        src = self.src
        if "[through " in usage.call:
            return fixhelpers.php_caller_sites(self, usage, m)  # a call of a same-file wrapper
        plain = fixhelpers.plain_call(usage.call)
        matches = [
            c for c in self.parsed.calls
            if (c.line, c.call, c.resolved.path, c.resolved.method) == (usage.line, plain, usage.path, usage.method)
        ]
        if not matches:
            return Sites(found_call=False)
        c, out = matches[0], Sites()
        if not src.balanced or src.uncertain or self._unbounded_near(c.pos):
            out.why = UNBOUNDED
            return out
        if c.params is None or any(src.text.startswith("...", src.skip_space(a)) for a, _ in c.spans[: c.params + 1]):
            out.incomplete = True
            return out
        a, b = c.spans[c.params]
        i = src.skip_space(a)
        opener = src.array_open(i)
        variable = BARE_VARIABLE.match(src.text, i)
        if "[fields from " in usage.call and fixhelpers.php_sites(self, c, i, b, m, out):
            return out  # the array a same-file helper returns
        if opener is not None:
            if src.skip_space(src.pairs[opener] + 1) < b:
                out.incomplete = True  # ``[...] + $more``: the literal is only part of the argument
            self._literal(opener, m, out)
        elif variable and variable.group(1) != "this" and src.skip_space(variable.end()) >= b:
            self._variable(variable.group(1), i, m, out)
        elif (parts := phpflow.merge_parts(self.parsed, i, b)) is not None:
            for kind, pos, name in parts:  # array_merge(A, $b): the key is renamed in every argument that holds it
                if kind == "literal":
                    self._literal(pos, m, out)
                else:
                    self._variable(name, pos, m, out)
        else:
            out.incomplete = True  # array_merge(...), a call, a property: not a literal this module can bound
        return out

    def _unbounded_near(self, pos: int) -> bool:
        """A heredoc, or HTML after a ``?>``, in the function (or top-level code) that holds the call."""
        lo, hi = self.parsed.scopes.extent(pos)
        return any(kind == HEREDOC_KIND or (kind == HTML and a > lo) for a, b, kind in self.src.spans if a < hi and b > lo)

    def _literal(self, opener: int, m: Migration, out: Sites) -> None:
        """Entries of ``m.target`` in the array literal at ``opener``, following a dotted target through
        nested literals only."""
        src = self.src
        fields = Fields()
        phpparse.read_array(src, opener, "", fields)
        parent = m.target.rpartition(".")[0]
        parents = fields.spans.get(parent, []) if parent else []
        inner = {p: self._whole_array(p) for p in parents}
        if any(v is None for v in inner.values()):
            out.incomplete = True  # the nested array is a variable or an expression
        for span in fields.spans.get(m.target, []):
            holder_open = opener
            if parent:
                holders = [p for p in parents if p.key_start < span.key_start < p.entry_end and inner[p] is not None]
                if not holders:
                    out.incomplete = True
                    continue
                holder_open = inner[max(holders, key=lambda p: p.key_start)]
            out.add(self._entry(span, m, holder_open))

    def _whole_array(self, span: Span) -> int | None:
        """Opening bracket of the array literal that is the whole value of the entry at ``span``."""
        src = self.src
        value = src.skip_space(src.skip_space(span.key_end) + 2)
        opener = src.array_open(value)
        if opener is None or src.skip_space(src.pairs[opener] + 1) != span.entry_end:
            return None
        return opener

    def _entry(self, span: Span, m: Migration, container: int):
        src = self.src
        els = phpparse.elements(src, container)
        el = next((e for e in els if e.start == span.key_start), None)
        if el is None or el.arrow < 0:
            return None
        if {HEREDOC_KIND, HTML} & src.kinds_in(el.start, el.end):
            return UNSUPPORTED
        new = None
        if m.action in ("rename_first", "wrap_type"):
            new = self._rewrite(m, el.value, el.end, self.text[span.key_start])
        cont = None
        if m.action == "delete":
            slots = tuple((e.start, e.end, e.stop if self.text[e.stop] == "," else None) for e in els)
            cont = Container(slots, True)
        entry = Entry(span.key_start, span.key_start + 1, span.key_end - 1, el.value, el.end)
        return fixvalue.entry_edit(self.text, entry, m, new, cont, comment="//")

    # --- values -----------------------------------------------------------------------

    def _rewrite(self, m: Migration, start: int, end: int, q: str):
        """``(new value, timestamp form)`` for rename_first / wrap_type, or UNSUPPORTED."""
        src, v = self.src, self.text[start:end]
        if not v or src.kinds_in(start, end) - {"string"}:
            return UNSUPPORTED
        if m.action == "rename_first":
            opener = src.array_open(start)
            if opener is not None:
                els = phpparse.elements(src, opener) if src.pairs[opener] + 1 == end else []
                if not els or els[0].arrow >= 0 or v[els[0].start - start :].startswith("..."):
                    return UNSUPPORTED  # empty, keyed, a spread first, or more than the literal
                return self.text[els[0].start : els[0].end], False
            if not REFERENCE.fullmatch(v) or self._bad_binding(v, start, m.action):
                return UNSUPPORTED
            return f"{v}[0] ?? null", False
        if m.timestamps:  # the old type was enum|timestamp: only literals are unambiguous
            content = _string(v)
            if content is not None:
                return (f"[{q}type{q} => {v}]", False) if content in m.wrap_values else UNSUPPORTED
            if INTEGER.fullmatch(v):
                return f"[{q}type{q} => {q}timestamp{q}, {q}timestamp{q} => {v}]", True
            return UNSUPPORTED
        leaves = self._leaves(start, end) if m.wrap_any else None
        if not leaves:
            return UNSUPPORTED
        for a, b in leaves:
            one = self.text[a:b]
            content = _string(one)
            if content is not None:
                if content not in m.wrap_values:
                    return UNSUPPORTED
            elif not REFERENCE.fullmatch(one) or self._bad_binding(one, start, m.action):
                return UNSUPPORTED  # numbers, null, calls, constants, subscripts: not a value the old enum held
        return f"[{q}type{q} => {v}]", False

    def _leaves(self, start: int, end: int) -> list[tuple[int, int]] | None:
        """The spans a ``c ? a : b`` / ``a ?: b`` / ``a ?? b`` expression can evaluate to; None when the
        expression is anything else at its top level."""
        src, text = self.src, self.text
        ops: list[tuple[int, str]] = []
        i = start
        while i < end:
            i = src.skip_space(i)
            if i >= end:
                break
            stop = src.masked(i)
            if stop:
                i = stop
            elif text[i] in "([{":
                i = src.pairs.get(i, end - 1) + 1
            elif text.startswith(("??", "?:", "::", "?->"), i):
                op = "?->" if text.startswith("?->", i) else text[i : i + 2]
                if op in ("??", "?:"):
                    ops.append((i, op))
                i += len(op)
            elif text[i] in "?:":
                ops.append((i, text[i]))
                i += 1
            else:
                i += 1
        trim = lambda a, b: (src.skip_space(a), src.back_space(b))  # noqa: E731
        kinds = [op for _, op in ops]
        if not ops:
            return [trim(start, end)]
        if kinds == ["?", ":"]:
            return [trim(ops[0][0] + 1, ops[1][0]), trim(ops[1][0] + 1, end)]
        if all(k in ("??", "?:") for k in kinds):
            cuts = [start] + [p for p, _ in ops] + [end]
            parts = [trim(cuts[n] + (2 if n else 0), cuts[n + 1]) for n in range(len(cuts) - 1)]
            return parts if all(a < b for a, b in parts) else None
        return None

    def _bad_binding(self, value: str, at: int, action: str) -> bool:
        """A visible binding of the bare variable ``value`` holds something the action must not rewrite
        blindly: a number or keyword, a keyed array, and for rename_first a string or an empty array."""
        m = BARE_VARIABLE.fullmatch(value)
        if m is None:
            return False
        for b in self.bindings.reaching(m.group(1), at):
            if b.value < 0:
                continue
            opener = self.src.array_open(b.value)
            if opener is not None:
                els = phpparse.elements(self.src, opener)
                if action != "rename_first" or not els or els[0].arrow >= 0 or self.text.startswith("...", els[0].start):
                    return True
            elif BAD_HEAD.match(self.text, b.value) or (action == "rename_first" and self.text[b.value] in "'\"`<"):
                return True
        return False

    # --- variables --------------------------------------------------------------------

    def _variable(self, name: str, at: int, m: Migration, out: Sites) -> None:
        bindings = self.bindings
        if bindings.dynamic(name, at):
            out.why = UNBOUNDED  # extract(), compact(), eval, an include or a variable variable is in play
            return
        reaching = bindings.reaching(name, at)
        if not reaching:
            out.incomplete = True
            return
        for b in reaching:
            if b.literal is None:
                out.incomplete = True
            else:
                self._literal(b.literal, m, out)
        stop = min(b.pos for b in reaching)
        head = m.target.split(".")[0]
        for write in phpflow.writes(self.parsed, name, stop, at):
            if write.key == head:
                if m.action == "rename" and head == m.target and write.plain:
                    out.add(_checked(self.text, write.key_start, write.key_end, m.target, m.replacement))
                else:
                    out.incomplete = True  # a value or deletion written by a statement: not edited
            elif write.key is None:
                out.incomplete = True  # $params[] = ..., $params[$k] = ..., $params += $more
        if head in bindings.touches(name, stop, at):
            out.incomplete = True  # the key is read, appended to or changed in place: not a plain edit
        if m.action != "rename" and bindings.escapes(name, at):
            out.incomplete = True  # the array is also used elsewhere: editing it would change that use too


def _string(v: str) -> str | None:
    """The content of a plain string literal (no escapes, no interpolation), else None."""
    m = STRING_VALUE.fullmatch(v)
    if m is None:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)
