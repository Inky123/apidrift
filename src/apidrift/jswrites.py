"""Keys written into a variable bound to an object literal before the SDK call that sends it.

    const params = { amount: 1 }
    params.customer = id        // a plain one-key write: customer is sent, its value is not visible
    params['metadata'] = meta   // the same with a quoted key
    stripe.paymentIntents.create(params)

Only a plain one-key write statement (``name.k = v`` / ``name['k'] = v``) in the call's own function, after the
binding and before the call, adds its key without hiding the rest: the key is sent (in an ``if`` too, the
conservative reading), and what is under it is not visible (``k.`` stays hidden, since the value replaces
whatever the literal wrote there). Anything else :meth:`Bindings.mutations` finds (a deeper write
``name.k.j = v``, a computed key, ``Object.assign(name, ..)``, ``name.merge!(..)``) still hides every key, as
before, and adds what it shows. Ruby writes (``params[:k] = v``) are read as before.
"""

from __future__ import annotations


def add_writes(src, bindings, binding, at: int, sub) -> None:
    """Add to ``sub`` (the fields of ``binding``'s literal) the keys written into it before ``at``."""
    from .jshelpers import _assign_at
    from .jsparse import parse_object

    for mutation in bindings.mutations(binding.name, binding.literal, at):
        plain = not src.ruby and mutation.key and mutation.literal is None
        if plain and _assign_at(src, mutation.key_end, mutation.end) is not None:
            sub.paths.add(mutation.key)
            sub.hide(mutation.key + ".")
            continue
        sub.hide("")
        if mutation.key:
            sub.paths.add(mutation.key)
        if mutation.literal is not None:
            sub.paths |= parse_object(src, mutation.literal + 1).paths
