"""Find Stripe calls in PHP and read their array arguments into dotted field paths.

Two call styles are read: static resource classes (``\\Stripe\\PaymentIntent::create([...])``, also through
``use Stripe\\PaymentIntent;``) and ``StripeClient`` services (``$stripe->checkout->sessions->create([...])``,
``$this->owner->stripe()->subscriptions->update($id, [...])``). A static call counts only when its class is
in the ``Stripe`` namespace, because ``Customer::create([...])`` is just as often an Eloquent model. A
service call counts only when the services are properties (``->invoices()->create`` is a relation) and the
thing they hang off is a Stripe client by name, by type, or by ``new StripeClient``.

stripe-php argument order is fixed: one id per ``{placeholder}`` in the path, then the parameters, then
the request options, which are never read as parameters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import phpdispatch, phpflow, phpinstance, phpvendor
from .jsparse import Fields, Found, Span
from .phplex import OPENERS, STRING, Lex, lex
from .phpscope import WRITE, Bindings, Scopes
from .sdkmap import Resolved, without_id

VERB = re.compile(r"(\?->|->|::)\s*([A-Za-z_]\w*)\s*\(")
NEW = re.compile(r"new\s+(\\?[A-Za-z_][\w\\]*)")
USE_HEAD = re.compile(r"(?<![\w$>:\\])use\s+(?!function\b|const\b)")
USE_STOP = re.compile(r"[;(){}]")
NAMESPACE_DECL = re.compile(r"(?<![\w$>:\\])namespace\s+([A-Za-z_][\w\\]*)\s*[;{]")
USE_REACH = 4000  # an import statement longer than this is not read
CLIENT = re.compile(
    r"(?:\$this\s*->\s*|\$)([A-Za-z_]\w*)\s*=\s*new\s+(\\?[A-Za-z_][\w\\]*)"  # $stripe = new StripeClient(
    r"|(?<![\w\\])(\\?[A-Za-z_][\w\\]*)\s+\$([A-Za-z_]\w*)"  # a typed property or parameter: StripeClient $stripe
)
MERGE = re.compile(r"(?:array_merge|array_replace|array_merge_recursive|array_replace_recursive|array_filter)\s*\(")
VARIABLE = re.compile(r"\$([A-Za-z_]\w*)")
PLAIN_KEY = re.compile(r"'([A-Za-z_]\w*)'|\"([A-Za-z_]\w*)\"")
INTEGER = re.compile(r"\d+")
WORD = re.compile(r"[A-Za-z_]\w*$")
NOT_CALLED = {  # ``return (new StripeClient($k))->...``: the parenthesis after these is not a call
    "return", "echo", "print", "yield", "throw", "and", "or", "xor", "case", "else", "clone", "await", "from",
    "include", "include_once", "require", "require_once",
}
CLIENT_CLASSES = {"Stripe\\StripeClient", "Stripe\\StripeClientInterface", "Stripe\\BaseStripeClient"}


@dataclass(frozen=True)
class Element:
    """One element of an array literal: ``key => value`` or a positional value."""

    start: int
    arrow: int  # index of its top-level ``=>``; -1 for a positional element
    value: int  # first token of the value
    end: int  # end of the last token of the value
    stop: int  # the comma after it, or the array's closing bracket


@dataclass(frozen=True)
class Seg:
    name: str
    called: bool = False
    hint: bool = False  # a call whose arguments mention stripe: ``app('stripe')``, ``resolve(StripeClient::class)``


@dataclass
class Call:
    """One resolved SDK call before its arguments are read: what the fixer walks to find edit sites."""

    line: int
    call: str
    resolved: Resolved
    pos: int  # index of the call's ``(``
    spans: list[tuple[int, int]]
    params: int | None  # which argument holds the request parameters
    start: int = -1  # where the call's chain starts
    saved: tuple[str, int] | None = None  # ``$sub->save()``: (variable, where its tracking starts), see phpinstance
    hidden_by: str = ""  # what may change the parameters after the call site (a dispatch wrapper): never edited
    extra: tuple[str, ...] = ()  # key paths a dispatch wrapper adds to the parameters (phpdispatch)


@dataclass
class Parsed:
    src: Lex
    scopes: Scopes
    bindings: Bindings
    calls: list[Call]
    unresolved: list[tuple[int, str]]
    memo: dict = field(default_factory=dict)  # per-parse caches of the modules that read it (phpinstance)


def extract(text: str, resolve, table=None, rel: str = "") -> tuple[list[Found], list[tuple[int, str]]]:
    """Stripe SDK calls in a PHP source, plus (line, call) pairs that look like Stripe but match no endpoint.
    ``table`` (:mod:`crosstable`): wrappers followed across files, for the file at ``rel``."""
    from . import phpcross
    from .phphelpers import PhpHelpers, Via  # builds on this module

    parsed = parse(text, resolve)
    found, helpers = [], PhpHelpers(parsed)
    for c in parsed.calls:
        fields, via = Fields(), Via(helpers)
        collect(parsed, c, fields, via)
        if c.saved is not None and fields.complete and not fields.paths:
            continue  # ``save()`` with nothing assigned sends no request
        found.append(Found(c.line, c.call + via.suffix, c.resolved, fields))
        callers = helpers.callers_of(c)  # calls of a same-file wrapper that forwards its parameter here
        for f in callers:  # what the dispatch wrapper writes or adds reaches them too
            if c.hidden_by:
                f.fields.hide("")
            for path in c.extra:
                phpflow.add_path(f.fields, path)
        found += callers
    return phpcross.extend(parsed, helpers, table, rel, found), parsed.unresolved


def parse(text: str, resolve) -> Parsed:
    src = lex(text)
    scopes = Scopes(src)
    parsed = Parsed(src, scopes, Bindings(src, scopes), [], [])
    uses = _uses(src)
    clients = _clients(src, uses)
    other: dict[int, tuple[int, str]] = {}  # chain start -> a Stripe-looking call that matched no endpoint
    resolved_at: dict[int, Call] = {}
    pending = phpdispatch.calls(src)  # ``wrapper('PaymentIntent', 'create', $p)``, read in text order with the rest
    for m in VERB.finditer(src.code):
        while pending and pending[0][0] < m.start():
            phpdispatch.add(parsed, pending.pop(0), resolve, resolved_at, other)
        walked = walk_back(src, m.start())
        if walked is None:
            continue
        start, segs, root = walked
        verb, chain = m.group(2), _names(segs, root, uses)
        static = m.group(1) == "::" and root == "class" and len(segs) == 1
        owner, on_object = None, None
        if root == "var" and segs[0].name != "this" and not any(s.called for s in segs):
            owner = phpinstance.resource(
                parsed, segs[0].name, start, resolved_at, uses, lambda n, v: _static(n, v, resolve),
            )
        if static:
            resolved = phpinstance.account(_static(chain[0], verb, resolve), src, src.args(m.end() - 1), verb, resolve)
        elif owner is not None:  # a Stripe object: only its own methods and collections (phpinstance)
            on_object = phpinstance.instance(owner, [s.name for s in segs[1:]], verb, resolve)
            resolved = on_object[0] if on_object else None
        else:
            resolved = _service(chain, verb, clients, resolve)
            if resolved is None and root == "var" and segs[0].name != "this":
                resolved = next((r for c in _aliased(parsed, segs, start, uses) if (r := _service(c, verb, clients, resolve))), None)
        call = display(src, start, m.end(2))
        if resolved is None:
            if any("stripe" in n.lower() for n in chain[0] + [verb]) or chain[0][0] in clients:
                other[start] = (src.line(start), call)
            continue
        spans = src.args(m.end() - 1)
        ids = resolved.path.count("{")
        if not on_object and (not spans or src.text[spans[0][0] : spans[0][1]].strip().lower() == "null"):
            resolved = without_id(resolve, resolved)  # Balance::retrieve(): the singleton (sdkmap.without_id)
        if on_object:
            params, saved = on_object[1], None
            if params is not None and params >= len(spans):
                params = None
            if verb == "save":
                saved = (segs[0].name, phpinstance.since(parsed, segs[0].name, start))
        else:
            params, saved = _params(src, spans, ids, id_only=static and verb == "retrieve" and ids == 1), None
        resolved_at[start] = Call(src.line(start), call, resolved, m.end() - 1, spans, params, start, saved)
        parsed.calls.append(resolved_at[start])
    for item in pending:
        phpdispatch.add(parsed, item, resolve, resolved_at, other)
    # ``$owner->stripe()`` on the way to ``->subscriptions->update(`` is that call's receiver, not another call
    parsed.unresolved = [entry for start, entry in sorted(other.items()) if start not in resolved_at]
    return parsed


# --- call chains ---------------------------------------------------------------------


def _word(c: str) -> bool:
    return c.isalnum() or c == "_"


def walk_back(src: Lex, end: int) -> tuple[int, list[Seg], str] | None:
    """The receiver chain that ends at ``end``: where it starts, its segments root first, and what the root
    is (``var``, ``class``, ``new``, ``func`` or ``expr`` for anything this reader does not follow)."""
    text, segs, root, i = src.text, [], "expr", end
    while True:
        j = src.back_space(i)
        start, called, hint = j, False, False
        if j > 0 and text[j - 1] == ")":
            opener = src.back.get(j - 1)
            if opener is None:
                return None
            k = src.back_space(opener)
            word = WORD.search(text, max(0, k - 16), k)
            if word and word.group(0) not in NOT_CALLED:
                called, hint, j = True, "stripe" in text[opener:j].lower(), k
            else:  # a parenthesised receiver: (new \Stripe\StripeClient($key))->customers
                m = NEW.match(text, src.skip_space(opener + 1))
                if m:
                    segs.append(Seg(m.group(1)))
                    root = "new"
                start = opener
                break
        if j == 0 or not _word(text[j - 1]):
            break
        s = j
        while s > 0 and _word(text[s - 1]):
            s -= 1
        name = text[s:j]
        if s > 0 and text[s - 1] == "$":
            p = src.back_space(s - 1)
            if text[p - 2 : p] == "::":  # a static property: self::$stripe->customers
                segs.append(Seg(name, called, hint))
                i = p - 2
                continue
            start = s - 1
            if text[p - 2 : p] != "->" and text[p - 1 : p] != "$":  # not ->$prop or $$name
                segs.append(Seg(name, called, hint))
                root = "var"
            break
        p = src.back_space(s)
        if text[p - 2 : p] in ("->", "::"):
            segs.append(Seg(name, called, hint))
            i = p - 2 - (1 if text[p - 2 : p] == "->" and text[p - 3 : p - 2] == "?" else 0)
            continue
        q = s
        while q > 0 and (_word(text[q - 1]) or text[q - 1] == "\\"):
            q -= 1
        before = src.back_space(q)
        is_new = text[max(0, before - 3) : before] == "new" and not _word(text[before - 4 : before - 3])
        segs.append(Seg(text[q:j], called, hint))
        root, start = ("new" if is_new else "func") if called else "class", q
        break
    segs.reverse()
    return (start, segs, root) if segs else None


def _uses(src: Lex) -> dict[str, list[str]]:
    """``use Stripe\\Checkout\\Session;`` / ``use Stripe\\{A, B as C};``: local name -> namespace parts."""
    out: dict[str, list[str]] = {}
    code = src.code
    for m in USE_HEAD.finditer(code):
        stop = USE_STOP.search(code, m.end(), m.end() + USE_REACH)
        if stop is None or stop.group(0) not in ";{":
            continue  # a closure's ``use (...)``, or not a statement this reader follows
        items = code[m.end() : stop.start()].split(",")
        if stop.group(0) == "{":  # use Stripe\{Customer, PaymentIntent as PI};
            close = USE_STOP.search(code, stop.end(), stop.end() + USE_REACH)
            if close is None or close.group(0) != "}" or code[src.skip_space(close.end()) : src.skip_space(close.end()) + 1] != ";":
                continue
            items = [items[0].strip() + part.strip() for part in code[stop.end() : close.start()].split(",")] if len(items) == 1 else []
        for item in items:
            name, _, alias = item.strip().partition(" as ")
            parts = [p for p in name.strip().split("\\") if p]
            if parts and all(re.fullmatch(r"\w+", p) for p in parts):
                out[alias.strip() or parts[-1]] = parts
    spaces = {m.group(1) for m in NAMESPACE_DECL.finditer(code)}
    if spaces:  # under "": the file's namespace, which a name no ``use`` covers is relative to
        out[""] = [p for p in spaces.pop().split("\\") if p] if len(spaces) == 1 else ["?"]
    return out


def _qualify(name: str, uses: dict[str, list[str]]) -> list[str]:
    """Namespace parts of a class name as written, through the file's ``use`` imports; a copy of stripe-php
    vendored under a prefix (``EDD\\Vendor\\Stripe\\...``) reads as ``Stripe\\...`` (:mod:`phpvendor`)."""
    parts = [p for p in name.split("\\") if p]
    if name.startswith("\\") or not parts:
        return phpvendor.unprefix(parts)
    if parts[0] in uses:
        return phpvendor.unprefix(uses[parts[0]] + parts[1:])
    return phpvendor.unprefix(uses.get("", []) + parts)  # ``namespace App; Stripe\X`` is App\Stripe\X


def _clients(src: Lex, uses: dict[str, list[str]]) -> set[str]:
    """Variable and property names that hold a ``StripeClient``: assigned ``new StripeClient(...)`` or typed so."""
    out: set[str] = set()
    for m in CLIENT.finditer(src.code):
        name, cls = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        if "\\".join(_qualify(cls, uses)) in CLIENT_CLASSES:
            out.add(name)
    return out


Chain = tuple[list[str], list[bool], list[bool]]  # names, which are calls, which calls mention stripe


def _names(segs: list[Seg], root: str, uses: dict[str, list[str]]) -> Chain:
    """Chain names with the class at its root spelled out through ``use`` imports."""
    head = _qualify(segs[0].name, uses) if root in ("class", "new") else [segs[0].name]
    pad = [False] * (len(head) - 1)
    return (
        head + [s.name for s in segs[1:]], pad + [s.called for s in segs], pad + [s.hint for s in segs],
    )


def _aliased(parsed: Parsed, segs: list[Seg], at: int, uses) -> list[Chain]:
    """``$customers = static::stripe()->customers; $customers->create(...)``: the chain with the variable
    replaced by what every binding that reaches it holds, when they all hold the same chain."""
    src = parsed.src
    found: set[tuple[tuple, tuple, tuple]] = set()
    for binding in parsed.bindings.reaching(segs[0].name, at):
        end = src.skip_scalar(binding.value, ";") if binding.value >= 0 else -1
        walked = walk_back(src, end) if end > binding.value >= 0 and binding.literal is None else None
        if walked is None or walked[0] != binding.value:
            return []
        names, called, hints = _names(walked[1], walked[2], uses)
        rest = segs[1:]
        found.add((
            tuple(names + [s.name for s in rest]), tuple(called + [s.called for s in rest]),
            tuple(hints + [s.hint for s in rest]),
        ))
    return [(list(n), list(c), list(h)) for n, c, h in found] if len(found) == 1 else []


def _lookup(names: list[str], verb: str, resolve) -> Resolved | None:
    resolved = resolve(names + [verb])
    if resolved is None and verb.startswith("all") and (len(verb) == 3 or verb[3].isupper()):
        resolved = resolve(names + ["list" + verb[3:]])  # stripe-php calls list ``all``
    return resolved


def _static(names: list[str], verb: str, resolve) -> Resolved | None:
    """``\\Stripe\\Checkout\\Session::create``: the whole class name below ``Stripe`` must be the resource."""
    if len(names) < 2 or names[0] != "Stripe":
        return None
    resolved = _lookup(names, verb, resolve)
    return resolved if resolved is not None and resolved.key.count(".") == len(names) - 1 else None


def _service(chain: Chain, verb: str, clients: set[str], resolve) -> Resolved | None:
    """``<client>->checkout->sessions->create``: services are properties, and they hang off a Stripe client."""
    names, called, hints = chain
    resolved = _lookup(names, verb, resolve)
    if resolved is None:
        return None
    first = len(names) - resolved.key.count(".")
    if first < 1 or any(called[first:]):
        return None
    c = first - 1
    while c > 0 and names[c] in ("v1", "v2") and not called[c]:
        c -= 1
    owner = names[c]
    # evidence: ``new StripeClient`` or a StripeClient type on it, ``app('stripe')``, or a name that says client
    # and nothing else (``$stripe_customer->sources`` is a Customer's collection, not the client's ``sources``)
    is_client = phpinstance.client_name(owner) or hints[c] or (owner in clients and (c == 0 or names[c - 1] == "this"))
    return resolved if is_client else None


def display(src: Lex, start: int, end: int) -> str:
    """A call chain as shown to the user: arguments collapsed to ``()``, whitespace and comments removed."""
    text, out, i = src.text, [], start
    while i < end:
        j = src.skip_space(i)
        if j > i:
            if out and out[-1] == "new":
                out.append(" ")
            i = j
            continue
        if text[i] == "(" and i in src.pairs:
            inner = src.skip_space(i + 1)
            if i == start and text.startswith("new", inner):
                out.append("(" + display(src, inner, src.pairs[i]) + ")")
            else:
                out.append("()")
            i = src.pairs[i] + 1
        elif text.startswith("new", i) and not _word(text[i + 3 : i + 4]) and (i == 0 or not _word(text[i - 1])):
            out.append("new")
            i += 3
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


# --- arguments -----------------------------------------------------------------------


def _params(src: Lex, spans: list[tuple[int, int]], ids: int, *, id_only: bool = False) -> int | None:
    """Index of the parameters argument: after one id per path placeholder, or an earlier array written
    where an id would go (the legacy ``retrieve(['id' => ..., 'expand' => [...]])``). ``id_only``: the
    static ``Resource::retrieve($id, $opts)`` takes no parameters after the id, only request options."""
    for k in range(min(ids, len(spans))):
        i = src.skip_space(spans[k][0])
        if src.array_open(i) is not None or MERGE.match(src.text, i):
            return k
    return ids if ids < len(spans) and not id_only else None


def elements(src: Lex, opener: int) -> list[Element]:
    """The elements of the array literal whose opening bracket is at ``opener``."""
    text, close, out = src.text, src.pairs[opener], []
    i = opener + 1
    while True:
        i = src.skip_space(i)
        if i >= close:
            return out
        if text[i] == ",":
            i += 1
            continue
        start, arrow, last, j = i, -1, i, i
        while j < close:
            j = src.skip_space(j)
            if j >= close or text[j] == ",":
                break
            end = src.masked(j)
            if end:
                j = end
            elif text[j] in OPENERS:
                j = src.pairs.get(j, close - 1) + 1
            elif arrow < 0 and text.startswith("=>", j):
                arrow = j
                j += 2
                continue
            else:
                j += 1
            last = j
        value = src.skip_space(arrow + 2) if arrow >= 0 else start
        out.append(Element(start, arrow, min(value, j), max(last, min(value, j)), j))
        i = j


def literal_key(src: Lex, el: Element) -> tuple[str, int] | None:
    """(key, end of its string token) when the element's key is one plain string literal."""
    span = src.span_at(el.start)
    if el.arrow < 0 or span is None or span[2] != STRING or span[0] != el.start or src.back_space(el.arrow) != span[1]:
        return None
    m = PLAIN_KEY.fullmatch(src.text, el.start, span[1])
    return ((m.group(1) or m.group(2)), span[1]) if m else None


