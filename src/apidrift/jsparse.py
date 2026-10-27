"""Pull request fields out of JavaScript, TypeScript and Ruby call arguments.

No real parser: a cursor that understands strings, comments and bracket
nesting is enough to read object literals (``{ amount: 100, shipping: {...} }``)
and Ruby hashes or keyword arguments into dotted field paths.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

from .sdkmap import Resolved, SdkIndex

IDENT = re.compile(r"[A-Za-z_$][\w$]*")
JS_CALL = re.compile(r"(?<![\w$.])((?:[A-Za-z_$][\w$]*\s*\.\s*){1,5}[A-Za-z_$][\w$]*)\s*\(")
RUBY_CALL = re.compile(r"(?<![\w:.])((?:[A-Za-z_]\w*\s*(?:\.|::)\s*){1,5}[A-Za-z_]\w*)\s*\(")
RUBY_KEYWORD = re.compile(r"(?:[A-Za-z_]\w*\s*:(?!:)|:[A-Za-z_]\w*\s*=>|[\"'][^\"']*[\"']\s*=>)")
STRIPE_ROOTS = [
    re.compile(
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+?)?=\s*(?:await\s+)?(?:new\s+)?"
        r"(?:require\(\s*['\"]stripe['\"]\s*\)|Stripe)\s*\("
    ),
    re.compile(r"this\.([A-Za-z_$][\w$]*)\s*=\s*(?:new\s+)?Stripe\s*\("),
    re.compile(r"\b([A-Za-z_$][\w$]*)\s*:\s*Stripe\b"),
]
ASSIGNED_OBJECT = re.compile(r"\b([A-Za-z_$][\w$]*)(?:\s*:\s*[\w.<>\[\]|, ]+?)?\s*=\s*(\{)")
JS_MASK = re.compile(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`|//[^\n]*|/\*.*?\*/", re.S)
RUBY_MASK = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|#[^\n]*")
OPTION_KEYS = {
    "idempotencyKey", "stripeAccount", "apiKey", "apiVersion", "maxNetworkRetries", "timeout", "host",
    "stripeContext", "idempotency_key", "stripe_account", "api_key", "stripe_version", "api_version",
}
OPENERS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = set(OPENERS.values())
QUOTES = "\"'`"


@dataclass
class Fields:
    paths: set[str] = field(default_factory=set)
    complete: bool = True


@dataclass
class Found:
    line: int
    call: str
    resolved: Resolved
    fields: Fields


class Source:
    """Cursor over source text that knows how to step over strings, comments and brackets."""

    def __init__(self, text: str, *, ruby: bool = False) -> None:
        self.text = text if ruby else text.replace("?.", " .")  # same length, keeps offsets
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
            if c in "'\"":
                key, i = self.read_string(i)
            elif c == "`":
                out.complete = False
                i = self.skip_string(i)
            elif self.ruby and c == ":":
                m = IDENT.match(text, i + 1)
                key, i = (m.group(0), m.end()) if m else (None, i + 1)
            else:
                m = IDENT.match(text, i)
                if not m:
                    out.complete = False
                    i = self.skip_scalar(i + 1)
                    continue
                key, i = m.group(0), m.end()
            i = self.skip_space(i)
            if text.startswith("=>", i):
                sep = 2
            elif i < self.n and text[i] == ":" and not text.startswith("::", i):
                sep = 1
            else:
                sep = 0
            if sep == 0:
                if i < self.n and text[i] == "(":  # method shorthand: key(args) { body }
                    i = self.skip_space(self.skip_balanced(i))
                    if i < self.n and text[i] == "{":
                        i = self.skip_balanced(i)
                elif key is not None:  # shorthand property { amount, currency }
                    out.paths.add(prefix + key)
                    i = self.skip_scalar(i)
                else:
                    i = self.skip_scalar(i)
                continue
            if key is None:
                i = self.skip_scalar(i + sep)
                continue
            path = prefix + key
            out.paths.add(path)
            i = self.value(i + sep, path, out)
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


def stripe_roots(text: str) -> set[str]:
    """Variable names that hold a Stripe client (``const client = new Stripe(...)``)."""
    return {m.group(1) for pattern in STRIPE_ROOTS for m in pattern.finditer(text)}


class Mask:
    """Spans of string literals and comments, so matches inside them can be ignored."""

    def __init__(self, text: str, *, ruby: bool) -> None:
        self.spans = [(m.start(), m.end()) for m in (RUBY_MASK if ruby else JS_MASK).finditer(text)]
        self.starts = [a for a, _ in self.spans]

    def covers(self, pos: int) -> bool:
        k = bisect.bisect_right(self.starts, pos) - 1
        return k >= 0 and self.spans[k][0] <= pos < self.spans[k][1]


def assigned_objects(text: str, mask: Mask) -> dict[str, int]:
    """Variable name -> index of the ``{`` of the last object literal assigned to it."""
    return {m.group(1): m.start(2) for m in ASSIGNED_OBJECT.finditer(text) if not mask.covers(m.start(2))}


def eligible(chain: list[str], roots: set[str]) -> bool:
    if any("stripe" in part.lower() for part in chain):
        return True
    if chain[0] in roots:
        return True
    return chain[0] in ("this", "self") and len(chain) > 2 and chain[1] in roots


def extract(text: str, index: SdkIndex, *, ruby: bool = False) -> tuple[list[Found], list[tuple[int, str]]]:
    """Stripe SDK calls in a JS/TS/Ruby source, plus (line, call) pairs that matched no endpoint."""
    src = Source(text, ruby=ruby)
    mask = Mask(src.text, ruby=ruby)
    roots = set() if ruby else stripe_roots(src.text)
    objects = assigned_objects(src.text, mask)
    found: list[Found] = []
    unresolved: list[tuple[int, str]] = []
    for m in (RUBY_CALL if ruby else JS_CALL).finditer(src.text):
        if mask.covers(m.start()):
            continue
        chain = [p for p in re.split(r"\s*(?:\.|::)\s*", m.group(1).strip()) if p]
        if not eligible(chain, roots):
            continue
        call = re.sub(r"\s+", "", m.group(1))
        line = src.text.count("\n", 0, m.start()) + 1
        resolved = index.resolve(chain)
        if resolved is None:
            unresolved.append((line, call))
            continue
        fields = Fields()
        collect(src, src.args(m.end() - 1), "{" in resolved.path, objects, fields, ruby=ruby)
        found.append(Found(line, call, resolved, fields))
    return found, unresolved


def collect(
    src: Source, spans: list[tuple[int, int]], has_id: bool, objects: dict[str, int], out: Fields, *, ruby: bool
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
            if name and text[i:b].strip() == name and name in objects:
                merge(out, parse_object(src, objects[name] + 1))
            elif k == 0 and has_id:
                continue
            else:
                out.complete = False


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
    out.complete = out.complete and sub.complete
