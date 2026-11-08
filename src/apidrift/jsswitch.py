"""Helpers that send a key or its replacement depending on the Stripe API version they compare against.

    getPaymentMethodTypesParams(types) {
      if (!types) return {}
      return apiVersion >= ALLOWED_VERSION        // const ALLOWED_VERSION = "2026-07-29"
        ? { allowed_payment_method_types: types }
        : { payment_method_types: types }
    }

A call that spreads (or ``Object.assign``s, or passes) what such a helper returns sends ``payment_method_types``
only on API versions before 2026-07-29 and ``allowed_payment_method_types`` from it on. The reader records that
as a switch ``(old, new, since)`` (``since`` like ``">=2026-07-29"``) so that impact.py can tell whether the
release that removes ``old`` falls on the new side; the fields themselves stay as read (both keys are sent).

A switch is recorded only when all of these hold, else the helper is read as before (both keys, no switch):

- every value the helper returns is an object literal or a ``return cond ? {..} : {..}`` of literals;
- exactly that choice compares the API version the file's Stripe client is configured with (the expression given
  as ``apiVersion`` to ``new Stripe(..)``, or a ``const`` bound to it: :mod:`jsclientversion`) with a release
  date: a ``'YYYY-MM-DD[.name]'`` literal, or a ``const`` bound once in the file to one, by ``<``, ``<=``, ``>``
  or ``>=``; the branch taken on later versions is the one with ``new``;
- ``new`` is visibly sent in that branch, and ``old`` (or anything below it, or a hidden prefix over it) is in
  no other alternative: not the later-version branch, not any other return.

:func:`settle` then reads the call again with the switch's ``old`` taken out of the helpers that hold that same
switch (``Helpers.mute``). If ``old`` is still sent, the switch is dropped: something else sends it. Otherwise
the switch is kept with whether anything hidden at the call could still send ``old`` (a rest spread that names
``old`` in its own destructuring cannot: :func:`rest_excludes`).
"""

from __future__ import annotations

import re

from . import jsspread
from .jsclientversion import configured
from .jsroots import NAME

DATE = re.compile(r"\d{4}-\d{2}-\d{2}(?:\.[a-z]+)?")
OPERAND = rf"(?:'[^'\\\n]*'|\"[^\"\\\n]*\"|{NAME}(?:\s*\.\s*{NAME})*)"
COMPARISON = re.compile(rf"\s*({OPERAND})\s*(>=|<=|>(?!>)|<(?!<))\s*({OPERAND})\s*", re.S)
FLIP = {">=": "<=", "<=": ">=", ">": "<", "<": ">"}
LATER = {">=": ">=", ">": ">", "<": ">=", "<=": ">"}  # V op T  ->  the later branch is taken when V LATER[op] T


def mark(helpers, d, values, out) -> None:
    """Record the version switches of helper ``d`` (its return ``values``) in ``out``, the fields read from it;
    when the call is being re-read for one of them (``helpers.mute``), leave that switch's old key out."""
    cache = helpers.__dict__.setdefault("_switches", {})
    if d not in cache:
        cache[d] = _switches(helpers, values)
    found = cache[d]
    out.switched |= found
    mute = getattr(helpers, "mute", None)
    if mute in found:
        old = mute[0]
        out.paths -= {p for p in out.paths if _below(p, old)}
        out.hidden -= {h for h in out.hidden if h.startswith((old + ".", old + "["))}
        for p in [p for p in out.spans if _below(p, old)]:
            del out.spans[p]


