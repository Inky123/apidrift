"""What each resolved JS/TS/Ruby SDK call sends: the second half of :func:`jsparse.extract`, kept apart so
the parser stays small. Fields come from the call's arguments (:func:`jsparse.collect`, with same-file
helpers and spreads), version switches settle afterwards (:mod:`jsswitch`), Ruby adds the calls made on
returned objects (:mod:`rbobjects`), and with a whole-repo table the callers of wrappers defined in other
files are added (:mod:`jscross`).
"""

from __future__ import annotations

from . import jsbare, jshelpers, jsspread, jsswitch, rbobjects


def found(text: str, index, resolved_calls, *, ruby: bool, resolve, table, rel: str) -> list:
    from .jsparse import Fields, Found, collect  # jsparse builds on this module

    out: list[Found] = []
    helpers = jshelpers.Helpers(resolved_calls[0].bindings) if resolved_calls else None
    if helpers is not None and not ruby:
        helpers.src.spreads = jsspread.Reader(helpers)
    for c in resolved_calls:
        fields, via = Fields(), jshelpers.Via(helpers)
        collect(c.args_src, c.spans, jsbare.id_slot(c), c.bindings, fields, ruby=ruby, helper=via)
        jsswitch.settle(helpers, fields, lambda sink, c=c: collect(
            c.args_src, c.spans, jsbare.id_slot(c), c.bindings, sink, ruby=ruby, helper=jshelpers.Via(helpers)))
        out.append(Found(c.line, c.call + via.suffix, c.resolved, fields))
    out += rbobjects.extract(text, resolved_calls, resolve or index.resolve) if ruby else []  # pi.confirm(...)
    if table is not None:  # wrappers defined in other files (jscross builds on jsparse)
        from . import jscross

        out = jscross.extend(text, table, rel, out, resolved_calls, helpers, ruby)
    return out
