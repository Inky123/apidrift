"""Tokens and brace structure of a Go source file, for :mod:`goparse`.

Go's lexical grammar is small enough to tokenise exactly where it matters here: comments, interpreted
strings, raw strings (no escapes, may span lines) and rune literals never leak braces or names into the
token stream, and the automatic semicolon rule gives statement boundaries without a parser. Every ``{``
is then classified as a statement block, a composite literal or a type body, using the rule the Go
grammar itself uses: between ``if``/``for``/``switch`` and its block a ``TypeName{`` literal cannot
appear unparenthesised, so the first such brace is the block.
"""

from __future__ import annotations

import bisect
import re

_TOKEN = re.compile(
    r"(?P<ws>[ \t\r]+)|(?P<nl>\n)|(?P<lc>//[^\n]*)|(?P<bc>/\*.*?\*/)|(?P<raw>`[^`]*`)"
    r'|(?P<str>"(?:\\.|[^"\\\n])*")|(?P<rune>\'(?:\\.|[^\'\\\n])+\')|(?P<id>[^\W\d]\w*)'
    r"|(?P<num>\.?\d(?:[\w.]|(?<=[eEpP])[+-])*)"
    r"|(?P<op>\.\.\.|<<=|>>=|&\^=|&\^|:=|[-+*/%&|^<>=!]=|&&|\|\||<-|\+\+|--|<<|>>|.)",
    re.S,
)
MASK = re.compile(r"//[^\n]*|/\*.*?\*/|`[^`]*`|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])+'", re.S)
ID, NUM, STR, OP, SEMI = "id", "num", "str", "op", ";"
BLOCK, LIT, TYPE = "block", "lit", "type"
HEADS = {"if", "for", "switch", "select"}
NO_SEMI = {  # keywords after which a newline does not end the statement
    "if", "else", "for", "switch", "select", "case", "func", "go", "defer", "var", "const", "type", "import",
    "package", "range", "map", "chan", "struct", "interface", "goto", "default",
}
ASSIGN_OPS = {"=", ":=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>=", "&^="}
OPEN = {"(": ")", "[": "]", "{": "}"}
CLOSE = {")", "]", "}"}


