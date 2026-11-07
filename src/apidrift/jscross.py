"""JavaScript / TypeScript wrappers followed across files (:mod:`crosstable` holds the passes).

A candidate is a function or class method whose body hands one of its parameters, unchanged and used
nowhere else, to one SDK call as that call's only parameters argument (``return
this.stripe.subscriptions.update(id, updateData)``). It is followed into other files when:

- a *method* of class ``C``: ``C`` is declared once in the repository's source, nothing ``extends`` it or
  overrides it through dependency injection (``provide: C``) or ``C.prototype``, and the method is
  defined once in ``C``. A caller reaches it only through ``this.NAME(`` inside ``C``, or ``this.P.NAME(``
  where ``P`` is a property of the caller's class *declared with type* ``C`` (a constructor parameter
  property ``private readonly p: C`` or a typed field), ``C`` imported from the wrapper's own module, and
  ``P`` assigned nowhere (other definitions of the same method name in unrelated classes do not matter:
  TypeScript checks the declared type, and no subclass exists);
- an exported *function*: it is defined once in the repository's source, and a caller imports it by name
  (``import { f }`` / ``import { f as g }`` / ``import * as m``) from a specifier that resolves to the
  wrapper's file (relative, or through the nearest ``tsconfig.json`` ``paths``).

The caller's argument must be an object literal without spreads, or a local bound only to one with only
keyed writes. Test files are not callers; vendored files are never read as callers. Ruby is not followed.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from . import crosstable, jsparse, jsroots, scanner
from .crosstable import Table, Wrapper
from .jshelpers import Helpers
from .jsparse import Fields, Found, Mask, Source

LANG = "js"
SUFFIXES = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
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
        word = re.compile(rf"(?<![\w$.]){re.escape(name)}(?![\w$])")
        if sum(1 for m in word.finditer(text, d.open, d.close) if not helpers.mask.covers(m.start())) != 1:
            continue  # also read, written or passed on in the wrapper
        if d.kind == "method":
            owner = _class_name(helpers, d.pos)
            if owner is None:
                continue  # a method of an object literal
            table.add(Wrapper(LANG, d.name, rel, c.line, c.call, c.resolved, names.index(name), True, owner[1], rel))
        elif d.kind == "function" and _exported(text, d.name):
            table.add(Wrapper(LANG, d.name, rel, c.line, c.call, c.resolved, names.index(name), False, "", rel))


def _exported(text: str, name: str) -> bool:
    n = re.escape(name)
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
    owners = {w.owner for w in names.values() if w.method}
    funcs = [w.name for w in names.values() if not w.method]
    source = {p.resolve() for p in sources}
    classes, extended, provided, prototyped, assigned = Counter(), set(), set(), set(), set()
    defined: Counter[str] = Counter()
    mention = re.compile(rf"(?<![\w$])(?:{'|'.join(re.escape(w.name) for w in names.values())})(?![\w$])")
    mentioned: list[Path] = []
    for item in crosstable.walk(base, SUFFIXES):
        if item is None:
            for w in names.values():
                table.note(w, "cap: too many JS/TS files")
            return
        path, text = item
        rel = path.relative_to(base).as_posix()
        if scanner._doc_like(rel, path.suffix.lower()):
            continue  # a test's mocks and subclasses only reach the test's own calls
        for o in owners:
            classes[o] += len(re.findall(rf"\bclass\s+{re.escape(o)}\b", text))
        extended |= set(re.findall(rf"\bextends\s+(?:{IDENT}\.)*({IDENT})", text))
        provided |= set(re.findall(rf"\bprovide\s*:\s*({IDENT})", text))
        prototyped |= set(re.findall(rf"\b({IDENT})\s*\.\s*prototype\b", text))
        assigned |= {m.group(2) for m in re.finditer(rf"(\w+|\))\s*\.\s*({IDENT})\s*=(?![=>])", text) if m.group(1) != "this"}
        for f in funcs:
            defined[f] += len(re.findall(rf"\bfunction\s*\*?\s*{re.escape(f)}\s*[<(]|\b(?:const|let|var)\s+{re.escape(f)}\b", text))
        if path.resolve() in source and mention.search(text):
            mentioned.append(path)
    for key, w in names.items():
        if w.method and (classes[w.owner] != 1 or w.owner in extended | provided | prototyped):
            table.note(w, "the class is declared twice, extended or replaced")
        elif not w.method and defined[w.name] != 1:
            table.note(w, f"{defined[w.name]} definitions of the name")
        else:
            table.followed[key] = w
    table.facts[LANG] = assigned
    followed = table.wrappers(LANG)
    if len(mentioned) > crosstable.MAX_PARSE_FILES:
        for w in followed:
            table.note(w, "cap: too many files name it")
            table.followed.pop(w.key)
        return
    for path in mentioned:
        rel, text = path.relative_to(base).as_posix(), scanner._read(path)
        ctx = Context(text) if text is not None else None
        for w in followed:
            for line, _, visible in (uses(ctx, w, rel, base, assigned) if ctx else [(0, -1, None)]):
                crosstable.record(table, w, rel, line, visible is not None)
    crosstable.settle(table, LANG)


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


def uses(ctx: Context, w: Wrapper, rel: str, base: Path, assigned: set[str]):
    """(line, ``(``, (argument span, fields) or None) for each use of ``w``'s name, or of a local name it is
    imported under, that may reach it; uses that provably reach something else are skipped."""
    src, text = ctx.src, ctx.src.text
    own = None
    if rel == w.file:
        kind = "method" if w.method else "function"
        own = next((d for d in ctx.helpers.defs.get(w.name, ()) if d.expr is None and d.kind == kind), None)
    aliases = set() if w.method else {
        k for k, v in ctx.imports.items() if v[0] == w.name and _resolves(base, rel, v[1], w.file)
    }
    for name in sorted({w.name} | aliases):
        for m in re.finditer(rf"(?<![\w$]){re.escape(name)}(?![\w$])", text):
            p = m.start()
            if ctx.comment(p):
                continue
            line = text.count("\n", 0, p) + 1
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
                if re.search(r"\bthis\s*\.$", before):
                    if here is not None and here[1] != w.owner:
                        continue  # ``this`` of an unrelated class (nothing extends the owner): its own method
                    reach = here is not None and is_call
                else:
                    hit = re.search(rf"\bthis\s*\.\s*({IDENT})\s*\.$", before)
                    reach = is_call and hit is not None and here is not None and _typed(ctx, here, hit.group(1), w, rel, base, assigned)
                    if not reach and _definition(ctx, p, here, w):
                        continue  # the same name defined in an unrelated class
            elif re.search(r"export\s*\{[^}]*$", before) and rel == w.file:
                continue  # ``export { name }``
            else:
                reach = is_call and _imported(ctx, before, name, aliases, w, rel, base)
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


def _typed(ctx: Context, here, prop: str, w: Wrapper, rel: str, base: Path, assigned: set[str]) -> bool:
    """``this.prop`` is declared in the caller's class with the wrapper's class as its type, and never set."""
    (lo, hi), _ = here
    body, p = ctx.src.text[lo:hi], re.escape(prop)
    decl = re.search(rf"(?:(?:private|protected|public|readonly)\s+)+{p}\s*[?!]?\s*:\s*({IDENT})\s*[,)=;\n]"
                     rf"|^\s*(?:(?:private|protected|public|readonly)\s+)*{p}\s*[?!]?\s*:\s*({IDENT})\s*[;=\n]", body, re.M)
    if decl is None or prop in assigned or re.search(rf"this\s*\.\s*{p}\s*=(?![=>])", body):
        return False
    local = decl.group(1) or decl.group(2)
    if rel == w.file:
        return local == w.owner
    imported = ctx.imports.get(local)
    return imported is not None and imported[0] == w.owner and _resolves(base, rel, imported[1], w.file)


def _imported(ctx: Context, before: str, name: str, aliases: set[str], w: Wrapper, rel: str, base: Path) -> bool:
    """A call of ``name`` reaches the wrapper function: a local it is imported under, ``ns.NAME(`` of a
    namespace import of its module, or a plain call in its own file."""
    qualified = re.search(rf"({IDENT})\s*\.$", before)
    if qualified:
        got = ctx.imports.get(qualified.group(1))
        return name == w.name and got is not None and got[0] == "*" and _resolves(base, rel, got[1], w.file)
    if before.endswith((".", "?.")):
        return False
    if name in aliases:
        return True
    return rel == w.file and name == w.name and name not in ctx.imports


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
    """``found`` with the table applied: a covered wrapper's own site dropped, visible callers added."""
    bindings = calls[0].bindings if calls else None
    if table is None or ruby:
        return found
    if table.collecting:
        if calls:
            collect(calls, helpers, table, rel)
        return found
    out = [f for f in found if not table.drops(LANG, rel, f.line, f.call, f.resolved.path)]
    wrappers = [w for w in table.wrappers(LANG) if w.name in text]
    if not wrappers or scanner._doc_like(rel, Path(rel).suffix.lower()):
        return out
    for w, line, _, (_, fields) in sites(Context(text, bindings), table, rel):
        out.append(Found(line, w.call + w.through(), w.resolved, fields))
    return out


def sites(ctx: Context, table: Table, rel: str):
    base = getattr(table, "base", None)
    for w in table.wrappers(LANG):
        if w.name in ctx.src.text:
            for line, paren, visible in uses(ctx, w, rel, base, table.facts.get(LANG, set())):
                if visible is not None:
                    yield w, line, paren, visible
