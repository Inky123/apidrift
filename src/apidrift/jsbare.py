"""Ruby SDK calls written without parentheses, and JS/Ruby calls that pass no id.

Ruby calls a method without parentheses: ``Stripe::Balance.retrieve``, ``Stripe::Account.retrieve.id``,
``StripeService::Topup.list[:data]``, ``cli.v1.subscriptions.list`` and ``Stripe::Customer.retrieve id``
are all requests. :data:`jsparse.RUBY_CALL` reads a call with ``(`` or with keyword arguments; :func:`calls`
reads the rest. A chain is tried whole and then without its trailing names (``.id``, ``.data``), longest
first, and is taken only when the name that ends it is a request verb (``retrieve``,
``list_payment_methods``): ``client.v1.accounts.capabilities`` is a service object, not a call. Names that follow
a bare call read its result and are never its arguments; a bare positional argument (``retrieve id``) is
read like a parenthesised one. A chain that resolves nowhere is not listed: without parentheses, a constant
or attribute read (``Stripe.api_key``) looks the same.

:func:`no_id` says whether a call passes no id, which picks a resource's singleton form
(:func:`sdkmap.without_id`): ``Stripe::Account.retrieve`` is ``GET /v1/account``, ``retrieve(id)``
``GET /v1/accounts/{account}``.
"""

from __future__ import annotations

import re

from . import jsparse
from .jsroots import display
from .sdkmap import STANDARD_TYPES, snake, without_id

BARE = re.compile(r"(?<![\w:.])(?:::)?((?:[A-Za-z_]\w*(?:[ \t]*\.|[ \t]*::)[ \t]*){1,5}[A-Za-z_]\w*)")
NAMES = re.compile(r"[ \t]*(?:\.|::)[ \t]*")
VERBS = (*STANDARD_TYPES, "del", "modify", "search")
# a word after a bare call that is Ruby syntax, not an argument: ``x = Stripe::Balance.retrieve if ok``
RUBY_WORDS = {
    "if", "unless", "and", "or", "not", "then", "do", "rescue", "while", "until", "end", "in", "ensure", "else",
    "elsif", "when", "case", "begin", "return", "yield",
}
POSITIONAL = re.compile(r"[ \t]+(?=[@$\"']|:[A-Za-z_]|[0-9]|([A-Za-z_]\w*))")
CALLED = re.compile(r"[ \t]*\(")
NIL = {"nil", "null", "undefined"}
RUBY_ID = re.compile(r"(?:id\s*:(?!:)|:id\s*=>|[\"']id[\"']\s*=>)")


def is_verb(name: str) -> bool:
    """``retrieve``, ``list``, ``create_login_link``, ``listPaymentMethods``: a name that is a request."""
    word = snake(name)
    return word in VERBS or any(word.startswith(verb + "_") for verb in VERBS)


def calls(src: jsparse.Source, mask: jsparse.Mask, eligible, resolve, taken: set[int]):
    """(line, call, resolved, pos, args_src, spans) of each bare Ruby call. ``taken``: the chain starts that
    :data:`jsparse.RUBY_CALL` resolved; ``eligible(names)``: whether a chain may be Stripe's."""
    text, out = src.text, []
    for m in BARE.finditer(text):
        start = m.start(1)
        if start in taken or mask.covers(m.start()):
            continue
        names = [p for p in NAMES.split(m.group(1).strip()) if p]
        if len(names) < 2 or not eligible(names):
            continue
        called = CALLED.match(text, m.end()) is not None  # the whole chain is RUBY_CALL's
        for n in range(len(names) - called, 1, -1):
            if not is_verb(names[n - 1]):
                continue
            resolved = resolve(names[:n])
            if resolved is None:
                continue
            end = _end_of(text, start, names[:n])
            args_src, spans, pos = src, [], end
            arg = POSITIONAL.match(text, end) if n == len(names) else None
            if arg and arg.group(1) not in RUBY_WORDS and not jsparse.RUBY_KEYWORD.match(text, arg.end()):
                pos = arg.end() - 1  # the blank before the argument stands for "("
                args_src = jsparse.Source(text[: src.line_end(pos)] + ")", ruby=True)
                spans = args_src.args(pos)
            if not spans:
                resolved = without_id(resolve, resolved)
            out.append((text.count("\n", 0, start) + 1, display(text[start:end]), resolved, pos, args_src, spans))
            break
    return out


def no_id(src: jsparse.Source, spans: list[tuple[int, int]], *, ruby: bool) -> bool:
    """The call passes no id: no argument, ``nil``/``null``/``undefined``, or a hash or options object first.
    In Ruby a hash or keywords that hold ``id:`` pass one: stripe-ruby takes it out (``Util.normalize_id``)."""
    if not spans:
        return True
    a, b = spans[0]
    first = src.text[a:b].strip()
    if first in NIL:
        return True
    if not ruby:
        return first.startswith("{")
    if first.startswith("{"):
        keys = jsparse.parse_object(src, src.skip_space(a) + 1).paths
        return "id" not in keys
    if jsparse.RUBY_KEYWORD.match(first):
        return not any(RUBY_ID.match(src.text[x:y].strip()) for x, y in spans)
    return False


def by_id(resolve, resolved, src: jsparse.Source, spans: list[tuple[int, int]], *, ruby: bool):
    """``resolved`` for a call with these arguments: the singleton form when it passes no id."""
    return without_id(resolve, resolved) if "{" in resolved.path and no_id(src, spans, ruby=ruby) else resolved


def _end_of(text: str, start: int, names: list[str]) -> int:
    """Index just past the last of ``names``, the leading names of the chain that starts at ``start``."""
    i = start
    for name in names:
        i = text.index(name, i) + len(name)
    return i
