"""Which ``this`` and which name a JS/TS caller's receiver is: the proofs :mod:`jscross` asks for.

Each check fails closed (False: the use is not a visible caller):

- :func:`this_is_instance`: ``this`` at a position is an instance of the class around it only in the body
  of a method written directly in that class's body (not ``static``), or in arrow functions nested in one.
  An object-literal method, a ``function () {}`` expression (``forEach(function () {...}, other)``), a
  class-field initialiser or a static block each has a ``this`` of its own.
- :func:`typed_property`: ``this.P`` holds the wrapper's class ``C``: ``P`` is declared once in the caller's
  class, ``private`` or ``readonly``, typed exactly ``C`` (imported from the wrapper's module), with no
  decorator (``@Inject('TOKEN')`` lets the container put anything there), and only ever set from a
  constructor parameter typed exactly ``C``: a constructor parameter property, or a field assigned once
  in the constructor from such a parameter. TypeScript types are structural, so the walk's facts must
  also say that nothing ``implements`` or ``extends`` ``C``, that ``C`` is in no DI provider object, and
  that the caller's class is never built with ``new`` nor extended in the repository's source (DI then
  builds it from the declared type; a ``new Caller(new Other())`` may pass any look-alike).
- :func:`shadowed`: a local name (an import of the wrapper, or a namespace import) is used in the file
  as something other than a call of it (or ``ns.`` member access): a parameter, a local, a destructured
  name, a nested function of the same name. One such use and no call of the name in the file is followed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .jsparse import QUOTES, OPENERS

IDENT = r"[A-Za-z_$][\w$]*"
IDENT_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$")
SUPERS = re.compile(r"(?:extends(?<![\w$.]extends)|implements(?<![\w$.]implements))\s+([^{};=]{1,300})\{")  # bounded
NEW = re.compile(rf"new(?<![\w$.]new)\s+({IDENT})\s*[(<]")
PROVIDE = re.compile(r"provide(?<![\w$.]provide)\s*:")
CLASS_DECL = re.compile(rf"class(?<![\w$.]class)\s+({IDENT})")
# ``x.p = `` / ``x['p'] = ``: group 1 the receiver word ("" after ``)`` or ``]``), group 2 a ``?.``
WRITE = re.compile(
    rf"(?:(?<![\w$])({IDENT}+)|[)\]])\s*+(\?)?(?:\.\s*({IDENT}+)|\[\s*['\"]({IDENT}+)['\"]\s*\])\s*"
    r"(?:[-+*/%&|^]|\?\?|\|\||&&)?=(?![=>])"
)
# The same writes found from the member access on (``.p =``, ``['p'] =``), which the engine scans for, the
# receiver then read back from it by :func:`property_writes` as WRITE's first two groups would
WRITE_AT = re.compile(rf"(?:\.\s*({IDENT}+)|\[\s*['\"]({IDENT}+)['\"]\s*\])\s*(?:[-+*/%&|^]|\?\?|\|\||&&)?=(?![=>])")


@dataclass
class Facts:
    """What the walk over every JS/TS file the scan reads (tests left out) learnt."""

    classes: dict[str, int] = field(default_factory=dict)  # class name -> declarations
    replaced: set[str] = field(default_factory=set)  # named after ``extends`` / ``implements``
    provided: set[str] = field(default_factory=set)  # named anywhere in a DI provider object
    prototyped: set[str] = field(default_factory=set)  # ``C.prototype``
    constructed: set[str] = field(default_factory=set)  # ``new C(``
    written: set[str] = field(default_factory=set)  # properties set on something other than ``this``

    def learn(self, text: str) -> None:
        for m in CLASS_DECL.finditer(text):
            self.classes[m.group(1)] = self.classes.get(m.group(1), 0) + 1
        if "extends" in text or "implements" in text:
            for m in SUPERS.finditer(text):
                self.replaced |= set(re.findall(IDENT, m.group(1)))
        if "provide" in text:
            for lo, hi in _windows(m.start() for m in PROVIDE.finditer(text)):
                self.provided |= set(re.findall(IDENT, text[lo:hi]))
        if "prototype" in text:
            self.prototyped |= set(re.findall(rf"(?<![\w$])({IDENT})\s*\.\s*prototype\b", text))
        if "new" in text:
            self.constructed |= {m.group(1) for m in NEW.finditer(text)}
        for receiver, optional, name in property_writes(text):
            if receiver != "this" or optional:
                self.written.add(name)


def property_writes(text: str):
    """(receiver word or None after ``)`` / ``]``, whether through ``?.``, property) for every WRITE match,
    found from the member access: WRITE's receiver is the maximal word-or-``$`` run before it (its look-behind
    admits no start inside one) beginning with an ASCII letter, ``_`` or ``$``, or a closing bracket, with
    whitespace and then an optional ``?`` between."""
    for m in WRITE_AT.finditer(text):
        i = m.start() - 1
        optional = i >= 0 and text[i] == "?"
        if optional:
            i -= 1
        while i >= 0 and text[i].isspace():
            i -= 1
        if i < 0:
            continue
        if text[i] in ")]":
            yield None, optional, m.group(1) or m.group(2)
            continue
        j = i
        while j >= 0 and (text[j] in "_$" or text[j].isalnum()):  # ``\w`` is alphanumeric or ``_``
            j -= 1
        if j == i or text[j + 1] not in IDENT_START:
            continue  # no word there, or one that cannot begin an identifier
        yield text[j + 1 : i + 1], optional, m.group(1) or m.group(2)


def _windows(starts) -> list[tuple[int, int]]:
    """Text around each ``provide:`` key, wide enough for the provider object holding it: fixed windows,
    merged where they overlap so the walk stays linear (a neighbour's names only make the check stricter)."""
    out: list[tuple[int, int]] = []
    for at in starts:
        lo, hi = max(0, at - 400), at + 800
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], hi)
        else:
            out.append((lo, hi))
    return out


