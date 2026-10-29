"""The edit one key entry needs for its migration's action, and the value rewrites those edits carry.

An entry is located by the walk in :mod:`fixedit` (JS/TS/Ruby spans or Python ``ast`` nodes); this module
turns it into exactly one :class:`Edit` (so ``apply`` never sees two overlapping edits for one site) or a
reason it cannot be edited:

- ``rename``: the key name only.
- ``rename_first``: key name and value in one edit; a whole array literal becomes its first element, a
  bare name or member chain ``V`` becomes ``V[0]`` (Python ``V[0] if V else None``), a JS/Ruby call chain
  ``(V)[0]``.
- ``delete``: the entry plus one adjacent comma; whole lines when the entry fills them.
- ``wrap_type``: the value becomes ``{ type: V }`` (``{'type': V}`` in Python) under the table's rules.

Values are rewritten from an ALLOW-LIST: a value outside the small grammar below is ``unsupported_value``,
never a guess. Text near a ``/``, ``<`` or Ruby ``%r`` is not edited at all (:func:`js_unsafe`): the
cursor in :mod:`jsparse` cannot tell a regex or a generic from an operator.
Replacement text is built from the ORIGINAL source of the value, never from a parsed form.
"""

from __future__ import annotations

import ast
import bisect
import io
import re
import tokenize
from dataclasses import dataclass

from .jsparse import CLOSERS, OPENERS, QUOTES, Source
from .migrations import Migration

UNSUPPORTED, EMPTY, LEAVE = "unsupported_value", "would_empty_literal", "leave"
JS_STRING = re.compile(r"'([^'\\\n]*)'|\"([^\"\\\n]*)\"")
RUBY_SYMBOL = re.compile(r":([A-Za-z_]\w*)")
INTEGER = re.compile(r"\d+")
JS_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
RUBY_IDENT = re.compile(r"(?:@@?|\$)?[A-Za-z_]\w*[?!]?")
NOT_NAMES = {
    "null", "undefined", "nil", "true", "false", "None", "True", "False", "NaN", "Infinity", "this", "self", "new",
    "typeof", "await", "void", "delete", "yield", "function", "class", "async", "not", "defined?", "lambda", "proc",
}
DROPPED = {"map", "filter", "keys", "values"}  # rename_first: these do not return the list the old field took
@dataclass(frozen=True)
class Edit:
    start: int
    end: int
    text: str
    key: str  # the migration target this edit belongs to
    shorthand: bool = False  # ``{ k }``: the old name stays behind as the value (``new: k``)
    timestamp: bool = False  # wrap_type wrote the ``{type: 'timestamp', timestamp: N}`` form


@dataclass(frozen=True)
class Entry:
    """One key entry in the ORIGINAL text: the key token, its bare name, and the value."""

    start: int  # key token start (quotes or Ruby colon included)
    name_start: int
    name_end: int
    value_start: int  # == name_end for a shorthand entry
    value_end: int
    shorthand: bool = False


@dataclass(frozen=True)
class Container:
    """The entries of one object/hash/dict literal or one call's arguments: (start, end, comma after)."""

    slots: tuple[tuple[int, int, int | None], ...]
    literal: bool  # a literal; a call's own argument list may lose its last keyword
    lead_comma: int | None = None  # the comma before the first slot (Ruby keyword arguments after an id)

    def index(self, start: int) -> int | None:
        return next((k for k, s in enumerate(self.slots) if s[0] == start), None)


def leaf(path: str) -> str:
    return path.rsplit(".", 1)[-1]


