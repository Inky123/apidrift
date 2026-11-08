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
- the actual value is a ``let``/``subject`` that is exactly ``<x>.__send__(:builder, ...)`` (or ``send``),
  where ``<x>`` is the described class itself: ``described_class``, the ``describe`` constant, ``subject``
  (unless a bare ``subject`` block builds something else), or a ``let``/``subject(:name)`` whose body is
  exactly one of those, with at most ``.new(...)`` after it. Another class's same-named builder
  (``Billing::Link.new.send(:payload)``) is not this fix's, so its literal is left alone;
- the expected ``let`` is defined once and is one hash literal, and every use of it is ``eq(...)`` against
  such an actual value or the start of a ``merge``/``deep_merge``/``except``/``dup`` whose result is used
  the same way. Any other use (passed to a factory, splatted, indexed) and the literal is left alone.
"""

from __future__ import annotations

import bisect
import re
from collections import Counter
from dataclasses import dataclass

from .fixtestfind import Expect, Overloaded
from .jsparse import Mask, Source

MAX_DEFS = 2000  # methods in one edited Ruby file; past this its builder specs are listed, never edited
SPEC = re.compile(r"(?P<pre>(?:.*/)?)spec/(?P<rest>.+)_spec\.rb")
SCOPE = re.compile(r"(module|class)\s+(?!<<)((?:::)?[A-Z][\w:]*)")
DEF = re.compile(r"def\s+(?:self\.)?([a-z_]\w*[?!]?)")
END = re.compile(r"end\b.*")
TOKEN = re.compile(r"(?<![\w:])[A-Za-z_]\w*[?!]?(?![\w?!])")  # a method name as a bare token (not ``X::name``)
DESCRIBE = re.compile(r"^(?:RSpec\s*\.\s*)?describe\s*\(?\s*(?:::)?([A-Z][\w:]*)", re.M)
LET = re.compile(r"\b(?:let!?|subject)\(\s*:(\w+[?!]?)\s*\)\s*(\{|do\b)")
BARE_SUBJECT = re.compile(r"(?<![\w:.])(subject)\s*(\{|do\b)")  # the implicit subject, redefined
SENDS = re.compile(r"\s*(?P<recv>[\w:.]+)\s*\.\s*(?:__send__|send|public_send)\(\s*:(?P<method>\w+[?!]?)\s*[,)]")
NEW = re.compile(r"\s*\.\s*new\b")
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


def builders(file: str, original: str, edits: list[tuple[int, int]], sites: list[tuple[str, str, int, str]]) -> list[Builder]:
    """Builder methods of an edited Ruby file: ``edits`` holds the (start, end) spans the fix rewrites in
    ``original`` (the lines they touch are the changed lines), ``sites`` (path, target, call line, call head).
    Linear in the file: the methods, their ends and every bare mention of a name are indexed in one pass each.
    Raises :class:`fixtestfind.Overloaded` past :data:`MAX_DEFS` methods."""
    lines = original.splitlines()
    changed = _changed_lines(original, edits)
    defs = _defs(lines)
    if len(defs) > MAX_DEFS:
        raise Overloaded(f"{len(defs)} methods in one file")
    counts = Counter(name for name, *_ in defs)
    mentioned = _mentions(lines)
    out = []
    for name, start, end, klass in defs:
        where = mentioned.get(name, [])  # lines naming it, ascending: a use outside the method itself is needed
        if counts[name] != 1 or not where or not (where[0] < start or where[-1] > end):
            continue
        for path, target, line, head in sites:
            leaf = re.escape(target.rsplit(".", 1)[-1])
            if start <= line <= end:
                continue  # the call is inside: this method sends, it does not only build
            if any(start < n < end and re.search(rf"(?<![\w@$]){leaf}(?!\w)", lines[n - 1]) for n in changed):
                out.append(Builder(file, klass, name, path, target, head))
    return out


def _changed_lines(text: str, edits: list[tuple[int, int]]) -> set[int]:
    """The 1-based lines of ``text`` an edit's span touches (an insertion: the line it is made on)."""
    newlines = [m.start() for m in re.finditer("\n", text)]
    out: set[int] = set()
    for start, end in edits:
        first = bisect.bisect_left(newlines, start) + 1  # the line holding offset ``start``
        last = bisect.bisect_left(newlines, max(start, end - 1)) + 1  # ... and the span's last character
        out.update(range(first, last + 1))
    return out


