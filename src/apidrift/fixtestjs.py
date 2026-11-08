"""How a Jest / Vitest receiver is wired into a ``vi.mock('stripe', ...)`` / ``jest.mock`` factory.

``expect(mocks.create).toHaveBeenCalledWith(...)`` is an expectation of ``stripe.paymentIntents.create`` only
when the factory wires ``mocks.create`` in at exactly one key path (``default: class { paymentIntents = {
create: mocks.create } }``). :func:`wiring` indexes every such ``key: name`` once per file, in one pass over
each factory, so :mod:`fixtestfind` looks a receiver up instead of rescanning the factories per expectation.
"""

from __future__ import annotations

import re

from .jsparse import CLOSERS, OPENERS, QUOTES, Mask, Source

JS_FACTORY = re.compile(r"\b(?:vi|jest)\s*\.\s*(?:mock|doMock)\(\s*(['\"])stripe\1\s*,")
JS_KEYED = re.compile(r"(?:([A-Za-z_$][\w$]*)|'([\w$]+)'|\"([\w$]+)\")\s*[:=]\s*$")
JS_CLASS = re.compile(r"\bclass(?:\s+[A-Za-z_$][\w$]*)?(?:\s+extends\s+[\w$.]+)?\s*$")
JS_RETURNED = re.compile(r"(?:=>\s*\(|\breturn)\s*$")
JS_WIRED = re.compile(  # ``key: name`` / ``key = name.chain`` inside a mock factory: what a receiver is wired to
    r"(?:([A-Za-z_$][\w$]*)|'([\w$]+)'|\"([\w$]+)\")\s*[:=]\s*(?P<chain>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)(?![\w$.(\[])"
)


def wiring(text: str, src: Source, mask: Mask) -> dict[str, list[tuple[str, ...] | None]]:
    """Every ``key: name`` inside a ``vi.mock('stripe', ...)`` / ``jest.mock`` factory, by name: the key path
    of the object literals around it (outermost first, the key last), or None when one of them is a function
    body, an array or a call (not a plain client mock). Read once per file, in one pass over each factory."""
    out: dict[str, list[tuple[str, ...] | None]] = {}
    for f in JS_FACTORY.finditer(text):
        if mask.covers(f.start()):
            continue
        open_idx = text.index("(", f.start())
        end = src.skip_balanced(open_idx)
        hits = [v for v in JS_WIRED.finditer(text, open_idx, end) if not mask.covers(v.start())]
        memo: dict[int, str | None] = {}
        for v, stack in zip(hits, _stacks(text, src, open_idx, [v.start() for v in hits])):
            keys = _keys(text, src, open_idx, stack, memo) if stack is not None else None
            key = next(g for g in v.groups()[:3] if g)
            out.setdefault(v.group("chain"), []).append(None if keys is None else (*keys, key))
    return out


def _stacks(text: str, src: Source, lo: int, positions: list[int]) -> list[list[int] | None]:
    """The open brackets (their indexes, outermost first) at each of ``positions`` (ascending) inside the
    factory at ``lo``; None from an unmatched closer on. One scan serves every position."""
    out: list[list[int] | None] = []
    stack: list[int] = []
    i, broken = lo + 1, False
    for pos in positions:
        while i < pos and not broken:
            j = src.skip_space(i)
            if j != i:
                i = j
                continue
            c = text[i]
            if c in QUOTES:
                i = src.skip_string(i)
                continue
            if c in OPENERS:
                stack.append(i)
            elif c in CLOSERS:
                if not stack:
                    broken = True
                    break
                stack.pop()
            i += 1
        out.append(None if broken else list(stack))
    return out


def _keys(text: str, src: Source, lo: int, stack: list[int], memo: dict[int, str | None]) -> list[str] | None:
    """Keys of the object literals ``stack`` opens, outermost first; None when one of them is a function body,
    an array or a call. ``memo`` remembers each opener's answer ("" for a returned object or class body)."""
    keys: list[str] = []
    for o in stack:
        if o not in memo:
            memo[o] = _key_of(text, src, lo, o)
        key = memo[o]
        if key is None:
            return None
        if key:
            keys.append(key)
    return keys


def _key_of(text: str, src: Source, lo: int, o: int) -> str | None:
    tail = text[max(lo, o - 120) : o]
    if text[o] == "(":
        return "" if tail.rstrip().endswith("=>") and text[src.skip_space(o + 1)] == "{" else None
    if text[o] != "{":
        return None
    keyed = JS_KEYED.search(tail)
    if keyed:
        return next(g for g in keyed.groups() if g)
    return "" if JS_CLASS.search(tail) or JS_RETURNED.search(tail) else None