def entry_edit(text: str, entry: Entry, m: Migration, new, cont: Container | None, *, comment: str):
    """The Edit for ``entry`` under ``m``; None when the key token is not the bare name (an unsupported key
    form); otherwise a string is the reason it cannot be fixed. ``new`` is the rewritten value for
    rename_first / wrap_type: ``(text, timestamp_form)`` or a reason (:func:`js_rewrite`, :func:`py_rewrite`)."""
    name, new_name = leaf(m.target), leaf(m.replacement) if m.replacement else ""
    if text[entry.name_start : entry.name_end] != name:
        return None
    if m.action == "rename":
        if entry.shorthand:
            return Edit(entry.start, entry.name_end, f"{new_name}: {name}", m.target, shorthand=True)
        return Edit(entry.name_start, entry.name_end, new_name, m.target)
    if m.action == "delete":
        if cont is None or (k := cont.index(entry.start)) is None:
            return UNSUPPORTED
        return delete_edit(text, cont, k, m.target, comment, shorthand=entry.shorthand)
    if not isinstance(new, tuple):
        return new or UNSUPPORTED
    value, stamp = new
    key_name = new_name if m.action == "rename_first" else name
    if entry.shorthand:
        return Edit(entry.start, entry.name_end, f"{key_name}: {value}", m.target, shorthand=True, timestamp=stamp)
    if m.action == "rename_first":
        head = text[entry.start : entry.name_start] + new_name + text[entry.name_end : entry.value_start]
        return Edit(entry.start, entry.value_end, head + value, m.target)
    return Edit(entry.value_start, entry.value_end, value, m.target, timestamp=stamp)


def delete_edit(text: str, cont: Container, k: int, key: str, comment: str, *, shorthand: bool = False) -> Edit | str:
    """Remove slot ``k`` with one adjacent comma; whole lines when the entry fills them."""
    slots = cont.slots
    if cont.literal and len(slots) == 1:
        return EMPTY
    start, end, comma = slots[k]
    prev = slots[k - 1][2] if k > 0 else cont.lead_comma
    line_start = text.rfind("\n", 0, start) + 1
    tail = comma + 1 if comma is not None else end
    nl = text.find("\n", tail)
    rest = text[tail : nl if nl >= 0 else len(text)].strip()
    own_line = not text[line_start:start].strip() and nl >= 0 and (not rest or _is_comment(rest, comment))
    if own_line:
        if comma is None and prev is not None:  # last entry: drop the comma before it, keep that line's comment
            return Edit(prev, nl + 1, text[prev + 1 : line_start], key, shorthand=shorthand)
        return Edit(line_start, nl + 1, "", key, shorthand=shorthand)
    if comma is not None:
        j = comma + 1
        while j < len(text) and text[j] in " \t":
            j += 1
        s = start
        if j >= len(text) or text[j] in "\r\n":  # the entry ends its line: keep the line break
            while s > line_start and text[s - 1] in " \t":
                s -= 1
        else:
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
        return Edit(s, j, "", key, shorthand=shorthand)
    if prev is not None:
        return Edit(prev, end, "", key, shorthand=shorthand)
    return Edit(start, end, "", key, shorthand=shorthand)


def _is_comment(rest: str, comment: str) -> bool:
    if comment == "//":
        return rest.startswith("//") or (rest.startswith("/*") and rest.endswith("*/"))
    return rest.startswith("#")


# --- JavaScript / TypeScript / Ruby -------------------------------------------------


def js_value_start(src: Source, key_end: int) -> int:
    i = src.skip_space(key_end)
    if src.text.startswith("=>", i):
        i += 2
    elif i < src.n and src.text[i] == ":":
        i += 1
    return src.skip_space(i)


def js_value_end(src: Source, i: int) -> int:
    """End of the last token of the value starting at ``i`` (before the comma or closer, comments excluded)."""
    end = i
    while i < src.n:
        i = src.skip_space(i)
        if i >= src.n:
            break
        c = src.text[i]
        if c == "," or c in CLOSERS:
            break
        if c in QUOTES:
            i = src.skip_string(i)
        elif c in OPENERS:
            i = src.skip_balanced(i)
        else:
            i += 1
        end = i
    return end


def js_container(src: Source, i: int, *, literal: bool) -> Container:
    """The top-level entries of an object/hash body (or Ruby keyword arguments) starting at ``i``."""
    text, slots = src.text, []
    first = None
    while i < src.n:
        i = src.skip_space(i)
        if i >= src.n or text[i] in CLOSERS:
            break
        if text[i] in ",;":
            i += 1
            continue
        first = i if first is None else first
        sep = src.skip_scalar(i)
        comma = sep if sep < src.n and text[sep] == "," else None
        slots.append((i, js_value_end(src, i), comma))
        i = sep + 1 if comma is not None else sep
    lead = None
    if not literal and first is not None:
        j = first - 1
        while j >= 0 and text[j] in " \t\r\n":
            j -= 1
        lead = j if j >= 0 and text[j] == "," else None
    return Container(tuple(slots), literal, lead)