def _mentions(lines: list[str]) -> dict[str, list[int]]:
    """Every bare token of each line, by token: the lines (ascending) that mention it."""
    out: dict[str, list[int]] = {}
    for n, line in enumerate(lines, 1):
        for m in TOKEN.finditer(line):
            found = out.setdefault(m.group(0), [])
            if not found or found[-1] != n:
                found.append(n)
    return out


def _defs(lines: list[str]) -> list[tuple[str, int, int, str]]:
    """(name, def line, end line, enclosing class path) by indentation, the layout rubocop keeps: a method ends
    at the first later non-blank line at its indent or less, when that line is its ``end``. One pass, with a
    stack of the methods still open."""
    scopes: list[tuple[int, str]] = []
    open_defs: list[tuple[int, str, int, str]] = []  # (indent, name, def line, class path)
    out = []
    for n, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        while open_defs and indent <= open_defs[-1][0]:
            d_indent, name, start, klass = open_defs.pop()
            if indent == d_indent and END.fullmatch(stripped):
                out.append((name, start, n, klass))
        if stripped.startswith("#"):
            continue
        while scopes and indent <= scopes[-1][0]:
            scopes.pop()
        scope = SCOPE.match(stripped)
        if scope:
            scopes.append((indent, scope.group(2).lstrip(":")))
            continue
        d = DEF.match(stripped)
        if d:
            open_defs.append((indent, d.group(1), n, "::".join(name for _, name in scopes)))
    return sorted(out, key=lambda d: d[1])


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
    roots = _receivers(text, src, mask, described, bodies)
    actual = {n: m for n, spans in bodies.items() if len(spans) == 1 and (m := _sent(text, src, *spans[0], roots))}
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


def _receivers(text: str, src: Source, mask: Mask, described: str, bodies: dict[str, list[tuple[int, int]]]) -> set[str]:
    """The names that stand for the described class in this spec: ``described_class``, the ``describe``
    constant, ``subject`` unless a bare ``subject`` block builds something else, and every ``let`` /
    ``subject(:name)`` whose one body is exactly an instance of it (:func:`_instance`)."""
    roots = {"described_class", described, "::" + described.lstrip(":")}
    roots |= {n for n, spans in bodies.items() if len(spans) == 1 and spans[0][0] >= 0 and _instance(text, src, *spans[0], described)}
    bare = [m for m in BARE_SUBJECT.finditer(text) if not mask.covers(m.start())]
    spans = [_body(text, src, m) for m in bare]
    if not bare or (len(bare) == 1 and spans[0] and _instance(text, src, *spans[0], described)):
        roots.add("subject")
    return roots


def _instance(text: str, src: Source, lo: int, hi: int, described: str) -> bool:
    """The body ``[lo, hi)`` is exactly the described class or a ``.new(...)`` of it."""
    m = re.compile(rf"\s*(?:described_class|(?:::)?{re.escape(described)})(?![\w:])").match(text, lo)
    if not m or m.end() > hi:
        return False
    i, n = m.end(), NEW.match(text, m.end())
    if n and n.end() <= hi:
        i = n.end()
        if text[i : i + 1] == "(":
            i = src.skip_balanced(i)
            if text[i - 1 : i] != ")":
                return False
    return src.skip_space(i) >= hi


def _sent(text: str, src: Source, lo: int, hi: int, roots: set[str]) -> str | None:
    """The builder a ``let`` body calls when the body is exactly ``<x>.__send__(:builder, ...)`` and ``<x>`` is
    one of ``roots`` (the described class), at most with ``.new`` after it."""
    m = SENDS.match(text, lo) if lo >= 0 else None
    if not m or m.end() > hi:
        return None
    root, *rest = m.group("recv").split(".")
    if root not in roots or rest not in ([], ["new"]):
        return None
    end = src.skip_balanced(text.rindex("(", lo, m.end()))
    return m.group("method") if text[end - 1 : end] == ")" and src.skip_space(end) >= hi else None


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