class GoSource:
    """One file's tokens (comments dropped, automatic semicolons added) and what each brace is."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.kind: list[str] = []
        self.tok: list[str] = []
        self.start: list[int] = []
        self._lex()
        self.n = len(self.tok)
        self.match = [-1] * self.n  # bracket partner
        self.parent = [-1] * self.n  # innermost enclosing open bracket
        self.brace: dict[int, str] = {}  # ``{`` -> BLOCK / LIT / TYPE
        self.head: dict[int, str] = {}  # block ``{`` -> func / if / for / switch / select / else / ""
        self.funcs: dict[int, int] = {}  # function body ``{`` -> its ``func`` token
        self._structure()
        self._lines = [m.end() for m in re.finditer(r"\n", text)]
        self._ids: dict[str, list[int]] | None = None

    def _lex(self) -> None:
        last_kind, last_tok = "", ""
        for m in _TOKEN.finditer(self.text):
            group = m.lastgroup
            if group in ("ws", "lc") or (group == "bc" and "\n" not in m.group()):
                continue
            if group in ("nl", "bc"):
                ends = last_kind in (NUM, STR) or (last_kind == ID and last_tok not in NO_SEMI)
                if ends or last_tok in (")", "]", "}", "++", "--"):
                    self._push(SEMI, ";", m.start())
                    last_kind, last_tok = SEMI, ";"
                continue
            text = m.group()
            kind = STR if group in ("raw", "str", "rune") else (SEMI if text == ";" else group)
            self._push(kind, text, m.start())
            last_kind, last_tok = kind, text
        if last_kind and last_kind != SEMI:
            self._push(SEMI, ";", len(self.text))

    def _push(self, kind: str, text: str, pos: int) -> None:
        self.kind.append(kind)
        self.tok.append(text)
        self.start.append(pos)

    def _structure(self) -> None:
        stack: list[int] = []
        state: dict[int, list] = {-1: [None, None]}  # open bracket -> [pending ``func`` token, pending header keyword]
        for i in range(self.n):
            kind, text = self.kind[i], self.tok[i]
            top = stack[-1] if stack else -1
            self.parent[i] = top
            st = state[top]
            if kind == ID:
                if text == "func":
                    st[0] = i
                elif text in HEADS:
                    st[1] = text
            elif kind == SEMI:
                st[0] = None  # a function type with no body
            elif kind == OP and text in OPEN:
                if text == "{":
                    self._classify(i, top, st)
                stack.append(i)
                state[i] = [None, None]
            elif kind == OP and text in CLOSE and stack and OPEN[self.tok[stack[-1]]] == text:
                opener = stack.pop()
                del state[opener]
                self.match[opener], self.match[i] = i, opener
                self.parent[i] = stack[-1] if stack else -1

    def _classify(self, i: int, top: int, st: list) -> None:
        pk, pt = (self.kind[i - 1], self.tok[i - 1]) if i else ("", "")
        head = None
        if pt in ("struct", "interface"):
            what = TYPE
        elif st[0] is not None:
            what, head = BLOCK, "func"
            self.funcs[i] = st[0]
            st[0] = None
        elif st[1] is not None:
            if (pk == ID and pt not in HEADS and self._type_before(i - 1)) or pt == "}":
                what = LIT  # ``range []T{...}`` / ``map[K]V{...}``: not the TypeName form, so allowed in a header
            else:
                what, head = BLOCK, st[1]
                st[1] = None
        elif top >= 0 and self.brace.get(top) == LIT:
            what = LIT
        elif (pk == ID and pt != "else") or pt in ("]", "}"):
            what = LIT
        else:
            what, head = BLOCK, ("else" if pt == "else" else "")
        self.brace[i] = what
        if head is not None:
            self.head[i] = head

    def _type_before(self, j: int) -> bool:
        """The (qualified) name ending at token ``j`` is the element type of ``[]T`` / ``[N]T`` / ``map[K]T``."""
        while j >= 2 and self.tok[j - 1] == "." and self.kind[j - 2] == ID:
            j -= 2
        while j >= 1 and self.tok[j - 1] == "*":
            j -= 1
        return j >= 1 and self.tok[j - 1] == "]"

    # --- positions ----------------------------------------------------------------

    def end(self, i: int) -> int:
        """Text offset just past token ``i`` (an automatic semicolon has no width)."""
        if self.kind[i] == SEMI and self.text[self.start[i] : self.start[i] + 1] != ";":
            return self.start[i]
        return self.start[i] + len(self.tok[i])

    def line(self, i: int) -> int:
        return bisect.bisect_right(self._lines, self.start[i]) + 1

    def ids(self, name: str) -> list[int]:
        """Token indices where identifier ``name`` appears."""
        if self._ids is None:
            self._ids = {}
            for i in range(self.n):
                if self.kind[i] == ID:
                    self._ids.setdefault(self.tok[i], []).append(i)
        return self._ids.get(name, [])

    # --- structure ------------------------------------------------------------------

    def block_of(self, i: int) -> int:
        """The innermost statement block around token ``i`` (its ``{``), -1 at file level."""
        p = self.parent[i]
        while p >= 0 and self.brace.get(p) != BLOCK:
            p = self.parent[p]
        return p

    def func_of(self, i: int) -> int:
        """The innermost function body around token ``i`` (its ``{``), -1 outside any function."""
        p = self.block_of(i)
        while p >= 0 and self.head.get(p) != "func":
            p = self.block_of(p)
        return p

    def region(self, i: int) -> tuple[int, int]:
        """Token span of the innermost block around ``i``; inside ``switch``/``select`` only its own clause,
        because the clauses of one switch exclude each other."""
        block = self.block_of(i)
        if block < 0:
            return -1, self.n
        lo, hi = block, self.match[block] if self.match[block] >= 0 else self.n
        if self.head.get(block) in ("switch", "select"):
            j = block + 1
            while j < hi:
                if self.kind[j] == ID and self.tok[j] in ("case", "default"):
                    if j <= i:
                        lo = j
                    else:
                        hi = j
                        break
                j = self._skip(j)
        return lo, hi

    def loops_around(self, i: int) -> list[int]:
        """``for`` blocks around token ``i`` inside its own function, innermost first."""
        out, p = [], self.block_of(i)
        while p >= 0 and self.head.get(p) != "func":
            if self.head.get(p) == "for":
                out.append(p)
            p = self.block_of(p)
        return out

    def _skip(self, j: int) -> int:
        """The token after ``j``, jumping over a bracketed group that opens at ``j``."""
        if self.tok[j] in OPEN and self.kind[j] == OP and self.match[j] >= 0:
            return self.match[j] + 1
        return j + 1

    def stmt_end(self, i: int) -> int:
        """Index of the token that ends the statement or element holding ``i``: its ``;`` or closing bracket."""
        j = i
        while j < self.n:
            if self.kind[j] == SEMI or (self.kind[j] == OP and self.tok[j] in CLOSE):
                return j
            j = self._skip(j)
        return self.n

    def starts_stmt(self, i: int) -> bool:
        """Token ``i`` begins a statement of a block (after ``;``, ``{``, a ``case x:`` label or ``if``/``for``)."""
        if i == 0:
            return True
        p = self.parent[i]
        if p >= 0 and self.brace.get(p) != BLOCK:
            return False
        pk, pt = self.kind[i - 1], self.tok[i - 1]
        return pk == SEMI or pt in ("{", ":", "if", "for", "switch", "else")

    def split(self, a: int, b: int) -> list[tuple[int, int]]:
        """Top-level comma-separated token spans of ``[a, b)`` (empty spans dropped)."""
        out, lo, j = [], a, a
        while j < b:
            if self.kind[j] == OP and self.tok[j] == ",":
                out.append((lo, j))
                lo = j + 1
            j = self._skip(j)
        out.append((lo, min(j, b)))
        return [(x, y) for x, y in out if self._real(x, y)]

    def _real(self, a: int, b: int) -> bool:
        return any(self.kind[j] != SEMI for j in range(a, b))

    def trim(self, a: int, b: int) -> tuple[int, int]:
        """``[a, b)`` without automatic semicolons at either end."""
        while a < b and self.kind[a] == SEMI:
            a += 1
        while b > a and self.kind[b - 1] == SEMI:
            b -= 1
        return a, b