def js_unsafe(src: Source, lo: int, hi: int) -> bool:
    """A ``/`` (regex or division), ``<`` (TS generics, comparisons, heredocs) or Ruby ``%r`` outside strings and
    comments: text this module's scanner may misread, so an edit near it is not attempted."""
    i = lo
    while i < hi:
        j = src.skip_space(i)
        if j > i:
            i = j
            continue
        c = src.text[i]
        if c in QUOTES:
            i = src.skip_string(i)
        elif c in "/<" or (src.ruby and src.text.startswith("%r", i)):
            return True
        else:
            i += 1
    return False


def js_rewrite(m: Migration, text: str, src: Source, start: int, end: int, *, ruby: bool, quote: str, bad_name):
    """``(new value, timestamp_form)`` for rename_first / wrap_type, or UNSUPPORTED. Only an allow-listed
    grammar is rewritten; ``bad_name(name, action)`` says a visible binding of a bare name rules it out."""
    v = text[start:end]
    if not v:
        return UNSUPPORTED
    if m.action == "rename_first":
        if v.startswith("[") and src.skip_balanced(start) == end:  # the array literal is the whole value
            i = src.skip_space(start + 1)
            if i >= end - 1 or src.text[i] in ",]" or src.text.startswith("...", i) or (ruby and src.text[i] == "*"):
                return UNSUPPORTED  # empty, a hole, or a spread first
            return text[i : js_value_end(src, i)], False
        if "{" in v or "`" in v or "?." in v:
            return UNSUPPORTED
        kind = js_chain(v, ruby)
        if kind is None or (kind == "name" and bad_name(v, m.action)):
            return UNSUPPORTED
        return (f"{v}[0]" if kind in ("name", "member") else f"({v})[0]"), False
    if "{" in v or "`" in v:
        return UNSUPPORTED  # an object already, or a template
    if m.timestamps:  # the old type was enum|timestamp: only literals are unambiguous
        content = _string(v, ruby)
        if content is not None:
            return (f"{{ type: {v} }}", False) if content in m.wrap_values else UNSUPPORTED
        if INTEGER.fullmatch(v):
            return f"{{ type: {quote}timestamp{quote}, timestamp: {v} }}", True
        return UNSUPPORTED
    leaves = js_leaves(v, ruby) if m.wrap_any else None
    if not leaves:
        return UNSUPPORTED
    for one in leaves:
        content = _string(one, ruby)
        if content is not None:
            if content not in m.wrap_values:
                return UNSUPPORTED
            continue
        kind = js_chain(one, ruby)
        if kind not in ("name", "member") or (kind == "name" and bad_name(one, m.action)):
            return UNSUPPORTED  # numbers, null, calls, subscripts: not a value the old enum held
    return f"{{ type: {v} }}", False


def _string(v: str, ruby: bool) -> str | None:
    """The content of a plain string literal (Ruby: or a symbol), else None."""
    m = JS_STRING.fullmatch(v)
    if m and not (ruby and "#{" in v):
        return m.group(1) if m.group(1) is not None else m.group(2)
    s = RUBY_SYMBOL.fullmatch(v) if ruby else None
    return s.group(1) if s else None


def js_chain(v: str, ruby: bool) -> str | None:
    """``name`` (a bare identifier), ``member`` (a dotted chain) or ``call`` (any subscript or call in the
    chain); None when ``v`` is not such a postfix chain."""
    ident = RUBY_IDENT if ruby else JS_IDENT
    m = ident.match(v)
    if not m or m.group(0) in NOT_NAMES:
        return None
    if m.group(0)[-1] in "?!":
        return None  # Ruby predicates and bang methods: a boolean, or nil when nothing changed
    kind, i, src = "name", m.end(), None
    while i < len(v):
        if v[i] == "." or (ruby and v.startswith("::", i)):
            seg = ident.match(v, i + (2 if v[i] == ":" else 1))
            if not seg or seg.group(0)[-1] in "?!" or seg.group(0) in DROPPED:
                return None
            kind = "call" if v[i] == ":" else ("member" if kind == "name" else kind)
            i = seg.end()
        elif v[i] in "([":
            src = src or Source(v, ruby=ruby)
            j = src.skip_balanced(i)
            if v[j - 1 : j] != OPENERS[v[i]]:
                return None
            kind, i = "call", j
        else:
            return None
    return kind


