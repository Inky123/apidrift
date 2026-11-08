"""Request bodies of PHP raw-HTTP wrappers and their calls, for :mod:`httpphp`.

What a wrapper does to the body it is given (:func:`_writes`): a key it writes on every path is sent by
every caller; one written only under a condition is sent when the condition holds, decided from the
caller's literal path when the condition compares the path parameter with a literal, else hidden.
``$x = apply_filters('hook', $x)`` passes the body through unless the checkout registers a callback on
that hook (:mod:`phpfilters`). What a call sends (:func:`call_fields`): the keys of its body argument.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NamedTuple

from . import httpphppath, phpfilters, phpparse
from .jsparse import Fields

if TYPE_CHECKING:
    from .httpphp import File, Func


class Writes(NamedTuple):
    rewrites: bool  # rebound, or written in a way that is not visible
    adds: set  # literal keys written on every path to the call
    maybe: tuple  # (key, literal the path parameter must equal, or None): keys written only on some paths
    filters: frozenset  # literal filter hooks the array passes through (rewrites if the checkout hooks one)


def _writes(f: File, fn: Func, name: str, before: int, path_name: str | None = None) -> Writes:
    """How ``$name`` changes in ``fn`` before ``before``. ``$x = apply_filters('hook', $x)`` passes it
    through when no callback is registered on the hook (:mod:`phpfilters`); a key written inside an
    ``if`` (or a loop, a ``switch``) is written only on some paths."""
    code, src = f.code, f.src
    rewrites, adds, maybe, filters = False, set(), [], set()
    for m in re.finditer(rf"\${name}\b\s*(=(?![=>])|\.=|\+=|\?\?=)", code[: before]):
        if m.start() < fn.open:
            continue
        if m.group(1) != "=":
            rewrites = True
            continue
        end = src.skip_scalar(m.end(), ";")
        through, hook = phpfilters.filtered(f.text[m.end() : end], name)
        if through and hook is not None:
            filters.add(hook)
        else:
            rewrites = True
    if re.search(rf"&\s*\${name}\b", code[fn.open : before]):
        rewrites = True
    for m in re.finditer(rf"\${name}\s*\[([^\]]*)\]\s*((?:\[[^\]]*\]\s*)*)=(?![=>])", code[: before]):
        if m.start() < fn.open:
            continue
        key = re.fullmatch(r"\s*'([A-Za-z_]\w*)'\s*", f.text[m.start(1) : m.end(1)])
        if key is None or m.group(2).strip():
            rewrites = True
            continue
        when = _condition(f, fn, m.start(), path_name)
        if when is True:
            adds.add(key.group(1))
        else:
            maybe.append((key.group(1), when))
    return Writes(rewrites, adds, tuple(maybe), frozenset(filters))


def _condition(f: File, fn: Func, pos: int, path_name: str | None):
    """True when the statement at ``pos`` runs on every path through ``fn`` to a later call; else the
    literal of ``if ($path === 'lit') {`` around it, or None when the condition is anything else."""
    src, code = f.src, f.code
    block = src.enclosing(pos)
    while block >= 0 and code[block] != "{":
        block = src.parent.get(block, -1)
    if block == fn.open:
        prev = src.back_space(pos)
        if prev > 0 and code[prev - 1] == ")" and (prev - 1) in src.back:
            head = code[max(0, src.back[prev - 1] - 12) : src.back[prev - 1]]
            if re.search(r"\b(?:if|elseif|for|foreach|while)\s*$", head):
                return None  # a brace-less ``if (...) $x['k'] = ...;``
        if re.search(r"\belse\s*$", code[max(0, prev - 8) : prev]):
            return None
        return True
    paren_end = src.back_space(block)
    if path_name and paren_end > 0 and code[paren_end - 1] == ")" and (paren_end - 1) in src.back:
        opener = src.back[paren_end - 1]
        if re.search(r"\bif\s*$", code[max(0, opener - 8) : opener]):
            cond = f.text[opener + 1 : paren_end - 1]
            m = re.fullmatch(rf"\s*\${path_name}\s*===?\s*'([^'\\]*)'\s*|\s*'([^'\\]*)'\s*===?\s*\${path_name}\s*", cond)
            if m:
                return m.group(1) if m.group(1) is not None else m.group(2)
    return None


def call_fields(f, w, args, start: int, paren: int, display: str, several: bool) -> Fields:
    """The fields a wrapper call's body argument sends. When the call stands for several endpoints
    (``several``), keys added by statements after the literal (often under the same condition that picks
    the endpoint) may be sent to only some of them: they are hidden rather than claimed for each."""
    from .httpphp import _default, _empty

    fields = Fields()
    if w.body is None:
        return fields
    if w.body >= len(args):
        if not _empty(w.defaults[w.body] if w.body < len(w.defaults) else None):
            fields.hide("")
        return fields
    a, b = args[w.body]
    opener = f.src.array_open(f.src.skip_space(a))
    if opener is not None and f.src.pairs[opener] + 1 == f.src.back_space(b):
        httpphppath.form_array(f, opener, "", fields)  # a literal: form-encoded keys read too
        return fields
    call = phpparse.Call(f.line(start), display, None, paren, args, w.body)
    from .phphelpers import PhpHelpers, Via

    phpparse.collect(f.parsed, call, fields, Via(PhpHelpers(f.parsed)))
    var = re.fullmatch(r"\s*\$([A-Za-z_]\w*)\s*", f.text[a:b])
    if several and var:
        literal = Fields()
        for binding in f.parsed.bindings.reaching(var.group(1), a):
            if binding.literal is not None:
                phpparse.read_array(f.src, binding.literal, "", literal)
        later = fields.paths - literal.paths
        fields.paths -= later
        for key in later:
            fields.hide(key)
    return fields
