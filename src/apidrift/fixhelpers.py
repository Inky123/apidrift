"""Edit sites behind a same-file helper: the literal a builder returns, or the literal a caller hands a wrapper.

The readers (:mod:`jshelpers`, :mod:`pyhelpers`, :mod:`phphelpers`) make such a call certain; this module
finds what to edit, and refuses with a reason naming the condition that does not hold:

- ``helper_shared``: in this file the helper is named somewhere other than as the parameters of an SDK call
  read through it (a dynamic ``send(:name)`` / ``getattr(self, "name")`` included), or the local it returns
  is passed on before it is returned, or bound outside the helper.
- ``helper_feeds_other_calls``: an SDK call in this file reads its parameters through the helper and is
  not planned with the same rename / deletion / wrap: one edit would change that call too.
- ``helper_used_elsewhere``: another source file of the repository names the helper without defining its
  own, or (for a wrapper) defines a method of the same name that could stand in for it. Test files are
  not counted: they check the helper's output, they do not send it.

The edits themselves are the ordinary ones of :mod:`fixedit` / :mod:`phpfix`, and the fixer's oracle then
re-scans the file with the helper read the same way.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace

from . import jshelpers, phphelpers, phpparse, pyhelpers, pyparse, scanner
from .jsparse import Fields, collect

SHARED, OTHER_CALLS, ELSEWHERE = "helper_shared", "helper_feeds_other_calls", "helper_used_elsewhere"
REASONS = (SHARED, OTHER_CALLS, ELSEWHERE)
FAMILY = {"javascript": ("javascript", "typescript"), "typescript": ("javascript", "typescript")}
MAX_FILES = 20_000


def plain_call(call: str) -> str:
    return jshelpers.plain_call(call)


def _same(m, other) -> bool:
    return (m.target, m.replacement, m.action) == (other.target, other.replacement, other.action)


def _planned(loc, line: int, call: str, m) -> bool:
    group = getattr(loc, "group", None) or []
    return any(
        i.usage.line == line and plain_call(i.usage.call) == call and "[through " not in i.usage.call and _same(m, mm)
        for i, mm in group
    )


def _head(m) -> str:
    return m.target.split(".")[0]


def _touches(paths, head: str) -> bool:
    return any(p == head or p.startswith((head + ".", head + "[")) for p in paths)


# --- JavaScript / TypeScript / Ruby ------------------------------------------------------


def _js_reads(loc) -> tuple[jshelpers.Helpers, list]:
    if getattr(loc, "_helper_reads", None) is None:
        helpers = jshelpers.Helpers(loc.js_calls[0].bindings)
        reads = []
        for c in loc.js_calls:
            via = jshelpers.Via(helpers)
            collect(c.args_src, c.spans, "{" in c.resolved.path, c.bindings, Fields(), ruby=loc.ruby, helper=via)
            reads += [(c, r) for r in via.reads]
        loc._helper_reads = (helpers, reads)
    return loc._helper_reads


def js_sites(loc, c, i: int, b: int, m, out) -> bool:
    """Edit sites for the argument ``text[i:b]`` of ``c`` when it is a helper's result; False otherwise."""
    helpers, reads = _js_reads(loc)
    read = next((r for cc, r in reads if cc is c and (r.local_at if r.local else r.call) == i), None)
    if read is None:
        return False
    before = len(out.edits)
    chain, d = [], read.helper
    while d is not None and d not in chain:
        chain.append(d)
        nxt = None
        for v in helpers.returns(d) or ():
            if v.literal is not None:
                loc._js_literal(helpers.src, v.literal + 1, m, out, literal=True)
            elif v.helper is not None:
                nxt = v.helper
            elif not _js_local(loc, helpers, d, v.name, v.at, m, out):
                out.why = out.why or SHARED
        d = nxt
    if read.local is not None:
        _js_writes(loc, helpers, read.local, read.call, i, m, out)
    if len(out.edits) > before and not out.why:
        for d in chain:
            out.why = out.why or _js_screen(loc, helpers, reads, d, chain, m)
    return True


