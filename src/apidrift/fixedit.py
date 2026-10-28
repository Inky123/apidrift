"""Find the key tokens a documented rename edits, and apply the edits to the original text.

Sites are found only by walking a call site's arguments: the inline literal, every binding of an
argument variable that can reach the call, and the mutation statements of that key between them.
Nothing is found by searching for the key name, so a literal that feeds another endpoint is never
touched. Offsets are code-point indices into the ORIGINAL text; ``jsparse.Source`` rewrites are
length-preserving and Python byte columns are converted here.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from . import jsparse, pyparse, scanner
from .jsparse import IDENT, QUOTES, RUBY_KEYWORD, Fields, Source
from .scanner import Usage
from .sdkmap import SdkIndex

PY_MASK = re.compile(
    r"'''(?:\\.|[^\\])*?'''|\"\"\"(?:\\.|[^\\])*?\"\"\"|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|#[^\n]*", re.S
)
NEWLINE = re.compile(r"\r\n|\r|\n")


@dataclass(frozen=True)
class Edit:
    start: int
    end: int
    text: str
    key: str
    shorthand: bool = False  # ``{ k }`` becomes ``new: k``: the old name stays as the value


@dataclass
class Sites:
    edits: list[Edit] = field(default_factory=list)
    found_call: bool = True
    incomplete: bool = False  # an argument, binding or write the walk could not read
    unsupported: bool = False  # the key was seen but its token cannot be renamed in place (``r'k'``, escapes)

    def take(self, edits: list[Edit | None]) -> None:
        for e in edits:
            if e is None:
                self.unsupported = True
            else:
                self.edits.append(e)


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


@dataclass(frozen=True)
class Token:
    pos: int
    line: int
    quoted: bool  # the whole content of a string literal (``'k'``)


def tokens(text: str, language: str, key: str) -> list[Token]:
    """Where ``key`` appears as a token in code: outside strings and comments, or as the whole content of
    a string literal (a quoted key)."""
    mask_re = PY_MASK if language == "python" else (jsparse.RUBY_MASK if language == "ruby" else jsparse.JS_MASK)
    spans = [(m.start(), m.end()) for m in mask_re.finditer(text)]
    quoted = {a for a, b in spans if text[a] in "'\"" and text[a + 1 : b - 1] == key}
    out: list[Token] = []
    k = 0
    for m in re.finditer(rf"(?<![\w$]){re.escape(key)}(?![\w$])", text):
        while k < len(spans) and spans[k][1] <= m.start():
            k += 1
        covered = k < len(spans) and spans[k][0] <= m.start() < spans[k][1]
        if not covered:
            out.append(Token(m.start(), text.count("\n", 0, m.start()) + 1, False))
        elif spans[k][0] in quoted and m.start() == spans[k][0] + 1:
            out.append(Token(m.start(), text.count("\n", 0, m.start()) + 1, True))
    return out


def token_lines(text: str, language: str, key: str) -> list[int]:
    return [t.line for t in tokens(text, language, key)]


def variable_uses(text: str, language: str, found: list[Token]) -> set[int]:
    """Positions among ``found`` that are plain identifier uses (a variable named like the key), not keys:
    unquoted, not after ``.``/``:``/``[``, not before ``:``/``=>``, and not a shorthand entry of an object."""
    if language == "python" or not found:
        return set()
    openers = _opener_at(text, [t.pos for t in found if not t.quoted], ruby=language == "ruby")
    out: set[int] = set()
    for t in found:
        if t.quoted:
            continue
        before = text[:t.pos].rstrip(" \t")
        after = text[t.pos + len(_key_at(text, t.pos)):].lstrip(" \t")
        if before.endswith((".", "[")) or text[t.pos - 1 : t.pos] == ":":
            continue  # member access, computed access, Ruby symbol
        if after.startswith((":", "=>")) and not after.startswith("::"):
            continue  # a key
        is_value = before.endswith(":") and text[t.pos - 1 : t.pos] in (" ", "\t")  # ``new: payment_method_types``
        if not is_value and after[:1] in (",", "}") and openers.get(t.pos) == "{":
            continue  # a shorthand entry of another object
        out.add(t.pos)
    return out


def _key_at(text: str, pos: int) -> str:
    m = re.compile(r"[\w$]+").match(text, pos)
    return m.group(0) if m else ""


def _opener_at(text: str, positions: list[int], *, ruby: bool) -> dict[int, str]:
    """The innermost unclosed bracket before each position (strings and comments skipped)."""
    src = Source(text, ruby=ruby)
    want, out, stack, k, i = sorted(positions), {}, [], 0, 0
    while i < src.n and k < len(want):
        while k < len(want) and want[k] <= i:
            out[want[k]] = stack[-1] if stack else ""
            k += 1
        j = src.skip_space(i)
        if j > i:
            i = j
            continue
        c = text[i]
        if c in QUOTES:
            i = src.skip_string(i)
        elif c in jsparse.OPENERS:
            stack.append(c)
            i += 1
        elif c in jsparse.CLOSERS:
            if stack:
                stack.pop()
            i += 1
        else:
            i += 1
    for pos in want[k:]:
        out[pos] = stack[-1] if stack else ""
    return out


class Locator:
    """Edit sites for the usages of one file, parsing it once."""

    def __init__(self, text: str, language: str, index: SdkIndex) -> None:
        self.text, self.language = text, language
        resolve = lambda chain: scanner._resolve(index, chain)  # noqa: E731
        if language == "python":
            self.py_calls, _ = pyparse.calls(text, resolve)
            self.offsets = _Offsets(text)
        else:
            self.js_calls, _ = jsparse.calls(text, index, ruby=language == "ruby", resolve=resolve)

    def sites(self, usage: Usage, target: str, replacement: str) -> Sites:
        if self.language == "python":
            return self._py(usage, target, replacement)
        return self._js(usage, target, replacement)

    # --- JavaScript / TypeScript / Ruby ---------------------------------------

    def _js(self, usage: Usage, target: str, replacement: str) -> Sites:
        matches = [
            c for c in self.js_calls
            if c.line == usage.line and c.call == usage.call and c.resolved.path == usage.path
            and c.resolved.method == usage.method
        ]
        if not matches:
            return Sites(found_call=False)
        c, out, ruby = matches[0], Sites(), self.language == "ruby"
        src, text = c.args_src, c.args_src.text
        for k, (a, b) in enumerate(c.spans):
            i = src.skip_space(a)
            if i >= b:
                continue
            ch = text[i]
            if ch == "{":
                out.take(_literal_edits(src, i, target, replacement))
                after = src.skip_space(src.skip_balanced(i))
                if text.startswith(jsparse.MERGERS, after):
                    out.incomplete = True
            elif text.startswith("...", i) or (ruby and text.startswith("**", i)):
                out.incomplete = True
            elif ruby and RUBY_KEYWORD.match(text, i):
                sub = Fields()
                src.object_fields(i, "", sub, closer=")")
                out.take(_span_edits(sub, text, target, replacement))
                break
            elif ch in QUOTES or ch.isdigit():
                continue
            else:
                m = IDENT.match(text, i)
                name = m.group(0) if m else ""
                if not name or text[i:b].strip() != name:
                    out.incomplete = True
                    continue
                self._js_variable(c, name, i, k == 0 and "{" in c.resolved.path, target, replacement, out)
        return out

    def _js_variable(self, c: jsparse.Call, name: str, at: int, maybe_id: bool, target: str, replacement: str, out: Sites) -> None:
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
                out.take(_literal_edits(src, binding.literal, target, replacement))
        stop = min(b.pos for b in bindings)
        for m in c.bindings.mutations(name, stop, at):
            if m.key == target:
                out.take([_checked(src.text, m.key_start, m.key_end, target, replacement)])
            if m.literal is not None:
                out.take(_literal_edits(src, m.literal, target, replacement))
            elif m.key is None:
                out.incomplete = True  # params.merge!(other), Object.assign(params, other)
        if any(t.key == target for t in c.bindings.touches(name, stop, at)):
            out.incomplete = True  # the key is read, deleted or changed in place: not a plain rename

    # --- Python -----------------------------------------------------------------

    def _py(self, usage: Usage, target: str, replacement: str) -> Sites:
        matches = [
            c for c in self.py_calls
            if c.node.lineno == usage.line and c.call == usage.call and c.resolved.path == usage.path
            and c.resolved.method == usage.method
        ]
        if not matches:
            return Sites(found_call=False)
        c, out = matches[0], Sites()
        for kw in c.node.keywords:
            if kw.arg == target:
                out.take([self._kw_edit(kw, target, replacement)])
            elif kw.arg is None or kw.arg == "params":
                self._py_value(c, kw.value, target, replacement, out)
        for arg in c.node.args:
            if isinstance(arg, ast.Starred):
                out.incomplete = True
            elif isinstance(arg, ast.Dict):
                out.take(self._dict_edits(arg, target, replacement))
        return out

    def _py_value(self, c: pyparse.PyCall, value: ast.expr, target: str, replacement: str, out: Sites) -> None:
        if isinstance(value, (ast.Dict, ast.Call)):
            out.take(self._dict_edits(value, target, replacement))
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
                out.take(self._dict_edits(bound, target, replacement))
        stop = min(b[0] for b in bindings)
        for _, keys, node in c.scopes.mutation_nodes(name, scope, stop, c.node.lineno):
            if target in keys:
                out.take(self._mutation_edits(node, target, replacement))
            elif not keys:
                out.incomplete = True  # params.update(other), params.pop(...)
        if target in c.scopes.touched_keys(name, scope, stop, c.node.lineno):
            out.incomplete = True  # params['k'].append(x), del params['k'], params.get('k')

    def _dict_edits(self, node: ast.expr, target: str, replacement: str) -> list[Edit | None]:
        edits: list[Edit | None] = []
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value == target:
                    edits.append(self._constant_edit(key, target, replacement))
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == target:
                    edits.append(self._kw_edit(kw, target, replacement))
        return edits

    def _mutation_edits(self, node: ast.AST, target: str, replacement: str) -> list[Edit | None]:
        edits: list[Edit | None] = []
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and t.slice.value == target:
                    edits.append(self._constant_edit(t.slice, target, replacement))
                elif isinstance(t, ast.Attribute) and t.attr == target:
                    end = self.offsets.at(t.end_lineno, t.end_col_offset)
                    edits.append(_checked(self.text, end - len(target), end, target, replacement))
        elif isinstance(node, ast.AugAssign):
            edits += self._dict_edits(node.value, target, replacement)
        elif isinstance(node, ast.Call):
            edits += self._dict_edits(node, target, replacement)
            if node.args and isinstance(node.args[0], ast.Dict):
                edits += self._dict_edits(node.args[0], target, replacement)
            elif node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == target:
                edits.append(self._constant_edit(node.args[0], target, replacement))
        return edits

    def _constant_edit(self, node: ast.Constant, target: str, replacement: str) -> Edit | None:
        start = self.offsets.at(node.lineno, node.col_offset)
        end = self.offsets.at(node.end_lineno, node.end_col_offset)
        if self.text[start] not in "'\"" or self.text[end - 1] != self.text[start]:
            return None  # prefixed or triple-quoted: not a plain key token
        return _checked(self.text, start + 1, end - 1, target, replacement)

    def _kw_edit(self, kw: ast.keyword, target: str, replacement: str) -> Edit | None:
        start = self.offsets.at(kw.lineno, kw.col_offset)
        return _checked(self.text, start, start + len(target), target, replacement)


class _Offsets:
    """Python ``ast`` positions (1-based line, UTF-8 byte column) to code-point offsets in the text."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.starts = [0] + [m.end() for m in NEWLINE.finditer(text)]

    def at(self, lineno: int, byte_col: int) -> int:
        start = self.starts[lineno - 1]
        end = self.starts[lineno] if lineno < len(self.starts) else len(self.text)
        line = self.text[start:end].encode("utf-8")
        return start + len(line[:byte_col].decode("utf-8", "ignore"))


def _literal_edits(src: Source, open_idx: int, target: str, replacement: str) -> list[Edit | None]:
    return _span_edits(jsparse.parse_object(src, open_idx + 1), src.text, target, replacement)


def _span_edits(fields: Fields, text: str, target: str, replacement: str) -> list[Edit | None]:
    """Edits for the top-level key ``target`` wherever ``fields`` saw it (nested paths never match); None
    stands for a key token that cannot be renamed in place."""
    edits: list[Edit | None] = []
    for span in fields.spans.get(target, ()):
        if span.kind in ("quoted", "rocket_quoted"):
            edits.append(_checked(text, span.key_start + 1, span.key_end - 1, target, replacement))
        elif span.kind == "symbol":
            edits.append(_checked(text, span.key_start + 1, span.key_end, target, replacement))
        elif span.kind == "shorthand":
            edits.append(Edit(span.key_start, span.key_end, f"{replacement}: {target}", target, shorthand=True))
        else:
            edits.append(_checked(text, span.key_start, span.key_end, target, replacement))
    return edits


def _checked(text: str, start: int, end: int, target: str, replacement: str) -> Edit | None:
    """An edit only when the slice really is the bare key (escapes or a mis-measured span yield None)."""
    return Edit(start, end, replacement, target) if text[start:end] == target else None
