"""TypeScript's extra rule for ``wrap_type``: the value's declared type must still check after the edit.

``billing_cycle_anchor: anchor`` becomes ``billing_cycle_anchor: { type: anchor }``, right at runtime. But
in TypeScript ``anchor``'s type is whatever it was declared as, often a Stripe type
(``Stripe.SubscriptionUpdateParams.BillingCycleAnchor``, the old enum), and ``{ type: anchor }`` then does
not type-check under the locked SDK nor after the upgrade (twenty, billing-subscription-update.service.ts).
So in a TypeScript file a value that is not a string literal is refused (``unsupported_value``) unless it
is a local whose type is a string literal type TypeScript infers by itself: ``const NAME = 'now'`` (no
annotation, ``as const`` allowed), the only binding that reaches the use, in the same function, with a
documented value. A ``let`` widens to ``string``, so it is refused too. JavaScript files are unchanged.
"""

from __future__ import annotations

import re

from . import fixvalue

IDENT = r"[A-Za-z_$][\w$]*"
CONST = re.compile(r"(?<![\w$.])const\s+$")
VALUE = re.compile(rf"({IDENT})\s*=\s*('(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\")(?:\s+as\s+const)?\s*(?:;|\n|$)")


def refused(loc, src, vs: int, ve: int, m) -> bool:
    """The rewritten ``wrap_type`` value at ``[vs, ve)`` of a TypeScript file might not type-check."""
    v = src.text[vs:ve].strip()
    leaves = [v] if m.timestamps else fixvalue.js_leaves(v, False)
    if not leaves:
        return True
    for one in leaves:
        if fixvalue._string(one, False) is not None or (m.timestamps and fixvalue.INTEGER.fullmatch(one)):
            continue
        if not (re.fullmatch(IDENT, one) and _const_string(loc, one, vs, m)):
            return True
    return False


def _const_string(loc, name: str, at: int, m) -> bool:
    bindings = loc.bindings
    if bindings is None:
        return False
    reaching = bindings.reaching(name, at)
    if len(reaching) != 1 or reaching[0].value < 0:
        return False
    b, text = reaching[0], bindings.src.text
    if not CONST.search(text[max(0, b.pos - 12) : b.pos]):
        return False
    got = VALUE.match(text, b.pos)
    if got is None or got.group(1) != name:
        return False  # annotated, or not a plain string literal
    if bindings.scopes.innermost(b.pos) != bindings.scopes.innermost(at):
        return False  # bound in another function
    return fixvalue._string(got.group(2), False) in m.wrap_values
