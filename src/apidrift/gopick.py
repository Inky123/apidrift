"""Which stripe-go resource a call reaches when its receiver's name does not say it.

* An app accessor named its own way (``s.PromoCodes().List(&stripe.PromotionCodeListParams{...})``, where
  the client field is ``PromotionCodes``): the params type names the resource. stripe-go calls the params
  of a resource ``T`` ``TParams`` and those of a method ``M`` ``TMParams`` (``PromotionCodeListParams``),
  so the type with the method's own suffix taken off is the resource, and the call is read as that client
  field's method. Only a typed literal or a variable declared with one counts; a Go variable keeps its type.
* A method that reaches a resource's top-level path or the same path under a parent (``taxid.New`` posts to
  ``/v1/customers/{customer}/tax_ids`` when ``params.Customer`` is set, else to ``/v1/tax_ids``): the
  params decide, as stripe-go does. A visible parent field picks the nested operation; params read whole
  without one pick the other; params that may hold more decide nothing.
"""

from __future__ import annotations

from .gomap import API, VIA, flat
from .govars import Reader
from .golex import ID
from .sdkmap import Resolved, plural

SUFFIXES = {  # stripe-go method -> the suffixes its params type may carry after the resource
    "new": ("Create", ""), "get": ("Retrieve", ""), "getbyid": ("Retrieve", ""), "update": ("Update", ""),
    "del": ("Delete", ""), "list": ("List",), "search": ("Search",),
}


def params_type(f, paren: int) -> str | None:
    """The stripe-go struct type of a call's last argument: ``&stripe.T{...}``, or a variable declared as one."""
    src = f.src
    args = src.split(paren + 1, src.match[paren])
    if not args:
        return None
    a, b = src.trim(*args[-1])
    if a >= b:
        return None
    reader = Reader(src, f.go, ("", ""), f.roots)
    j = a + 1 if src.tok[a] == "&" else a
    found = reader.typed(j, b)
    if found:
        return found[0]
    if b - j != 1 or src.kind[j] != ID:
        return None
    decl = f.vars.decl_for(src.tok[j], j)
    if decl is None or decl.tok == j:
        return None
    if decl.zero:
        return decl.zero
    whole = reader.whole(*decl.value) if decl.value is not None and decl.declares else None
    return whole[0] if whole else None


def from_params(f, paren: int, verb: str) -> str | None:
    """The ``client.API`` field whose ``verb`` takes the params type of this call, if it names one."""
    struct = params_type(f, paren)
    if not struct or not struct.endswith("Params"):
        return None
    name = struct[: -len("Params")]
    for suffix in SUFFIXES.get(verb.lower(), (verb, "")):
        if not name.endswith(suffix) or len(name) == len(suffix):
            continue
        base = name[: len(name) - len(suffix)].lower()
        for owner in dict.fromkeys((base if base.endswith("s") else plural(base), base)):
            if f.go.owns(API, owner):
                return owner
    return None


def by_parent(f, style: str, owner: str, verb: str, paren: int) -> Resolved | None:
    """The operation of a method that reaches a path or the same path under a parent, picked by its params."""
    go = f.go
    top = go.resolve(style, owner, verb + VIA)
    if top is None or params_type(f, paren) is None:
        return None  # nil params (stripe-go would panic), or params whose struct is not written here
    params = f.read(paren, top)
    for name in sorted({flat(path) for path in params.fields if "." not in path and "[" not in path}):
        nested = go.resolve(style, owner, f"{verb}{VIA}{name}")
        if nested is not None:
            return Resolved(nested.method, nested.path, f"{owner}.{verb}")
    return Resolved(top.method, top.path, f"{owner}.{verb}") if "" not in params.hidden else None
