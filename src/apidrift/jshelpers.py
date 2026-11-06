"""Same-file helpers for the JavaScript / TypeScript / Ruby reader: parameters built by another function.

``Stripe::Checkout::Session.create(payment_url_payload(intent), opts)`` sends what ``payment_url_payload``
returns. When that function is defined exactly once in the file and every value it can return is an
object (hash) literal or a local variable bound to one, its fields are read as the call's own, together
with the keys written into that local before it is returned (``payload[:k] = v``, ``Object.assign(p, {..})``,
``payload.merge!(other_helper)``). The usage keeps where the fields came from (``[fields from NAME, line N]``).

Anything this module cannot follow leaves the argument hidden, exactly as before: a name defined twice or
also bound as a variable, a parameter or local that shadows it, a ``return`` it cannot read (Ruby: any
``return``, or a branch as the last expression), recursion, more than MAX_DEPTH helpers deep.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .jsroots import NAME

if TYPE_CHECKING:
    from .jsparse import Fields, Source
    from .jsroots import Bindings

MAX_DEPTH = 4
SUFFIX = " [fields from {name}, line {line}]"
RUBY_DEF = re.compile(r"^([ \t]*)def[ \t]+(self[ \t]*\.[ \t]*)?([A-Za-z_]\w*[?!]?)", re.M)
RUBY_CALLEE = re.compile(r"(?:self\s*\.\s*)?([A-Za-z_]\w*[?!]?)")
JS_CALLEE = re.compile(rf"(?:await\s+)?(this\s*\.\s*)?({NAME})\s*\(")
ARROW_EXPR = re.compile(  # const build = (x) => ({ ... })
    rf"(?:const|let|var)\s+({NAME})\s*(?::[^=\n]+)?=\s*(?:async\s*)?(?:\((?:[^()]|\([^()]*\))*\)|{NAME})"
    rf"\s*(?::[^=\n]+?)?=>\s*\(\s*\{{"
)
RETURN = re.compile(r"(?<![\w$.])return\b")
RUBY_CLOSERS = {"end", "else", "elsif", "when", "in", "rescue", "ensure", "then"}
REMOVERS = re.compile(r"\s*\.\s*(?:delete|except!|reject!|select!|compact!|delete_if|keep_if)\b")


@dataclass(frozen=True)
class Definition:
    name: str
    pos: int  # where the name is written in the definition
    open: int  # body start (JS: the ``{``; Ruby: the line after the header)
    close: int  # body end (JS: past the ``}``; Ruby: the ``end`` line)
    params: frozenset[str]
    kind: str  # "function", "method" (JS ``this.``), "instance" / "class" (Ruby ``def`` / ``def self.``)
    expr: int | None = None  # JS arrow with an expression body: the ``{`` of the literal it returns


@dataclass(frozen=True)
class Value:
    """One value a helper can return: a literal, a local variable, or (Ruby) another helper by bare name."""

    at: int  # where the returned expression starts
    literal: int | None = None
    name: str | None = None
    helper: Definition | None = None


@dataclass
class Read:
    """What one argument resolved to: the helper, where it is called, the local the call site binds (if any)."""

    helper: Definition
    call: int
    local: str | None = None
    local_at: int = -1
    seen: list[Definition] = field(default_factory=list)  # every helper whose literal was read


class Helpers:
    """The helper definitions of one file, and the arguments read through them."""

    def __init__(self, bindings: Bindings) -> None:
        self.bindings, self.src, self.mask, self.scopes = bindings, bindings.src, bindings.mask, bindings.scopes
        self.ruby = self.src.ruby
        self.defs: dict[str, list[Definition]] = {}
        for d in (self._ruby_defs() if self.ruby else self._js_defs()):
            self.defs.setdefault(d.name, []).append(d)
        self.reads: dict[int, Read] = {}  # argument start -> what it resolved to (the fixer walks these)

    def line(self, pos: int) -> int:
        return self.src.text.count("\n", 0, pos) + 1

    # --- definitions ----------------------------------------------------------------

    def _ruby_defs(self) -> list[Definition]:
        text, out = self.src.text, []
        for m in RUBY_DEF.finditer(text):
            if self.mask.covers(m.start(3)):
                continue
            i = m.end()
            params: set[str] = set()
            if text.startswith("(", i):
                close = self.src.skip_balanced(i)
                params = set(re.findall(r"[A-Za-z_]\w*", re.sub(r"=[^,)]*", "", text[i:close])))
                i = close
            eol = text.find("\n", i)
            if eol < 0:
                continue
            rest = text[i:eol].strip()
            if rest.startswith("=") or ";" in rest:
                continue  # endless or one-line definition
            if rest and not rest.startswith("#"):
                params |= set(re.findall(r"[A-Za-z_]\w*", re.sub(r"=[^,]*", "", rest)))
            end = re.compile(rf"^{re.escape(m.group(1))}end\b", re.M).search(text, eol)
            if end is None:
                continue
            kind = "class" if m.group(2) else "instance"
            out.append(Definition(m.group(3), m.start(3), eol + 1, end.start(), frozenset(params), kind))
        return out

    def _js_defs(self) -> list[Definition]:
        text, out = self.src.text, []
        for open_, close, name in self.scopes.functions:
            if not name:
                continue
            lo = max(0, open_ - 2000)
            heads = list(re.finditer(rf"(?<![\w$]){re.escape(name)}(?=\s*[(=<])", text[lo:open_]))
            if not heads:
                continue
            at = lo + heads[-1].start()  # the name as declared: followed by its parameters or ``=``
            head = text[max(0, at - 40):at]
            if re.search(r"\bfunction\s*\*?\s*$", head) or re.search(rf"(?:const|let|var)\s+$", head):
                kind = "function"
            elif re.search(r"[=:(,]\s*$", head):
                continue  # an anonymous function bound some other way
            else:
                kind = "method"
            out.append(Definition(name, at, open_, close, frozenset(self.scopes.params.get(open_, ())), kind))
        for m in ARROW_EXPR.finditer(text):
            if self.mask.covers(m.start()):
                continue
            brace = m.end() - 1
            paren = text.rfind("(", 0, brace)
            close = self.src.skip_balanced(paren)
            params = frozenset(re.findall(NAME, text[text.find("=", m.end(1)):text.rfind("=>", 0, brace)]))
            out.append(Definition(m.group(1), m.start(1), paren, close, params, "function", expr=brace))
        return out

    def unique(self, name: str, kind: str) -> Definition | None:
        found = self.defs.get(name, [])
        if len(found) != 1 or found[0].kind != kind:
            return None
        if not self.ruby and any(b.pos != found[0].pos for b in self.bindings.by_name.get(name, ())):
            return None  # the name is also assigned: which value is called is not visible
        return found[0]

    # --- which helper an argument calls ---------------------------------------------------

    def callee(self, i: int, b: int) -> Definition | None:
        """The helper whose call is the whole expression ``text[i:b]``, seen from position ``i``."""
        text = self.src.text
        b = len(text[:b].rstrip())
        if self.ruby:
            m = RUBY_CALLEE.match(text, i)
            if m is None:
                return None
            end = m.end()
            if text.startswith("(", end):
                end = self.src.skip_balanced(end)
            if end != b:
                return None
            name, explicit = m.group(1), text[i:m.start(1)].strip() != ""
            outer = self._ruby_enclosing(i)
            if outer is None:
                return None
            if not explicit and (name in outer.params or self._ruby_local(name, outer, i)):
                return None  # a parameter, block parameter or local variable of the same name
            d = self.unique(name, outer.kind)
        else:
            m = JS_CALLEE.match(text, i)
            if m is None or self.src.skip_balanced(m.end() - 1) != b:
                return None
            name = m.group(2)
            if m.group(1):
                d = self.unique(name, "method")
            else:
                if any(name in self.scopes.params.get(fn[0], ()) for fn in self.scopes.enclosing(i)):
                    return None  # a parameter shadows the function
                d = self.unique(name, "function")
        if d is None or d.open <= i < d.close:
            return None  # undefined, ambiguous, or the helper calling itself
        return d

    def _ruby_enclosing(self, pos: int) -> Definition | None:
        best = None
        for ds in self.defs.values():
            for d in ds:
                if d.open <= pos < d.close and (best is None or d.open > best.open):
                    best = d
        return best

    def _ruby_local(self, name: str, d: Definition, at: int) -> bool:
        text = self.src.text[d.open:at]
        if re.search(rf"\|[^|\n]*\b{re.escape(name)}\b[^|\n]*\|", text):
            return True  # a block parameter
        return any(d.open <= b.pos < at for b in self.bindings.by_name.get(name, ()))

    def argument(self, i: int, b: int) -> Read | None:
        """What the argument ``text[i:b]`` resolves to: a helper call, or a variable bound only to one."""
        d = self.callee(i, b)
        if d is not None:
            return Read(d, i)
        text = self.src.text
        m = re.compile(NAME).match(text, i)
        if m is None or text[i:b].strip() != m.group(0):
            return None
        name = m.group(0)
        reaching = self.bindings.reaching(name, i)
        if len(reaching) != 1 or reaching[0].value < 0 or reaching[0].literal is not None:
            return None
        binding = reaching[0]
        if self.ruby:
            outer = self._ruby_enclosing(i)
            if outer is None or not outer.open <= binding.pos < i:
                return None
        end = _value_end(self.src, binding.value)
        d = self.callee(binding.value, end)
        return Read(d, binding.value, name, i) if d is not None else None

    # --- what a helper returns ---------------------------------------------------------

    def returns(self, d: Definition) -> list[Value] | None:
        if d.expr is not None:
            return [Value(d.expr, literal=d.expr)]
        return self._ruby_returns(d) if self.ruby else self._js_returns(d)

    def _js_returns(self, d: Definition) -> list[Value] | None:
        src, text, out = self.src, self.src.text, []
        for m in RETURN.finditer(text, d.open, d.close):
            if self.mask.covers(m.start()) or self.scopes.innermost(m.start()) != (d.open, d.close, d.name):
                continue
            j = src.skip_space(m.end())
            k = src.skip_space(j + 1) if text.startswith("(", j) else j
            if text.startswith("{", k):
                end = src.skip_balanced(k)
                if k != j:
                    end = src.skip_space(end)
                    if not text.startswith(")", end):
                        return None
                    end += 1
                if not _statement_ends(src, end):
                    return None
                out.append(Value(m.start(), literal=k))
                continue
            n = re.compile(NAME).match(text, j)
            if n is None or not _statement_ends(src, n.end()):
                return None
            out.append(Value(j, name=n.group(0)))
        return out or None

    def _ruby_returns(self, d: Definition) -> list[Value] | None:
        src, text = self.src, self.src.text
        if any(not self.mask.covers(m.start()) for m in RETURN.finditer(text, d.open, d.close)):
            return None
        last, indent = None, None
        pos = d.open
        for line in text[d.open:d.close].splitlines(keepends=True):
            start, pos = pos, pos + len(line)
            body = line.strip()
            if not body or body.startswith("#") or self.mask.covers(start + len(line) - len(line.lstrip())):
                continue
            here = len(line) - len(line.lstrip(" \t"))
            indent = here if indent is None else indent
            if here < indent:
                return None  # ``rescue`` / ``ensure`` at the method's own level
            word = re.match(r"[A-Za-z_]\w*|\S", body).group(0)
            if here == indent and word not in RUBY_CLOSERS and word not in ("}", ")", "]", ".", "&"):
                last = start + here
        if last is None:
            return None
        tail = text[last:d.close].strip()
        if text.startswith("{", last):
            end = src.skip_balanced(last)
            return [Value(last, literal=last)] if text[end:d.close].strip() == "" else None
        n = RUBY_CALLEE.match(text, last)
        if n is None or tail != text[last:n.end()]:
            return None
        name = n.group(1)
        if self._ruby_local(name, d, last):
            return [Value(last, name=name)]
        helper = self.unique(name, d.kind) if name not in d.params else None
        return [Value(last, helper=helper)] if helper is not None and helper != d else None

    # --- reading -------------------------------------------------------------------------

    def read(self, i: int, b: int) -> tuple[Read, Fields] | None:
        """The fields the argument ``text[i:b]`` sends when it is a same-file helper's result."""
        got = self.argument(i, b)
        if got is None:
            return None
        fields = self.helper_fields(got.helper, [], got.seen)
        if fields is None:
            return None
        if got.local is not None:
            self._writes(got.local, got.call, got.local_at, fields, [got.helper], got.seen)
        self.reads[i] = got
        return got, fields

    def helper_fields(self, d: Definition, stack: list[Definition], seen: list[Definition]) -> Fields | None:
        from .jsparse import Fields, merge, parse_object

        if d in stack or len(stack) >= MAX_DEPTH:
            return None
        values = self.returns(d)
        if values is None:
            return None
        out, stack = Fields(), stack + [d]
        seen.append(d)
        for v in values:
            if v.literal is not None:
                merge(out, parse_object(self.src, v.literal + 1))
            elif v.helper is not None:
                sub = self.helper_fields(v.helper, stack, seen)
                if sub is None:
                    return None
                merge(out, sub)
            elif not self._local(v.name, v.at, out, stack, seen):
                return None
        return out

    def _local(self, name: str, at: int, out: Fields, stack: list[Definition], seen: list[Definition]) -> bool:
        """A returned local: every binding that reaches the return is a literal; its writes are read."""
        from .jsparse import merge, parse_object

        reaching = self.bindings.reaching(name, at)
        if not reaching or any(b.literal is None for b in reaching):
            return False
        for b in reaching:
            merge(out, parse_object(self.src, b.literal + 1))
        self._writes(name, min(b.pos for b in reaching), at, out, stack, seen)
        return True

    def _writes(self, name: str, after: int, at: int, out: Fields, stack: list[Definition], seen: list[Definition]) -> None:
        """Keys written into ``name`` between ``after`` and ``at``: a visible key adds itself (and the literal
        written under it), a merged literal or helper adds its keys, anything else may add any key."""
        from .jsparse import Fields, merge, parse_object

        src, text = self.src, self.src.text
        for mu in self.bindings.mutations(name, after, at):
            if mu.key is not None:
                out.paths.add(mu.key)
                eq = _assign_at(src, mu.key_end, mu.end)
                if eq is None:
                    out.hide(mu.key + ".")  # a deeper write: params[:a][:b] = v
                elif text.startswith("{", src.skip_space(eq + 1)):
                    sub = Fields()
                    src.object_fields(src.skip_space(eq + 1) + 1, mu.key + ".", sub)
                    out.paths |= sub.paths
                    out.hidden |= sub.hidden
                    out.complete = out.complete and sub.complete
                continue
            if mu.literal is not None:
                close = src.skip_balanced(mu.literal)
                merge(out, parse_object(src, mu.literal + 1))
                if not text.startswith(")", src.skip_space(close)):
                    out.hide("")  # Object.assign(p, {..}, more)
                continue
            inner = _merged_argument(src, mu.start, mu.end)
            sub = None
            if inner is not None:
                got = self.argument(*inner)
                if got is not None and got.local is None:
                    sub = self.helper_fields(got.helper, stack, seen)
            if sub is None:
                out.hide("")
            else:
                merge(out, sub)
        if self._escapes(name, after, at):
            out.hide("")  # passed on or aliased before it is sent: another function may add keys

    def _escapes(self, name: str, after: int, at: int) -> bool:
        text = self.src.text
        writes = [(mu.start, mu.end) for mu in self.bindings.mutations(name, after, at)]
        own = {b.pos for b in self.bindings.by_name.get(name, ())}
        for m in re.compile(rf"(?<![\w$.:@]){re.escape(name)}(?![\w$?!])").finditer(text, after, at):
            p = m.start()
            if p in own or self.mask.covers(p) or any(a <= p < e for a, e in writes):
                continue
            after_name = text[m.end():m.end() + 40]
            if REMOVERS.match(after_name) or re.match(r"\s*\[", after_name) or re.match(r"\s*\.\s*(?:key\?|fetch|dig|include\?|has_key\?)\b", after_name):
                continue  # a deletion or a read: nothing is added
            if text[max(0, p - 1):p] == ":" or re.match(r"\s*:(?!:)", after_name):
                continue  # a symbol or a key that shares the name
            return True
        return False

    def suffix(self, read: Read | None) -> str:
        if read is None:
            return ""
        return SUFFIX.format(name=read.helper.name, line=self.line(read.helper.pos))


