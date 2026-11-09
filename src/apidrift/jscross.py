"""JavaScript / TypeScript wrappers followed across files (:mod:`crosstable` holds the passes).

A candidate is a function or class method whose body hands one of its parameters, unchanged and used
nowhere else, to one SDK call as that call's only parameters argument (``return
this.stripe.subscriptions.update(id, updateData)``). It is followed into other files when:

- a *method* of class ``C``: ``C`` is declared once in the repository's source, nothing ``extends`` or
  ``implements`` it, it is named in no dependency-injection provider object and has no ``C.prototype``
  use, and the method is defined once in ``C``. A caller reaches it only through ``this.NAME(`` inside
  ``C``, or ``this.P.NAME(`` where ``P`` is a ``private`` or ``readonly`` undecorated property of the
  caller's class typed exactly ``C`` (imported from the wrapper's own module) and set only from a
  constructor parameter typed exactly ``C``, in a class the source never builds with ``new`` nor extends
  (:func:`jsreach.typed_property`); ``this`` must be the instance (:func:`jsreach.this_is_instance`).
  Other definitions of the same method name in unrelated classes do not matter: TypeScript checks the
  declared type;
- an exported *function*: it is defined once in the repository's source, and a caller imports it by name
  (``import { f }`` / ``import { f as g }`` / ``import * as m``) from a specifier that resolves to the
  wrapper's file (relative, or through the nearest ``tsconfig.json`` ``paths``), in a file where that
  local name is never anything else (a parameter, a local, a nested function: :func:`jsreach.shadowed`).

The caller's argument must be an object literal without spreads, or a local bound only to one with only
keyed writes. Test files are not callers. Every walk reads the files the scan reads (dependency and build
directories left out); ``.vue``, ``.svelte`` and ``.astro`` files count as definitions of a name but are
not read as callers. Ruby is not followed. The wrapper's own call site always stays (:mod:`crosstable`).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from . import crosstable, jsparse, jsreach, jsroots, scanner, textpos
from .crosstable import Table, Wrapper
from .jshelpers import Helpers
from .jsparse import Fields, Found, Mask, Source

LANG = "js"
SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}  # read as callers
DEFINES = SUFFIXES | {".vue", ".svelte", ".astro"}  # may define the name (in a script block)
EXTS = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")
IDENT = r"[A-Za-z_$][\w$]*"
IMPORT = re.compile(rf"\bimport\s+(?:type\s+)?(?:({IDENT})\s*,?\s*)?(?:\{{([^}}]*)\}}|\*\s*as\s+({IDENT}))?\s*from\s*['\"]([^'\"]+)['\"]")
CLASS_NAME = re.compile(rf"class\s+({IDENT})[^{{]*$")
MODIFIERS = re.compile(r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:(?:public|private|protected|readonly|override)\s+)*(\.\.\.)?")
JSONC = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


class Context:
    """One parsed JS/TS file, with its imports."""

    def __init__(self, text: str, bindings: jsroots.Bindings | None = None) -> None:
        if bindings is None:
            src = Source(text)
            mask = Mask(src.text, ruby=False)
            bindings = jsroots.Bindings(src, mask, jsroots.Scopes(src, mask))
        self.bindings, self.src, self.mask = bindings, bindings.src, bindings.mask
        self.helpers = Helpers(bindings)
        self.imports: dict[str, tuple[str, str]] = {}  # local name -> (imported name, "*" or "default"; specifier)
        self.statements: list[tuple[int, int]] = []
        for m in IMPORT.finditer(self.src.text):
            if self.mask.covers(m.start()):
                continue
            self.statements.append((m.start(), m.end()))
            if m.group(1):
                self.imports[m.group(1)] = ("default", m.group(4))
            if m.group(3):
                self.imports[m.group(3)] = ("*", m.group(4))
            for item in (m.group(2) or "").split(","):
                parts = item.replace("type ", " ").split()
                if len(parts) == 1:
                    self.imports[parts[0]] = (parts[0], m.group(4))
                elif len(parts) == 3 and parts[1] == "as":
                    self.imports[parts[2]] = (parts[0], m.group(4))

    def comment(self, p: int) -> bool:
        if not self.mask.covers(p):
            return False
        k = max(a for a, _ in self.mask.spans if a <= p)
        return self.src.text.startswith(("//", "/*"), k)

    def class_name(self, pos: int) -> tuple[tuple[int, int], str] | None:
        return _class_name(self.helpers, pos)


def _class_name(helpers: Helpers, pos: int) -> tuple[tuple[int, int], str] | None:
    """The class whose body holds ``pos`` (its span and name); None outside a class."""
    span = helpers.container(pos)
    if span is None:
        return None
    m = CLASS_NAME.search(helpers.src.text, max(0, span[0] - 400), span[0])
    return (span, m.group(1)) if m else None


def params(src: Source, d) -> list[str]:
    """The definition's parameter names in order ("" for a destructured or rest parameter)."""
    text = src.text
    paren = text.find("(", d.pos)
    if paren < 0:
        return []
    close = src.skip_balanced(paren) - 1
    out, depth, start = [], 0, paren + 1
    for i in range(paren + 1, close + 1):
        c = text[i]
        if c in "([{<":
            depth += 1
        elif c in ")]}>" and not (c == ">" and text[i - 1] == "="):
            depth -= 1
        if (c == "," and depth == 0) or i == close:
            piece = text[start:i]
            m = MODIFIERS.match(piece)
            name = re.match(IDENT, piece[m.end():])
            out.append("" if m.group(1) or name is None else name.group(0))
            start = i + 1
    return out if text[paren + 1 : close].strip() else []


