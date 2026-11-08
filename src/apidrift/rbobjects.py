"""API calls on objects Stripe returned, in Ruby: stripe-ruby's instance methods.

``pi = Stripe::PaymentIntent.retrieve(id)`` holds a PaymentIntent, so ``pi.confirm(payment_method: pm)`` posts
to ``/v1/payment_intents/{intent}/confirm``. stripe-ruby gives a resource an instance method for each of its
operations on its own item, the class method of the same name minus the id (:func:`instance_op`): ``save``
(the update), ``delete``, ``refresh`` (the retrieve) and every action one segment below the item
(``confirm``, ``cancel``: the SDK method name the spec gives the operation). A GET action is left out:
``account.persons`` and the response attribute ``account.capabilities`` look the same.

Read only for a local whose bindings in the outermost ``def`` or ``do`` block around the use (found by
indentation, :mod:`rbscopes`) all bind the same resource through a resolved retrieve, create or update
(``x = Stripe::X.retrieve(id)``, or both arms of ``x = c ? Stripe::X.create(a) : Stripe::X.create(b)``). The
binding that reaches the use is the last one before it in a block that holds it. A block, ``rescue`` or
loop variable, a parameter, a multiple or compound assignment, or any other value bound to the name, and
nothing is read.

``save`` sends what was written into the object after the binding that reaches it, or after the previous
``save``, in blocks that hold it (``sub.billing_cycle_anchor = "now"``, ``sub[:metadata] = {...}``). Any
other use in between (passed on, a method with arguments, a mutation, a write in a nested block) may
write more, and the call is hidden.

Work is bounded per file (:data:`MAX_WORK` mentions of the bound names); past it no more instance calls
are read in that file.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

from . import jsparse
from .jsbare import POSITIONAL, RUBY_WORDS
from .rbscopes import Scopes
from .sdkmap import Resolved, snake

INSTANCE = {"save": ("update", "post"), "delete": ("delete", "delete"), "refresh": ("retrieve", "get")}
MAKERS = {"retrieve", "create", "update"}  # class methods that return the resource itself
CLASS_ONLY = {"create", "retrieve", "update", "modify", "list", "search", "del"}  # no instance method of that name
MAX_WORK = 50_000
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
PAREN = re.compile(r"[ \t]*\(")
BIND, BAD, USE, OTHER = "bind", "bad", "use", "other"


@dataclass(frozen=True)
class Maker:
    """A resolved call that returns a resource object."""

    start: int  # chain start
    end: int  # just past the call
    item: str  # the resource's item path, its id as ``{}``
    key: tuple[str, ...]  # the alias the call resolved through, minus its verb (``("PaymentIntent",)``)


@dataclass(frozen=True)
class Mention:
    pos: int
    end: int
    kind: str  # BIND, BAD (a binding not read), USE (``name.verb``), OTHER
    maker: Maker | None = None
    verb: str = ""
    after: int = 0  # index just past the selected name of a USE


def extract(text: str, calls: list, resolve) -> list[jsparse.Found]:
    """Instance calls on locals bound to the resolved ``calls`` of one Ruby source (``jsparse.calls``)."""
    makers = sorted((m for c in calls if (m := _maker(text, c)) is not None), key=lambda m: m.start)
    names = sorted({n for m in makers if (n := _bound_name(text, m)) is not None})
    if not names:
        return []
    file = _File(text, calls[0].bindings, makers)
    asked: dict[tuple, Resolved | None] = {}
    out: list[jsparse.Found] = []
    work = 0
    for name in names:
        mentions = file.mentions(name)
        work += len(mentions)
        if work > MAX_WORK:
            break
        for root, group in file.by_root(mentions).items():
            binds = [m for m in group if m.kind == BIND]
            if not binds or any(m.kind == BAD for m in group) or len({(b.maker.item, b.maker.key) for b in binds}) != 1:
                continue
            if file.multiple(name, root):
                continue
            for k, use in enumerate(group):
                if use.kind != USE or (reach := file.reaching(group, k)) is None:
                    continue
                maker = group[reach].maker
                key = (maker.key, maker.item, use.verb)
                if key not in asked:
                    asked[key] = instance_op(resolve, maker.key, maker.item, use.verb)
                if asked[key] is None:
                    continue
                fields = file.written(group, reach, k) if use.verb == "save" else jsparse.Fields()
                jsparse.merge(fields, _args(file.src, text, use.after, file.bindings))
                out.append(jsparse.Found(file.line(use.pos), f"{name}.{use.verb}", asked[key], fields))
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
    if resolved.method == "get" or name in CLASS_ONLY:
        return None
    if ITEM.sub("/{}", resolved.path) == item:  # Subscription#cancel: DELETE on the item itself
        return resolved
    head, _, action = resolved.path.rpartition("/")
    return resolved if ITEM.sub("/{}", head) == item and re.fullmatch(r"[a-z_]+", action) else None


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


class _File:
    """One Ruby source, indexed once: its blocks, newlines, makers and multiple assignments."""

    def __init__(self, text: str, bindings, makers: list[Maker]) -> None:
        self.text, self.bindings, self.mask = text, bindings, bindings.mask
        self.src = jsparse.Source(text, ruby=True)
        self.scopes = Scopes(text)
        self.makers, self.maker_starts = makers, [m.start for m in makers]
        self.newlines = [i for i, c in enumerate(text) if c == "\n"]
        self.multiples = [(x.start(), set(re.findall(r"[a-z_]\w*", x.group(0)))) for x in MULTIPLE.finditer(text)]

    def line(self, pos: int) -> int:
        return bisect.bisect_left(self.newlines, pos) + 1

    def mentions(self, name: str) -> list[Mention]:
        text, out = self.text, []
        for m in re.finditer(rf"(?<![\w@$.:]){re.escape(name)}(?![\w?!])", text):
            p = m.start()
            if self.mask.covers(p):
                continue
            line = text.rfind("\n", 0, p) + 1
            if NOT_BOUND_HERE.search(text, line, p):
                out.append(Mention(p, m.end(), BAD))  # a block, rescue or loop variable, or a parameter
                continue
            assign = ASSIGN.match(text, m.end())
            if assign is not None:
                maker = None
                if assign.group(0).strip() == "=" and not text[line:p].strip():
                    maker = self._value(m.end() + len(assign.group(0)))
                out.append(Mention(p, m.end(), BIND if maker else BAD, maker))
                continue
            sel = SELECT.match(text, m.end())
            if sel is not None and not ASSIGN.match(text, sel.end()):
                out.append(Mention(p, m.end(), USE, verb=sel.group(1), after=sel.end()))
            else:
                out.append(Mention(p, m.end(), OTHER))
        return out

    def by_root(self, mentions: list[Mention]) -> dict[int, list[Mention]]:
        """Mentions grouped by the outermost block around them; mentions outside every block are left out."""
        out: dict[int, list[Mention]] = {}
        for m in mentions:
            root = self.scopes.root(m.pos)
            if root is not None:
                out.setdefault(root, []).append(m)
        return out

    def multiple(self, name: str, root: int) -> bool:
        """``a, name = ...`` somewhere in the block ``root``."""
        lo, hi = self.scopes.span(root)
        for pos, names in self.multiples[bisect.bisect_left(self.multiples, (lo,)):]:
            if pos >= hi:
                return False
            if name in names:
                return True
        return False

    def reaching(self, group: list[Mention], k: int) -> int | None:
        """Index of the binding in force at mention ``k``: the last one before it in a block that holds it."""
        at = group[k].pos
        for j in range(k - 1, -1, -1):
            if group[j].kind == BIND and self.scopes.holds(self.scopes.innermost(group[j].pos), at):
                return j
        return None

    def _value(self, i: int) -> Maker | None:
        """The maker the value from ``i`` to the end of its statement is: one call, or both arms of a ternary."""
        end = self.src.line_end(i)
        a, b = bisect.bisect_left(self.maker_starts, i), bisect.bisect_left(self.maker_starts, end)
        inside = [m for m in self.makers[a:b] if m.end <= end]
        if not inside or len({(m.item, m.key) for m in inside}) != 1:
            return None
        shape, last = [], i
        for m in inside:
            shape += [self.text[last:m.start], "\x00"]
            last = m.end
        value = "".join(shape) + self.text[last:end].split(";", 1)[0]  # ``x = X.retrieve(id); x.save``
        return inside[0] if ONE_CALL.fullmatch(value) or TERNARY.fullmatch(value) else None

    def written(self, group: list[Mention], reach: int, k: int) -> jsparse.Fields:
        """What ``save`` at mention ``k`` sends: the writes after binding ``reach`` (or after the last ``save``
        since) in blocks that hold the call. Any other use, or a use in a nested block, hides it."""
        text, src, at = self.text, self.src, group[k].pos
        out = jsparse.Fields()
        for m in group[reach + 1 : k]:
            inside = self.scopes.holds(self.scopes.innermost(m.pos), at)
            if m.kind == USE and m.verb == "save" and inside:
                out = jsparse.Fields()  # stripe-ruby sends only what changed since the last save
                continue
            parts, keyed, j = [], True, m.end
            while (f := FIELD.match(text, j)) is not None:
                parts.append(next(g for g in f.groups() if g))
                keyed = keyed and (len(parts) == 1 or f.group(1) is None)  # [:k] after the first name
                j = f.end()
            assign = ASSIGN.match(text, j) if parts else None
            if assign is None:
                if not (parts and _plain_read(text, j)):
                    out.hide("")
                continue
            if not inside:
                out.hide("")  # written in a nested block: whether and how often it ran is not visible
                continue
            if not keyed:  # sub.items.first.price = p: a name that may be a method, somewhere under items
                out.add(parts[0])
                out.hide(parts[0])
                continue
            path = ".".join(parts)
            out.add(path)
            value = src.skip_space(j + len(assign.group(0)))
            if value < src.n and text[value] == "{":
                src.object_fields(value + 1, path + ".", out)
            elif value < src.n and text[value] == "[":
                src.array_fields(value + 1, path, out)
            elif not LITERAL.match(text, value):
                out.hide(path)  # a value built elsewhere: a hash may hold any sub-field
        return out


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
    paren = PAREN.match(text, i)
    if paren is not None:
        jsparse.collect(src, src.args(paren.end() - 1), False, bindings, fields, ruby=True)
        return fields
    arg = POSITIONAL.match(text, i)
    if arg is not None and arg.group(1) not in RUBY_WORDS:
        copy = jsparse.Source(text[: src.line_end(i)] + ")", ruby=True)
        jsparse.collect(copy, copy.args(arg.end() - 1), False, bindings, fields, ruby=True)
    return fields
