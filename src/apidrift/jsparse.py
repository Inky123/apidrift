"""Pull request fields out of JavaScript, TypeScript and Ruby call arguments.

No real parser: a cursor that understands strings, comments and bracket
nesting is enough to read object literals (``{ amount: 100, shipping: {...} }``)
and Ruby hashes or keyword arguments into dotted field paths.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

from . import jsroots
from .jsroots import CALL_ARGS, NAME
from .sdkmap import Resolved, SdkIndex

IDENT = re.compile(r"[A-Za-z_$][\w$]*")
SEGMENT = rf"{NAME}(?:\s*{CALL_ARGS})*"  # identifier, or a call whose result is the receiver
NOT_A_RECEIVER = r"(?!(?:return|yield|throw|typeof|void|delete|case|else|do|in|of|instanceof|await)\b)"
JS_CALL = re.compile(
    rf"(?<![\w$.])((?:(?:new\s+)?{NOT_A_RECEIVER}{SEGMENT}|\(\s*await\s+(?:this\s*\.\s*)?{SEGMENT}\s*\))"
    rf"\s*\.\s*(?:{NAME}\s*\.\s*){{0,4}}{NAME})\s*\("
)
RUBY_CALL = re.compile(
    r"(?<![\w:.])((?:[A-Za-z_]\w*\s*(?:\.|::)\s*){1,5}[A-Za-z_]\w*)"
    r"(?:\s*(\()|[ \t]+(?=[A-Za-z_]\w*\s*:(?!:)|:[A-Za-z_]\w*\s*=>|[\"'][^\"'\n]*[\"']\s*=>))"
)
RUBY_KEYWORD = re.compile(r"(?:[A-Za-z_]\w*\s*:(?!:)|:[A-Za-z_]\w*\s*=>|[\"'][^\"']*[\"']\s*=>)")
JS_MASK = re.compile(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`|//[^\n]*|/\*.*?\*/", re.S)
RUBY_MASK = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|#[^\n]*")
NON_NULL = re.compile(r"(?<=[\w$)\]])!\.")
MERGERS = (".merge", ".update", ".deep_merge")
OPTION_KEYS = {
    "idempotencyKey", "stripeAccount", "apiKey", "apiVersion", "maxNetworkRetries", "timeout", "host",
    "stripeContext", "idempotency_key", "stripe_account", "api_key", "stripe_version", "api_version",
}
OPENERS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = set(OPENERS.values())
QUOTES = "\"'`"


@dataclass(frozen=True)
class Span:
    """Where a key sits in the ORIGINAL text (``Source`` rewrites are length-preserving).

    ``kind``: quoted (``'k':``), bare (``k:``), shorthand (``{ k }``), symbol (``:k =>``) or
    rocket_quoted (``"k" =>``). ``key_start``/``key_end`` cover the key token including quotes or the
    leading colon; ``entry_end`` is the index of the comma or closer after the value.
    """

    key_start: int
    key_end: int
    entry_end: int
    kind: str


@dataclass
class Fields:
    paths: set[str] = field(default_factory=set)
    complete: bool = True
    spans: dict[str, list[Span]] = field(default_factory=dict)

    def add(self, path: str, span: Span | None = None) -> None:
        self.paths.add(path)
        if span is not None:
            self.spans.setdefault(path, []).append(span)


@dataclass
class Found:
    line: int
    call: str
    resolved: Resolved
    fields: Fields


class Source:
    """Cursor over source text that knows how to step over strings, comments and brackets."""

    def __init__(self, text: str, *, ruby: bool = False) -> None:
        self.text = text if ruby else NON_NULL.sub(" .", text.replace("?.", " ."))  # same length, keeps offsets
        self.n = len(self.text)
        self.ruby = ruby

    def skip_space(self, i: int) -> int:
        text = self.text
        while i < self.n:
            c = text[i]
            if c.isspace():
                i += 1
            elif (self.ruby and c == "#") or (not self.ruby and text.startswith("//", i)):
                end = text.find("\n", i)
                i = self.n if end < 0 else end
            elif not self.ruby and text.startswith("/*", i):
                end = text.find("*/", i + 2)
                i = self.n if end < 0 else end + 2
            else:
                break
        return i

    def skip_string(self, i: int) -> int:
        quote = self.text[i]
        interpolate = "${" if quote == "`" else ("#{" if self.ruby and quote == '"' else None)
        i += 1
        while i < self.n:
            c = self.text[i]
            if c == "\\":
                i += 2
            elif c == quote:
                return i + 1
            elif interpolate and self.text.startswith(interpolate, i):
                i = self.skip_balanced(i + 1)
            else:
                i += 1
        return self.n

    def skip_balanced(self, i: int) -> int:
        """From an opener, return the index just past its matching closer."""
        closer = OPENERS[self.text[i]]
        i += 1
        while i < self.n:
            i = self.skip_space(i)
            if i >= self.n:
                break
            c = self.text[i]
            if c == closer:
                return i + 1
            if c in QUOTES:
                i = self.skip_string(i)
            elif c in OPENERS:
                i = self.skip_balanced(i)
            else:
                i += 1
        return self.n

    def skip_scalar(self, i: int, stops: str = ",") -> int:
        """Advance to the next stop or closing bracket at this nesting level (not consumed)."""
        while i < self.n:
            i = self.skip_space(i)
            if i >= self.n:
                break
            c = self.text[i]
            if c in stops or c in CLOSERS:
                return i
            if c in QUOTES:
                i = self.skip_string(i)
            elif c in OPENERS:
                i = self.skip_balanced(i)
            else:
                i += 1
        return i

    def read_string(self, i: int) -> tuple[str, int]:
        end = self.skip_string(i)
        return self.text[i + 1 : end - 1], end

    def args(self, open_idx: int) -> list[tuple[int, int]]:
        """Spans of the top-level arguments of a call whose ``(`` is at ``open_idx``."""
        spans: list[tuple[int, int]] = []
        start = i = open_idx + 1
        while i < self.n:
            i = self.skip_space(i)
            if i >= self.n:
                break
            c = self.text[i]
            if c == ")":
                if self.text[start:i].strip():
                    spans.append((start, i))
                return spans
            if c == ",":
                spans.append((start, i))
                i = start = i + 1
            elif c in QUOTES:
                i = self.skip_string(i)
            elif c in OPENERS:
                i = self.skip_balanced(i)
            elif c in CLOSERS:
                return spans
            else:
                i += 1
        return spans

    def line_end(self, i: int) -> int:
        """End of the logical line starting at ``i``: lines ending in a comma continue (paren-less Ruby calls)."""
        text, last = self.text, ""
        while i < self.n:
            c = text[i]
            if c == "\n":
                if last != ",":
                    return i
                i += 1
            elif c == "#":
                end = text.find("\n", i)
                i = self.n if end < 0 else end
            elif c in QUOTES:
                i, last = self.skip_string(i), c
            elif c in OPENERS:
                i, last = self.skip_balanced(i), c
            else:
                if not c.isspace():
                    last = c
                i += 1
        return i

    def object_fields(self, i: int, prefix: str, out: Fields, *, closer: str = "}") -> int:
        """Parse an object or hash body from just inside its opener; return the index past the closer."""
        text = self.text
        while i < self.n:
            i = self.skip_space(i)
            if i >= self.n:
                break
            c = text[i]
            if c == closer:
                return i + 1
            if c in CLOSERS:
                return i
            if c in ",;":
                i += 1
                continue
            if text.startswith("...", i) or text.startswith("**", i):
                out.complete = False
                i = self.skip_scalar(i + 2)
                continue
            if c == "[":  # computed key
                out.complete = False
                i = self.skip_space(self.skip_balanced(i))
                i = self.skip_scalar(i + 1) if i < self.n and text[i] == ":" else i
                continue
            key: str | None = None
            key_start, kind = i, "bare"
            if c in "'\"":
                key, i = self.read_string(i)
                kind = "quoted"
            elif c == "`":
                out.complete = False
                i = self.skip_string(i)
            elif self.ruby and c == ":":
                m = IDENT.match(text, i + 1)
                key, i = (m.group(0), m.end()) if m else (None, i + 1)
                kind = "symbol"
            else:
                m = IDENT.match(text, i)
                if not m:
                    out.complete = False
                    i = self.skip_scalar(i + 1)
                    continue
                key, i = m.group(0), m.end()
            key_end = i
            i = self.skip_space(i)
            if text.startswith("=>", i):
                sep = 2
                kind = "rocket_quoted" if kind == "quoted" else kind
            elif i < self.n and text[i] == ":" and not text.startswith("::", i):
                sep = 1
            else:
                sep = 0
            if sep == 0:
                if i < self.n and text[i] == "(":  # method shorthand: key(args) { body }
                    i = self.skip_space(self.skip_balanced(i))
                    if i < self.n and text[i] == "{":
                        i = self.skip_balanced(i)
                elif key is not None and kind == "bare":  # shorthand property { amount, currency }
                    i = self.skip_scalar(i)
                    out.add(prefix + key, Span(key_start, key_end, i, "shorthand"))
                elif key is not None:
                    out.add(prefix + key)
                    i = self.skip_scalar(i)
                else:
                    i = self.skip_scalar(i)
                continue
            if key is None:
                i = self.skip_scalar(i + sep)
                continue
            path = prefix + key
            i = self.value(i + sep, path, out)
            out.add(path, Span(key_start, key_end, i, kind))
        return i

    def value(self, i: int, path: str, out: Fields) -> int:
        i = self.skip_space(i)
        if i < self.n and self.text[i] == "{":
            i = self.object_fields(i + 1, path + ".", out)
        elif i < self.n and self.text[i] == "[":
            i = self.array_fields(i + 1, path, out)
        return self.skip_scalar(i)

    def array_fields(self, i: int, path: str, out: Fields) -> int:
        text = self.text
        while i < self.n:
            i = self.skip_space(i)
            if i >= self.n:
                break
            c = text[i]
            if c == "]":
                return i + 1
            if c in CLOSERS:
                return i
            if c == ",":
                i += 1
                continue
            if text.startswith("...", i) or c == "*":
                out.complete = False
                i = self.skip_scalar(i + 1)
                continue
            if c == "{":
                i = self.object_fields(i + 1, path + "[].", out)
            elif c == "[":
                i = self.array_fields(i + 1, path + "[]", out)
            i = self.skip_scalar(i)
        return i


class Mask:
    """Spans of string literals and comments, so matches inside them can be ignored."""

    def __init__(self, text: str, *, ruby: bool) -> None:
        self.spans = [(m.start(), m.end()) for m in (RUBY_MASK if ruby else JS_MASK).finditer(text)]
        self.starts = [a for a, _ in self.spans]

    def covers(self, pos: int) -> bool:
        k = bisect.bisect_right(self.starts, pos) - 1
        return k >= 0 and self.spans[k][0] <= pos < self.spans[k][1]


def eligible(chain: list[str], roots: set[str]) -> bool:
    if any("stripe" in part.lower() for part in chain):
        return True
    if chain[0] in roots:
        return True
    if chain[0] in ("this", "self") and len(chain) > 2 and chain[1] in roots:
        return True
    return any(part in ("v1", "v2") for part in chain[1:-1])  # client.v1.payment_intents.create


@dataclass
class Call:
    """One resolved SDK call before its arguments are read: what the fixer walks to find edit sites."""

    line: int
    call: str
    resolved: Resolved
    pos: int  # index of the call's ``(`` (or the first argument of a paren-less Ruby call)
    args_src: Source  # the source the argument spans index into (a copy for paren-less Ruby calls)
    spans: list[tuple[int, int]]
    bindings: jsroots.Bindings


def extract(text: str, index: SdkIndex, *, ruby: bool = False, resolve=None) -> tuple[list[Found], list[tuple[int, str]]]:
    """Stripe SDK calls in a JS/TS/Ruby source, plus (line, call) pairs that matched no endpoint.

    ``resolve`` maps a call chain to a :class:`Resolved` operation (defaults to ``index.resolve``).
    """
    found: list[Found] = []
    resolved_calls, unresolved = calls(text, index, ruby=ruby, resolve=resolve)
    for c in resolved_calls:
        fields = Fields()
        collect(c.args_src, c.spans, "{" in c.resolved.path, c.bindings, fields, ruby=ruby)
        found.append(Found(c.line, c.call, c.resolved, fields))
    return found, unresolved


def calls(text: str, index: SdkIndex, *, ruby: bool = False, resolve=None) -> tuple[list[Call], list[tuple[int, str]]]:
    """Resolved SDK calls with their argument spans, plus the (line, call) pairs that matched no endpoint."""
    src = Source(text, ruby=ruby)
    mask = Mask(src.text, ruby=ruby)
    scopes = jsroots.Scopes(src, mask)
    roots = jsroots.stripe_roots(src, mask, scopes)
    bindings = jsroots.Bindings(src, mask, scopes)
    resolve = resolve or index.resolve
    found: list[Call] = []
    unresolved: list[tuple[int, str]] = []
    for m in (RUBY_CALL if ruby else JS_CALL).finditer(src.text):
        if mask.covers(m.start()):
            continue
        if ruby:
            chain, hint = [p for p in re.split(r"\s*(?:\.|::)\s*", m.group(1).strip()) if p], False
        else:
            chain, hint = jsroots.chain_parts(src, m.start(1), m.end(1))
        if len(chain) < 2 or not (hint or eligible(chain, roots)):
            continue
        call = jsroots.display(m.group(1))
        line = src.text.count("\n", 0, m.start()) + 1
        resolved = resolve(chain)
        if resolved is None:
            unresolved.append((line, call))
            continue
        if ruby and m.group(2) is None:  # Stripe::PaymentIntent.create amount: 1, currency: 'usd'
            end = src.line_end(m.end())
            args_src = Source(src.text[:end] + ")", ruby=True)
            spans = args_src.args(m.end() - 1)
        else:
            args_src, spans = src, src.args(m.end() - 1)
        found.append(Call(line, call, resolved, m.end() - 1, args_src, spans, bindings))
    return found, unresolved


def collect(
    src: Source, spans: list[tuple[int, int]], has_id: bool, bindings: jsroots.Bindings, out: Fields, *, ruby: bool
) -> None:
    """Merge the request fields of each argument into ``out``."""
    text = src.text
    for k, (a, b) in enumerate(spans):
        i = src.skip_space(a)
        if i >= b:
            continue
        c = text[i]
        if c == "{":
            merge(out, parse_object(src, i + 1))
            after = src.skip_space(src.skip_balanced(i))
            if text.startswith(MERGERS, after):  # { amount: 1 }.merge(extra)
                out.complete = False
        elif text.startswith("...", i) or (ruby and text.startswith("**", i)):
            out.complete = False
        elif ruby and RUBY_KEYWORD.match(text, i):
            sub = Fields()
            src.object_fields(i, "", sub, closer=")")
            merge(out, sub)
            return
        elif c in QUOTES or c.isdigit():
            continue
        else:
            m = IDENT.match(text, i)
            name = m.group(0) if m else ""
            binding = bindings.at(name, i) if name and text[i:b].strip() == name else None
            if binding is not None and binding.literal is not None:
                merge(out, variable_fields(src, bindings, binding, i))
            elif k == 0 and has_id and (binding is None or binding.literal is None):
                continue
            else:
                out.complete = False


def variable_fields(src: Source, bindings: jsroots.Bindings, binding: jsroots.Binding, at: int) -> Fields:
    """Fields of the object literal a variable was bound to, plus keys written into it before the call."""
    sub = parse_object(src, binding.literal + 1)
    if not sub.paths:  # ``const params = {}`` is filled in elsewhere
        sub.complete = False
    for mutation in bindings.mutations(binding.name, binding.literal, at):
        sub.complete = False
        if mutation.key:
            sub.paths.add(mutation.key)
        if mutation.literal is not None:
            sub.paths |= parse_object(src, mutation.literal + 1).paths
    return sub


def parse_object(src: Source, i: int) -> Fields:
    sub = Fields()
    src.object_fields(i, "", sub)
    return sub


def merge(out: Fields, sub: Fields) -> None:
    """Add ``sub`` to ``out`` unless it is a request-options object rather than parameters."""
    top = {p for p in sub.paths if "." not in p and "[" not in p}
    if top and top <= OPTION_KEYS:
        return
    out.paths |= sub.paths
    for path, spans in sub.spans.items():
        out.spans.setdefault(path, []).extend(spans)
    out.complete = out.complete and sub.complete
