"""Receivers and scopes for the JavaScript / TypeScript / Ruby scanner.

Which names hold a Stripe client (``const stripe = new Stripe(k)``,
``const s = getStripe()``, a function that ``return new Stripe(``), where
function bodies start and end, and which object literal a variable refers to
at a given call site: the nearest preceding assignment in an enclosing scope,
plus any mutation of it (``params.x = ...``) between the two.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .jsparse import Mask, Source

NAME = r"[A-Za-z_$][\w$]*"
TYPE_ANN = r"(?:[ \t]*:[ \t]*[\w.<>\[\]|, ]+?)?"  # a type annotation never crosses a newline (``default:`` is a label)
CALL_ARGS = r"\((?:[^()]|\([^()]*\))*\)"
NO_ASSIGN_OP = r"(?<![=!<>+\-*/%&|^])=(?![=>])"
STRIPE_ROOTS = [
    re.compile(
        rf"(?:const|let|var)\s+({NAME}){TYPE_ANN}\s*=\s*(?:await\s+)?(?:new\s+)?"
        rf"(?:require\(\s*['\"]stripe['\"]\s*\)|Stripe)\s*\("
    ),
    re.compile(rf"(?<![\w$])({NAME})\s*=\s*(?:await\s+)?new\s+Stripe\s*\("),
    re.compile(rf"this\.({NAME})\s*=\s*(?:new\s+)?Stripe\s*\("),
    re.compile(rf"\b({NAME})\s*:\s*Stripe\b"),
]
RUBY_ROOTS = [re.compile(r"(?<![\w:.])([A-Za-z_]\w*)\s*=\s*(?:::)?Stripe::StripeClient\.new\b")]
STATEMENT_KEYWORDS = "if|for|while|switch|catch|with|function|return|else|do|try|typeof|await|new|yield|throw|case"
FUNC_HEAD = re.compile(
    rf"\bfunction\b(?:\s+(?P<fn>{NAME}))?[^{{;]*?\{{"
    rf"|(?P<arrow>=>)\s*\{{"
    rf"|(?<![\w$.])(?!(?:{STATEMENT_KEYWORDS})\b)(?P<method>{NAME})\s*{CALL_ARGS}\s*(?::\s*[^{{;=]+?)?\s*\{{"
)
ARROW_NAME = re.compile(
    rf"(?:const|let|var)\s+({NAME}){TYPE_ANN}\s*=\s*(?:async\s*)?(?:{CALL_ARGS}|{NAME})\s*(?::[^=]*?)?\s*$"
)
RETURNS_CLIENT = re.compile(rf"\breturn\s+(?:(?P<new>new\s+Stripe\s*\()|(?:this\s*\.\s*)?(?P<name>{NAME})\b)")
ASSIGN = re.compile(rf"(?<![\w$.])(?<!case )(?<!case\t)(?!(?:default|case)\b)({NAME}){TYPE_ANN}\s*{NO_ASSIGN_OP}\s*")
COND_HEADS = {"if", "else", "for", "while", "do", "case", "default", "switch"}  # a body or label: the binding is conditional
LOOP_HEADS = {"for", "while", "do"}
RUBY_LOOP = re.compile(r"\b(?:each|each_with_index|each_with_object|map|loop|while|until|times|upto|downto|for)\b")
PARAM_SPLIT = re.compile(r"\((?:[^()]|\([^()]*\))*\)")


@dataclass(frozen=True)
class Binding:
    name: str
    pos: int  # start of the assignment
    literal: int | None  # index of the ``{`` when the value is an object literal
    value: int = -1  # start of the assigned value (-1 for a synthetic binding: a parameter, a loop rebinding)


@dataclass(frozen=True)
class Mutation:
    key: str | None  # the key written when it is visible (``params.x =``, ``params['x'] =``)
    literal: int | None  # ``{`` of an object merged in (``Object.assign(params, {...})``)
    start: int = 0  # statement span in the original text
    end: int = 0
    key_start: int = 0  # span of the key name alone (no quotes or colon), when ``key`` is set
    key_end: int = 0


KEY_AFTER = re.compile(r"[ \t]*(?::(?!:)|=>)")
RUBY_WINDOW = re.compile(r"^[ \t]*(def|do|get|post|put|delete|patch)\b", re.M)


class Scopes:
    """Function bodies of a source, so a binding can be checked for visibility at a call site."""

    def __init__(self, src: Source, mask: Mask) -> None:
        self.functions: list[tuple[int, int, str | None]] = []  # (open brace, past close brace, name)
        self.params: dict[int, set[str]] = {}  # open brace -> parameter names of that function
        if src.ruby:
            return
        text = src.text
        for m in FUNC_HEAD.finditer(text):
            open_ = m.end() - 1
            if mask.covers(open_):
                continue
            name = m.group("fn") or m.group("method")
            if m.group("arrow"):
                before = ARROW_NAME.search(text, max(0, m.start() - 200), m.start())
                name = before.group(1) if before else None
                head = _arrow_params(text, m.start())
            else:
                paren = text.find("(", m.start(), open_)
                if paren < 0:
                    head = ""
                else:  # a default value may hold braces: ``function go(params = { a: 1 }) {``
                    close = src.skip_balanced(paren)
                    head = text[paren:close]
                    body = text.find("{", close)
                    if body > open_ and not mask.covers(body) and ";" not in text[close:body]:
                        open_ = body
            self.functions.append((open_, src.skip_balanced(open_), name))
            self.params[open_] = {n for n in re.findall(NAME, head) if n not in ("this", "new")}

    def enclosing(self, pos: int) -> list[tuple[int, int, str | None]]:
        """Functions whose body holds ``pos``, innermost first."""
        return sorted((fn for fn in self.functions if fn[0] < pos < fn[1]), key=lambda fn: -fn[0])

    def innermost(self, pos: int) -> tuple[int, int, str | None] | None:
        best = None
        for fn in self.functions:
            if fn[0] < pos < fn[1] and (best is None or fn[0] > best[0]):
                best = fn
        return best

    def visible(self, pos: int, at: int) -> bool:
        """Something written at ``pos`` is in scope at ``at``: top level, or a function body enclosing ``at``."""
        fn = self.innermost(pos)
        return fn is None or fn[0] < at < fn[1]


class Bindings:
    """Assignments to plain variables, looked up per call site."""

    def __init__(self, src: Source, mask: Mask, scopes: Scopes) -> None:
        self.src, self.mask, self.scopes = src, mask, scopes
        self.by_name: dict[str, list[Binding]] = {}
        for m in ASSIGN.finditer(src.text):
            if mask.covers(m.start()):
                continue
            value = m.end()
            literal = value if value < src.n and src.text[value] == "{" else None
            self.by_name.setdefault(m.group(1), []).append(Binding(m.group(1), m.start(), literal, value))

    def at(self, name: str, at: int) -> Binding | None:
        """The binding of ``name`` in force at index ``at``: the nearest preceding one in an enclosing scope."""
        for binding in reversed(self.by_name.get(name, ())):
            if binding.pos < at and self.scopes.visible(binding.pos, at):
                return binding
        return None

    def mutations(self, name: str, after: int, before: int) -> list[Mutation]:
        """Writes into ``name`` between its literal and the call that uses it, in the call's own function."""
        out: list[Mutation] = []
        for m in self._in_function(_mutation_re(name), after, before):
            group = next((g for g in ("attr", "key", "sym") if m.group(g)), None)
            key = m.group(group) if group else None
            literal = next((m.start(g) for g in ("obj", "obj2") if m.group(g)), None)
            key_start, key_end = (m.start(group), m.end(group)) if group else (0, 0)
            out.append(Mutation(key, literal, m.start(), _statement_end(self.src, m.end()), key_start, key_end))
        return out

    def touches(self, name: str, after: int, before: int) -> list[Mutation]:
        """Reads, in-place operations and deletes of a key through ``name`` (``params.k.push(x)``,
        ``delete params.k``, ``opts[:k] << x``): anything but a plain ``name.k = value`` write."""
        out: list[Mutation] = []
        for m in self._in_function(_touch_re(name), after, before):
            group = next(g for g in ("attr", "key", "sym") if m.group(g))
            out.append(Mutation(m.group(group), None, m.start(), m.end(), m.start(group), m.end(group)))
        return out

    def escapes(self, name: str, at: int) -> bool:
        """``name`` is used in its scope other than by its own assignments, the writes into it, and the use
        at ``at`` (returned, passed on, spread, read, aliased): an edit to its literal would reach that use too."""
        text, ruby = self.src.text, self.src.ruby
        if ruby:
            lo = self._ruby_window(at)[0]
            nxt = RUBY_WINDOW.search(text, at)
            hi = nxt.start() if nxt else self.src.n
        else:
            real = [b.pos for b in self.reaching(name, at) if b.value >= 0]
            fn = self.scopes.innermost(min(real) if real else at)
            lo, hi = (fn[0], fn[1]) if fn else (0, self.src.n)
        allowed = {b.pos for b in self.by_name.get(name, ())} | {at}
        writes = [(m.start(), m.end()) for m in _mutation_re(name).finditer(text, lo, hi) if not self.mask.covers(m.start())]
        for m in re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])").finditer(text, lo, hi):
            p = m.start()
            if p in allowed or self.mask.covers(p) or any(a <= p < b for a, b in writes):
                continue
            before = text[max(lo, p - 3) : p]
            if before.endswith(".") and not before.endswith("...") or (ruby and before.endswith(":") and not before.endswith("::")):
                continue  # another object's member, or a Ruby symbol
            if KEY_AFTER.match(text, m.end()) and text[lo:p].rstrip()[-1:] in ("{", ",", "("):
                continue  # a key that happens to share the name
            return True
        return False

    def _in_function(self, pattern: re.Pattern[str], after: int, before: int):
        """Matches between ``after`` and ``before`` outside strings, in the same innermost function as ``before``
        (Ruby: inside the call's window)."""
        if self.src.ruby:
            after = max(after, self._ruby_window(before)[0])
        fn = self.scopes.innermost(before)
        for m in pattern.finditer(self.src.text, after, before):
            if not self.mask.covers(m.start()) and self.scopes.innermost(m.start()) == fn:
                yield m

    def reaching(self, name: str, at: int) -> list[Binding]:
        """Every binding of ``name`` that can be the one in force at ``at``, nearest first.

        Walks the visible bindings backwards and stops after the first whose innermost enclosing
        block also encloses the call and that is not the body of a brace-less ``if``/``else``/loop or a
        ``case`` label: an unconditional rebinding earlier in the same block kills older ones, while
        conditional bindings are all kept. Ruby has no block spans, so it takes the nearest binding plus
        every other binding between the call and the previous line-start ``def``/``do``/route keyword.
        Non-literal bindings are included so the caller sees that the value is not fully visible; a
        synthetic one (``literal=None``) stands for a shadowing parameter or a rebinding later in an
        enclosing loop, which make the value at the call unknowable here.
        """
        if self.src.ruby:
            return self._reaching_ruby(name, at)
        out: list[Binding] = []
        dominator: Binding | None = None
        for binding in reversed(self.by_name.get(name, ())):
            if binding.pos >= at or not self.scopes.visible(binding.pos, at):
                continue
            out.append(binding)
            block = self._innermost_block(binding.pos)
            if block is not None and not (block[0] < at < block[1]):
                continue
            if _head_before(self.src.text, binding.pos) in COND_HEADS:
                continue
            dominator = binding
            break
        shadow = self._shadowing_parameter(name, at, dominator)
        if shadow is not None or self._rebound_in_loop(name, at):
            return [Binding(name, shadow if shadow is not None else at, None)]
        return out

    def _shadowing_parameter(self, name: str, at: int, dominator: Binding | None) -> int | None:
        """Start of a function enclosing ``at`` whose parameter ``name`` is not rebound unconditionally before the call."""
        bound_in = self.scopes.innermost(dominator.pos) if dominator is not None else None
        for fn in self.scopes.enclosing(at):
            if dominator is not None and fn == bound_in:
                return None
            if name in self.scopes.params.get(fn[0], ()):
                return fn[0]
        return None

    def _rebound_in_loop(self, name: str, at: int) -> bool:
        fn = self.scopes.innermost(at)
        for binding in self.by_name.get(name, ()):
            if binding.pos <= at or self.scopes.innermost(binding.pos) != fn:
                continue
            for open_, close, head in self._blocks_with_heads():
                if head in LOOP_HEADS and open_ < at < close and open_ < binding.pos < close:
                    return True
        return False

    def _reaching_ruby(self, name: str, at: int) -> list[Binding]:
        window, keyword = self._ruby_window(at)
        nearest = self.at(name, at)
        if nearest is None:
            return []
        text = self.src.text
        if keyword == "def":
            line_end = text.find("\n", window)
            head = text[window:line_end if line_end >= 0 else len(text)]
            if name in set(re.findall(r"[A-Za-z_]\w*", head.split("def", 1)[1].split("(")[-1] if "(" in head else head.split("def", 1)[1].partition(" ")[2])):
                return [Binding(name, window, None)]  # a parameter of this method
            if nearest.pos < window:
                return [Binding(name, window, None)]  # a local of another method
        if RUBY_LOOP.search(text, window, at) and self._ruby_later_binding(name, at):
            return [Binding(name, at, None)]
        out = [nearest]
        for binding in reversed(self.by_name.get(name, ())):
            if window <= binding.pos < at and binding != nearest:
                out.append(binding)
        return out

    def _ruby_window(self, at: int) -> tuple[int, str]:
        starts = [(m.start(), m.group(1)) for m in RUBY_WINDOW.finditer(self.src.text, 0, at)]
        return starts[-1] if starts else (0, "")

    def _ruby_later_binding(self, name: str, at: int) -> bool:
        nxt = RUBY_WINDOW.search(self.src.text, at)
        limit = nxt.start() if nxt else self.src.n
        return any(at < b.pos < limit for b in self.by_name.get(name, ()))

    def _innermost_block(self, pos: int) -> tuple[int, int] | None:
        best = None
        for open_, close, _ in self._blocks_with_heads():
            if open_ < pos < close and (best is None or open_ > best[0]):
                best = (open_, close)
        return best

    def _blocks_with_heads(self) -> list[tuple[int, int, str | None]]:
        if not hasattr(self, "_blocks"):
            text = self.src.text
            self._blocks = [
                (i, self.src.skip_balanced(i), _head_before(text, i))
                for i, c in enumerate(text) if c == "{" and not self.mask.covers(i)
            ]
        return self._blocks