class Via:
    """The hook :func:`jsparse.collect` calls for an argument it cannot read: reads it through a helper and
    remembers the first helper read, which the usage names (``suffix``)."""

    def __init__(self, helpers: Helpers | None) -> None:
        self.helpers, self.first, self.reads = helpers, None, []

    def __call__(self, i: int, b: int, out: Fields) -> bool:
        from .jsparse import merge

        got = self.helpers.read(i, b) if self.helpers is not None else None
        if got is None:
            return False
        merge(out, got[1])
        self.first = self.first or got[0]
        self.reads.append(got[0])
        return True

    @property
    def suffix(self) -> str:
        return self.helpers.suffix(self.first) if self.helpers is not None else ""


def plain_call(call: str) -> str:
    """A usage's call without the helper it was read through (what the parsers' ``calls`` report)."""
    return call.split(" [", 1)[0]


def _value_end(src: Source, i: int) -> int:
    """End of an assigned value starting at ``i`` (``;``, a closer, or in Ruby the end of the line)."""
    from .jsroots import _statement_end

    end = _statement_end(src, i)
    while end > i and src.text[end - 1] in " \t\r":
        end -= 1
    return end


def _statement_ends(src: Source, i: int) -> bool:
    """Only ``;``, a newline, ``}`` or a TypeScript ``as`` / ``satisfies`` cast follows ``i``."""
    text = src.text
    j = i
    while j < src.n and text[j] in " \t":
        j += 1
    if j >= src.n or text[j] in ";\n}\r":
        return True
    m = re.match(rf"(?:as|satisfies)\s+[\w$.<>\[\]|, ]+\s*(?:;|\n|$)", text[j:j + 200])
    return m is not None and "{" not in m.group(0)


def _assign_at(src: Source, key_end: int, end: int) -> int | None:
    """Index of the ``=`` that closes a one-key write (``p[:k] = v``, ``p.k = v``); None for a deeper write."""
    text, j = src.text, key_end
    while j < end and text[j] in "'\"]: \t":
        j += 1
    return j if j < end and text[j] == "=" and not text.startswith("==", j) else None


def _merged_argument(src: Source, start: int, end: int) -> tuple[int, int] | None:
    """The one argument of ``p.merge!(x)`` / ``p.update(x)`` written at ``start``."""
    text = src.text
    m = re.compile(r"[^(\n]*?(?:merge!|deep_merge!|update)\s*\(").match(text, start)
    if m is None:
        return None
    args = src.args(m.end() - 1)
    if len(args) != 1:
        return None
    a = src.skip_space(args[0][0])
    return a, args[0][1]