def _js_local(loc, helpers, d, name: str, at: int, m, out) -> bool:
    reaching = helpers.bindings.reaching(name, at)
    if not reaching or any(b.literal is None or not d.open <= b.pos < d.close for b in reaching):
        out.incomplete = True
        return True
    for b in reaching:
        loc._js_literal(helpers.src, b.literal + 1, m, out, literal=True)
    stop = min(b.pos for b in reaching)
    _js_writes(loc, helpers, name, stop, at, m, out)
    return not helpers._escapes(name, stop, at)


def _js_writes(loc, helpers, name: str, after: int, at: int, m, out) -> None:
    """The writes into ``name`` before ``at``: the target key written by a plain statement is renamed there;
    anything else that could write the target is left incomplete."""
    from .fixedit import _checked

    head = _head(m)
    for mu in helpers.bindings.mutations(name, after, at):
        if mu.key == head:
            if m.action == "rename" and head == m.target:
                out.add(_checked(loc.text, mu.key_start, mu.key_end, m.target, m.replacement))
            else:
                out.incomplete = True
        elif mu.literal is not None:
            loc._js_literal(helpers.src, mu.literal + 1, m, out, literal=True)
        elif mu.key is None:
            inner = jshelpers._merged_argument(helpers.src, mu.start, mu.end)
            got = helpers.argument(*inner) if inner else None
            sub = helpers.helper_fields(got.helper, [], []) if got is not None and got.local is None else None
            if sub is None or "" in sub.hidden or _touches(sub.paths, head):
                out.incomplete = True  # a merge that may carry the key: not edited
    if any(t.key == head for t in helpers.bindings.touches(name, after, at)):
        out.incomplete = True


def _js_screen(loc, helpers, reads, d, chain, m) -> str | None:
    text = helpers.src.text
    for cc, r in reads:
        if (d in r.seen or r.helper is d) and not _planned(loc, cc.line, cc.call, m):
            return OTHER_CALLS
    allowed = {d.pos}
    for cc, r in reads:
        if r.helper is d:
            allowed.add(text.find(d.name, r.call))
    for parent in chain:  # a Ruby helper whose last expression is this one
        for v in helpers.returns(parent) or ():
            if v.helper is d:
                allowed.add(v.at)
    pattern = re.compile(rf"(?<![\w$@]){re.escape(d.name)}(?![\w$?!])")
    for occurrence in pattern.finditer(text):
        p = occurrence.start()
        if p in allowed or _comment(helpers, p):
            continue
        return SHARED
    language = "ruby" if helpers.ruby else "javascript"
    return ELSEWHERE if _used_elsewhere(loc, d.name, language) else None


def _comment(helpers, p: int) -> bool:
    end = helpers.mask.end(p)
    if not end:
        return False
    k = max(a for a, _ in helpers.mask.spans if a <= p)
    return helpers.src.text.startswith(("#", "//", "/*"), k)


# --- Python -----------------------------------------------------------------------------


def _py_reads(loc) -> tuple[pyhelpers.PyHelpers, list]:
    if getattr(loc, "_helper_reads", None) is None:
        helpers = pyhelpers.PyHelpers(loc.py_calls[0].scopes)
        reads = []
        for c in loc.py_calls:
            via = pyhelpers.Via(helpers, c)
            pyparse._py_fields(c.node, c.scopes.lookup(c.node.lineno, c.fn), via)
            reads += [(c, r) for r in via.reads]
        loc._helper_reads = (helpers, reads)
    return loc._helper_reads


def py_sites(loc, c, value, segs: list[str], m, out) -> bool:
    helpers, reads = _py_reads(loc)
    read = next((r for cc, r in reads if cc is c and (r.call is value or r.local is value)), None)
    if read is None:
        return False
    before, call = len(out.edits), loc._call
    d = read.helper
    for ret in helpers.returns(d) or ():
        literal, local = helpers.returned(ret, d)
        loc._call = SimpleNamespace(node=ret, fn=d, scopes=helpers.scopes)  # values are looked up in the helper
        if literal is not None:
            loc._py_walk(literal, segs, m, out)
        else:
            loc._py_value(loc._call, ret.value, segs, m, out)
            bindings, scope, _ = helpers.scopes.reaching(local, ret, d)
            if scope is not d or helpers.scopes.escapes(local, d, ret.value):
                out.why = out.why or SHARED
    loc._call = call
    if read.local is not None:
        start = read.binding.lineno
        for _, keys, _ in helpers.scopes.mutation_nodes(read.local.id, c.fn, start, c.node.lineno):
            if not keys or segs[0] in keys:
                out.incomplete = True
        if segs[0] in helpers.scopes.touched_keys(read.local.id, c.fn, start, c.node.lineno):
            out.incomplete = True
    if len(out.edits) > before and not out.why:
        out.why = _py_screen(loc, helpers, reads, d, m)
    return True


