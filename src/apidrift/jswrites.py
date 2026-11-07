"""Keys written into a variable bound to an object literal before the SDK call that sends it.

    const params = { amount: 1 }
    params.customer = id        // a plain one-key write: customer is sent, its value is not visible
    params['metadata'] = meta   // the same with a quoted key
    stripe.paymentIntents.create(params)

Only a plain one-key write statement (``name.k = v`` / ``name['k'] = v``) in the call's own function, after the
binding and before the call, adds its key without hiding the rest, and only when that binding is the one
binding that can reach the call. The key is sent (inside an ``if`` too: the conservative reading); what is
under it is not visible (``k.`` stays hidden: the value replaces whatever the literal wrote there).

Everything else :meth:`Bindings.mutations` finds (a deeper write ``name.k.j = v``, a computed key,
``Object.assign(name, ..)``, ``name.merge!(..)``) still hides every key and adds what it shows, as before. So
does every write when another binding may reach the call (``let p`` set in both branches of an ``if``): the
caller reads the nearest binding only, and hiding every key keeps that honest. Ruby writes
(``params[:k] = v``) are read as before.
"""

from __future__ import annotations


def reaching_fields(src, bindings, binding, at: int):
    """Fields of a variable argument whose nearest binding ``binding`` is an object literal: the union of every
    binding that can reach the call at ``at`` (``let p`` set in both branches of an ``if``: a key in one branch
    is sent), each with the writes into it before the call. A reaching binding that is not a literal (a call, a
    parameter, another variable, a rebinding later in a loop) may send anything: the top level is hidden, and the
    literals, the nearest one always among them, still add their keys (a key seen counts as sent)."""
    from .jsparse import Fields, merge, variable_fields

    reaching = bindings.reaching(binding.name, at)
    if reaching == [binding]:
        return variable_fields(src, bindings, binding, at)
    out = Fields()
    for b in [binding, *(b for b in reaching if b != binding)]:  # the nearest literal too, as before
        if b.literal is None:
            out.hide("")
        else:
            merge(out, variable_fields(src, bindings, b, at))
    return out


def add_writes(src, bindings, binding, at: int, sub) -> None:
    """Add to ``sub`` (the fields of ``binding``'s literal) the keys written into it before ``at``."""
    from .jshelpers import _assign_at
    from .jsparse import parse_object

    only = not src.ruby and bindings.reaching(binding.name, at) == [binding]
    for mutation in bindings.mutations(binding.name, binding.literal, at):
        plain = only and mutation.key and mutation.literal is None
        if plain and _assign_at(src, mutation.key_end, mutation.end) is not None:
            sub.paths.add(mutation.key)
            sub.hide(mutation.key + ".")
            continue
        sub.hide("")
        if mutation.key:
            sub.paths.add(mutation.key)
        if mutation.literal is not None:
            sub.paths |= parse_object(src, mutation.literal + 1).paths


def merged(src, after: int, end: int, out, helper) -> bool:
    """Ruby: ``{ a: 1 }.merge(expand_options)``, the hash literal argument ending at ``end`` merged (once or in a
    chain) with what same-file helpers return. True when every merge was read through ``helper`` (jshelpers.Via)
    into ``out``; False for anything else (a parameter, a variable, a literal, ``.deep_merge``, JavaScript)."""
    text = src.text
    if not src.ruby or helper is None:
        return False
    while after < end and text.startswith(".merge(", after):
        args = src.args(after + len(".merge"))
        if len(args) != 1:
            return False
        a, b = src.skip_space(args[0][0]), args[0][1]
        if a >= b or text[a] in "{:\"'" or not helper(a, b, out):
            return False
        after = src.skip_space(src.skip_balanced(after + len(".merge")))
    return after >= len(text[:end].rstrip())


def merged_variable(src, bindings, i: int, end: int, out, helper) -> bool:
    """Ruby: ``attributes.merge(expand_options)`` where ``attributes`` is bound to hash literals (every binding that
    reaches, :func:`reaching_fields`) and every merge is a same-file helper's hash (:func:`merged`)."""
    import re

    from .jsparse import Fields, merge

    m = re.compile(r"[A-Za-z_]\w*").match(src.text, i)
    if not src.ruby or m is None or not src.text.startswith(".merge(", m.end()):
        return False
    binding = bindings.at(m.group(0), i)
    if binding is None or binding.literal is None:
        return False
    merges = Fields()
    if not merged(src, m.end(), end, merges, helper):
        return False
    merge(out, reaching_fields(src, bindings, binding, i))
    merge(out, merges)
    return True