def _head_before(text: str, pos: int) -> str | None:
    """The keyword that governs the statement or block starting at ``pos``: ``if`` for ``if (x) <here>``,
    ``else``, ``for``, ``case`` for a ``case X:``/``default:`` label, else the preceding word or None."""
    j = pos
    while j > 0 and text[j - 1].isspace():
        j -= 1
    if j == 0:
        return None
    if text[j - 1] == ":":
        return "case"
    if text[j - 1] == ")":
        depth, k = 0, j - 1
        while k >= 0:
            if text[k] == ")":
                depth += 1
            elif text[k] == "(":
                depth -= 1
                if depth == 0:
                    break
            k -= 1
        if k < 0:
            return None
        j = k
        while j > 0 and text[j - 1].isspace():
            j -= 1
    m = re.search(r"([A-Za-z_$][\w$]*)$", text[max(0, j - 40):j])
    return m.group(1) if m else None


def _arrow_params(text: str, arrow: int) -> str:
    """The parameter list before a ``=>`` at ``arrow``: ``(a, b)`` or a bare name."""
    j = arrow
    while j > 0 and text[j - 1].isspace():
        j -= 1
    if j > 0 and text[j - 1] == ")":
        depth, k = 0, j - 1
        while k >= 0:
            depth += (text[k] == ")") - (text[k] == "(")
            if depth == 0:
                return text[k:j]
            k -= 1
        return ""
    m = re.search(rf"({NAME})$", text[max(0, j - 80):j])
    return m.group(1) if m else ""