def settle(helpers, fields, reread) -> None:
    """Keep the switches of a call's ``fields`` whose old key nothing else sends, each with whether a hidden
    argument could still send it: ``(old, new, since, hidden)``. ``reread(out)`` reads the call into ``out``.
    The call ends here: the spread reader's budget (jsspread.MAX_WORK) starts again for each re-read and the next call."""
    reader = helpers.src.spreads if helpers is not None else None
    try:
        if helpers is None or not fields.switched:
            return
        kept = set()
        for switch in sorted(fields.switched):
            old = switch[0]
            sub = type(fields)()
            helpers.mute = switch
            if reader is not None:
                reader.next_call()
            try:
                reread(sub)
            finally:
                helpers.mute = None
            if any(_below(p, old) for p in sub.paths):
                continue  # another literal, write or helper sends it as well
            kept.add((*switch, any(old.startswith(h) for h in sub.hidden)))
        fields.switched = kept
    finally:
        if reader is not None:
            reader.next_call()


def rest_excludes(helpers, name: str, at: int) -> bool:
    """While a switch is being settled: ``...name`` at ``at`` cannot send its old key, because ``name`` is the
    rest of a ``const { old, ...name } = x`` that names it, in scope, never written, and settled (jsstable)."""
    mute = getattr(helpers, "mute", None)
    if mute is None or not re.fullmatch(NAME, name) or "." in mute[0] or "[" in mute[0] or helpers.bindings.by_name.get(name):
        return False
    src, text = helpers.src, helpers.src.text
    n = re.escape(name)
    decls = [
        m for m in re.finditer(rf"\bconst\s*\{{([^{{}}]*?),?\s*\.\.\.\s*{n}\s*\}}\s*=(?!=)", text)
        if not helpers.mask.covers(m.start())
    ]
    if len(decls) != 1 or len(re.findall(rf"\.\.\.\s*{n}\s*\}}\s*=(?!=)", text)) != 1:
        return False
    decl = decls[0]
    if not (decl.end() < at and helpers.scopes.visible(decl.start(), at)):
        return False
    if any(name in helpers.scopes.params.get(fn[0], ()) for fn in helpers.scopes.enclosing(at)):
        return False  # a parameter of an enclosing function shadows it
    from .jsroots import _mutation_re
    from .jsstable import settled

    fn = helpers.scopes.innermost(decl.start())
    lo, hi = (fn[0], fn[1]) if fn else (0, src.n)
    if not settled(src, helpers.bindings, name, decl.start(), at) or any(
        not helpers.mask.covers(w.start()) for w in _mutation_re(name).finditer(text, lo, hi)
    ):
        return False  # written anywhere (it may gain the key), mentioned in a callback, passed on before the spread
    keys = set()
    for part in decl.group(1).split(","):
        if not part.strip():
            continue
        key = re.split(r"[:=]", part, maxsplit=1)[0].strip().strip("'\"")
        if not re.fullmatch(NAME, key or "-"):
            return False  # a computed key or a nested pattern: what it takes out is not visible
        keys.add(key)
    return mute[0] in keys


def version_shaped(helpers, d) -> bool:
    """Helper ``d`` returns a choice whose condition compares something with a release date, whatever it compares:
    the fixer never edits inside such a helper (a branch renamed would undo the switch the code makes on purpose)."""
    from .jsspread import returned_choice

    src, text = helpers.src, helpers.src.text
    for m in re.compile(r"(?<![\w$.])return\b").finditer(text, d.open, d.close):
        if helpers.mask.covers(m.start()):
            continue
        choice = returned_choice(src, src.skip_space(m.end()))
        if choice is None:
            continue
        test = COMPARISON.fullmatch(text, choice[0], choice[1])
        if test and any(_date(helpers, test.group(g), test.start(g)) for g in (1, 3)):
            return True
    return False


def holds(since: str, release: str) -> bool:
    """Whether API version ``release`` takes the later branch of a switch recorded as ``since`` (``">=T"``)."""
    op, threshold = (since[:2], since[2:]) if since.startswith(">=") else (since[:1], since[1:])
    return (release >= threshold) if op == ">=" else (op == ">" and release > threshold)


# --- reading one helper ------------------------------------------------------------------