# --- pass 1: candidates ------------------------------------------------------------------


def collect(calls: list[jsparse.Call], helpers: Helpers, table: Table, rel: str) -> None:
    src, text = helpers.src, helpers.src.text
    for c in calls:
        inside = [d for ds in helpers.defs.values() for d in ds if d.expr is None and d.open < c.pos < d.close]
        if not inside or c.args_src is not src:
            continue
        d = max(inside, key=lambda d: d.open)
        names = params(src, d)
        args = [(k, text[a:b].strip()) for k, (a, b) in enumerate(c.spans)]
        if args and args[0][0] == 0 and "{" in c.resolved.path:
            args = args[1:]  # the id
        if len(args) != 1 or args[0][1] not in names or not re.fullmatch(IDENT, args[0][1]):
            continue
        name = args[0][1]
        word = re.compile(rf"{re.escape(name)}(?<![\w$.]{re.escape(name)})(?![\w$])")
        if sum(1 for m in word.finditer(text, d.open, d.close) if not _comment(helpers, m.start())) != 1:
            continue  # also read, written or passed on in the wrapper (in a template's ``${}`` too)
        if d.kind == "method":
            owner = _class_name(helpers, d.pos)
            if owner is None:
                continue  # a method of an object literal
            table.add(Wrapper(LANG, d.name, rel, c.line, c.call, c.resolved, names.index(name), True, owner[1], rel))
        elif d.kind == "function" and _exported(text, d.name):
            table.add(Wrapper(LANG, d.name, rel, c.line, c.call, c.resolved, names.index(name), False, "", rel))


def _comment(helpers: Helpers, p: int) -> bool:
    if not helpers.mask.covers(p):
        return False
    k = max(a for a, _ in helpers.mask.spans if a <= p)
    return helpers.src.text.startswith(("//", "/*"), k)


def _exported(text: str, name: str) -> bool:
    """Exported under its own name, and not as the module's default (a default import may bind any name,
    so its callers could not be found by this one)."""
    n = re.escape(name)
    if re.search(rf"\bexport\s+default\s+(?:async\s+)?(?:function\s*\*?\s*)?{n}\b|\b{n}\s+as\s+default\b", text):
        return False
    return re.search(rf"\bexport\s+(?:default\s+)?(?:async\s+)?(?:function\s*\*?\s*{n}\b|(?:const|let|var)\s+{n}\b)"
                     rf"|\bexport\s*\{{[^}}]*\b{n}\b[^}}]*\}}(?!\s*from)", text) is not None


# --- pass 2 ---------------------------------------------------------------------------------