def read_array(src: Lex, opener: int, path: str, out: Fields) -> None:
    """Field paths of an array literal. ``'k' => v`` is a field of ``path``; a positional array inside it is
    an element of a list (``path[]``). A spread or a key that is not a plain string hides this array only."""
    text = src.text
    prefix = path + "." if path else ""
    for el in elements(src, opener):
        if text.startswith("...", el.start):
            _hide(out, path)
            continue
        key = literal_key(src, el)
        positional = el.arrow < 0 or INTEGER.fullmatch(text, el.start, src.back_space(el.arrow)) is not None
        if key is None and not positional:
            _hide(out, path)  # a constant, a variable, a concatenation: any key could be meant
            continue
        if key is None and not path:
            out.hide("")  # the parameters themselves are a list: not something this reader understands
            continue
        child = prefix + key[0] if key else path + "[]"
        if key:
            out.add(child, Span(el.start, key[1], el.stop, "rocket_quoted"))
        inner = src.array_open(el.value)
        if inner is not None:
            read_array(src, inner, child, out)
            if src.pairs[inner] + 1 != el.end:
                _hide(out, child)  # ``[...] + $more``: the literal is only part of the value


def _hide(out: Fields, path: str) -> None:
    out.hide(path + "." if path else "")
    if path:
        out.hide(path + "[].")