def js_leaves(v: str, ruby: bool) -> list[str] | None:
    """The values a ``c ? a : b`` / ``a || b`` (``??``) expression can take, or None when not understood."""
    tops = _top(v, ruby)
    if tops is None:
        return None
    q = next((i for i in tops if _is_q(v, i, ruby)), None)
    if q is not None:
        depth = 0
        for i in tops:
            if i <= q:
                continue
            if _is_q(v, i, ruby):
                depth += 1
            elif (v[i] == ":" and not ruby) or (ruby and v[i - 1 : i + 2] == " : "):
                if depth:
                    depth -= 1
                    continue
                a, b = js_leaves(v[q + 1 : i].strip(), ruby), js_leaves(v[i + 1 :].strip(), ruby)
                return None if a is None or b is None else a + b
        return None
    parts, last, skip = [], 0, -1
    for i in tops:
        if i == skip:
            continue
        if v.startswith("||", i) or (not ruby and v.startswith("??", i)):
            parts.append(v[last:i].strip())
            last, skip = i + 2, i + 1
    if not parts:
        return [v]
    parts.append(v[last:].strip())
    return None if any(not p for p in parts) else parts


def _is_q(v: str, i: int, ruby: bool) -> bool:
    if v[i] != "?":
        return False
    if ruby:
        return v[i - 1 : i] == " " and v[i + 1 : i + 2] == " "
    return v[i + 1 : i + 2] not in (".", "?") and v[i - 1 : i] != "?"


def _top(v: str, ruby: bool) -> list[int] | None:
    """Indices of ``v`` outside strings, comments and brackets."""
    src, out, i = Source(v, ruby=ruby), [], 0
    while i < len(v):
        j = src.skip_space(i)
        if j > i:
            i = j
            continue
        c = v[i]
        if c in QUOTES:
            i = src.skip_string(i)
        elif c in OPENERS:
            i = src.skip_balanced(i)
        elif c in CLOSERS:
            return None
        else:
            out.append(i)
            i += 1
    return out


# --- Python ------------------------------------------------------------------------


class PyTokens:
    """Significant tokens of a Python file with bracket depth, to find a container's top-level commas."""

    def __init__(self, text: str, starts: list[int]) -> None:
        self.toks: list[tuple[int, int, str, int]] = []
        depth = 0
        pos = lambda rc: starts[rc[0] - 1] + rc[1]  # noqa: E731 (tokenize columns are code points)
        skip = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}
        for tok in tokenize.generate_tokens(io.StringIO(text, newline=None).readline):
            if tok.type in skip:
                continue
            if tok.type == tokenize.OP and tok.string in ("(", "[", "{"):
                self.toks.append((pos(tok.start), pos(tok.end), tok.string, depth))
                depth += 1
                continue
            if tok.type == tokenize.OP and tok.string in (")", "]", "}"):
                depth -= 1
            self.toks.append((pos(tok.start), pos(tok.end), tok.string, depth))
        self.starts = [t[0] for t in self.toks]

    def container(self, open_pos: int, entry_starts: list[int], *, literal: bool) -> Container | None:
        """Slots for the entries starting at ``entry_starts`` inside the bracket at ``open_pos``."""
        o = bisect.bisect_left(self.starts, open_pos)
        if o >= len(self.toks) or self.toks[o][0] != open_pos or self.toks[o][2] not in "([{":
            return None
        depth = self.toks[o][3]
        seps: list[int] = []  # token indices of top-level commas, then the closer
        j = o + 1
        while j < len(self.toks):
            _, _, s, d = self.toks[j]
            if d == depth and s in (")", "]", "}"):
                seps.append(j)
                break
            if d == depth + 1 and s == ",":
                seps.append(j)
            j += 1
        else:
            return None
        slots = []
        for start in sorted(entry_starts):
            sep = next((x for x in seps if self.toks[x][0] >= start), None)
            if sep is None:
                return None
            comma = self.toks[sep][0] if self.toks[sep][2] == "," else None
            slots.append((start, self.toks[sep - 1][1], comma))
        return Container(tuple(slots), literal)

    def before(self, pos: int) -> tuple[int, str] | None:
        """Start and text of the token just before ``pos``."""
        k = bisect.bisect_left(self.starts, pos) - 1
        return (self.toks[k][0], self.toks[k][2]) if k >= 0 else None

    def opener_after(self, pos: int) -> int | None:
        k = bisect.bisect_left(self.starts, pos)
        while k < len(self.toks) and self.toks[k][2] != "(":
            k += 1
        return self.toks[k][0] if k < len(self.toks) else None