def _switches(helpers, values) -> frozenset:
    from .jsparse import parse_object
    from .jsspread import returned_choice

    if not values or any(v.literal is None for v in values):
        return frozenset()
    src, text = helpers.src, helpers.src.text
    choices, plain = [], []
    for at in sorted({v.at for v in values}):
        j = src.skip_space(at + len("return")) if text.startswith("return", at) else -1
        choice = returned_choice(src, j) if j >= 0 else None
        if choice is None:
            plain += [parse_object(src, v.literal + 1) for v in values if v.at == at]
            continue
        test = _version_test(helpers, choice[0], choice[1])
        branches = [parse_object(src, k + 1) if k is not None else None for k in choice[2:]]
        if test is None:
            plain += [b for b in branches if b is not None]
        else:
            choices.append((test, branches))
    if len(choices) != 1:
        return frozenset()
    (since, later_is_then), (then, other) = choices[0]
    later, earlier = (then, other) if later_is_then else (other, then)
    if later is None or earlier is None:
        return frozenset()
    found = set()
    for old in sorted(earlier.paths - later.paths):
        if not all(_absent(f, old) for f in [later, *plain]):
            continue
        found |= {(old, new, since) for new in later.paths - earlier.paths}
    return frozenset(found)


def _version_test(helpers, lo: int, hi: int) -> tuple[str, bool] | None:
    """(since, whether the then-branch is the later one) for a condition ``text[lo:hi]`` comparing the API
    version with a release date; None for any other condition."""
    m = COMPARISON.fullmatch(helpers.src.text, lo, hi)
    if m is None:
        return None
    left, op, right = m.group(1), m.group(2), m.group(3)
    date = _date(helpers, right, m.start(3))
    if date is None:
        left, op, right, date = right, FLIP[op], left, _date(helpers, left, m.start(1))
    if date is None or left[:1] in "'\"" or not configured(helpers, left, m.start(1)):
        return None  # not the version the file's Stripe client is configured with (jsclientversion)
    later = LATER[op]
    return later + date, op in (">=", ">")


def _date(helpers, operand: str, at: int) -> str | None:
    """The release date an operand stands for: a quoted literal, or a ``const`` bound once to one."""
    if operand[:1] in "'\"":
        return operand[1:-1] if DATE.fullmatch(operand[1:-1]) else None
    if not re.fullmatch(NAME, operand):
        return None
    bound = helpers.bindings.by_name.get(operand, [])
    if len(bound) != 1 or any(operand in helpers.scopes.params.get(fn[0], ()) for fn in helpers.scopes.enclosing(at)):
        return None
    b, src = bound[0], helpers.src
    text = src.text
    if not re.search(r"\bconst\s+$", text[max(0, b.pos - 20):b.pos]) or not helpers.scopes.visible(b.pos, at):
        return None
    if b.value < 0 or b.value >= src.n or text[b.value] not in "'\"":
        return None
    value, end = src.read_string(b.value)
    eol = text.find("\n", end)
    eol = src.n if eol < 0 else eol
    tail = text[end:eol].strip()
    nxt = text[src.skip_space(eol):src.skip_space(eol) + 1]
    if not DATE.fullmatch(value) or tail not in ("", ";") or (tail == "" and nxt and nxt in jsspread.CONTINUES):
        return None  # ``const V = "2026-07-29" + suffix`` or a value continued on the next line
    return value


def _below(path: str, key: str) -> bool:
    return path == key or path.startswith((key + ".", key + "["))


def _absent(fields, key: str) -> bool:
    """``fields`` cannot send ``key``: not it, nothing below it, no hidden prefix over it, and no ancestor of it
    sent with its contents unseen."""
    if any(_below(p, key) for p in fields.paths) or any(key.startswith(h) for h in fields.hidden):
        return False
    ancestors = [p for p in fields.paths if key.startswith((p + ".", p + "[]."))]
    if not ancestors:
        return True
    deepest = max(ancestors, key=len)
    return any(p != deepest and p.startswith((deepest + ".", deepest + "[].")) for p in fields.paths)
