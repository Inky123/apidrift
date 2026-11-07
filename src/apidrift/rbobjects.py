"""API calls on objects Stripe returned, in Ruby: stripe-ruby's instance methods.

``pi = Stripe::PaymentIntent.retrieve(id)`` holds a PaymentIntent, so ``pi.confirm(payment_method: pm)`` posts
to ``/v1/payment_intents/{intent}/confirm``. stripe-ruby gives a resource an instance method for each of its
operations on its own item, the class method of the same name minus the id (:func:`instance_op`): ``save``
(the update), ``delete``, ``refresh`` (the retrieve) and every action one segment below the item
(``confirm``, ``cancel``: the SDK method name the spec gives the operation). A GET action is left out:
``account.persons`` and the response attribute ``account.capabilities`` look the same.

Read only for a local whose bindings in the outermost ``def`` or ``do`` block around the use (found by
indentation) all bind the same resource through a resolved retrieve, create or update (``x =
Stripe::X.retrieve(id)``, or both arms of ``x = c ? Stripe::X.create(a) : Stripe::X.create(b)``), one of them
before the use and in a block that holds it. A block, ``rescue`` or loop variable, a parameter, a multiple or
compound assignment, or any other value bound to the name, and nothing is read.

``save`` sends what was written into the object before it (``sub.billing_cycle_anchor = "now"``,
``sub[:metadata] = {...}``); any other use in between (passed on, a method with arguments, a mutation) may
write more, and the call is hidden.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import jsparse
from .jsbare import POSITIONAL, RUBY_WORDS
from .sdkmap import Resolved, snake

INSTANCE = {"save": ("update", "post"), "delete": ("delete", "delete"), "refresh": ("retrieve", "get")}
MAKERS = {"retrieve", "create", "update"}  # class methods that return the resource itself
SCOPE = re.compile(r"^([ \t]*)(?:(?:private|protected|public)[ \t]+)?(?:def\b|\S[^\n]*\bdo(?:[ \t]*\|[^|\n]*\|)?[ \t]*$)", re.M)
CHAIN_TAIL = re.compile(r"(?:::)?(?:[A-Za-z_]\w*(?:\s*\.|[ \t]*::)\s*)*[A-Za-z_]\w*\s*\Z")
LEAD = re.compile(r"(?:::)?[A-Za-z_]\w*(?:\s*\.|[ \t]*::)\s*")
ITEM = re.compile(r"/\{[^}/]*\}$")
ONE_CALL = re.compile(r"\s*\x00\s*(?:(?:if|unless)\b[^\x00]*)?", re.S)
TERNARY = re.compile(r"[^\x00\n]*\?\s*\x00\s*:\s*\x00\s*", re.S)
ASSIGN = re.compile(r"[ \t]*(?:=(?![=~>])|\|\|=|&&=|\*\*=|<<=|>>=|[-+*/%|&^]=)")
SELECT = re.compile(r"[ \t]*\.[ \t]*([A-Za-z_]\w*[?!]?)")
FIELD = re.compile(r"[ \t]*\.[ \t]*([a-z_]\w*)(?![\w?!(])|\[[ \t]*(?::([a-z_]\w*)|\"([a-z_]\w*)\"|'([a-z_]\w*)')[ \t]*\]")
MULTIPLE = re.compile(r"^[ \t]*\*?[a-z_]\w*(?:[ \t]*,[ \t]*\*?[a-z_]\w*)+[ \t]*=(?![=~>])", re.M)
NOT_BOUND_HERE = re.compile(r"(?:(?:\bdo|\{)[ \t]*\|[^|\n]*|\bdef\b[^\n]*|\brescue\b[^\n]*=>[ \t]*|\bfor\b[^\n]*)\Z")
BOUND = re.compile(r"[ \t]*([a-z_]\w*)[ \t]*=(?![=~>])")
LITERAL = re.compile(r"[\"'0-9:-]|(?:nil|true|false)\b")


@dataclass(frozen=True)
class Maker:
    """A resolved call that returns a resource object."""

    start: int  # chain start
    end: int  # just past the call
    item: str  # the resource's item path, its id as ``{}``
    key: tuple[str, ...]  # the alias the call resolved through, minus its verb (``("PaymentIntent",)``)


def extract(text: str, calls: list, resolve) -> list[jsparse.Found]:
    """Instance calls on locals bound to the resolved ``calls`` of one Ruby source (``jsparse.calls``)."""
    makers = [m for c in calls if (m := _maker(text, c)) is not None]
    if not makers:
        return []
    src, bindings = jsparse.Source(text, ruby=True), calls[0].bindings
    reader = _Reader(text, src, bindings.mask, _scopes(text), makers)
    asked: dict[tuple, Resolved | None] = {}
    out: list[jsparse.Found] = []
    for name in sorted({n for m in makers if (n := _bound_name(text, m)) is not None}):
        for use in _word(name).finditer(text):
            sel = SELECT.match(text, use.end())
            if sel is None or reader.mask.covers(use.start()) or ASSIGN.match(text, sel.end()):
                continue
            held = reader.held(name, use.start())
            if held is None:
                continue
            maker, first = held
            verb = sel.group(1)
            key = (maker.key, maker.item, verb)
            if key not in asked:
                asked[key] = instance_op(resolve, maker.key, maker.item, verb)
            if asked[key] is None:
                continue
            fields = _args(src, text, sel.end(), bindings)
            if verb == "save":
                reader.written(name, first, use.start(), fields)
            out.append(jsparse.Found(text.count("\n", 0, use.start()) + 1, f"{name}.{verb}", asked[key], fields))
    return out


def instance_op(resolve, key: tuple[str, ...], item: str, verb: str) -> Resolved | None:
    """The operation ``verb`` reaches on an object of the resource whose item path is ``item``: the class
    method of the same name (``save`` is the update, ``refresh`` the retrieve) when it acts on that item."""
    name, method = INSTANCE.get(verb, (verb, None))
    resolved = resolve([*key, name])
    if resolved is None or resolved.path.count("{") != 1:
        return None
    if method is not None:
        return resolved if ITEM.sub("/{}", resolved.path) == item and resolved.method == method else None
    head, _, action = resolved.path.rpartition("/")
    if resolved.method == "get" or ITEM.sub("/{}", head) != item or not re.fullmatch(r"[a-z_]+", action):
        return None
    return resolved


def _word(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w@$.:]){re.escape(name)}(?![\w?!])")


def _maker(text: str, c) -> Maker | None:
    """The call as a binding of a resource object: a retrieve, create or update with an item path."""
    *key, verb = c.resolved.key.split(".")
    path = c.resolved.path
    if snake(verb) not in MAKERS or not key:
        return None
    if snake(verb) == "create" and "{" not in path:
        item = path + "/{}"
    elif path.count("{") == 1 and ITEM.search(path):
        item = ITEM.sub("/{}", path)
    else:
        return None  # a singleton (GET /v1/account) or a nested resource
    pos, n = c.pos, len(text)
    paren = pos < n and text[pos] == "("
    j = pos + 1 if not paren and c.spans else pos  # paren-less: ``pos`` is the blank before the arguments
    tail = CHAIN_TAIL.search(text, max(0, j - 400), j)
    if tail is None:
        return None
    start = tail.start()
    while re.sub(r"\s+", "", text[start:j]).lstrip(":") != c.call:
        lead = LEAD.match(text, start, j)
        if lead is None:
            return None
        start = lead.end()
    if paren:
        end = c.args_src.skip_balanced(pos)
    else:
        end = len(c.args_src.text) - 1 if c.spans else pos
    return Maker(start, end, item, tuple(key))


def _bound_name(text: str, m: Maker) -> str | None:
    """``name`` when the statement that holds ``m`` starts ``name = ``."""
    head = BOUND.match(text, text.rfind("\n", 0, m.start) + 1)
    return head.group(1) if head and head.end() <= m.start else None


def _scopes(text: str) -> list[tuple[int, int]]:
    """(start, end) of each ``def`` and line-ending ``do`` block: to the next line indented no deeper."""
    lines = [(m.start(), m.group(0)) for m in re.finditer(r"[^\n]*\n?", text) if m.group(0)]
    index = {s: k for k, (s, _) in enumerate(lines)}
    out = []
    for m in SCOPE.finditer(text):
        k, indent = index.get(m.start()), len(m.group(1).expandtabs())
        if k is None:
            continue
        end = len(text)
        for s, body in lines[k + 1 :]:
            stripped = body.strip()
            if stripped and not stripped.startswith("#") and len(body.expandtabs()) - len(body.expandtabs().lstrip()) <= indent:
                end = s + len(body) if re.match(r"end\b", stripped) else s
                break
        out.append((m.start(), end))
    return out


class _Reader:
    def __init__(self, text: str, src: jsparse.Source, mask, scopes: list[tuple[int, int]], makers: list[Maker]):
        self.text, self.src, self.mask, self.scopes, self.makers = text, src, mask, scopes, makers

    def _around(self, pos: int) -> list[tuple[int, int]]:
        return [s for s in self.scopes if s[0] <= pos < s[1]]

    def held(self, name: str, at: int) -> tuple[Maker, int] | None:
        """(the resource ``name`` holds at ``at``, its first binding): every binding of the name in the outermost
        scope around ``at`` binds that resource, and one before ``at`` sits in a scope that holds ``at``."""
        text, around = self.text, self._around(at)
        if not around:
            return None
        lo, hi = min(around)
        if any(name in re.findall(r"[a-z_]\w*", x.group(0)) for x in MULTIPLE.finditer(text, lo, hi)):
            return None  # a, b = ...
        found: list[tuple[int, Maker]] = []
        for m in _word(name).finditer(text, lo, hi):
            p, line = m.start(), text.rfind("\n", 0, m.start()) + 1
            if self.mask.covers(p):
                continue
            if NOT_BOUND_HERE.search(text, line, p):
                return None  # a block, rescue or loop variable, or a parameter
            assign = ASSIGN.match(text, m.end())
            if assign is None:
                continue
            if assign.group(0).strip() != "=" or text[line:p].strip():
                return None  # x ||= ..., x += ..., or an assignment inside an expression
            maker = self._value(m.end() + len(assign.group(0)))
            if maker is None:
                return None
            found.append((p, maker))
        if not found or len({(mk.item, mk.key) for _, mk in found}) != 1:
            return None
        for p, maker in found:
            inner = max(self._around(p))
            if p < at and inner[0] <= at < inner[1]:
                return maker, found[0][0]
        return None

    def _value(self, i: int) -> Maker | None:
        """The maker the value from ``i`` to the end of its statement is: one call, or both arms of a ternary."""
        end = self.src.line_end(i)
        inside = sorted((m for m in self.makers if i <= m.start and m.end <= end), key=lambda m: m.start)
        if not inside or len({(m.item, m.key) for m in inside}) != 1:
            return None
        shape, last = [], i
        for m in inside:
            shape += [self.text[last:m.start], "\x00"]
            last = m.end
        value = "".join(shape) + self.text[last:end].split(";", 1)[0]  # ``x = X.retrieve(id); x.save``
        return inside[0] if ONE_CALL.fullmatch(value) or TERNARY.fullmatch(value) else None

    def written(self, name: str, first: int, at: int, out: jsparse.Fields) -> None:
        """Attributes written into ``name`` between its first binding and ``save`` at ``at``; any use that is
        neither such a write nor a plain read hides the call."""
        text, src = self.text, self.src
        for m in _word(name).finditer(text, first, at):
            if self.mask.covers(m.start()) or ASSIGN.match(text, m.end()):
                continue  # a binding (checked by held)
            parts, j = [], m.end()
            while (f := FIELD.match(text, j)) is not None:
                parts.append(next(g for g in f.groups() if g))
                j = f.end()
            path = ".".join(parts)
            assign = ASSIGN.match(text, j) if path else None
            if assign is not None:
                out.add(path)
                value = src.skip_space(j + len(assign.group(0)))
                if value < src.n and text[value] in "{[":
                    src.value(value, path, out)
                elif not LITERAL.match(text, value):
                    out.hide(path)  # a value built elsewhere: a hash may hold any sub-field
            elif not (path and _plain_read(text, j)):
                out.hide("")


def _plain_read(text: str, j: int) -> bool:
    """What follows a field path only reads it: no call, argument, index or mutation."""
    if text.startswith(("(", "<<", ".", "!", "["), j):
        return False
    blank = re.match(r"[ \t]+", text[j:j + 80])
    if blank is None:
        return True
    rest = text[j + blank.end():j + 80]
    word = re.match(r"[A-Za-z_]\w*", rest)
    return not (word and word.group(0) not in RUBY_WORDS) and not rest.startswith(("<<", "@", '"', "'", ":"))


def _args(src: jsparse.Source, text: str, i: int, bindings) -> jsparse.Fields:
    """The request fields of an instance call's arguments: ``(...)``, or arguments after a blank."""
    fields = jsparse.Fields()
    paren = re.compile(r"[ \t]*\(").match(text, i)
    if paren is not None:
        jsparse.collect(src, src.args(paren.end() - 1), False, bindings, fields, ruby=True)
        return fields
    arg = POSITIONAL.match(text, i)
    if arg is not None and arg.group(1) not in RUBY_WORDS:
        copy = jsparse.Source(text[: src.line_end(i)] + ")", ruby=True)
        jsparse.collect(copy, copy.args(arg.end() - 1), False, bindings, fields, ruby=True)
    return fields
