"""PHP wrappers followed across files (:mod:`crosstable` holds the passes; this is the PHP half).

A candidate is a wrapper :mod:`phphelpers` already accepts in its own file (its parameter goes straight
into the SDK call's parameters and is used nowhere else there), defined in a ``class`` (not a trait,
interface or enum) or as a plain function. It is followed when:

- no other ``function NAME(`` exists anywhere in the checkout, tests and vendored directories included
  (PHP method names are case-insensitive, so the count is too), the name is not too common, and the
  repository is not a Composer library (whose methods have callers outside it);

and a use of the name elsewhere is a *visible caller* only when its receiver provably holds an instance
of the wrapper's class, resolved through ``namespace`` and ``use``:

- ``$this->NAME(`` / ``self::`` / ``static::`` in that class or a class that ``extends`` it, ``parent::``
  in a direct subclass, or ``Owner::NAME(`` (the class by name);
- ``$v->NAME(`` where ``$v`` is a parameter of the enclosing method typed as the class and never rebound;
- ``$this->P->NAME(`` where ``P`` is a typed property (or promoted constructor parameter) of the class, or
  an untyped declared property that the class only ever assigns from such a typed parameter, that no
  other code assigns through another object or a trait, in a class nothing extends;
- for a plain function, ``NAME(`` in the same namespace or imported with ``use function``;

and the argument at the forwarded position is an array literal, or a local bound only to one with no
writes, exactly as :meth:`phphelpers.PhpHelpers.callers_of` requires. Every other use (a string naming
the method, an untyped receiver, a variable built elsewhere) is not visible and keeps the wrapper's own
call site as it is.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import crosstable, phpparse, scanner
from .crosstable import Table, Wrapper
from .jsparse import Fields, Found
from .phphelpers import PhpHelpers, _named
from .phplex import COMMENT
from .phpscope import BRACE_OR_END, HEADER_REACH

LANG = "php"
SUFFIXES = {".php"}
CLASS_KW = re.compile(r"(?<![\w$>:\\])(class|trait|interface|enum)\b", re.I)
CLASS_HEAD = re.compile(
    r"\s+([A-Za-z_]\w*)(?:\s+extends\s+(\\?[A-Za-z_][\w\\]*))?(?:\s+implements\s+[\w\\,\s]+)?\s*$", re.I
)
NAMESPACE = re.compile(r"(?<![\w$>:\\])namespace\s+([A-Za-z_][\w\\]*)\s*[;{]", re.I)
USE_FUNCTION = re.compile(r"(?<![\w$>:\\])use\s+function\s+\\?([A-Za-z_][\w\\]*)(?:\s+as\s+([A-Za-z_]\w*))?\s*;", re.I)
EXTENDS = re.compile(r"\bextends\s+\\?([A-Za-z_][\w\\]*)", re.I)
OUTSIDE = re.compile(r"(\$?[A-Za-z_]\w*|[)\]}])\s*(?:\?->|->)\s*([A-Za-z_]\w*)\s*(?:=(?![=>])|\?\?=)")
TRAIT = re.compile(r"(?<![\w$>:\\])trait\s+[A-Za-z_]", re.I)
TYPE = r"(\??\\?[A-Za-z_][\w\\]*)"
TYPED_PARAM = re.compile(rf"\s*(?:(?:public|protected|private|readonly)\s+)*{TYPE}\s+\$([A-Za-z_]\w*)\s*(?:=[\s\S]*)?$", re.I)


@dataclass(frozen=True)
class ClassInfo:
    brace: int
    kind: str
    name: str
    fqn: str  # lower case
    parent: str  # lower-case FQN of the class it extends ("" when none)


@dataclass
class Facts:
    """What the walk over every PHP file learnt."""

    extended: set[str]  # short names (lower case) that some class extends
    outside: set[str]  # properties assigned through something other than ``$this``
    trait_assigned: set[str]  # properties a trait assigns through ``$this``


class Context:
    """One parsed file, with what the receiver rules need."""

    def __init__(self, parsed: phpparse.Parsed, helpers: PhpHelpers) -> None:
        self.parsed, self.helpers, self.src = parsed, helpers, parsed.src
        code = self.src.code
        spaces = {m.group(1).lower() for m in NAMESPACE.finditer(code)}
        self.ns = next(iter(spaces)) if len(spaces) == 1 else ("" if not spaces else None)
        self.uses = {k.lower(): v for k, v in phpparse._uses(self.src).items()}
        self.functions = {
            (m.group(2) or m.group(1).rsplit("\\", 1)[-1]).lower(): m.group(1).lower() for m in USE_FUNCTION.finditer(code)
        }
        self.classes: dict[int, ClassInfo] = {}
        for m in CLASS_KW.finditer(code):
            stop = BRACE_OR_END.search(code, m.end(), m.end() + HEADER_REACH)
            if stop is None or stop.group(0) != "{" or stop.start() not in parsed.scopes.classes:
                continue
            brace = stop.start()
            head = CLASS_HEAD.match(code, m.end(), brace)
            if head is None:
                continue
            fqn = self.qualify(head.group(1), relative=False)
            parent = self.qualify(head.group(2)) if head.group(2) else ""
            self.classes[brace] = ClassInfo(brace, m.group(1).lower(), head.group(1), fqn, parent)

    def qualify(self, name: str, *, relative: bool = True) -> str:
        """Lower-case fully qualified class name of ``name`` as written here (``relative``: through ``use``)."""
        name = name.lstrip("?")
        if name.startswith("\\"):
            return name[1:].lower()
        parts = name.split("\\")
        head = self.uses.get(parts[0].lower()) if relative else None
        full = (head + parts[1:]) if head else ((self.ns.split("\\") if self.ns else []) + parts)
        return "\\".join(full).lower()

    def owner(self, pos: int) -> ClassInfo | None:
        return self.classes.get(self.helpers.container(pos))


# --- pass 1: candidates ------------------------------------------------------------------


def collect(parsed: phpparse.Parsed, helpers: PhpHelpers, table: Table, rel: str) -> None:
    ctx = None
    for c in parsed.calls:
        got = helpers.forwarded(c)
        if got is None:
            continue
        d, k = got
        variable = re.compile(rf"\${re.escape(d.params[k])}(?!\w)")
        named = [m for m in variable.finditer(parsed.src.text, d.open, d.close) if not _comment(parsed.src, m.start())]
        if len(named) != 1:
            continue  # also named in a string's interpolation (``"{$params['x']}"``): not forwarded unchanged
        ctx = ctx or Context(parsed, helpers)
        if ctx.ns is None:
            continue
        owner = ctx.owner(d.pos) if d.method else None
        if d.method and (owner is None or owner.kind != "class"):
            continue  # a trait's method belongs to every class that uses it
        table.add(Wrapper(LANG, d.name, rel, c.line, c.call, c.resolved, k, d.method, owner.fqn if owner else "", ctx.ns))


def _comment(src, p: int) -> bool:
    span = src.span_at(p)
    return span is not None and span[2] == COMMENT


# --- pass 2: what is followed, and who calls it --------------------------------------------


def finish(table: Table, base: Path, sources: list[Path], resolve) -> None:
    names = crosstable.single(table, LANG)
    if not names:
        return
    if crosstable.composer_library(base):
        for w in names.values():
            table.note(w, "a Composer library: callers outside the repository")
        return
    pattern = "|".join(re.escape(w.name) for w in names.values())
    definition = re.compile(rf"function\s*&?\s*({pattern})\s*\(", re.I)
    mention = re.compile(rf"(?<![\w$])(?:{pattern})(?!\w)", re.I)
    defs: Counter[str] = Counter()
    facts = Facts(set(), set(), set())
    source = {p.resolve() for p in sources}
    mentioned: list[Path] = []
    for item in crosstable.walk(base, SUFFIXES):
        if item is None:
            for w in names.values():
                table.note(w, "cap: too many PHP files to prove the name unique")
            return
        path, text = item
        defs.update(m.group(1).lower() for m in definition.finditer(text))
        facts.extended |= {m.group(1).rsplit("\\", 1)[-1].lower() for m in EXTENDS.finditer(text)}
        facts.outside |= {m.group(2) for m in OUTSIDE.finditer(text) if m.group(1) != "$this"}
        if TRAIT.search(text):
            facts.trait_assigned |= {m.group(2) for m in OUTSIDE.finditer(text) if m.group(1) == "$this"}
        rel = path.relative_to(base).as_posix()
        if path.resolve() in source and not scanner._doc_like(rel, path.suffix.lower()) and mention.search(text):
            mentioned.append(path)
    for key, w in names.items():
        if defs[key[1]] != 1:
            table.note(w, f"{defs[key[1]]} definitions of the name in the checkout")
        else:
            table.followed[key] = w
    table.facts[LANG] = facts
    followed = table.wrappers(LANG)
    if len(mentioned) > crosstable.MAX_PARSE_FILES:
        for w in followed:
            table.note(w, "cap: too many files name it")
            table.followed.pop(w.key)
        return
    for path in mentioned:
        rel, text = path.relative_to(base).as_posix(), scanner._read(path)
        if text is None:  # binary: its uses are not known
            for w in followed:
                crosstable.record(table, w, rel, 0, False)
            continue
        ctx = parse(text, resolve)
        for w in followed:
            for line, _, visible in uses(ctx, w, rel, facts):
                crosstable.record(table, w, rel, line, visible is not None)
    crosstable.settle(table, LANG)


def parse(text: str, resolve) -> Context:
    parsed = phpparse.parse(text, resolve)
    helpers = PhpHelpers(parsed)
    for c in parsed.calls:
        helpers.callers_of(c)  # same-file callers: accounted for by the same-file reader
    return Context(parsed, helpers)


# --- uses of a wrapper's name ----------------------------------------------------------------


def uses(ctx: Context, w: Wrapper, rel: str, facts: Facts):
    """(line, ``(`` of the call, argument span and fields or None) for every use of ``w``'s name in the file
    other than its definition and comments: None when the use is not a visible caller."""
    src, text, code = ctx.src, ctx.src.text, ctx.src.code
    same = {k.pos for k in ctx.helpers.callers if k.wrapper.name.lower() == w.name.lower()}
    own = ctx.helpers.unique(w.name, w.method) if rel == w.file else None
    for m in re.finditer(rf"(?<![\w$]){re.escape(w.name)}(?!\w)", text, re.I):
        p = m.start()
        span = src.span_at(p)
        if span is not None and span[2] == COMMENT:
            continue
        if span is None and re.search(r"function\s*&?\s*$", code[max(0, p - 40) : p], re.I):
            continue  # the one definition
        paren = src.skip_space(m.end())
        line = src.line(p)
        if span is not None or not text.startswith("(", paren) or paren not in src.pairs:
            yield line, -1, None
            continue
        if paren in same:
            yield line, paren, ("same-file", None)  # the same-file reader's call site
            continue
        if ctx.ns is None or (own is not None and own.open <= p < own.close) or not _reaches(ctx, w, p, facts):
            yield line, paren, None
            continue
        args = src.args(paren)
        if w.index >= len(args) or any(text.startswith("...", src.skip_space(a)) or _named(src, a) for a, _ in args):
            yield line, paren, None
            continue
        fields = argument(ctx, *args[w.index])
        yield line, paren, None if fields is None else (args[w.index], fields)


def argument(ctx: Context, a: int, b: int) -> Fields | None:
    """The fields of a caller's argument when it is written in the caller's own function: an array literal,
    or a local whose every reaching binding is one, written into only by ``$p['key'] = ...`` statements
    (read as for a direct SDK call: a key so written is sent, and the rest may be). None otherwise."""
    src, bindings = ctx.src, ctx.parsed.bindings
    i = src.skip_space(a)
    out = Fields()
    opener = src.array_open(i)
    if opener is not None:
        if src.skip_space(src.pairs[opener] + 1) < b:
            return None
        phpparse.read_array(src, opener, "", out)
        return None if "" in out.hidden else out
    m = phpparse.VARIABLE.match(src.text, i)
    if m is None or m.group(1) == "this" or src.skip_space(m.end()) < b or bindings.dynamic(m.group(1), i):
        return None
    reaching = bindings.reaching(m.group(1), i)
    if not reaching or any(r.literal is None or not phpparse.elements(src, r.literal) for r in reaching):
        return None  # a parameter, a value built elsewhere, or ``$p = []`` filled in some other way
    if any(w.key is None for w in bindings.mutations(m.group(1), min(r.pos for r in reaching), i)):
        return None  # ``$p += $more``, ``$p[] = ...``, ``$p[$k] = ...``
    phpparse.variable_fields(ctx.parsed, m.group(1), i, out)
    return out


def _reaches(ctx: Context, w: Wrapper, p: int, facts: Facts) -> bool:
    src, text = ctx.src, ctx.src.text
    j = src.back_space(p)
    op = "::" if text[j - 2 : j] == "::" else ("->" if text[j - 2 : j] == "->" else "")
    if not w.method:
        if op or text[j - 1 : j] in ("\\", "$") or re.search(r"\bnew\s*$", text[max(0, j - 8) : j]):
            return False
        own = ctx.functions.get(w.name.lower())
        return own == f"{w.scope}\\{w.name}".lower().lstrip("\\") if own else ctx.ns == w.scope
    if not op:
        return False
    start = j - 3 if op == "->" and text[j - 3 : j - 2] == "?" else j - 2
    walked = phpparse.walk_back(src, start)
    if walked is None:
        return False
    _, segs, root = walked
    here = ctx.owner(p)
    inside = here is not None and here.kind == "class" and w.owner in (here.fqn, here.parent)
    names = [s.name for s in segs]
    if any(s.called for s in segs):
        return False
    if op == "::":
        if root != "class" or len(segs) != 1:
            return False
        word = names[0].lower()
        if word in ("self", "static"):
            return inside
        if word == "parent":
            return here is not None and here.parent == w.owner
        return ctx.qualify(names[0]) == w.owner
    if root != "var":
        return False
    if names == ["this"]:
        return inside
    if len(names) == 2 and names[0] == "this":
        return here is not None and _property(ctx, here, names[1], w, facts)
    if len(names) == 1:
        return _parameter(ctx, names[0], p, w)
    return False


def _parameter(ctx: Context, name: str, p: int, w: Wrapper) -> bool:
    """``$name`` is a parameter of the method around ``p`` typed as the wrapper's class, never rebound."""
    d = ctx.helpers.enclosing(p)
    if d is None or not ctx.parsed.scopes.parameter(name, p) or ctx.parsed.bindings.dynamic(name, p):
        return False
    if any(d.open < b.pos < p for b in ctx.parsed.bindings.by_name.get(name, ())):
        return False
    return _typed(ctx, d, name) == w.owner


def _typed(ctx: Context, d, name: str) -> str | None:
    src = ctx.src
    paren = src.code.index("(", d.pos)
    for a, b in src.args(paren):
        m = TYPED_PARAM.match(src.text[a:b])
        if m and m.group(2) == name and "|" not in src.text[a:b] and "&" not in src.text[a:b]:
            return ctx.qualify(m.group(1))
    return None


def _property(ctx: Context, cls: ClassInfo, prop: str, w: Wrapper, facts: Facts) -> bool:
    src = ctx.src
    if cls.kind != "class":
        return False
    body = src.code[cls.brace : src.pairs.get(cls.brace, cls.brace)]
    name = re.escape(prop)
    typed = re.search(rf"(?:public|protected|private|var)(?:\s+readonly)?\s+{TYPE}\s+\${name}\b", body, re.I)
    if typed and typed.group(1).lower() not in ("function", "static", "readonly", "const"):
        return ctx.qualify(typed.group(1)) == w.owner  # PHP checks a typed property on every assignment
    if not re.search(rf"(?:public|protected|private|var)(?:\s+readonly)?\s+\${name}\b", body, re.I):
        return False  # not declared: a dynamic property anyone may set
    if prop in facts.outside or prop in facts.trait_assigned or cls.name.lower() in facts.extended:
        return False
    if re.search(rf"&\s*\$this\s*->\s*{name}\b|\bas\s+\$this\s*->\s*{name}\b", body):
        return False  # by reference, or a foreach target
    for m in re.finditer(rf"\$this\s*->\s*{name}\s*[,\])]", body):
        if _destructured(src, cls.brace + m.start()):
            return False  # ``[$this->P, $x] = ...`` / ``list(...) = ...``
    writes = list(re.finditer(rf"\$this\s*->\s*{name}\s*(=(?![=>])|\?\?=|\.=)", body))
    if not writes:
        return False
    for m in writes:
        at = cls.brace + m.start()
        value = re.match(r"\s*\$([A-Za-z_]\w*)\s*;", src.code[cls.brace + m.end() :])
        d = ctx.helpers.enclosing(at)
        if m.group(1) != "=" or value is None or d is None or _typed(ctx, d, value.group(1)) != w.owner:
            return False
        if any(d.open < b.pos < at for b in ctx.parsed.bindings.by_name.get(value.group(1), ())):
            return False
    return True


def _destructured(src, pos: int) -> bool:
    """``pos`` is inside an array (or ``list(...)``) that is assigned to: a destructuring target."""
    opener = src.enclosing(pos)
    while opener >= 0 and (src.text[opener] == "[" or re.search(r"\blist\s*$", src.code[max(0, opener - 8) : opener])):
        after = src.skip_space(src.pairs[opener] + 1)
        if src.text.startswith("=", after) and src.text[after + 1 : after + 2] not in ("=", ">"):
            return True
        opener = src.parent.get(opener, -1)
    return False


# --- pass 3: the call sites ----------------------------------------------------------------


def extend(parsed: phpparse.Parsed, helpers: PhpHelpers, table: Table | None, rel: str, found: list[Found]) -> list[Found]:
    """``found`` (this file's call sites from the per-file reader) with the table applied: a covered
    wrapper's own call site dropped, and each visible caller of a wrapper defined elsewhere added."""
    if table is None:
        return found
    if table.collecting:
        collect(parsed, helpers, table, rel)
        return found
    out = [f for f in found if not table.drops(LANG, rel, f.line, f.call, f.resolved.path)]
    wrappers = table.wrappers(LANG)
    if not wrappers or scanner._doc_like(rel, ".php"):
        return out  # a test file's calls check the wrapper; they do not send
    facts = table.facts.get(LANG)
    ctx = Context(parsed, helpers)
    for w in wrappers:
        for line, paren, visible in uses(ctx, w, rel, facts):
            if visible is not None and visible[0] != "same-file":
                out.append(Found(line, w.call + w.through(), w.resolved, visible[1]))
    return out


def sites(ctx: Context, table: Table, rel: str):
    """(wrapper, line, ``(``, argument span) of every visible cross-file caller in the file, for the fixer."""
    facts = table.facts.get(LANG)
    for w in table.wrappers(LANG):
        for line, paren, visible in uses(ctx, w, rel, facts):
            if visible is not None and visible[0] != "same-file":
                yield w, line, paren, visible[0]