def finish(table: Table, base: Path, sources: list[Path], resolve) -> None:
    table.base = base
    names = {}
    for key, w in crosstable.single(table, LANG).items():
        if _library(base, w.file):
            table.note(w, "a published package: callers outside the repository")
        else:
            names[key] = w
    if not names:
        return
    funcs = [w.name for w in names.values() if not w.method]
    counted = _definitions_pattern(funcs)  # one pass per file for every name (was one per name and file)
    source = {str(p) for p in sources}
    facts = jsreach.Facts()
    defined: Counter[str] = Counter()
    mention = re.compile(rf"(?:{crosstable.names_pattern([re.escape(w.name) for w in names.values()], r'\w$')})(?![\w$])")
    mentioned: list[Path] = []
    test = lambda rel: scanner._doc_like(rel, Path(rel).suffix.lower())  # noqa: E731  a test's mocks reach its own calls
    for item in crosstable.walk(base, DEFINES, skip=test):
        if item is None:
            for w in names.values():
                table.note(w, "cap: too many JS/TS files")
            return
        path, text = item
        facts.learn(text)
        if counted is not None:
            for m in counted.finditer(text):
                defined[m.group(1) or m.group(2) or m.group(3) or m.group(4)] += 1
        if str(path) in source and path.suffix.lower() in SUFFIXES and mention.search(text):
            mentioned.append(path)
    for key, w in names.items():
        if w.method and (facts.classes.get(w.owner, 0) != 1 or w.owner in facts.replaced | facts.provided | facts.prototyped):
            table.note(w, "the class is declared twice, extended, implemented or replaced")
        elif not w.method and defined[w.name] != 1:
            table.note(w, f"{defined[w.name]} definitions of the name")
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
        rel, text = scanner.rel_path(path, base), scanner._read(path)
        try:
            ctx = Context(text) if text is not None else None
            found = {w.key: list(uses(ctx, w, rel, table)) if ctx else [(0, -1, None)] for w in followed}
        except RecursionError:  # nested too deeply to walk: its uses cannot be accounted for, like an unread file's
            found = {w.key: [(0, -1, None)] for w in followed}
        for w in followed:
            for line, _, visible in found[w.key]:
                crosstable.record(table, w, rel, line, visible is not None)
        if crosstable.over_cap(table, LANG):
            break  # settled below: nothing more is followed
    crosstable.settle(table, LANG)


def _definitions_pattern(funcs: list[str]) -> re.Pattern[str] | None:
    """``function NAME(`` / ``const|let|var NAME`` for every function wrapper name at once, the name captured:
    a definition is one whole identifier, so each occurrence counts for exactly the name it spells, as the
    one-name-at-a-time patterns counted it."""
    if not funcs:
        return None
    n = "|".join(re.escape(f) for f in funcs)
    return re.compile(rf"function(?<![\w$]function)\s*\*?\s*({n})\s*[<(]|const(?<![\w$]const)\s+({n})(?![\w$])"
                      rf"|let(?<![\w$]let)\s+({n})(?![\w$])|var(?<![\w$]var)\s+({n})(?![\w$])")


def _library(base: Path, rel: str) -> bool:
    """The nearest package.json above the wrapper describes a package others install."""
    here = (base / rel).parent
    while True:
        try:
            pkg = json.loads((here / "package.json").read_text(errors="replace"))
        except (OSError, ValueError):
            pkg = None
        if isinstance(pkg, dict):
            return pkg.get("private") is not True and any(k in pkg for k in ("main", "module", "exports", "types"))
        if here == base or base not in here.parents:
            return False
        here = here.parent


# --- uses ----------------------------------------------------------------------------------


