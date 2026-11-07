"""API calls on objects stripe-php returned: ``$pi = \\Stripe\\PaymentIntent::retrieve($id); $pi->confirm([...])``.

stripe-php generates an instance method for every custom operation on one object of a resource
(``POST /v1/payment_intents/{intent}/confirm`` is ``$pi->confirm($params, $opts)``), plus ``save()`` (the update,
sending the properties assigned since), ``delete()`` and ``refresh()``, and the list properties of an object are
collections (``$customer->sources->create($params)`` is ``POST /v1/customers/{customer}/sources``). Each is mapped
only through the spec's own SDK metadata (:mod:`sdkmap`): the object's collection path plus the method name must
be an alias whose path is that object's own, ``/v1/<collection>/{id}[/<action>]``.

What the variable holds is known only from code that leaves no doubt, in the same function:

- it is assigned exactly once, and that assignment is a whole statement ``$x = <resolved Stripe call>;`` that
  returns the resource (its ``create``, ``retrieve`` or ``update``), or
- it is a parameter declared with a stripe-php resource class (``\\Stripe\\Customer $customer``) and never assigned.

Anything else (a reference, ``foreach ... as``, ``global``, a second assignment, a value from a helper) means the
object is not known, and no call on it is mapped.
"""

from __future__ import annotations

import re

from .jsparse import Fields
from .phpscope import BIND, OPAQUE

Owner = tuple[str, ...]  # an object's collection path below /v1: ("payment_intents",), ("checkout", "sessions")

RETURNS_RESOURCE = {"create", "retrieve", "update", "modify"}
STATIC_ONLY = RETURNS_RESOURCE | {"list", "all", "search", "constructFrom"}
INSTANCE_STANDARD = {"save": ("update", "post"), "delete": ("delete", "delete"), "refresh": ("retrieve", "get")}
COLLECTION_VERBS = {"create": "create", "all": "list", "retrieve": "retrieve"}
PARAM = r"^\s*(?:(?:public|private|protected|readonly)\s+)*\??(\\?[A-Za-z_][\w\\]*)\s+\${}\s*(?:=|$)"
PROPERTY = re.compile(r"\s*->\s*([A-Za-z_]\w*)")
OP_ASSIGN = re.compile(r"(?:\*\*|<<|>>|\?\?|[-+*/.%&|^])=|\+\+|--")
CLIENT_WORDS = {"stripe", "client", "api", "sdk", "get", "service", "instance", "default", "new", "make", "the", "my", "app"}
WORDS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def client_name(name: str) -> bool:
    """A name that says StripeClient and nothing else: ``$stripe``, ``stripe()``, ``$stripeClient``,
    ``getStripeClient()``; not ``$stripe_customer`` or ``createAsStripeCustomer()``, which name a resource."""
    words = [w.lower() for w in WORDS.findall(name)]
    return "stripe" in words and all(w in CLIENT_WORDS for w in words)


def owner_of(resolved) -> Owner | None:
    """The collection whose object a resolved ``create`` / ``retrieve`` / ``update`` call returns."""
    verb = resolved.key.rsplit(".", 1)[-1]
    segs = resolved.path.strip("/").split("/")
    if segs[0] != "v1" or verb not in RETURNS_RESOURCE:
        return None
    holes = [i for i, s in enumerate(segs) if s.startswith("{")]
    if verb == "create" and resolved.method == "post" and not holes:
        return tuple(segs[1:])
    if verb != "create" and holes == [len(segs) - 1] and resolved.method == ("get" if verb == "retrieve" else "post"):
        return tuple(segs[1:-1])
    return None


def resource(parsed, name: str, at: int, starts: dict, uses, static) -> Owner | None:
    """The collection of the stripe-php object the local variable ``$name`` holds at ``at``, when certain.
    ``starts``: resolved calls by the start of their chain; ``static``: phpparse's static-call resolver."""
    src, scopes, bindings = parsed.src, parsed.scopes, parsed.bindings
    body = scopes.innermost(at)
    lo, hi = scopes.extent(at)
    mine = [u for u in bindings.uses.get(name, ()) if bindings._scope[u.pos] == body]
    if any(u.kind == OPAQUE for u in mine) or scopes.arrow_parameter(name, at) or bindings.unbounded(lo, hi):
        return None
    binds = [b for b in bindings.by_name.get(name, ()) if bindings._scope[b.pos] == body]
    if scopes.parameter(name, at):
        return None if binds else _typed(parsed, name, body, uses, static)
    if len(binds) != 1 or binds[0].pos > at:
        return None
    call = starts.get(binds[0].value)
    if call is None or src.text[src.skip_space(src.pairs[call.pos] + 1) : src.skip_space(src.pairs[call.pos] + 1) + 1] != ";":
        return None  # ``$x = X::retrieve($id)->charges;``, ``$x = X::create($p) ?: null;``
    return owner_of(call.resolved)