def _py_screen(loc, helpers, reads, d, m) -> str | None:
    for cc, r in reads:
        if (d in r.seen or r.helper is d) and not _planned(loc, cc.node.lineno, cc.call, m):
            return OTHER_CALLS
    calls = {id(r.call.func) for _, r in reads if r.helper is d}
    for node in ast.walk(helpers.scopes.tree):
        named = (isinstance(node, ast.Name) and node.id == d.name) or (isinstance(node, ast.Attribute) and node.attr == d.name)
        if (named and id(node) not in calls) or (isinstance(node, ast.Constant) and node.value == d.name):
            return SHARED
    return ELSEWHERE if _used_elsewhere(loc, d.name, "python") else None


# --- PHP --------------------------------------------------------------------------------


def _php_reads(loc) -> tuple[phphelpers.PhpHelpers, list]:
    if getattr(loc, "_helper_reads", None) is None:
        helpers = phphelpers.PhpHelpers(loc.parsed)
        reads = []
        for c in loc.parsed.calls:
            via = phphelpers.Via(helpers)
            phpparse.collect(loc.parsed, c, Fields(), via)
            reads += [(c, r) for r in via.reads]
            helpers.callers_of(c)
        loc._helper_reads = (helpers, reads)
    return loc._helper_reads


def php_sites(loc, c, i: int, b: int, m, out) -> bool:
    """A builder's array: edit the literal(s) it returns."""
    helpers, reads = _php_reads(loc)
    read = next((r for cc, r in reads if cc is c and (r.local_at if r.local else r.call) == i), None)
    if read is None:
        return False
    before = len(out.edits)
    d = read.helper
    if loc._unbounded_near(d.open + 1):
        out.why = "unbounded"
        return True
    for opener, local, at in helpers.returns(d) or ():
        if opener is not None:
            loc._literal(opener, m, out)
            continue
        reaching = helpers.bindings.reaching(local, at)
        if helpers.bindings.escapes(local, at):
            out.why = out.why or SHARED
        loc._variable(local, at, m, out)
        if any(r.literal is not None and not d.open <= r.pos < d.close for r in reaching):
            out.why = out.why or SHARED
    if read.local is not None:
        head = _head(m)
        for w in helpers.bindings.mutations(read.local, read.call, i):
            if w.key in (head, None):
                out.incomplete = True
        if head in helpers.bindings.touches(read.local, read.call, i):
            out.incomplete = True
    if len(out.edits) > before and not out.why:
        out.why = _php_screen(loc, helpers, reads, d, m)
    return True


def _php_screen(loc, helpers, reads, d, m) -> str | None:
    for cc, r in reads:
        if (d in r.seen or r.helper is d) and not _planned(loc, cc.line, cc.call, m):
            return OTHER_CALLS
    text = helpers.src.text
    allowed = {d.pos} | {r.call + text[r.call:].lower().find(d.name.lower()) for _, r in reads if r.helper is d}
    for occurrence in re.finditer(rf"(?<![\w$\\]){re.escape(d.name)}(?!\w)", text, re.I):
        p = occurrence.start()
        if p in allowed or _php_comment(helpers.src, p):
            continue
        return SHARED
    return ELSEWHERE if _used_elsewhere(loc, d.name, "php") else None


def _php_comment(src, p: int) -> bool:
    span = src.span_at(p)
    return span is not None and span[2] == "comment"