def py_rewrite(m: Migration, node: ast.expr, text_of, *, quote: str, bad_name):
    """Python counterpart of :func:`js_rewrite`. A name or attribute chain for rename_first becomes
    ``X[0] if X else None`` (an empty list sent nothing before; indexing it would raise)."""
    if m.action == "rename_first":
        if isinstance(node, (ast.List, ast.Tuple)):
            if not node.elts or isinstance(node.elts[0], ast.Starred):
                return UNSUPPORTED
            return text_of(node.elts[0]), False
        if not _py_ref(node, subscripts=True) or (isinstance(node, ast.Name) and bad_name(node.id, m.action)):
            return UNSUPPORTED  # calls, sets, generators, comprehensions, operators: not allow-listed
        v = text_of(node)
        return f"{v}[0] if {v} else None", False
    if m.timestamps:
        if _plain_str(node, text_of):
            return _py_wrap(text_of(node), text_of(node)[0]) if node.value in m.wrap_values else UNSUPPORTED
        if isinstance(node, ast.Constant) and type(node.value) is int and INTEGER.fullmatch(text_of(node)):
            q = quote
            return f"{{{q}type{q}: {q}timestamp{q}, {q}timestamp{q}: {text_of(node)}}}", True
        return UNSUPPORTED
    if not m.wrap_any:
        return UNSUPPORTED
    for one in _py_leaves(node):
        if _plain_str(one, text_of):
            if one.value not in m.wrap_values:
                return UNSUPPORTED
        elif not _py_ref(one, subscripts=False) or (isinstance(one, ast.Name) and bad_name(one.id, m.action)):
            return UNSUPPORTED
    v = text_of(node)
    return _py_wrap(v, v[0] if _plain_str(node, text_of) else quote)


def _py_wrap(v: str, q: str) -> tuple[str, bool]:
    return f"{{{q}type{q}: {v}}}", False


def _plain_str(node: ast.expr, text_of) -> bool:
    """An unprefixed, single-quoted-or-double-quoted string literal without escapes or concatenation."""
    if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
        return False
    s = text_of(node)
    return len(s) >= 2 and s[0] in "'\"" and s[-1] == s[0] and not s.startswith(s[0] * 3) and s[1:-1] == node.value


def _py_leaves(node: ast.expr) -> list[ast.expr]:
    if isinstance(node, ast.IfExp):
        return _py_leaves(node.body) + _py_leaves(node.orelse)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return [leaf_ for v in node.values for leaf_ in _py_leaves(v)]
    return [node]


def _py_ref(node: ast.expr, *, subscripts: bool) -> bool:
    """A name, an attribute chain, or (``subscripts``) a subscript by a constant or a name: no calls."""
    while True:
        if isinstance(node, ast.Name):
            return node.id not in ("None", "True", "False")
        if isinstance(node, ast.Attribute):
            node = node.value
        elif subscripts and isinstance(node, ast.Subscript) and (
            isinstance(node.slice, ast.Name) or (isinstance(node.slice, ast.Constant) and type(node.slice.value) in (str, int))
        ):
            node = node.value
        else:
            return False