# --- this ---------------------------------------------------------------------------------


def this_is_instance(ctx, p: int, cls: tuple[int, int]) -> bool:
    """``this`` written just before ``p`` is an instance of the class whose body is ``cls``."""
    text = ctx.src.text
    for open_, close, _ in ctx.helpers.scopes.enclosing(p):  # innermost first
        if open_ <= cls[0]:
            return False  # out of the class without meeting a method
        if text[:open_].rstrip().endswith("=>"):
            continue  # an arrow keeps the ``this`` around it
        d = next((d for ds in ctx.helpers.defs.values() for d in ds if d.open == open_ and d.kind == "method"), None)
        return d is not None and _direct_member(ctx, d.pos, cls) and not _static(text, d.pos)
    return False


def _direct_member(ctx, pos: int, cls: tuple[int, int]) -> bool:
    """``pos`` is written directly in the class body: no bracket of a field initialiser or object around it."""
    src, text = ctx.src, ctx.src.text
    i = cls[0] + 1
    while i < pos:
        i = src.skip_space(i)
        if i >= pos:
            break
        c = text[i]
        if c in QUOTES:
            i = src.skip_string(i)
        elif c in OPENERS:
            j = src.skip_balanced(i)
            if j > pos:
                return False
            i = j
        else:
            i += 1
    return True


def _static(text: str, pos: int) -> bool:
    return re.search(r"(?<![\w$])static\s+(?:async\s+)?(?:\*\s*)?(?:get\s+|set\s+)?$", text[max(0, pos - 40) : pos]) is not None


# --- this.P ---------------------------------------------------------------------------------


def typed_property(ctx, cls: tuple[int, int], cls_name: str, prop: str, owner_local: str, facts: Facts | None) -> bool:
    """``this.prop`` in the class ``cls_name`` (body span ``cls``) provably holds the class written
    ``owner_local`` in this file (see the module docstring)."""
    if facts is None or cls_name in facts.constructed | facts.replaced | facts.prototyped or prop in facts.written:
        return False
    src, text = ctx.src, ctx.src.text
    lo, hi = cls
    constructor = _constructor(ctx, cls)
    if constructor is None:
        return False
    p = re.escape(prop)
    decls = []
    for m in re.finditer(rf"(?<![\w$.]){p}\s*[?!]?\s*:(?!:)", text[lo:hi]):
        at = lo + m.start()
        if ctx.mask.covers(at):
            continue
        if constructor[0] < at < constructor[1]:
            decls.append((at, "parameter"))
        elif _direct_member(ctx, at, cls):
            decls.append((at, "field"))
    if len(decls) != 1:
        return False  # undeclared here (an object key, a method's parameter), or declared twice
    at, kind = decls[0]
    typed = re.compile(rf"{p}\s*[?!]?\s*:\s*({IDENT})[ \t]*(?=[,);\n]|$)").match(text, at)
    if typed is None or typed.group(1) != owner_local:
        return False  # not exactly C: a union, a generic, a default value or initialiser (``= new Other()``)
    head = _member_head(text, at)
    modifiers = head if kind == "parameter" else head.rsplit("\n", 1)[-1]
    if "@" in head or not re.search(r"(?<![\w$])(?:private|readonly)\s", modifiers):
        return False  # decorated (the container decides), or public and writable from anywhere
    if kind == "parameter" and not text.startswith(("(", ","), at - len(head) - 1):
        return False  # not a parameter of the constructor itself
    body = text[lo:hi]
    if re.search(r"(?<![\w$.])this\s*\[|Object\s*\.\s*(?:assign|defineProperty)\s*\(\s*this\b", body):
        return False
    writes = [lo + m.start() for m in re.finditer(rf"\.\s*{p}\s*(?:[-+*/%&|^]|\?\?|\|\||&&)?=(?![=>])", body)]
    writes = [w for w in writes if not ctx.mask.covers(w)]
    if kind == "parameter":
        return not writes  # a constructor parameter property: set by the parameter alone
    if len(writes) != 1 or not (constructor[2] < writes[0] < constructor[3]):
        return False  # a field: set once, in the constructor
    value = re.compile(rf"\.\s*{p}\s*=\s*({IDENT})\s*;").match(text, writes[0])
    receiver = text[: writes[0]].rstrip()
    return (value is not None and re.search(r"(?<![\w$.])this$", receiver) is not None
            and _typed_parameter(ctx, constructor, value.group(1), owner_local))


