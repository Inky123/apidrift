"""PHP variables: which array literal ``$params`` holds at a call, and everything else done to it.

PHP scoping is simple and strict: a variable lives in one function body (a closure sees outer variables
only through ``use``), so a binding counts only when it is in the same body as the call. Every plain
occurrence of a variable is classified once: an assignment, a write of one key (``$p['k'] = v``), an
in-place change, a write this module cannot read (a reference, ``foreach ... as``, destructuring,
``global``, by-reference built-ins) or a plain use. :meth:`Bindings.reaching` answers with a synthetic,
literal-less binding whenever the value at the call is not knowable from what is written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .phplex import STRING, Lex

# These run over whole files, some of them damaged: no pattern may hold two runs of whitespace that can
# trade characters with each other, or a long blank stretch costs its length squared.
FUNC = re.compile(r"(?<![\w$>:\\])function\b\s*(?:&\s*)?(?:[A-Za-z_]\w*\s*)?\(")
ARROW = re.compile(r"(?<![\w$>:\\])fn\s*(?:&\s*)?\(")
USE = re.compile(r"use\s*\(")
CLASS = re.compile(r"(?<![\w$>:\\])(?:class|trait|interface|enum)\b")
BRACE_OR_END = re.compile(r"[{;]")
HEADER_REACH = 4000  # how far past ``class`` its ``{`` is looked for
VAR = re.compile(r"\$([A-Za-z_]\w*)")
WORD_BEFORE = re.compile(r"([A-Za-z_]\w*)$")
OP_ASSIGN = re.compile(r"(?:\*\*|<<|>>|\?\?|[-+*/.%&|^])=|\+\+|--")
LOOPS = {"for", "foreach", "while", "do"}
BY_REF = {  # built-ins and Laravel helpers that change their first argument in place
    "unset", "array_push", "array_unshift", "array_pop", "array_shift", "array_splice", "sort", "rsort", "usort",
    "uasort", "uksort", "ksort", "krsort", "asort", "arsort", "shuffle", "array_walk", "array_walk_recursive",
    "array_multisort", "settype", "data_set", "data_fill", "data_forget", "array_set", "array_forget", "set",
    "forget", "pull",
}
ALT_SYNTAX = re.compile(r"\b(?:endif|endforeach|endwhile|endfor|endswitch)\b")
UNBOUNDED = re.compile(  # code that can create or change a variable without naming it
    r"\b(?:extract|eval|parse_str)\s*\(|\b(?:include|include_once|require|require_once)\b|\$\$|\$\{"
)
READS_ALL = re.compile(r"\b(?:compact|get_defined_vars)\s*\(")
BIND, WRITE, INPLACE, OPAQUE, USED = "bind", "write", "inplace", "opaque", "use"


@dataclass(frozen=True)
class Binding:
    name: str
    pos: int  # the ``$`` of the assigned variable
    literal: int | None  # opening bracket of the array literal that is the whole value
    value: int = -1  # start of the value; -1 for a synthetic binding (a parameter, a write that cannot be read)


@dataclass(frozen=True)
class Use:
    pos: int
    kind: str
    key: str | None = None  # the literal first subscript of ``$p['k']...``
    key_start: int = 0  # span of the key name inside its quotes
    key_end: int = 0
    plain: bool = False  # exactly ``$p['k'] = value``: the one write the fixer may rename
    literal: int | None = None  # ``$p += [...]``: the array merged in


class Scopes:
    """Function and closure bodies, with the names their parameter lists and ``use`` clauses bring in."""

    def __init__(self, src: Lex) -> None:
        self.src = src
        self.bodies: dict[int, tuple[int, int, frozenset[str]]] = {}  # open brace -> (open, close, names)
        self.arrows: list[tuple[int, int, frozenset[str]]] = []  # (``fn``, end of its expression, names)
        self.headers: set[int] = set()  # ``(`` of parameter lists: ``$x = []`` there is a default, not code
        self.classes: set[int] = set()  # class body braces
        text = src.code
        for m in CLASS.finditer(text):
            brace = BRACE_OR_END.search(text, m.end(), m.end() + HEADER_REACH)
            if brace and brace.group(0) == "{":
                self.classes.add(brace.start())
        for m in FUNC.finditer(text):
            close = src.pairs.get(m.end() - 1)
            if close is None:
                continue
            names = set(VAR.findall(text, m.end(), close))
            self.headers.add(m.end() - 1)
            j = src.skip_space(close + 1)
            use = USE.match(text, j)
            if use and (use.end() - 1) in src.pairs:
                names |= set(VAR.findall(text, use.end(), src.pairs[use.end() - 1]))
                j = src.pairs[use.end() - 1] + 1
            j = src.skip_scalar(j, "{;")  # past a return type; an abstract method ends in ``;``
            if j < src.n and text[j] == "{" and j in src.pairs:
                self.bodies[j] = (j, src.pairs[j], frozenset(names))
        for m in ARROW.finditer(text):
            close = src.pairs.get(m.end() - 1)
            if close is None:
                continue
            self.headers.add(m.end() - 1)
            self.arrows.append((m.start(), src.skip_scalar(close + 1, ",;"), frozenset(VAR.findall(text, m.end(), close))))

    def innermost(self, pos: int) -> tuple[int, int, frozenset[str]] | None:
        """The function body that holds ``pos``, found by climbing the brackets around it."""
        block = self.src.enclosing(pos)
        while block >= 0 and (block not in self.bodies or self.src.pairs[block] == pos):
            block = self.src.parent[block]
        return self.bodies[block] if block >= 0 else None

    def extent(self, pos: int) -> tuple[int, int]:
        """The span of the scope that holds ``pos``: its function body, or the whole file."""
        body = self.innermost(pos)
        return (body[0], body[1]) if body else (0, self.src.n)

    def parameter(self, name: str, pos: int) -> bool:
        """``name`` is a parameter (or ``use`` import) of the function around ``pos``."""
        body = self.innermost(pos)
        return body is not None and name in body[2]

    def arrow_parameter(self, name: str, pos: int) -> bool:
        return any(a < pos < b and name in names for a, b, names in self.arrows)

    def declaration(self, pos: int) -> bool:
        """``pos`` is in a parameter list or directly in a class body: a default or a property, not a variable."""
        block = self.src.enclosing(pos)
        while block >= 0 and self.src.text[block] != "{":
            if block in self.headers:
                return True
            block = self.src.parent[block]
        return block in self.classes


class Bindings:
    """Every plain occurrence of every variable, classified, and the questions the reader and fixer ask."""

    def __init__(self, src: Lex, scopes: Scopes) -> None:
        self.src, self.scopes = src, scopes
        self.by_name: dict[str, list[Binding]] = {}
        self.uses: dict[str, list[Use]] = {}
        self._loops: list[tuple[int, int]] | None = None
        self._scope: dict[int, tuple | None] = {}  # occurrence -> the function body it is in
        text = src.text
        for m in VAR.finditer(src.code):
            p = src.back_space(m.start())
            if text[p - 2 : p] in ("->", "::") or text[p - 1 : p] == "$":
                continue  # a property, a static property, a variable variable
            if scopes.declaration(m.start()):
                continue
            name = m.group(1)
            self._scope[m.start()] = scopes.innermost(m.start())
            use = self._classify(name, m.start(), m.end(), p)
            self.uses.setdefault(name, []).append(use)
            if use.kind == BIND:
                value = src.skip_space(src.skip_space(m.end()) + 1)
                self.by_name.setdefault(name, []).append(Binding(name, m.start(), self._whole_literal(value), value))

    def _whole_literal(self, value: int) -> int | None:
        src = self.src
        opener = src.array_open(value)
        if opener is None:
            return None
        after = src.skip_space(src.pairs[opener] + 1)
        return opener if after < src.n and src.text[after] == ";" else None

    def _classify(self, name: str, start: int, end: int, p: int) -> Use:
        src, text = self.src, self.src.text
        before = text[max(0, p - 12) : p]
        word = WORD_BEFORE.search(before)
        if (before.endswith("&") and not before.endswith("&&")) or (word and word.group(1) in ("global", "static", "as")):
            return Use(start, OPAQUE)
        if self._written_by_context(start):
            return Use(start, OPAQUE)
        j = src.skip_space(end)
        if text.startswith("=", j) and text[j + 1 : j + 2] not in ("=", ">"):
            return Use(start, BIND)
        op = OP_ASSIGN.match(text, j)
        if op:
            merged = src.array_open(src.skip_space(op.end())) if op.group(0) == "+=" else None
            return Use(start, WRITE, literal=merged)
        if not text.startswith("[", j) or j not in src.pairs:
            return Use(start, USED)
        key, ks, ke = None, 0, 0
        inner = src.skip_space(j + 1)
        span = src.span_at(inner)
        if span and span[2] == STRING and span[0] == inner and src.skip_space(span[1]) == src.pairs[j]:
            body = text[inner + 1 : span[1] - 1]
            if re.fullmatch(r"[A-Za-z_]\w*", body) and text[inner] in "'\"":
                key, ks, ke = body, inner + 1, span[1] - 1
        depth, j = 1, src.skip_space(src.pairs[j] + 1)
        while text.startswith("[", j) and j in src.pairs:
            depth, j = depth + 1, src.skip_space(src.pairs[j] + 1)
        if text.startswith("=", j) and text[j + 1 : j + 2] not in ("=", ">"):
            return Use(start, WRITE, key, ks, ke, plain=key is not None and depth == 1)
        if OP_ASSIGN.match(text, j):
            return Use(start, WRITE, key, ks, ke)
        return Use(start, INPLACE if key is not None else USED, key, ks, ke)

    def _written_by_context(self, pos: int) -> bool:
        """``foreach (... as $x)``, ``[$a, $x] = ...`` / ``list($x) = ...``, or the first argument of a
        built-in that changes it in place: the variable is written here in a way that cannot be read."""
        src, text = self.src, self.src.text
        opener = src.enclosing(pos)
        if opener < 0:
            return False
        head = WORD_BEFORE.search(text, max(0, opener - 40), src.back_space(opener))
        word = head.group(1) if head else ""
        if text[opener] == "(":
            if word == "foreach":
                return re.search(r"\bas\b", text[opener:pos]) is not None
            if word in BY_REF and src.skip_space(opener + 1) == pos:
                return True
            if word != "list":
                return False
        elif text[opener] == "[" and word == "as":
            return True  # foreach ($rows as [$a, $x])
        elif text[opener] != "[" or word or text[src.back_space(opener) - 1 : src.back_space(opener)] in ("]", ")", "}"):
            return False  # a block, or a subscript: ``$x[$i]``
        while src.parent.get(opener, -1) >= 0 and text[src.parent[opener]] in "[(" and self._destructures(src.parent[opener]):
            opener = src.parent[opener]
        after = src.skip_space(src.pairs[opener] + 1)
        return text.startswith("=", after) and text[after + 1 : after + 2] not in ("=", ">")

    def _destructures(self, opener: int) -> bool:
        text, p = self.src.text, self.src.back_space(opener)
        if text[opener] == "(":
            return text[max(0, p - 4) : p] == "list"
        return not (text[p - 1 : p].isalnum() or text[p - 1 : p] in ("_", "]", ")", "}"))

    # --- questions ----------------------------------------------------------------------

    def _in_scope(self, name: str, at: int) -> list[Use]:
        body = self.scopes.innermost(at)
        return [u for u in self.uses.get(name, ()) if self._scope[u.pos] == body]

    def reaching(self, name: str, at: int) -> list[Binding]:
        """Every binding of ``name`` that can be the one in force at ``at``, nearest first. An unconditional
        assignment at statement level ends the walk; conditional ones are all kept. One synthetic binding
        (``literal=None``) is returned instead when the value cannot be known: a parameter that is not
        overwritten first, an assignment inside an expression, a write that cannot be read, a rebinding
        later in the same loop, ``if (...): ... endif`` syntax, or code that can change variables unnamed."""
        src, scopes = self.src, self.scopes
        body = scopes.innermost(at)
        lo, hi = scopes.extent(at)
        unknown = [Binding(name, lo, None)]
        mine = [b for b in self.by_name.get(name, ()) if self._scope[b.pos] == body]
        out: list[Binding] = []
        dominated = False
        for binding in reversed([b for b in mine if b.pos < at]):
            conditional = self._conditional(binding.pos, at, lo)
            if conditional is None:
                return unknown
            out.append(binding)
            if not conditional:
                dominated = True
                break
        if scopes.arrow_parameter(name, at) or (not dominated and scopes.parameter(name, at)):
            return unknown
        first = min((b.pos for b in out), default=at) if dominated else lo
        uses = self._in_scope(name, at)
        if any(u.kind == OPAQUE and (u.pos > first or src.text[src.back_space(u.pos) - 1] == "&") for u in uses if u.pos < at):
            return unknown
        first = min((b.pos for b in out), default=at)
        if any(b.pos > at and self._same_loop(b.pos, at) for b in mine):
            return unknown
        if ALT_SYNTAX.search(src.code, lo, hi):
            return unknown
        if out and self.unbounded(first, at):
            return unknown
        return out

    def _conditional(self, pos: int, at: int, scope_open: int) -> bool | None:
        """Whether the assignment at ``pos`` may not have run when ``at`` is reached; None when it sits inside
        an expression (``$a && $p = [...]``) or in the ``switch`` that holds the call, where this module does
        not decide."""
        src, text = self.src, self.src.text
        p = src.back_space(pos)
        prev = text[p - 1] if p > 0 else ""
        word = WORD_BEFORE.search(text, max(0, p - 12), p)
        block = src.enclosing(pos)
        while block > scope_open and text[block] != "{":
            block = src.parent[block]
        inside = block > scope_open and block < at < src.pairs[block]  # the call is in the assignment's block
        if inside and self._head(block) == "switch":
            return None  # another ``case`` of the same switch: not decided
        if prev in (")", ":") or (word and word.group(1) in ("else", "do")):
            return True  # the body of a brace-less ``if``/loop, or a ``case`` label
        if not (p == 0 or prev in ";{}" or src.masked(p - 1)):
            return None
        return block > scope_open and not inside

    def _head(self, brace: int) -> str:
        """The keyword that owns the block at ``brace``: ``foreach`` for ``foreach (...) {``, ``else``, ``do``."""
        src, text = self.src, self.src.text
        p = src.back_space(brace)
        if p > 0 and text[p - 1] == ")" and (p - 1) in src.back:
            p = src.back_space(src.back[p - 1])
        word = WORD_BEFORE.search(text, max(0, p - 12), p)
        return word.group(1) if word else ""

    def _same_loop(self, pos: int, at: int) -> bool:
        if self._loops is None:
            self._loops = [(o, c) for o, c in self.src.pairs.items() if self.src.text[o] == "{" and self._head(o) in LOOPS]
        return any(o < pos < c and o < at < c for o, c in self._loops)

    def mutations(self, name: str, after: int, before: int) -> list[Use]:
        """Writes into ``name`` between its literal and the call, in the call's own scope."""
        return [u for u in self._in_scope(name, before) if after < u.pos < before and u.kind == WRITE]

    def touches(self, name: str, after: int, before: int) -> set[str]:
        """Keys of ``name`` that are read, appended to or written below (``$p['k'][] = x``, ``$p['k']['j'] = x``,
        ``foo($p['k'])``): anything but the plain ``$p['k'] = value``."""
        return {
            u.key for u in self._in_scope(name, before)
            if after < u.pos < before and u.key is not None and (u.kind == INPLACE or (u.kind == WRITE and not u.plain))
        }

    def escapes(self, name: str, at: int) -> bool:
        """``name`` is used in its scope other than by its assignments, the writes into it and the use at
        ``at`` (returned, passed on, captured, read by ``compact``): an edit to its literal reaches that use."""
        lo, hi = self.scopes.extent(at)
        if READS_ALL.search(self.src.code, lo, hi):
            return True
        return any(u.pos != at and u.kind in (USED, INPLACE, OPAQUE) for u in self._in_scope(name, at))

    def dynamic(self, name: str, at: int) -> bool:
        """The scope of the call at ``at`` reads all its variables by name (``compact``), or can change them
        unnamed between the first assignment of ``name`` and the call."""
        lo, hi = self.scopes.extent(at)
        body = self.scopes.innermost(at)
        first = min((b.pos for b in self.by_name.get(name, ()) if b.pos < at and self._scope[b.pos] == body), default=lo)
        return READS_ALL.search(self.src.code, lo, hi) is not None or self.unbounded(first, at)

    def unbounded(self, lo: int, hi: int) -> bool:
        """``extract()``, ``eval``, an include or a variable variable between ``lo`` and ``hi``."""
        return UNBOUNDED.search(self.src.code, lo, hi) is not None
