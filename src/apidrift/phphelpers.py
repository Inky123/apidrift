"""Same-file helpers and wrappers for the PHP reader.

Two shapes, both inside one file:

- A builder: ``PaymentIntent::create($this->params($order))`` (or ``self::`` / ``static::`` / a plain
  function, or ``$p = $this->params(); ...create($p)``). When the method is defined once in the file and
  every ``return`` in it gives an array literal or a local bound only to one, its keys are the call's.
- A wrapper: ``function createSetupIntent(array $params) { return SetupIntent::create($params, ...); }``,
  where ``$params`` is used nowhere else in the method. Each call of the wrapper in the same file that
  passes an array literal (or a local bound to one) at that position becomes a call site of its own, at
  the caller's line, with the caller's keys (``[through createSetupIntent, line N]``). The wrapper's own
  call site stays as it was: callers elsewhere are not visible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import phpparse
from .jsparse import Fields, Found
from .jshelpers import MAX_DEPTH, SUFFIX

NAMED = re.compile(r"(?<![\w$>:\\])function\s*(?:&\s*)?([A-Za-z_]\w*)\s*\(")
RETURN = re.compile(r"(?<![\w$>:\\])return\b")
CALLEE = re.compile(r"(?:\$this\s*->\s*|(?:self|static)\s*::\s*)?([A-Za-z_]\w*)\s*\(")
THROUGH = " [through {name}, line {line}]"


@dataclass(frozen=True)
class Definition:
    name: str
    pos: int
    open: int  # body brace
    close: int
    params: tuple[str, ...]  # parameter names in order ("" for a by-reference or variadic one)
    method: bool


@dataclass
class Caller:
    """A call of a wrapper whose argument at the forwarded position is read: a call site of its own."""

    wrapper: Definition
    call: phpparse.Call  # the SDK call inside the wrapper
    pos: int  # the ``(`` of the wrapper call
    arg: tuple[int, int]  # span of the forwarded argument
    line: int


@dataclass
class Read:
    helper: Definition
    call: int  # where the helper is called
    local: str | None = None
    local_at: int = -1
    seen: list = field(default_factory=list)


class PhpHelpers:
    def __init__(self, parsed: phpparse.Parsed) -> None:
        self.parsed, self.src, self.bindings, self.scopes = parsed, parsed.src, parsed.bindings, parsed.scopes
        self.defs: dict[str, list[Definition]] = {}
        code = self.src.code
        for m in NAMED.finditer(code):
            paren = m.end() - 1
            close = self.src.pairs.get(paren)
            if close is None:
                continue
            j = self.src.skip_scalar(self.src.skip_space(close + 1), "{;")
            if j not in self.scopes.bodies:
                continue
            params = tuple(
                "" if ("&" in p or "..." in p) else (re.search(r"\$([A-Za-z_]\w*)", p) or [None, ""])[1]
                for p in _split(self.src, paren)
            )
            method = self.src.enclosing(m.start()) in self.scopes.classes
            d = Definition(m.group(1), m.start(1), j, self.src.pairs[j], params, method)
            self.defs.setdefault(d.name.lower(), []).append(d)
        self.callers: list[Caller] = []

    def line(self, pos: int) -> int:
        return self.src.line(pos)

    def unique(self, name: str, method: bool) -> Definition | None:
        found = self.defs.get(name.lower(), [])
        return found[0] if len(found) == 1 and found[0].method == method else None

    def enclosing(self, pos: int) -> Definition | None:
        body = self.scopes.innermost(pos)
        if body is None:
            return None
        return next((d for ds in self.defs.values() for d in ds if d.open == body[0]), None)

    # --- builders ----------------------------------------------------------------------

    def callee(self, i: int, b: int) -> Definition | None:
        text = self.src.text
        m = CALLEE.match(text, i)
        if m is None or self.src.pairs.get(m.end() - 1, -1) + 1 != self.src.back_space(b):
            return None
        method = text[i] in "$sS" and m.start(1) > i
        d = self.unique(m.group(1), method)
        if d is None or d.open <= i < d.close or (method and self.container(d.pos) != self.container(i)):
            return None  # undefined, ambiguous, recursive, or a method of another class in this file
        return d

    def container(self, pos: int) -> int:
        """The brace of the class (trait, interface, enum) whose body holds ``pos``; -1 at top level."""
        block = self.src.enclosing(pos)
        while block >= 0 and block not in self.scopes.classes:
            block = self.src.parent[block]
        return block

    def argument(self, i: int, b: int) -> Read | None:
        d = self.callee(i, b)
        if d is not None:
            return Read(d, i)
        m = phpparse.VARIABLE.match(self.src.text, i)
        if m is None or m.group(1) == "this" or self.src.skip_space(m.end()) < b:
            return None
        reaching = self.bindings.reaching(m.group(1), i)
        if len(reaching) != 1 or reaching[0].value < 0 or reaching[0].literal is not None:
            return None
        value = reaching[0].value
        d = self.callee(value, self.src.skip_scalar(value, ";"))
        return Read(d, value, m.group(1), i) if d is not None else None

    def returns(self, d: Definition) -> list[tuple[int | None, str | None, int]] | None:
        """(array opener, or None; local name, or None; position) for every ``return`` of ``d``."""
        src, text, out = self.src, self.src.text, []
        for m in RETURN.finditer(src.code, d.open, d.close):
            if self.scopes.innermost(m.start()) is None or self.scopes.innermost(m.start())[0] != d.open:
                continue
            j = src.skip_space(m.end())
            opener = src.array_open(j)
            if opener is not None:
                if not text.startswith(";", src.skip_space(src.pairs[opener] + 1)):
                    return None
                out.append((opener, None, j))
                continue
            v = phpparse.VARIABLE.match(text, j)
            if v is None or v.group(1) == "this" or not text.startswith(";", src.skip_space(v.end())):
                return None
            out.append((None, v.group(1), j))
        return out or None

    def read(self, i: int, b: int) -> tuple[Read, Fields] | None:
        got = self.argument(i, b)
        if got is None:
            return None
        out = Fields()
        if not self.helper_fields(got.helper, out, [], got.seen):
            return None
        if got.local is not None:
            self._writes(got.local, got.call, got.local_at, out)
        return got, out

    def helper_fields(self, d: Definition, out: Fields, stack: list, seen: list) -> bool:
        if d in stack or len(stack) >= MAX_DEPTH:
            return False
        rets = self.returns(d)
        if rets is None:
            return False
        seen.append(d)
        for opener, local, at in rets:
            if opener is not None:
                phpparse.read_array(self.src, opener, "", out)
                continue
            if self.bindings.dynamic(local, at):
                return False
            reaching = self.bindings.reaching(local, at)
            if not reaching or any(r.literal is None for r in reaching):
                return False
            for r in reaching:
                phpparse.read_array(self.src, r.literal, "", out)
            self._writes(local, min(r.pos for r in reaching), at, out)
        return True

    def _writes(self, name: str, after: int, at: int, out: Fields) -> None:
        src = self.src
        for w in self.bindings.mutations(name, after, at):
            if w.key is not None:
                out.paths.add(w.key)
                if not w.plain:
                    out.hide(w.key + ".")
                    continue
                eq = src.skip_space(src.pairs[src.text.index("[", w.pos)] + 1)
                opener = src.array_open(src.skip_space(eq + 1))
                if opener is not None:
                    phpparse.read_array(src, opener, w.key, out)
            elif w.literal is not None:
                phpparse.read_array(src, w.literal, "", out)
            else:
                out.hide("")
        for key in self.bindings.touches(name, after, at):
            out.hide(key + ".")
        if self.bindings.escapes(name, at):
            out.hide("")

    def suffix(self, read: Read | None) -> str:
        return "" if read is None else SUFFIX.format(name=read.helper.name, line=self.line(read.helper.pos))

    # --- wrappers ------------------------------------------------------------------------

    def forwarded(self, c: phpparse.Call) -> tuple[Definition, int] | None:
        """The wrapper around ``c`` and the position of its parameter that is ``c``'s parameters argument."""
        if c.params is None:
            return None
        a, b = c.spans[c.params]
        i = self.src.skip_space(a)
        m = phpparse.VARIABLE.match(self.src.text, i)
        if m is None or m.group(1) == "this" or self.src.skip_space(m.end()) < b:
            return None
        d = self.enclosing(i)
        name = m.group(1)
        if d is None or name not in d.params or not self.scopes.parameter(name, i):
            return None
        if self.bindings.dynamic(name, i) or len(self.bindings._in_scope(name, i)) != 1:
            return None  # also written, read or passed on in the wrapper: not forwarded unchanged
        if self.unique(d.name, d.method) is None:
            return None
        return d, d.params.index(name)

    def callers_of(self, c: phpparse.Call) -> list[Found]:
        got = self.forwarded(c)
        if got is None:
            return []
        d, k = got
        src, text, out = self.src, self.src.text, []
        name = re.escape(d.name)
        head = r"(?:\$this\s*->\s*|(?:self|static)\s*::\s*)" if d.method else r"(?<![\w$>:\\])(?<!function )"
        for m in re.finditer(rf"{head}{name}\s*\(", src.code, re.I):
            paren = m.end() - 1
            if d.open <= m.start() < d.close or paren not in src.pairs:
                continue
            if d.method and self.container(d.pos) != self.container(m.start()):
                continue
            args = src.args(paren)
            if k >= len(args) or any(text.startswith("...", src.skip_space(x)) or _named(src, x) for x, _ in args):
                continue
            fields = self._argument_fields(*args[k])
            if fields is None:
                continue
            line = src.line(m.start())
            self.callers.append(Caller(d, c, paren, args[k], line))
            call = c.call + THROUGH.format(name=d.name, line=c.line)
            out.append(Found(line, call, c.resolved, fields))
        return out

    def _argument_fields(self, a: int, b: int) -> Fields | None:
        src = self.src
        i = src.skip_space(a)
        out = Fields()
        opener = src.array_open(i)
        if opener is not None:
            if src.skip_space(src.pairs[opener] + 1) < b:
                return None
            phpparse.read_array(src, opener, "", out)
        else:
            m = phpparse.VARIABLE.match(src.text, i)
            if m is None or m.group(1) == "this" or src.skip_space(m.end()) < b:
                return None
            phpparse.variable_fields(self.parsed, m.group(1), i, out)
        return None if "" in out.hidden else out


class Via:
    """The hook :func:`phpparse.collect` calls for a parameters argument it cannot read."""

    def __init__(self, helpers: PhpHelpers) -> None:
        self.helpers, self.first, self.reads = helpers, None, []

    def __call__(self, i: int, b: int, out: Fields) -> bool:
        got = self.helpers.read(i, b)
        if got is None:
            return False
        out.paths |= got[1].paths
        for path, spans in got[1].spans.items():
            out.spans.setdefault(path, []).extend(spans)
        out.hidden |= got[1].hidden
        out.complete = out.complete and got[1].complete
        self.first = self.first or got[0]
        self.reads.append(got[0])
        return True

    @property
    def suffix(self) -> str:
        return self.helpers.suffix(self.first)


def _split(src, paren: int) -> list[str]:
    return [src.text[a:b] for a, b in src.args(paren)]


def _named(src, a: int) -> bool:
    """A PHP 8 named argument: ``name: value``."""
    return re.match(r"\s*[A-Za-z_]\w*\s*:(?!:)", src.text[a : a + 80]) is not None
