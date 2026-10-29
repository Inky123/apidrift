"""Find the entries a documented migration edits, and apply the edits to the original text.

Sites are found only by walking a call site's arguments: the inline literal, every binding of an
argument variable that can reach the call, and the mutation statements of that key between them.
Nothing is found by searching for the key name, so a literal that feeds another endpoint is never
touched. A dotted target is followed one nested literal at a time. Offsets are code-point indices into
the ORIGINAL text; ``jsparse.Source`` rewrites are length-preserving and Python byte columns are converted
here. What each entry becomes (rename, rename_first, delete, wrap_type) is built in :mod:`fixvalue`.
"""

from __future__ import annotations

import ast
import re
import tokenize
from dataclasses import dataclass, field

from . import fixvalue, jsparse, pyparse, scanner
from .fixtokens import _Offsets
from .fixvalue import Edit, Entry
from .jsparse import IDENT, QUOTES, RUBY_KEYWORD, Fields, Source
from .migrations import Migration
from .scanner import Usage
from .sdkmap import SdkIndex

BAD_HEAD = re.compile(r"-?\.?\d|(?:null|undefined|nil|true|false|NaN)\b")  # a number or keyword literal


@dataclass
class Sites:
    edits: list[Edit] = field(default_factory=list)
    found_call: bool = True
    incomplete: bool = False  # an argument, binding or write the walk could not read
    unsupported: bool = False  # the key was seen but its token cannot be edited in place (``r'k'``, escapes)
    why: str | None = None  # the first action-specific reason (unsupported_value, would_empty_literal, ...)
    leave: bool = False  # a value already has the new shape

    def take(self, edits: list[Edit | None]) -> None:
        for e in edits:
            self.add(e)

    def add(self, result: Edit | str | None) -> None:
        if result is None:
            self.unsupported = True
        elif result == fixvalue.LEAVE:
            self.leave = True
        elif isinstance(result, str):
            self.why = self.why or result
        else:
            self.edits.append(result)


def apply(text: str, edits: list[Edit]) -> str:
    """The text with non-overlapping ``edits`` applied, highest offset first."""
    ordered = sorted(set(edits), key=lambda e: e.start, reverse=True)
    out, last = text, len(text) + 1
    for e in ordered:
        if e.end > last:
            raise ValueError("overlapping edits")
        out = out[: e.start] + e.text + out[e.end :]
        last = e.start
    return out