def uses(ctx: Context, w: Wrapper, rel: str, table: Table):
    """(line, ``(``, (argument span, fields) or None) for each use of ``w``'s name, or of a local name it is
    imported under, that may reach it; uses that provably reach something else are skipped."""
    src, text = ctx.src, ctx.src.text
    base, facts = table.base, table.facts.get(LANG)
    own = None
    if rel == w.file:
        kind = "method" if w.method else "function"
        own = next((d for d in ctx.helpers.defs.get(w.name, ()) if d.expr is None and d.kind == kind), None)
    aliases = set() if w.method else {
        k for k, v in ctx.imports.items() if v[0] == w.name and _resolves(base, rel, v[1], w.file)
    }
    lines = textpos.LineCounter(text)
    for name in sorted({w.name} | aliases):
        for m in re.finditer(rf"(?<![\w$]){re.escape(name)}(?![\w$])", text):
            p = m.start()
            if ctx.comment(p):
                continue
            line = lines.line(p)
            if ctx.mask.covers(p):
                yield line, -1, None  # a string naming it: maybe a dynamic call
                continue
            if own is not None and p == own.pos:
                continue
            if any(a <= p < b for a, b in ctx.statements):
                if name in aliases or (not w.method and name == w.name and any(
                        v[0] == w.name and _resolves(base, rel, v[1], w.file) for v in ctx.imports.values())):
                    continue  # the import of the wrapper itself
                if w.method:
                    continue  # a class of the same name imported: its uses are judged where they are
                yield line, -1, None
                continue
            before = text[:p].rstrip()
            paren = src.skip_space(m.end())
            is_call = text.startswith("(", paren)
            if w.method:
                here = ctx.class_name(p)
                if re.search(r"(?<![\w$.])this\s*\.$", before):
                    if here is not None and here[1] != w.owner:
                        continue  # ``this`` of an unrelated class (nothing extends the owner): its own method
                    reach = here is not None and is_call and jsreach.this_is_instance(ctx, p, here[0])
                else:
                    hit = re.search(rf"(?<![\w$.])this\s*\.\s*({IDENT})\s*\.$", before)
                    reach = (is_call and hit is not None and here is not None and jsreach.this_is_instance(ctx, p, here[0])
                             and _typed(ctx, here, hit.group(1), w, rel, base, facts))
                    if not reach and _definition(ctx, p, here, w):
                        continue  # the same name defined in an unrelated class
            elif re.search(r"export\s*\{[^}]*$", before) and rel == w.file:
                continue  # ``export { name }``
            else:
                reach = is_call and _imported(ctx, before, name, aliases, w, rel, base, own)
            if not reach or (own is not None and own.open <= p < own.close):
                yield line, paren if is_call else -1, None
                continue
            spans = src.args(paren)
            if w.index >= len(spans) or any(text[a:b].strip().startswith("...") for a, b in spans):
                yield line, paren, None
                continue
            fields = argument(ctx, *spans[w.index])
            yield line, paren, None if fields is None else (spans[w.index], fields)


def _definition(ctx: Context, p: int, here, w: Wrapper) -> bool:
    """A method of the same name declared in a class other than the wrapper's."""
    return here is not None and here[1] != w.owner and any(d.pos == p and d.kind == "method" for d in ctx.helpers.defs.get(w.name, ()))


def _typed(ctx: Context, here, prop: str, w: Wrapper, rel: str, base: Path, facts) -> bool:
    """``this.prop`` provably holds the wrapper's class: :func:`jsreach.typed_property`, with the class it
    is typed as written here resolving to the wrapper's (its own file, or imported from its module)."""
    (lo, hi), cls_name = here
    m = re.search(rf"(?<![\w$.]){re.escape(prop)}\s*[?!]?\s*:\s*({IDENT})", ctx.src.text[lo:hi])
    local = m.group(1) if m else None
    if local is None:
        return False
    if rel == w.file:
        if local != w.owner or local in ctx.imports:
            return False
    else:
        imported = ctx.imports.get(local)
        if imported is None or imported[0] != w.owner or not _resolves(base, rel, imported[1], w.file):
            return False
        if re.search(rf"(?<![\w$.])(?:class|interface|type|enum|namespace)\s+{re.escape(local)}(?![\w$])", ctx.src.text):
            return False  # a type of the same name declared here may shadow the import
    return jsreach.typed_property(ctx, (lo, hi), cls_name, prop, local, facts)