def _member_head(body: str, at: int) -> str:
    """The text before ``at`` back to the previous member or parameter boundary (decorators included)."""
    depth, i = 0, at
    while i > 0:
        i -= 1
        c = body[i]
        if c in ")]":
            depth += 1
        elif c in "([":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and c in ",;{}":
            break
    return body[i + 1 : at]


def _constructor(ctx, cls: tuple[int, int]):
    """(``(``, past ``)``, body ``{``, past ``}``) of the class's one constructor, written in its body."""
    src, text = ctx.src, ctx.src.text
    found = [m for m in re.finditer(r"(?<![\w$.])constructor\s*\(", text[cls[0] : cls[1]])]
    out = []
    for m in found:
        paren = cls[0] + m.end() - 1
        if ctx.mask.covers(paren) or not _direct_member(ctx, cls[0] + m.start(), cls):
            continue
        close = src.skip_balanced(paren)
        brace = src.skip_space(close)
        if text.startswith("{", brace):
            out.append((paren, close, brace, src.skip_balanced(brace)))
    return out[0] if len(out) == 1 else None


def _typed_parameter(ctx, constructor, name: str, owner_local: str) -> bool:
    """``name`` is a parameter of the constructor typed exactly ``owner_local``, undecorated, never rebound."""
    text = ctx.src.text
    params = text[constructor[0] + 1 : constructor[1] - 1]
    hits = [m for m in re.finditer(rf"(?<![\w$.]){re.escape(name)}\s*\??\s*:\s*({IDENT})\s*(?=[,)]|$)", params)]
    if len(hits) != 1 or hits[0].group(1) != owner_local or "@" in _member_head(params, hits[0].start()):
        return False
    rebound = re.search(rf"(?<![\w$.]){re.escape(name)}\s*(?:[-+*/%&|^]|\?\?|\|\||&&)?=(?![=>])", text[constructor[2] : constructor[3]])
    return rebound is None


# --- shadowing ------------------------------------------------------------------------------


def shadowed(ctx, name: str, member: bool, skip=()) -> bool:
    """``name`` is written somewhere in the file (outside comments, strings, imports and ``skip``) as
    something other than a plain call ``name(`` (``member``: a member access ``name.``)."""
    key = (name, member, tuple(skip))
    cache = ctx.__dict__.setdefault("_shadowed", {})
    if key in cache:
        return cache[key]
    src, text, out = ctx.src, ctx.src.text, False
    for m in re.finditer(rf"(?<![\w$]){re.escape(name)}(?![\w$])", text):
        p = m.start()
        if p in skip or ctx.mask.covers(p) or any(a <= p < b for a, b in ctx.statements):
            continue
        before = text[:p].rstrip()
        if before.endswith(".") and not before.endswith("..."):
            continue  # a member of something else (``obj.name``): not this binding
        after = src.skip_space(m.end())
        if member:
            ok = text.startswith(".", after) and not text.startswith("...", after)
        else:
            ok = text.startswith("(", after) and not re.search(r"(?<![\w$])(?:function|async|get|set|static|new)\s*\*?\s*$", before)
            if ok:
                nxt = src.skip_space(src.skip_balanced(after))
                ok = not text.startswith(("{", ":", "=>"), nxt)  # a method or function defined here
        if not ok:
            out = True
            break
    cache[key] = out
    return out