class Locator:
    """Edit sites for the usages of one file, parsing it once."""

    def __init__(self, text: str, language: str, index: SdkIndex) -> None:
        self.text, self.language, self.ruby = text, language, language == "ruby"
        self.quote = '"' if text.count('"') > text.count("'") else "'"  # the file's usual string quote
        resolve = lambda chain: scanner._resolve(index, chain)  # noqa: E731
        self._tokens: fixvalue.PyTokens | None | bool = False
        if language == "python":
            self.py_calls, _ = pyparse.calls(text, resolve)
            self.offsets = _Offsets(text)
        else:
            self.js_calls, _ = jsparse.calls(text, index, ruby=self.ruby, resolve=resolve)
            self.bindings = self.js_calls[0].bindings if self.js_calls else None

    def sites(self, usage: Usage, migration: Migration) -> Sites:
        if self.language == "python":
            return self._py(usage, migration)
        return self._js(usage, migration)

    # --- JavaScript / TypeScript / Ruby ---------------------------------------

    def _js(self, usage: Usage, m: Migration) -> Sites:
        matches = [
            c for c in self.js_calls
            if c.line == usage.line and c.call == usage.call and c.resolved.path == usage.path
            and c.resolved.method == usage.method
        ]
        if not matches:
            return Sites(found_call=False)
        c, out = matches[0], Sites()
        src, text = c.args_src, c.args_src.text
        for k, (a, b) in enumerate(c.spans):
            i = src.skip_space(a)
            if i >= b:
                continue
            ch = text[i]
            if ch == "{":
                self._js_literal(src, i + 1, m, out, literal=True)
                after = src.skip_space(src.skip_balanced(i))
                if text.startswith(jsparse.MERGERS, after):
                    out.incomplete = True
            elif text.startswith("...", i) or (self.ruby and text.startswith("**", i)):
                out.incomplete = True
            elif self.ruby and RUBY_KEYWORD.match(text, i):
                self._js_literal(src, i, m, out, literal=False)
                break
            elif ch in QUOTES or ch.isdigit():
                continue
            else:
                match = IDENT.match(text, i)
                name = match.group(0) if match else ""
                if not name or text[i:b].strip() != name:
                    out.incomplete = True
                    continue
                self._js_variable(c, name, i, k == 0 and "{" in c.resolved.path, m, out)
        return out

    def _js_literal(self, src: Source, start: int, m: Migration, out: Sites, *, literal: bool) -> None:
        """Entries of ``m.target`` in the object/hash body at ``start`` (Ruby keyword arguments unless ``literal``),
        following a dotted target through nested literals."""
        fields = Fields()
        src.object_fields(start, "", fields, closer="}" if literal else ")")
        parent = m.target.rpartition(".")[0]
        parents = fields.spans.get(parent, []) if parent else []
        for p in parents:
            vs = fixvalue.js_value_start(src, p.key_end)
            if p.kind == "shorthand" or vs >= src.n or src.text[vs] != "{":
                out.incomplete = True  # the nested object is a variable or an expression
        spans = fields.spans.get(m.target, [])
        if spans and m.action != "rename" and fixvalue.js_unsafe(src, start - 1 if literal else start, src.skip_scalar(start, "")):
            out.add(fixvalue.UNSUPPORTED)  # a regex, generic or heredoc in this literal: the spans may be misread
            return
        for span in spans:
            cont_start, cont_literal = start, literal
            if parent:
                holders = [p for p in parents if p.key_start < span.key_start < p.entry_end]
                if not holders:
                    out.incomplete = True
                    continue
                holder = max(holders, key=lambda p: p.key_start)
                cont_start, cont_literal = fixvalue.js_value_start(src, holder.key_end) + 1, True
            out.add(self._js_entry(src, span, m, cont_start, cont_literal))

    def _js_entry(self, src: Source, span: jsparse.Span, m: Migration, cont_start: int, cont_literal: bool):
        if span.kind in ("quoted", "rocket_quoted"):
            ns, ne = span.key_start + 1, span.key_end - 1
        elif span.kind == "symbol":
            ns, ne = span.key_start + 1, span.key_end
        else:
            ns, ne = span.key_start, span.key_end
        shorthand = span.kind == "shorthand"
        if shorthand:
            vs, ve = ns, ne
        else:
            vs = fixvalue.js_value_start(src, span.key_end)
            ve = fixvalue.js_value_end(src, vs)
        new = None
        if m.action in ("rename_first", "wrap_type"):
            bad = lambda name, action: self._js_bad_binding(name, vs, action)  # noqa: E731
            new = fixvalue.js_rewrite(m, self.text, src, vs, ve, ruby=self.ruby, quote=self.quote, bad_name=bad)
        entry = Entry(span.key_start, ns, ne, ne if shorthand else vs, ve, shorthand)
        cont = fixvalue.js_container(src, cont_start, literal=cont_literal) if m.action == "delete" else None
        return fixvalue.entry_edit(self.text, entry, m, new, cont, comment="#" if self.ruby else "//")

    def _js_bad_binding(self, name: str, at: int, action: str) -> bool:
        """A visible binding of ``name`` holds a value the action must not rewrite blindly: an object (wrapping
        it again), a number or keyword literal, and for rename_first a string, template or set."""
        for b in self.bindings.reaching(name, at) if self.bindings else ():
            if b.literal is not None:
                return True
            head = self.text[b.value : b.value + 16] if b.value >= 0 else ""
            if head.startswith(("{", "Object.freeze", "Object.assign")) or BAD_HEAD.match(head):
                return True
            if action == "rename_first" and head.startswith(("'", '"', "`", "new Set", "Set.new", "%")):
                return True
        return False

    def _js_variable(self, c: jsparse.Call, name: str, at: int, maybe_id: bool, m: Migration, out: Sites) -> None:
        bindings = c.bindings.reaching(name, at)
        if maybe_id and not any(b.literal is not None for b in bindings):
            return  # the id slot of a ``/{id}`` path: a plain value, not a parameters object
        if not bindings:
            out.incomplete = True
            return
        src = c.bindings.src
        for binding in bindings:
            if binding.literal is None:
                out.incomplete = True
            else:
                self._js_literal(src, binding.literal + 1, m, out, literal=True)
        stop = min(b.pos for b in bindings)
        head = m.target.split(".")[0]
        for mu in c.bindings.mutations(name, stop, at):
            if mu.key == head:
                if m.action == "rename" and head == m.target:
                    out.add(_checked(self.text, mu.key_start, mu.key_end, m.target, m.replacement))
                else:
                    out.incomplete = True  # a value or deletion written by a statement: not edited in v2
            if mu.literal is not None:
                self._js_literal(src, mu.literal + 1, m, out, literal=True)
            elif mu.key is None:
                out.incomplete = True  # params.merge!(other), Object.assign(params, other)
        if any(t.key == head for t in c.bindings.touches(name, stop, at)):
            out.incomplete = True  # the key is read, deleted or changed in place: not a plain edit
        if m.action != "rename" and c.bindings.escapes(name, at):
            out.incomplete = True  # the literal is also used elsewhere: editing it would change that use too

    # --- Python -----------------------------------------------------------------

    def _py(self, usage: Usage, m: Migration) -> Sites:
        matches = [
            c for c in self.py_calls
            if c.node.lineno == usage.line and c.call == usage.call and c.resolved.path == usage.path
            and c.resolved.method == usage.method
        ]
        if not matches:
            return Sites(found_call=False)
        c, out, segs = matches[0], Sites(), m.target.split(".")
        self._call = c
        self._py_walk(c.node, segs, m, out)
        for kw in c.node.keywords:
            if kw.arg is None or kw.arg == "params":
                self._py_value(c, kw.value, segs, m, out)
        for arg in c.node.args:
            if isinstance(arg, ast.Starred):
                out.incomplete = True
            elif isinstance(arg, ast.Dict):
                self._py_walk(arg, segs, m, out)
        return out

    def _py_value(self, c: pyparse.PyCall, value: ast.expr, segs: list[str], m: Migration, out: Sites) -> None:
        if _is_dict(value):
            self._py_walk(value, segs, m, out)
            return
        if not isinstance(value, ast.Name):
            out.incomplete = True
            return
        name = value.id
        bindings, scope, dominated = c.scopes.reaching(name, c.node, c.fn)
        if not bindings or c.scopes.shadowed_by_parameter(name, c.fn, scope, dominated):
            out.incomplete = True
            return
        if c.scopes.rebound_in_loop(name, c.node, scope):
            out.incomplete = True
            return
        for _, bound, _ in bindings:
            if pyparse._as_dict(bound, pyparse._NOTHING) is None:
                out.incomplete = True
            else:
                self._py_walk(bound, segs, m, out)
        stop = min(b[0] for b in bindings)
        for _, keys, node in c.scopes.mutation_nodes(name, scope, stop, c.node.lineno):
            if segs[0] in keys:
                if m.action == "rename" and len(segs) == 1:
                    out.take(self._mutation_edits(node, m.target, m.replacement))
                else:
                    out.incomplete = True  # params['k'] = v / params.update(k=v): a statement, not edited in v2
            elif not keys:
                out.incomplete = True  # params.update(other), params.pop(...)
        if segs[0] in c.scopes.touched_keys(name, scope, stop, c.node.lineno):
            out.incomplete = True  # params['k'].append(x), del params['k'], params.get('k')
        if m.action != "rename" and c.scopes.escapes(name, scope, value):
            out.incomplete = True  # the dict is also used elsewhere: editing it would change that use too

    def _py_walk(self, node: ast.expr, segs: list[str], m: Migration, out: Sites) -> None:
        """Entries named ``segs[0]`` in a dict literal, ``dict(...)`` or the SDK call's keywords; deeper
        segments only through nested literals."""
        pairs = self._py_pairs(node)
        for name, start, name_span, value in pairs:
            if name is None or name != segs[0]:
                continue
            if len(segs) > 1:
                if _is_dict(value):
                    self._py_walk(value, segs[1:], m, out)
                else:
                    out.incomplete = True  # the nested dict is a variable or an expression
                continue
            out.add(self._py_entry(node, pairs, start, name_span, value, m))

    def _py_pairs(self, node: ast.expr) -> list[tuple[str | None, int, tuple[int, int] | None, ast.expr]]:
        """(key name, entry start, bare-name span, value) for every entry, in source order."""
        out = []
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if k is None:  # ``**base``: starts at its ``**`` token
                    before = self.tokens.before(self._start(v)) if self.tokens else None
                    out.append((None, before[0] if before and before[1] == "**" else self._start(v), None, v))
                    continue
                s, e = self._start(k), self._end(k)
                name = k.value if isinstance(k, ast.Constant) and isinstance(k.value, str) else None
                plain = self.text[s] in "'\"" and self.text[e - 1] == self.text[s]
                out.append((name, s, (s + 1, e - 1) if plain else None, v))
            return out
        if isinstance(node, ast.Call):
            out = [(None, self._start(a), None, a) for a in node.args]
            for kw in node.keywords:
                s = self._start(kw)
                out.append((kw.arg, s, (s, s + len(kw.arg)) if kw.arg else None, kw.value))
        return sorted(out, key=lambda p: p[1])

    def _py_entry(self, node, pairs, start: int, name_span, value: ast.expr, m: Migration):
        if name_span is None:
            return None  # prefixed or triple-quoted key: not a plain key token
        vs, ve = self._start(value), self._end(value)
        new = None
        if m.action in ("rename_first", "wrap_type"):
            text_of = lambda n: self.text[self._start(n) : self._end(n)]  # noqa: E731
            new = fixvalue.py_rewrite(m, value, text_of, quote=self.quote, bad_name=self._py_bad_binding)
        cont = None
        if m.action == "delete":
            opener = self._start(node) if isinstance(node, ast.Dict) else (
                self.tokens.opener_after(self._end(node.func)) if self.tokens else None
            )
            cont = self.tokens.container(opener, [p[1] for p in pairs], literal=_is_dict(node)) if (
                self.tokens and opener is not None
            ) else None
            if cont is None:
                return fixvalue.UNSUPPORTED
        entry = Entry(start, name_span[0], name_span[1], vs, ve)
        return fixvalue.entry_edit(self.text, entry, m, new, cont, comment="#")

    def _py_bad_binding(self, name: str, action: str) -> bool:
        """Python counterpart of :meth:`_js_bad_binding`, from the bindings that reach the call."""
        c = self._call
        bindings, _, _ = c.scopes.reaching(name, c.node, c.fn)
        for _, bound, _ in bindings:
            if _is_dict(bound) or isinstance(bound, (ast.Set, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.JoinedStr)):
                return True
            if isinstance(bound, ast.Constant) and (action == "rename_first" or not isinstance(bound.value, str)):
                return True
            if isinstance(bound, ast.UnaryOp) and isinstance(bound.operand, ast.Constant):
                return True
        return False

    @property
    def tokens(self) -> fixvalue.PyTokens | None:
        if self._tokens is False:
            try:
                self._tokens = fixvalue.PyTokens(self.text, self.offsets.starts)
            except (tokenize.TokenError, SyntaxError, IndexError):
                self._tokens = None
        return self._tokens  # type: ignore[return-value]

    def _start(self, node: ast.AST) -> int:
        return self.offsets.at(node.lineno, node.col_offset)

    def _end(self, node: ast.AST) -> int:
        return self.offsets.at(node.end_lineno, node.end_col_offset)

    def _mutation_edits(self, node: ast.AST, target: str, replacement: str) -> list[Edit | None]:
        edits: list[Edit | None] = []
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value == target:
                    edits.append(self._constant_edit(t.slice, target, replacement))
                elif isinstance(t, ast.Attribute) and t.attr == target:
                    end = self._end(t)
                    edits.append(_checked(self.text, end - len(target), end, target, replacement))
        elif isinstance(node, ast.AugAssign):
            edits += self._dict_renames(node.value, target, replacement)
        elif isinstance(node, ast.Call):
            edits += self._dict_renames(node, target, replacement)
            if node.args and isinstance(node.args[0], ast.Dict):
                edits += self._dict_renames(node.args[0], target, replacement)
            elif node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == target:
                edits.append(self._constant_edit(node.args[0], target, replacement))
        return edits

    def _dict_renames(self, node: ast.expr, target: str, replacement: str) -> list[Edit | None]:
        edits: list[Edit | None] = []
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value == target:
                    edits.append(self._constant_edit(key, target, replacement))
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == target:
                    start = self._start(kw)
                    edits.append(_checked(self.text, start, start + len(target), target, replacement))
        return edits

    def _constant_edit(self, node: ast.Constant, target: str, replacement: str) -> Edit | None:
        start, end = self._start(node), self._end(node)
        if self.text[start] not in "'\"" or self.text[end - 1] != self.text[start]:
            return None  # prefixed or triple-quoted: not a plain key token
        return _checked(self.text, start + 1, end - 1, target, replacement)


def _is_dict(node: ast.AST) -> bool:
    return isinstance(node, ast.Dict) or (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict" and not node.args
    )


def _checked(text: str, start: int, end: int, target: str, replacement: str) -> Edit | None:
    """A rename edit only when the slice really is the bare key (escapes or a mis-measured span yield None)."""
    return Edit(start, end, replacement, target) if text[start:end] == target else None