def _statement_end(src: Source, i: int) -> int:
    """Past the value of an assignment starting at ``i``: up to ``;`` or a closer; in Ruby a newline too."""
    text = src.text
    while i < src.n:
        c = text[i]
        if c == ";" or c in "})]" or (src.ruby and c == "\n"):
            return i
        if c in "\"'`":
            i = src.skip_string(i)
        elif c in "([{":
            i = src.skip_balanced(i)
        elif (src.ruby and c == "#") or (not src.ruby and text.startswith("//", i)):
            end = text.find("\n", i)
            i = src.n if end < 0 else end
        else:
            i += 1
    return i


def _mutation_re(name: str) -> re.Pattern[str]:
    n = re.escape(name)
    return re.compile(
        rf"(?<![\w$.:]){n}\s*(?:\.\s*(?P<attr>{NAME})|\[\s*(?:['\"](?P<key>[\w$]+)['\"]|:(?P<sym>\w+)|[^\]]*)\s*\])"
        rf"(?:\s*(?:\.\s*{NAME}|\[[^\]]*\]))*\s*{NO_ASSIGN_OP}"
        rf"|Object\.assign\(\s*{n}\s*,\s*(?P<obj>\{{)?"
        rf"|(?<![\w$.:]){n}\s*\.\s*(?:merge!|deep_merge!|(?:update|store)\b)(?:\s*\(\s*(?P<obj2>\{{))?"
    )


