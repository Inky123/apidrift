"""The receiver chain of a PHP call, read backwards from its ``->method(``, and how it is shown.

Every ``->x(`` of a 5000-call chain used to walk the whole chain again (and print it again): quadratic.
:func:`walk_back` takes a memo of chains already walked, keyed by where they end, since the walk from a
position does not depend on how it was reached; :func:`display` takes a cache of what it has printed from
the same start, and prints on from there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .phplex import Lex

NEW = re.compile(r"new\s+(\\?[A-Za-z_][\w\\]*)")
WORD = re.compile(r"[A-Za-z_]\w*$")
NOT_CALLED = {  # ``return (new StripeClient($k))->...``: the parenthesis after these is not a call
    "return", "echo", "print", "yield", "throw", "and", "or", "xor", "case", "else", "clone", "await", "from",
    "include", "include_once", "require", "require_once",
}


@dataclass(frozen=True)
class Seg:
    name: str
    called: bool = False
    hint: bool = False  # a call whose arguments mention stripe: ``app('stripe')``, ``resolve(StripeClient::class)``


def _word(c: str) -> bool:
    return c.isalnum() or c == "_"


def walk_back(src: Lex, end: int, memo: dict | None = None) -> tuple[int, list[Seg], str] | None:
    """The receiver chain that ends at ``end``: where it starts, its segments root first, and what the root
    is (``var``, ``class``, ``new``, ``func`` or ``expr`` for anything this reader does not follow).
    ``memo``: walks already done in this file, by their end (the walk on from a position is the same
    whichever call reached it, so a chain's inner calls are not walked again for its outer ones)."""
    got = _walk(src, end, memo)
    return (got[0], list(got[1]), got[2]) if got is not None and got[1] else None


def _walk(src: Lex, end: int, memo: dict | None) -> tuple[int, list[Seg], str] | None:
    """(start, segments root first, root), the segments possibly none; None when no chain can end here."""
    if memo is not None and end in memo:
        return memo[end]
    text, segs, root, i = src.text, [], "expr", end
    result: tuple[int, list[Seg], str] | None = None
    failed = False  # an unmatched ``)``: no chain
    while True:
        if memo is not None and i != end and i in memo:  # the rest of the walk is known
            inner = memo[i]
            if inner is None:
                failed = True
            else:
                segs.reverse()
                result = (inner[0], inner[1] + segs, inner[2])
            break
        j = src.back_space(i)
        start, called, hint = j, False, False
        if j > 0 and text[j - 1] == ")":
            opener = src.back.get(j - 1)
            if opener is None:
                failed = True
                break
            k = src.back_space(opener)
            word = WORD.search(text, max(0, k - 16), k)
            if word and word.group(0) not in NOT_CALLED:
                called, hint, j = True, "stripe" in text[opener:j].lower(), k
            else:  # a parenthesised receiver: (new \Stripe\StripeClient($key))->customers
                m = NEW.match(text, src.skip_space(opener + 1))
                if m:
                    segs.append(Seg(m.group(1)))
                    root = "new"
                start = opener
                break
        if j == 0 or not _word(text[j - 1]):
            break
        s = j
        while s > 0 and _word(text[s - 1]):
            s -= 1
        name = text[s:j]
        if s > 0 and text[s - 1] == "$":
            p = src.back_space(s - 1)
            if text[p - 2 : p] == "::":  # a static property: self::$stripe->customers
                segs.append(Seg(name, called, hint))
                i = p - 2
                continue
            start = s - 1
            if text[p - 2 : p] != "->" and text[p - 1 : p] != "$":  # not ->$prop or $$name
                segs.append(Seg(name, called, hint))
                root = "var"
            break
        p = src.back_space(s)
        if text[p - 2 : p] in ("->", "::"):
            segs.append(Seg(name, called, hint))
            i = p - 2 - (1 if text[p - 2 : p] == "->" and text[p - 3 : p - 2] == "?" else 0)
            continue
        q = s
        while q > 0 and (_word(text[q - 1]) or text[q - 1] == "\\"):
            q -= 1
        before = src.back_space(q)
        is_new = text[max(0, before - 3) : before] == "new" and not _word(text[before - 4 : before - 3])
        segs.append(Seg(text[q:j], called, hint))
        root, start = ("new" if is_new else "func") if called else "class", q
        break
    if not failed and result is None:
        segs.reverse()
        result = (start, segs, root)
    if memo is not None:
        memo[end] = result
    return result


def display(src: Lex, start: int, end: int, cache: dict | None = None) -> str:
    """A call chain as shown to the user: arguments collapsed to ``()``, whitespace and comments removed.
    ``cache``: by ``start``, how far a chain from there was shown and what was written; a longer chain from
    the same start goes on from there. (The scan lands exactly on the end of a method name, where a shorter
    chain's display stopped, so what it writes from there is what a fresh scan would write.)"""
    text, out, i = src.text, [], start
    if cache is not None and start in cache and cache[start][0] <= end:
        i, done = cache[start]
        out = list(done)
    while i < end:
        j = src.skip_space(i)
        if j > i:
            if out and out[-1] == "new":
                out.append(" ")
            i = j
            continue
        if text[i] == "(" and i in src.pairs:
            inner = src.skip_space(i + 1)
            if i == start and text.startswith("new", inner):
                out.append("(" + display(src, inner, src.pairs[i]) + ")")
            else:
                out.append("()")
            i = src.pairs[i] + 1
        elif text.startswith("new", i) and not _word(text[i + 3 : i + 4]) and (i == 0 or not _word(text[i - 1])):
            out.append("new")
            i += 3
        else:
            out.append(text[i])
            i += 1
    if cache is not None:
        cache[start] = (end, out)
    return "".join(out)
