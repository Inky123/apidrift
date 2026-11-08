"""RSpec examples that compare a builder method's result with a literal hash of the expected parameters.

The fix may rename a key inside a private method that builds a call's parameters (``payment_url_payload``
returns the hash ``Stripe::Checkout::Session.create`` is given). Its spec often does::

    let(:payment_url_payload) { service.__send__(:payment_url_payload, intent) }
    let(:payload) { { mode: "payment", payment_method_types: [...] } }
    it { expect(payment_url_payload).to eq(payload) }

That ``payload`` literal is the expected request, and it is tied to the edit only when all of these hold:

- the spec is ``spec/<x>_spec.rb`` for the edited ``app/<x>.rb`` or ``lib/<x>.rb``, and its top-level
  ``describe`` names the class the builder is defined in;
- the builder is defined once in that file, an edited line of this migration lies inside it, the call the
  fix changed is outside it (it builds, it does not send), and the file calls it;
- the actual value is a ``let``/``subject`` that is exactly ``<x>.__send__(:builder, ...)`` (or ``send``);
- the expected ``let`` is defined once and is one hash literal, and every use of it is ``eq(...)`` against
  such an actual value or the start of a ``merge``/``deep_merge``/``except``/``dup`` whose result is used
  the same way. Any other use (passed to a factory, splatted, indexed) and the literal is left alone.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import dataclass

from .fixtestfind import Expect
from .jsparse import Mask, Source

SPEC = re.compile(r"(?P<pre>(?:.*/)?)spec/(?P<rest>.+)_spec\.rb")
SCOPE = re.compile(r"(module|class)\s+(?!<<)((?:::)?[A-Z][\w:]*)")
DEF = re.compile(r"def\s+(?:self\.)?([a-z_]\w*[?!]?)")
DESCRIBE = re.compile(r"^(?:RSpec\s*\.\s*)?describe\s*\(?\s*(?:::)?([A-Z][\w:]*)", re.M)
LET = re.compile(r"\b(?:let!?|subject)\(\s*:(\w+[?!]?)\s*\)\s*(\{|do\b)")
SENDS = re.compile(r"\s*[\w:.]+\s*\.\s*(?:__send__|send|public_send)\(\s*:(\w+[?!]?)\s*[,)]")
DERIVES = r"\s*\.\s*(?:merge|deep_merge|except|dup)\b"
DERIVED_LET = re.compile(r"\s*(\w+)" + DERIVES)
LOCAL = re.compile(r"(?m)^[ \t]*(\w+)\s*=\s*(\w+)" + DERIVES)
COMPARE = re.compile(r"\bexpect\(\s*(\w+[?!]?)\s*\)\s*\.\s*to\s+eq\(\s*(\w+)\s*\)")


@dataclass(frozen=True)
class Builder:
    file: str  # the edited app file
    klass: str  # ``Invoices::Payments::StripeService``
    method: str
    path: str
    target: str
    head: str  # the fixed call as written (``::Stripe::Checkout::Session.create``)


def builders(file: str, original: str, content: str, sites: list[tuple[str, str, int, str]]) -> list[Builder]:
    """Builder methods of an edited Ruby file: ``sites`` holds (path, target, call line, call head)."""
    lines, changed = original.splitlines(), set()
    matcher = difflib.SequenceMatcher(None, lines, content.splitlines(), autojunk=False)
    for tag, a, b, _, _ in matcher.get_opcodes():
        if tag != "equal":
            changed |= set(range(a + 1, b + 1))
    defs = _defs(lines)
    counts = Counter(name for name, *_ in defs)
    out = []
    for name, start, end, klass in defs:
        outside = "\n".join(lines[: start - 1] + lines[end:])
        if counts[name] != 1 or not re.search(rf"(?<![\w:]){re.escape(name)}(?![\w?!])", outside):
            continue
        for path, target, line, head in sites:
            leaf = re.escape(target.rsplit(".", 1)[-1])
            if start <= line <= end:
                continue  # the call is inside: this method sends, it does not only build
            if any(start < n < end and re.search(rf"(?<![\w@$]){leaf}(?!\w)", lines[n - 1]) for n in changed):
                out.append(Builder(file, klass, name, path, target, head))
    return out


def _defs(lines: list[str]) -> list[tuple[str, int, int, str]]:
    """(name, def line, end line, enclosing class path) by indentation, the layout rubocop keeps."""
    scopes: list[tuple[int, str]] = []
    out = []
    for n, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        while scopes and indent <= scopes[-1][0]:
            scopes.pop()
        scope = SCOPE.match(stripped)
        if scope:
            scopes.append((indent, scope.group(2).lstrip(":")))
            continue
        d = DEF.match(stripped)
        if not d:
            continue
        end = next(
            (k for k in range(n + 1, len(lines) + 1)
             if lines[k - 1].strip() and len(lines[k - 1]) - len(lines[k - 1].lstrip()) <= indent),
            None,
        )
        if end is not None and re.fullmatch(r"end\b.*", lines[end - 1].strip()) and \
                len(lines[end - 1]) - len(lines[end - 1].lstrip()) == indent:
            out.append((d.group(1), n, end, "::".join(name for _, name in scopes)))
    return out


def find(text: str, file: str, sources: list[Builder]) -> list[Expect]:
    spec = SPEC.fullmatch(file)
    if not spec or not sources:
        return []
    pre, rest = spec.group("pre"), spec.group("rest")
    mask, src = Mask(text, ruby=True), Source(text, ruby=True)
    described = next((m.group(1) for m in DESCRIBE.finditer(text) if not mask.covers(m.start())), None)
    mine = [b for b in sources if b.file in (f"{pre}app/{rest}.rb", f"{pre}lib/{rest}.rb") and b.klass == described]
    if not mine:
        return []
    bodies: dict[str, list[tuple[int, int]]] = {}
    for m in LET.finditer(text):
        if not mask.covers(m.start()):
            span = _body(text, src, m)
            bodies.setdefault(m.group(1), []).append(span if span else (-1, -1))
    actual = {n: m for n, spans in bodies.items() if len(spans) == 1 and (m := _sent(text, src, *spans[0]))}
    derived = {n: d.group(1) for n, spans in bodies.items() if len(spans) == 1 and spans[0][0] >= 0
               and (d := DERIVED_LET.match(text[spans[0][0] : spans[0][1]]))}
    out = []
    for name, spans in bodies.items():
        if len(spans) != 1 or spans[0][0] < 0:
            continue
        lit = _literal(text, src, *spans[0])
        method, derivations = _compared_only(text, mask, src, name, actual, derived, bodies) if lit else (None, [])
        for b in mine:
            if lit and method == b.method and not _names_key(text, mask, derivations, b.target):
                out.append(Expect("builder", b.head, lit[0], lit[1], b.head + "(", ")", (), (b.path, b.target)))
    return sorted(out, key=lambda e: e.lo)


def _body(text: str, src: Source, m: re.Match[str]) -> tuple[int, int] | None:
    """Inside of a ``let`` block: between its braces, or between ``do`` and the ``end`` at the let's indent."""
    if m.group(2) == "{":
        end = src.skip_balanced(m.end() - 1)
        return (m.end(), end - 1) if text[end - 1 : end] == "}" else None
    line_start = text.rfind("\n", 0, m.start()) + 1
    indent = re.match(r"[ \t]*", text[line_start:]).group(0)
    close = re.compile(rf"(?m)^{re.escape(indent)}end\b").search(text, m.end())
    return (m.end(), close.start()) if close else None