def collect(parsed: Parsed, call: Call, out: Fields, helper=None) -> None:
    """Read the request fields of ``call`` into ``out``."""
    src, text = parsed.src, parsed.src.text
    if call.saved is not None:  # ``$sub->save()``: the properties assigned to the object
        phpinstance.saved_fields(parsed, call.saved[0], call.saved[1], call.start, out)
    if call.hidden_by:
        out.hide("")
    for path in call.extra:
        phpflow.add_path(out, path)
    if call.params is None:
        return
    if any(text.startswith("...", src.skip_space(a)) for a, _ in call.spans[: call.params + 1]):
        out.hide("")  # argument unpacking: which argument is which is not visible
        return
    a, b = call.spans[call.params]
    i = src.skip_space(a)
    opener = src.array_open(i)
    if opener is not None:
        read_array(src, opener, "", out)
        if src.skip_space(src.pairs[opener] + 1) < b:
            out.hide("")
        return
    if MERGE.match(text, i):  # array_merge([...], $options): visible only when every argument is (phpflow)
        phpflow.merge_fields(parsed, i, b, out)
        return
    if helper is not None and helper(i, b, out):
        return  # a same-file helper's array (phphelpers)
    m = VARIABLE.match(text, i)
    if m is None or m.group(1) == "this" or src.skip_space(m.end()) < b:
        out.hide("")
        return
    variable_fields(parsed, m.group(1), i, out)


def variable_fields(parsed: Parsed, name: str, at: int, out: Fields) -> None:
    """Fields of the array literals a variable can hold at the call, plus keys written into it before it."""
    phpflow.variable_fields(parsed, name, at, out)