def php_caller_sites(loc, usage, m):
    """A wrapper's caller (``[through NAME, line N]``): edit the literal the caller passes, or None when the
    usage is not one."""
    from .fixedit import Sites

    if "[through " not in usage.call:
        return None
    helpers, _ = _php_reads(loc)
    plain = plain_call(usage.call)
    found = [
        k for k in helpers.callers
        if k.line == usage.line and k.call.call == plain and k.call.resolved.path == usage.path
        and k.call.resolved.method == usage.method
    ]
    if not found:
        return Sites(found_call=False)
    k, out, src = found[0], Sites(), loc.src
    if not src.balanced or src.uncertain or loc._unbounded_near(k.pos):
        out.why = "unbounded"
        return out
    i = src.skip_space(k.arg[0])
    opener = src.array_open(i)
    if opener is not None:
        loc._literal(opener, m, out)
    else:
        name = phpparse.VARIABLE.match(src.text, i).group(1)
        loc._variable(name, i, m, out)
        if helpers.bindings.escapes(name, i):
            out.why = out.why or SHARED  # the caller's array is also used elsewhere
    if out.edits and not out.why and _defined_elsewhere(loc, k.wrapper.name):
        out.why = ELSEWHERE
    return out


# --- the rest of the repository ------------------------------------------------------------


def _files(loc, language: str):
    context = getattr(loc, "context", None)
    if context is None:
        return None
    base, file = context
    family = FAMILY.get(language, (language,))
    out = []
    for path in scanner._iter_files(Path(base))[0][:MAX_FILES]:
        rel = path.relative_to(base).as_posix()
        if rel == file or scanner.LANGUAGES.get(path.suffix.lower()) not in family:
            continue
        if scanner._doc_like(rel, path.suffix.lower()):
            continue  # tests check what the helper returns; they do not send it
        out.append(path)
    return out


def _library(loc) -> bool:
    """The repository is a package others install (a composer ``library``, a gem): its public methods have
    callers and overrides outside it, which no check here can see."""
    context = getattr(loc, "context", None)
    if context is None:
        return True
    base = Path(context[0])
    try:
        composer = json.loads((base / "composer.json").read_text(errors="replace"))
    except (OSError, ValueError):
        composer = None
    if isinstance(composer, dict) and composer.get("type", "library") == "library":
        return True  # Composer's default type is ``library``; applications say ``project``
    return any(base.glob("*.gemspec"))


def _used_elsewhere(loc, name: str, language: str) -> bool:
    """Another source file names ``name`` without defining its own (None context: no repository, refuse)."""
    _elsewhere = loc.__dict__.setdefault("_elsewhere", {})
    key = (name, language, "use")
    if key not in _elsewhere:
        files = None if _library(loc) else _files(loc, language)
        if files is None:
            _elsewhere[key] = True
        else:
            word = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])", re.I if language == "php" else 0)
            own = re.compile(_definition(name, language), re.M | (re.I if language == "php" else 0))
            _elsewhere[key] = any(
                (text := scanner._read(p)) is not None and word.search(text) and not own.search(text) for p in files
            )
    return _elsewhere[key]


def _defined_elsewhere(loc, name: str) -> bool:
    """Another PHP file defines a method ``name``: a subclass there could stand in for this wrapper."""
    _elsewhere = loc.__dict__.setdefault("_elsewhere", {})
    key = (name, "php", "def")
    if key not in _elsewhere:
        files = None if _library(loc) else _files(loc, "php")
        own = re.compile(_definition(name, "php"), re.I)
        _elsewhere[key] = files is None or any((t := scanner._read(p)) is not None and own.search(t) for p in files)
    return _elsewhere[key]


def _definition(name: str, language: str) -> str:
    n = re.escape(name)
    if language == "ruby":
        return rf"^\s*def\s+(?:self\.)?{n}\b"
    if language == "python":
        return rf"^\s*(?:async\s+)?def\s+{n}\s*\("
    if language == "php":
        return rf"function\s*&?\s*{n}\s*\("
    return rf"(?:function\s*\*?\s*{n}\s*\(|^\s*(?:(?:public|private|protected|static|async|override)\s+)*{n}\s*\([^)]*\)\s*(?::[^{{;]*)?\{{)"
