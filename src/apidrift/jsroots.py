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
TYPE_ANN = r"(?:\s*:\s*[\w.<>\[\]|, ]+?)?"
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
ASSIGN = re.compile(rf"(?<![\w$.])({NAME}){TYPE_ANN}\s*{NO_ASSIGN_OP}\s*")


@dataclass(frozen=True)
class Binding:
    name: str
    pos: int  # start of the assignment
    literal: int | None  # index of the ``{`` when the value is an object literal


@dataclass(frozen=True)
class Mutation:
    key: str | None  # the key written when it is visible (``params.x =``, ``params['x'] =``)
    literal: int | None  # ``{`` of an object merged in (``Object.assign(params, {...})``)


class Scopes:
    """Function bodies of a source, so a binding can be checked for visibility at a call site."""

    def __init__(self, src: Source, mask: Mask) -> None:
        self.functions: list[tuple[int, int, str | None]] = []  # (open brace, past close brace, name)
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
            self.functions.append((open_, src.skip_balanced(open_), name))

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
            self.by_name.setdefault(m.group(1), []).append(Binding(m.group(1), m.start(), literal))

    def at(self, name: str, at: int) -> Binding | None:
        """The binding of ``name`` in force at index ``at``: the nearest preceding one in an enclosing scope."""
        for binding in reversed(self.by_name.get(name, ())):
            if binding.pos < at and self.scopes.visible(binding.pos, at):
                return binding
        return None

    def mutations(self, name: str, after: int, before: int) -> list[Mutation]:
        """Writes into ``name`` between its literal and the call that uses it."""
        out: list[Mutation] = []
        for m in _mutation_re(name).finditer(self.src.text, after, before):
            if self.mask.covers(m.start()):
                continue
            key = m.group("attr") or m.group("key") or m.group("sym")
            literal = m.start("obj") if m.group("obj") else None
            out.append(Mutation(key, literal))
        return out


def _mutation_re(name: str) -> re.Pattern[str]:
    n = re.escape(name)
    return re.compile(
        rf"(?<![\w$.:]){n}\s*(?:\.\s*(?P<attr>{NAME})|\[\s*(?:['\"](?P<key>[\w$]+)['\"]|:(?P<sym>\w+)|[^\]]*)\s*\])"
        rf"(?:\s*(?:\.\s*{NAME}|\[[^\]]*\]))*\s*{NO_ASSIGN_OP}"
        rf"|Object\.assign\(\s*{n}\s*,\s*(?P<obj>\{{)?"
        rf"|(?<![\w$.:]){n}\s*\.\s*(?:merge!|update|store|deep_merge!)\b"
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