def _touch_re(name: str) -> re.Pattern[str]:
    """``name.k`` / ``name['k']`` / ``name[:k]`` not followed by a plain ``=``: a read, in-place op, deeper write or delete."""
    n = re.escape(name)
    return re.compile(
        rf"(?<![\w$.:]){n}\s*(?:\.\s*(?P<attr>{NAME})|\[\s*(?:['\"](?P<key>[\w$]+)['\"]|:(?P<sym>\w+))\s*\])"
        rf"(?!\s*{NO_ASSIGN_OP})(?!\s*\()"
    )


# --- Stripe client roots ------------------------------------------------------


def stripe_roots(src: Source, mask: Mask, scopes: Scopes) -> set[str]:
    """Names that hold a Stripe client, plus functions that return one and variables bound to their result."""
    text = src.text
    if src.ruby:
        return {m.group(1) for pattern in RUBY_ROOTS for m in pattern.finditer(text)}
    roots = {m.group(1) for pattern in STRIPE_ROOTS for m in pattern.finditer(text) if not mask.covers(m.start())}
    for _ in range(3):
        factories = _factories(text, scopes, roots)
        grown = roots | factories | _aliases(text, mask, roots, factories)
        if grown == roots:
            break
        roots = grown
    return roots


def _factories(text: str, scopes: Scopes, roots: set[str]) -> set[str]:
    """Named functions whose body returns ``new Stripe(...)`` or a known root."""
    out: set[str] = set()
    for open_, close, name in scopes.functions:
        if not name:
            continue
        for m in RETURNS_CLIENT.finditer(text, open_, close):
            if m.group("new") or m.group("name") in roots:
                out.add(name)
                break
    return out