def _imported(ctx: Context, before: str, name: str, aliases: set[str], w: Wrapper, rel: str, base: Path, own=None) -> bool:
    """A call of ``name`` reaches the wrapper function: a local it is imported under, ``ns.NAME(`` of a
    namespace import of its module, or a plain call in its own file; never where the local name is also
    something else in the file (a parameter, a local, a nested function of that name)."""
    qualified = re.search(rf"({IDENT})\s*\.$", before)
    if qualified:
        got = ctx.imports.get(qualified.group(1))
        return (name == w.name and got is not None and got[0] == "*" and _resolves(base, rel, got[1], w.file)
                and not jsreach.shadowed(ctx, qualified.group(1), member=True))
    if before.endswith((".", "?.")):
        return False
    if name in aliases:
        return not jsreach.shadowed(ctx, name, member=False)
    if rel != w.file or name != w.name or name in ctx.imports:
        return False
    exports = {m.start(1) for m in re.finditer(rf"export\s*\{{[^}}]*?(?<![\w$])({re.escape(name)})(?![\w$])", ctx.src.text)}
    return not jsreach.shadowed(ctx, name, member=False, skip=exports | ({own.pos} if own is not None else set()))


def _resolves(base: Path, rel: str, spec: str, target: str) -> bool:
    """``spec`` imported in ``rel`` is the module file ``target`` (relative, or through tsconfig ``paths``)."""
    goal = (base / target).resolve()
    here = (base / rel).parent
    candidates = [here / spec] if spec.startswith(".") else _paths(base, here, spec)
    for c in candidates:
        for option in [Path(str(c) + e) for e in ("",) + EXTS] + [c / ("index" + e) for e in EXTS]:
            if option.resolve() == goal:
                return True
    return False


def _paths(base: Path, here: Path, spec: str) -> list[Path]:
    d = here
    while True:
        config = d / "tsconfig.json"
        if config.is_file():
            try:
                opts = json.loads(re.sub(r",\s*([}\]])", r"\1", JSONC.sub("", config.read_text(errors="replace")))).get("compilerOptions", {})
            except (OSError, ValueError, AttributeError):
                return []
            root = d / opts.get("baseUrl", ".")
            out = []
            for pattern, targets in (opts.get("paths") or {}).items():
                head, star, tail = pattern.partition("*")
                if spec.startswith(head) and spec.endswith(tail) and (star or spec == pattern):
                    middle = spec[len(head) : len(spec) - len(tail)] if star else ""
                    out += [root / t.replace("*", middle) for t in targets if isinstance(t, str)]
            return out
        if d == base or base not in d.parents:
            return []
        d = d.parent


def argument(ctx: Context, a: int, b: int) -> Fields | None:
    """An object literal without spreads, or a local bound only to one and written into only by key."""
    src, text, bindings = ctx.src, ctx.src.text, ctx.bindings
    i = src.skip_space(a)
    if text.startswith("{", i):
        if src.skip_space(src.skip_balanced(i)) < b:
            return None
        sub = jsparse.parse_object(src, i + 1)
        return None if "" in sub.hidden else sub
    m = re.compile(IDENT).match(text, i)
    if m is None or text[i:b].strip() != m.group(0):
        return None
    reaching = bindings.reaching(m.group(0), i)
    if len(reaching) != 1 or reaching[0].literal is None:
        return None
    if any(mu.key is None for mu in bindings.mutations(m.group(0), reaching[0].literal, i)):
        return None
    return jsparse.variable_fields(src, bindings, reaching[0], i)


# --- pass 3 -------------------------------------------------------------------------------


def extend(text: str, table: Table | None, rel: str, found: list[Found], calls, helpers, ruby: bool) -> list[Found]:
    """``found`` with each visible caller of a wrapper defined elsewhere added (the wrapper's own site stays)."""
    bindings = calls[0].bindings if calls else None
    if table is None or ruby:
        return found
    if table.collecting:
        if calls:
            collect(calls, helpers, table, rel)
        return found
    out = list(found)
    wrappers = [w for w in table.wrappers(LANG) if w.name in text]
    if not wrappers or scanner._doc_like(rel, Path(rel).suffix.lower()):
        return out
    for w, line, _, (_, fields) in sites(Context(text, bindings), table, rel):
        out.append(Found(line, w.call + w.through(), w.resolved, fields))
    return out


def sites(ctx: Context, table: Table, rel: str):
    for w in table.wrappers(LANG):
        if w.name in ctx.src.text:
            for line, paren, visible in uses(ctx, w, rel, table):
                if visible is not None:
                    yield w, line, paren, visible
