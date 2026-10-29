"""Where a key appears as a token in code, and which of those tokens are plain variable uses or reads.

The fixer's oracle counts these before and after an edit (:mod:`fixer`), so they deliberately use a
different reader from the one that finds edit sites: a mask of strings and comments, not the walk.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from . import jsparse
from .jsparse import QUOTES, Source

PY_MASK = re.compile(
    r"'''(?:\\.|[^\\])*?'''|\"\"\"(?:\\.|[^\\])*?\"\"\"|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|#[^\n]*", re.S
)
NEWLINE = re.compile(r"\r\n|\r|\n")
KEY_AFTER = re.compile(r"[ \t]*(?:=>|:(?!:))")
PY_KWARG_AFTER = re.compile(r"[ \t]*=(?!=)")


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
    unquoted, not after ``.``/``:``/``[``, not before ``:``/``=>``, and not a shorthand entry of an object.
    Python: the positions of ``Name`` and parameter nodes with that name."""
    if not found:
        return set()
    if language == "python":
        return _py_names(text, {t.pos for t in found if not t.quoted})
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


def _py_names(text: str, positions: set[int]) -> set[int]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    offsets = _Offsets(text)
    return {
        p for node in ast.walk(tree) if isinstance(node, (ast.Name, ast.arg))
        if (p := offsets.at(node.lineno, node.col_offset)) in positions
    }


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


def accessed(text: str, language: str, key: str, t: Token) -> bool:
    """The token is a read or a write through another object (``p.k``, ``p['k']``, ``p[:k]``, ``get('k')``),
    not a key in a literal or a variable named like the key."""
    end = t.pos + len(key) + (1 if t.quoted else 0)
    if KEY_AFTER.match(text, end) or (language == "python" and not t.quoted and PY_KWARG_AFTER.match(text, end)):
        return False  # ``k:`` / ``'k':`` / ``:k =>`` / ``k=`` in a call
    if t.quoted:
        return True  # a string naming the key, not as a key: a lookup
    before = text[max(0, t.pos - 3) : t.pos]
    if before.endswith(".") and not before.endswith("..."):
        return True
    return before.endswith("[") or (language == "ruby" and before.endswith(":") and not before.endswith("::"))


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