def _aliases(text: str, mask: Mask, roots: set[str], factories: set[str]) -> set[str]:
    """``const s = getStripe()``, ``const { paymentIntents } = getStripe()``, ``const s = stripe``."""
    names = sorted(factories | roots, key=len, reverse=True)
    factory = "|".join([rf"[\w$]*[sS]tripe[\w$]*"] + [re.escape(n) for n in names])
    pattern = re.compile(
        rf"(?:const|let|var)\s+(?:({NAME})|\{{([^}}]*)\}}){TYPE_ANN}\s*=\s*(?:await\s+)?(?:{factory})\s*(?:\(|;|$)",
        re.M,
    )
    out: set[str] = set()
    for m in pattern.finditer(text):
        if mask.covers(m.start()):
            continue
        if m.group(1):
            out.add(m.group(1))
            continue
        for part in m.group(2).split(","):
            part = part.split("=")[0].strip().lstrip(".")
            local = part.split(":")[-1].strip()
            if re.fullmatch(NAME, local):
                out.add(local)
    return out


# --- Call chains ------------------------------------------------------------------


def chain_parts(src: Source, start: int, end: int) -> tuple[list[str], bool]:
    """Identifiers of a receiver chain, dropping call arguments, ``new`` and ``await``.

    The flag says whether a call in the receiver mentions ``'stripe'`` (``require('stripe')('sk').x``).
    """
    text, parts, hint, i = src.text, [], False, start
    while i < end:
        c = text[i]
        if c == "(":
            if not parts:  # the "(" of "(await getStripe())"
                i += 1
                continue
            close = src.skip_balanced(i)
            hint = hint or re.search(r"['\"]stripe['\"]", text[i:close]) is not None
            i = close
            continue
        m = re.compile(NAME).match(text, i)
        if m:
            if m.group(0) not in ("new", "await"):
                parts.append(m.group(0))
            i = m.end()
        else:
            i += 1
    return parts, hint


def display(raw: str) -> str:
    """A call chain as shown to the user: arguments collapsed, whitespace removed."""
    raw = re.sub(r"//[^\n]*|/\*.*?\*/", "", raw, flags=re.S)
    awaited = re.match(r"\(\s*await\s+(.*?\))\s*\)(.*)$", raw, re.S)
    if awaited:
        return f"(await {display(awaited.group(1))}){display(awaited.group(2))}"
    shown = re.sub(r"\b(new|await)\s+", "\\1\x00", re.sub(CALL_ARGS, "()", raw))
    return re.sub(r"\s+", "", shown).replace("\x00", " ")