def _typed(parsed, name: str, body, uses, static) -> Owner | None:
    """``function f(\\Stripe\\Customer $customer)``: PHP itself checks what the parameter holds. A union type,
    a reference or a variadic parameter is not read."""
    src, scopes = parsed.src, parsed.scopes
    if body is None:
        return None
    header = max((h for h in scopes.headers if src.pairs[h] < body[0]), default=None)
    if header is None:
        return None
    pattern = re.compile(PARAM.replace("{}", re.escape(name)))
    for a, b in src.args(header):
        m = pattern.match(src.code[a:b])
        if m:
            from .phpparse import _qualify  # phpparse builds on this module

            parts = _qualify(m.group(1), uses)
            resolved = static(parts, "retrieve") if len(parts) >= 2 and parts[0] == "Stripe" else None
            return owner_of(resolved) if resolved is not None else None
    return None


def instance(owner: Owner, props: list[str], verb: str, resolve):
    """(resolved, which argument holds the parameters) for ``$object[->prop]->verb(`` on an object of ``owner``."""
    base = "/v1/" + "/".join(owner) + "/{"
    if not props:
        if verb in STATIC_ONLY:
            return None  # static methods called through an object: ids are arguments again
        standard, method = INSTANCE_STANDARD.get(verb, (verb, None))
        chain = list(owner) + [standard]
        r = resolve(chain)
        if r is None or r.key != ".".join(chain) or not r.path.startswith(base) or r.path.count("{") != 1:
            return None
        rest = r.path[r.path.index("}", len(base)) + 1 :]
        if (method and r.method != method) or (rest and (rest.count("/") != 1 or not rest.startswith("/"))):
            return None
        if verb not in INSTANCE_STANDARD and not rest and r.method != "delete":
            return None  # a custom method on the object's own path is only ever its delete (Subscription.cancel)
        return r, (None if verb in ("save", "refresh") else 0)
    if len(props) != 1 or verb not in COLLECTION_VERBS:
        return None
    chain = list(owner) + [props[0], COLLECTION_VERBS[verb]]
    r = resolve(chain)
    if r is None or r.key != ".".join(chain) or not r.path.startswith(base) or r.path.count("{") != (2 if verb == "retrieve" else 1):
        return None
    tail = r.path[r.path.index("}", len(base)) + 1 :]
    want = "/" + props[0]
    if tail != want and not (verb == "retrieve" and tail.startswith(want + "/{") and tail.endswith("}")):
        return None
    return r, (1 if verb == "retrieve" else 0)


def since(parsed, name: str, at: int) -> int:
    """Where the object ``$name`` holds at ``at`` came to be: its one assignment, else its function's start."""
    bindings, body = parsed.bindings, parsed.scopes.innermost(at)
    binds = [b.pos for b in bindings.by_name.get(name, ()) if bindings._scope[b.pos] == body and b.pos < at]
    return max(binds, default=parsed.scopes.extent(at)[0])


def saved_fields(parsed, name: str, start: int, at: int, out: Fields) -> None:
    """``$sub->default_payment_method = $pm; $sub->save();``: the properties assigned before ``save()`` are its
    fields. Anything else done to the object before the call (passed on, a method called on it, a dynamic
    write, ``save()`` inside a loop) hides what else it sends; a nested write hides that property's contents."""
    from .phpparse import read_array  # phpparse builds on this module

    src, text, bindings = parsed.src, parsed.src.text, parsed.bindings
    body = parsed.scopes.innermost(at)
    if bindings._same_loop(at, at):
        out.hide("")  # values assigned after the call reach it on the next pass
    for u in bindings.uses.get(name, ()):
        if bindings._scope[u.pos] != body or not start <= u.pos < at or u.kind == BIND:
            continue
        m = PROPERTY.match(text, u.pos + 1 + len(name))
        if m is None:
            out.hide("")  # passed on, returned, ``$sub['k'] = v``: not followed here
            continue
        prop, j = m.group(1), src.skip_space(m.end())
        if text.startswith("(", j):
            out.hide("")  # a method of the object: it may set values too
        elif text.startswith("=", j) and text[j + 1 : j + 2] not in ("=", ">"):
            out.add(prop)
            opener = src.array_open(src.skip_space(j + 1))
            if opener is not None:
                read_array(src, opener, prop, out)
        elif OP_ASSIGN.match(text, j):
            out.add(prop)
        elif _assigned(src, j):  # ``$sub->metadata['k'] = v``, ``$sub->items->data = ...``
            out.add(prop)
            out.hide(prop + ".")
            out.hide(prop + "[].")


def _assigned(src, j: int) -> bool:
    """Whether the subscripts and properties starting at ``j`` end in an assignment (a method call: no)."""
    text, moved = src.text, False
    while True:
        j = src.skip_space(j)
        if text.startswith("[", j) and j in src.pairs:
            j, moved = src.pairs[j] + 1, True
            continue
        m = PROPERTY.match(text, j)
        if m and not text.startswith("(", src.skip_space(m.end())):
            j, moved = m.end(), True
            continue
        break
    j = src.skip_space(j)
    return moved and ((text.startswith("=", j) and text[j + 1 : j + 2] not in ("=", ">")) or bool(OP_ASSIGN.match(text, j)))
