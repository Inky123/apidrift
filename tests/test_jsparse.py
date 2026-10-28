"""Positions and reaching bindings: what the fixer relies on in jsparse, jsroots and pyparse."""

from apidrift import jsparse, jsroots, pyparse
from apidrift.jsparse import Mask, Source
from apidrift.sdkmap import Resolved


def spans_of(text, *, ruby=False):
    src = Source(text, ruby=ruby)
    fields = jsparse.parse_object(src, text.index("{") + 1)
    return {path: [(text[s.key_start:s.key_end], s.kind, text[s.entry_end:s.entry_end + 1]) for s in spans] for path, spans in fields.spans.items()}


def test_object_fields_records_a_span_and_kind_for_every_key():
    js = "{ 'a': 1, \"b\": [2], c: { d: 3 }, e, f(x) { return 1 } }"
    assert spans_of(js) == {
        "a": [("'a'", "quoted", ",")], "b": [('"b"', "quoted", ",")], "c": [("c", "bare", ",")],
        "c.d": [("d", "bare", "}")], "e": [("e", "shorthand", ",")],
    }
    rb = "{ :a => 1, \"b\" => 2, c: 3, 'd' => 4 }"
    assert spans_of(rb, ruby=True) == {
        "a": [(":a", "symbol", ",")], "b": [('"b"', "rocket_quoted", ",")], "c": [("c", "bare", ",")],
        "d": [("'d'", "rocket_quoted", "}")],
    }


def test_spans_index_the_original_text_despite_optional_chaining_rewrites():
    text = "{ a: x?.y, b: z!.w, c: 1 }"
    src = Source(text)
    assert src.text != text and len(src.text) == len(text)
    fields = jsparse.parse_object(src, 1)
    assert [text[s.key_start:s.key_end] for path in ("a", "b", "c") for s in fields.spans[path]] == ["a", "b", "c"]


def test_duplicate_keys_keep_both_spans():
    assert len(spans_of("{ a: 1, a: 2 }")["a"]) == 2


def bindings_for(text, *, ruby=False):
    src = Source(text, ruby=ruby)
    mask = Mask(src.text, ruby=ruby)
    scopes = jsroots.Scopes(src, mask)
    return jsroots.Bindings(src, mask, scopes)


def test_reaching_keeps_conditional_bindings_and_stops_at_an_unconditional_one():
    text = (
        "function other() { params = { z: 0 }; }\n"
        "function go(flag) {\n"
        "  let params = { old: 1 };\n"
        "  params = { base: 1 };\n"
        "  if (flag) { params = { a: 1 }; } else { params = { b: 1 }; }\n"
        "  params.c = 3;\n"
        "  params['d'] = 4;\n"
        "  return stripe.paymentIntents.create(params);\n"
        "}\n"
    )
    b = bindings_for(text)
    at = text.index("create(params)") + len("create(")
    reaching = b.reaching("params", at)
    assert [text[x.literal:x.literal + 6] for x in reaching] == ["{ b: 1", "{ a: 1", "{ base"]
    assert [x.literal for x in reaching] == sorted((x.literal for x in reaching), reverse=True)
    mutations = b.mutations("params", reaching[-1].pos, at)
    assert [(m.key, text[m.key_start:m.key_end], text[m.start:m.end]) for m in mutations] == [
        ("c", "c", "params.c = 3"), ("d", "d", "params['d'] = 4"),
    ]
    assert b.reaching("params", text.index("params = { z")) == []  # before any binding in `other`; go() is not visible
    assert [x.literal for x in b.reaching("params", text.index("; }"))] == [text.index("{ z")]


def test_reaching_in_ruby_uses_the_window_since_the_last_route_keyword():
    text = (
        "params = { top: 1 }\n"
        "post '/x' do\n"
        "  params = { a: 1 } if flag\n"
        "  params = { b: 1 } unless flag\n"
        "  params[:c] = 1\n"
        "  Stripe::PaymentIntent.create(params)\n"
        "end\n"
    )
    b = bindings_for(text, ruby=True)
    at = text.index("create(params)") + len("create(")
    reaching = b.reaching("params", at)
    assert [text[x.literal:x.literal + 6] for x in reaching] == ["{ b: 1", "{ a: 1"]
    m = b.mutations("params", reaching[-1].pos, at)
    assert [(x.key, text[x.key_start:x.key_end], text[x.start:x.end]) for x in m] == [("c", "c", "params[:c] = 1")]


def test_python_reaching_follows_statement_lists():
    text = (
        "import stripe\n"
        "def go(flag):\n"
        "    params = {'old': 1}\n"
        "    params = {'base': 1}\n"
        "    if flag:\n"
        "        params = {'a': 1}\n"
        "    else:\n"
        "        params = {'b': 1}\n"
        "    params['c'] = 1\n"
        "    try:\n"
        "        return stripe.PaymentIntent.create(**params)\n"
        "    except Exception:\n"
        "        raise\n"
    )
    calls, _ = pyparse.calls(text, lambda chain: Resolved("post", "/v1/payment_intents", "x"))
    (c,) = calls
    bindings, scope, dominated = c.scopes.reaching("params", c.node, c.fn)
    assert [b[0] for b in bindings] == [8, 6, 4] and scope is c.fn and dominated
    assert [(m[0], m[1]) for m in c.scopes.mutation_nodes("params", scope, 4, c.node.lineno)] == [(9, ["c"])]
