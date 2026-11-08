"""Whether a JavaScript local still holds only what the reader saw when the code reads it.

A variable passed to an SDK call (``create(params)``), or spread into its parameters (``{ ...base }``), is read as
the literals bound to it plus the plain writes into it before the call. That reading is only right when nothing
else can add a key by the time the call runs. :func:`settled` says it cannot, looking at every mention of the
name in the scope of its binding (the function that declares it, or the whole file for a module-level binding):

- a pure read anywhere is fine: the binding, a declaration (``let p``), another spread (``...p`` copies), a member
  read (``p.k``), a key of the same name;
- any other mention inside a nested function or callback (``list.forEach(t => { p.k = t })``, a hoisted
  ``function fill() { p.k = 1 }``) may run at any time, so it fails; when the read itself is in a callback
  (``emitter.on('x', () => create({ ...p }))``, ``retry(() => create(p))``), the callback may run any time after
  it is written, so only reads may mention the name from there on;
- a write (``p.k = v``, ``Object.assign(p, ..)``) before the read: the caller adds or hides what it writes;
- a write, or any other use (passed to a function, aliased, returned), after the read, unless a loop holds both
  the read and that use but not the binding: then it may run before the next read.

Anything else before the read (``fill(p)``, ``const q = p``, ``p.list.push(x)``) fails. Ruby is read as before
(its blocks have no scopes here): :func:`settled` is True for it.
"""

from __future__ import annotations

import re

from .jsroots import LOOP_HEADS, NAME, NO_ASSIGN_OP

MEMBER = re.compile(rf"\s*(?:\.\s*{NAME}|\[[^\]\n]*\])")
UPDATE = re.compile(r"\s*(?:\+\+|--|[-+*/%&|^]=|\*\*=|\?\?=|&&=|\|\|=)")
WRITE = re.compile(rf"(?:\s*(?:\.\s*{NAME}|\[[^\]]*\]))+\s*{NO_ASSIGN_OP}")  # ``p.k = v``, ``p['k'].j = v``
WORD = re.compile(r"(?<![\w$.])[A-Za-z_$][\w$]*(?![\w$])")


def mentions(bindings, name: str) -> list[int]:
    """Where ``name`` is written as a word in code (strings and comments left out), in order; read once per file."""
    index = bindings.__dict__.get("_mentions")
    if index is None:
        index = {}
        for m in WORD.finditer(bindings.src.text):
            if not bindings.mask.covers(m.start()):
                index.setdefault(m.group(0), []).append(m.start())
        bindings.__dict__["_mentions"] = index
    return index.get(name, [])


def settled(src, bindings, name: str, decl: int, at: int) -> bool:
    """Nothing can add a key to ``name`` (bound at ``decl``) by the time the code at ``at`` reads it."""
    if src.ruby:
        return True
    scopes, text = bindings.scopes, src.text
    fn = scopes.innermost(decl)
    lo, hi = (fn[0], fn[1]) if fn else (0, src.n)
    inner = [(a, b) for a, b in _nested(src, bindings, lo, hi) if not a <= decl < b]
    nested = lambda q: any(a < q < b for a, b in inner)  # noqa: E731
    # a read inside a callback runs once the callback exists, maybe much later: from where it is written on,
    # nothing but reads may mention the name
    anchor = min([a for a, b in inner if a < at < b], default=at)
    loops = [(o, c) for o, c in _loops(bindings) if o < anchor < c and not o < decl < c]
    own = {b.pos for b in bindings.by_name.get(name, ())}
    end = len(name)
    for p in mentions(bindings, name):
        if not lo <= p < hi or p == at or p in own:
            continue
        if text[max(0, p - 3):p] == "..." or _key(text, p, p + end) or _declared(text, p):
            continue  # copied by a spread, a key that shares the name, or ``let p``
        write = WRITE.match(text, p + end) is not None
        if not write and _member_read(text, p + end):
            continue  # ``p.k`` only reads
        if nested(p):
            return False  # a write or a use in a nested function or callback: it may run at any time
        if p > anchor:
            if anchor != at or any(o < p < c for o, c in loops):
                return False  # may run before the read: the read is in a callback, or a loop runs it again
            continue
        if not write:
            return False  # passed on, aliased or changed in place before the read
    return True


def _nested(src, bindings, lo: int, hi: int) -> list[tuple[int, int]]:
    """Function bodies and brace-less arrow bodies inside ``(lo, hi)``, once per function."""
    cache = bindings.__dict__.setdefault("_nested", {})
    if (lo, hi) not in cache:
        bodies = [(f[0], f[1]) for f in bindings.scopes.functions if lo < f[0] and f[1] <= hi]
        cache[(lo, hi)] = bodies + _expression_arrows(src, bindings.mask, lo, hi)
    return cache[(lo, hi)]


def _loops(bindings) -> list[tuple[int, int]]:
    if "_loops" not in bindings.__dict__:
        bindings.__dict__["_loops"] = [(o, c) for o, c, head in bindings._blocks_with_heads() if head in LOOP_HEADS]
    return bindings.__dict__["_loops"]


def _expression_arrows(src, mask, lo: int, hi: int) -> list[tuple[int, int]]:
    """Bodies of the arrow functions written without braces (``() => create({ ...p })``) between ``lo`` and
    ``hi``: they are functions too, though they open no scope here."""
    text, out = src.text, []
    for m in re.compile(r"=>").finditer(text, lo, hi):
        if mask.covers(m.start()):
            continue
        body = src.skip_space(m.end())
        if body < src.n and text[body] != "{":
            out.append((body, src.skip_scalar(body)))
    return out


def _declared(text: str, p: int) -> bool:
    return re.search(r"\b(?:let|var|const)\s+$", text[max(0, p - 12):p]) is not None


def _key(text: str, start: int, end: int) -> bool:
    return re.match(r"\s*:(?!:)", text[end:end + 3]) is not None and text[max(0, start - 200):start].rstrip()[-1:] in "{,"


def _member_read(text: str, end: int) -> bool:
    """``p.k`` / ``p['k']`` that only reads: not followed by another member, a call, an assignment or an update."""
    m = MEMBER.match(text, end)
    if m is None:
        return False
    rest = text[m.end():m.end() + 4]
    if UPDATE.match(rest):
        return False
    nxt = rest.lstrip()[:1]
    return nxt not in (".", "[", "(", "=") or rest.lstrip().startswith(("==", "=>"))
