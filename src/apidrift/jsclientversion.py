"""Whether an expression a helper compares is the API version the file's Stripe client is configured with.

A version switch (:mod:`jsswitch`) is only as good as the version it compares. medusa's

    this.stripe_ = new Stripe(options.apiKey, { apiVersion: (options.apiVersion ?? DEFAULT) as Stripe.LatestApiVersion })
    ...
    const apiVersion = this.options_.apiVersion ?? DEFAULT      // this.options_ = options, once, in the constructor

compares what the client sends. ``event.api_version``, a parameter, a local or module const fixed to an old date
compare something else, and the old key may still go out on the new version. :func:`configured` is True only when
the operand (or the one expression a ``const`` operand is bound to) is, once normalised, the same as the
``apiVersion`` given to a ``new Stripe(..)`` / ``Stripe(..)`` / ``require('stripe')(..)`` client in this file.
Normalised: whitespace, ``this.``, outer parentheses and a trailing ``as T`` dropped, and ``x_`` read as ``y``
where the file assigns ``this.x_ = y`` exactly once. A parameter or a value from another file is never configured.
"""

from __future__ import annotations

import re

from .jsroots import NAME

CLIENT = re.compile(r"(?:\bnew\s+Stripe|(?<![\w$.])Stripe|require\(\s*['\"]stripe['\"]\s*\))\s*\(")
API_VERSION = re.compile(r"(?:^|[{,])\s*['\"]?apiVersion['\"]?\s*:")
CAST = re.compile(r"\s+(?:as|satisfies)\s+[\w$.<>\[\]|, ]+$")
ALIAS = re.compile(rf"this\s*\.\s*({NAME})\s*=\s*({NAME})\s*(?=[;\n])")


def configured(helpers, operand: str, at: int) -> bool:
    """``operand`` (as written at ``at``) is the version the file's Stripe client is configured with."""
    versions = _client_versions(helpers)
    if not versions:
        return False
    aliases = _aliases(helpers)
    if _norm(operand, aliases) in versions:
        return True
    if not re.fullmatch(NAME, operand):
        return False
    bound = helpers.bindings.by_name.get(operand, [])
    if len(bound) != 1 or bound[0].value < 0 or not helpers.scopes.visible(bound[0].pos, at):
        return False
    b, src, text = bound[0], helpers.src, helpers.src.text
    if not re.search(r"\bconst\s+$", text[max(0, b.pos - 20):b.pos]):
        return False
    if any(operand in helpers.scopes.params.get(fn[0], ()) for fn in helpers.scopes.enclosing(at)):
        return False
    from .jsspread import asi_end
    from .jsroots import _statement_end

    value = text[b.value:asi_end(src, b.value, _statement_end(src, b.value))]
    return _norm(value, aliases) in versions


def _client_versions(helpers) -> set[str]:
    cache = helpers.__dict__.get("_client_versions")
    if cache is not None:
        return cache
    src, text, out = helpers.src, helpers.src.text, set()
    for m in CLIENT.finditer(text):
        if helpers.mask.covers(m.start()):
            continue
        args = src.args(m.end() - 1)
        if len(args) < 2:
            continue
        a, b = src.skip_space(args[1][0]), args[1][1]
        if a >= b or text[a] != "{":
            continue
        body_end = src.skip_balanced(a) - 1
        k = a + 1
        while k < body_end:  # top-level entries of the options literal
            e = src.skip_scalar(k)
            entry = text[k:e]
            hit = API_VERSION.match("," + entry)
            if hit is not None:
                out.add(_norm(("," + entry)[hit.end():], {}))
            k = e + 1
    aliases = _aliases(helpers)
    out = {_norm(v, aliases) for v in out}
    helpers.__dict__["_client_versions"] = out
    return out


def _aliases(helpers) -> dict[str, str]:
    cache = helpers.__dict__.get("_version_aliases")
    if cache is None:
        found: dict[str, list[str]] = {}
        for m in ALIAS.finditer(helpers.src.text):
            if not helpers.mask.covers(m.start()):
                found.setdefault(m.group(1), []).append(m.group(2))
        cache = {k: v[0] for k, v in found.items() if len(v) == 1}
        helpers.__dict__["_version_aliases"] = cache
    return cache


def _norm(expr: str, aliases: dict[str, str]) -> str:
    e = expr.strip()
    while True:
        before = e
        e = CAST.sub("", e).strip()
        if e.startswith("(") and e.endswith(")") and _balanced(e[1:-1]):
            e = e[1:-1].strip()
        if e == before:
            break
    e = re.sub(r"\bthis\s*\.\s*", "", e)
    e = re.sub(r"\s+", "", e)
    head = re.match(NAME, e)
    if head and head.group(0) in aliases and e[head.end():head.end() + 1] in (".", ""):
        e = aliases[head.group(0)] + e[head.end():]
    return e


def _balanced(s: str) -> bool:
    depth = 0
    for c in s:
        depth += {"(": 1, ")": -1}.get(c, 0)
        if depth < 0:
            return False
    return depth == 0