def _sent(text: str, src: Source, lo: int, hi: int) -> str | None:
    """The builder a ``let`` body calls when the body is exactly ``<x>.__send__(:builder, ...)``."""
    m = SENDS.match(text, lo) if lo >= 0 else None
    if not m or m.end() > hi:
        return None
    end = src.skip_balanced(text.rindex("(", lo, m.end()))
    return m.group(1) if text[end - 1 : end] == ")" and src.skip_space(end) >= hi else None


def _literal(text: str, src: Source, lo: int, hi: int) -> tuple[int, int] | None:
    """The one hash literal that is the whole body ``[lo, hi)``."""
    i = src.skip_space(lo)
    if i >= hi or text[i] != "{":
        return None
    end = src.skip_balanced(i)
    return (i, end) if text[end - 1 : end] == "}" and src.skip_space(end) >= hi else None


def _compared_only(
    text: str, mask: Mask, src: Source, name: str, actual: dict[str, str], derived: dict[str, str],
    bodies: dict[str, list[tuple[int, int]]],
) -> tuple[str | None, list[tuple[int, int]]]:
    """The builder ``name`` is compared with when every use of it (and of what is derived from it) is
    ``expect(<actual>).to eq(...)`` or the start of a derivation, and the spans of those derivations; None
    when any use is something else."""
    names, todo, methods = {name}, [name], set()
    spans: list[tuple[int, int]] = []
    locals_ = [(m.group(1), m.group(2), m.start(2), m.start(1)) for m in LOCAL.finditer(text) if not mask.covers(m.start(1))]
    compares = [(m.group(1), m.group(2), m.start(2)) for m in COMPARE.finditer(text) if not mask.covers(m.start())]
    while todo:
        current = todo.pop()
        allowed: set[int] = set()
        for a, e, at in compares:
            if e == current:
                if a not in actual:
                    return None, []
                methods.add(actual[a])
                allowed.add(at)
        for child, base in [*derived.items(), *((c, b) for c, b, _, _ in locals_)]:
            if base == current and child not in names:
                names.add(child)
                todo.append(child)
                spans += [s for s in bodies.get(child, []) if s[0] >= 0]
        spans += [(at, src.line_end(at)) for _, b, at, _ in locals_ if b == current]
        allowed |= {at for _, b, at, _ in locals_ if b == current} | {at for c, _, _, at in locals_ if c == current}
        allowed |= {_derived_at(text, mask, c) for c, b in derived.items() if b == current}
        for use in re.finditer(rf"(?<![\w:.@$]){re.escape(current)}(?![\w?!])(?!:[ \t]*[^\s,)}}\]])", text):
            if not mask.covers(use.start()) and use.start() not in allowed:
                return None, []
    return (next(iter(methods)) if len(methods) == 1 else None), spans


def _names_key(text: str, mask: Mask, spans: list[tuple[int, int]], target: str) -> bool:
    """A derivation names the migrated key (``payload.merge(payment_method_types: [...])``, ``except(:k)``): the
    derived expectation would still state the old key, so the literal is listed, not edited. Any mention
    counts, even in a string or comment: when unsure, list."""
    key = re.compile(rf"(?<![\w@$]){re.escape(target.rsplit('.', 1)[-1])}(?![\w?!])")
    return any(key.search(text, lo, hi) for lo, hi in spans)


def _derived_at(text: str, mask: Mask, child: str) -> int:
    """Where the base name of ``let(:child) { base.merge(...) }`` starts."""
    for m in LET.finditer(text):
        if m.group(1) == child and not mask.covers(m.start()):
            d = DERIVED_LET.match(text, m.end())
            return d.start(1) if d else -1
    return -1
