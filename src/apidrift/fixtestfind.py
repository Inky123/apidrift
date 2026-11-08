"""Expectations in a test file that state the exact request an SDK call sends.

Each one is read as the call it expects: an :class:`Expect` names the SDK call (``head``) and the span of the
test text that holds the expected arguments, so the scanner and the fixer's own :class:`fixedit.Locator` can
read it as if it were that call (:meth:`Expect.virtual`; offsets are a constant shift, so an edit found there
lands on the same bytes of the test). Only these forms are recognised; anything else is not an expectation:

- RSpec: ``expect|allow(Stripe::X).to receive|have_received(:m)[.once|...].with(ARGS)``.
- Jest / Vitest: ``expect(M).toHaveBeenCalledWith(ARGS)`` where ``M`` is a Stripe client chain
  (``stripe.checkout.sessions.create``) or a mock wired into ``vi.mock('stripe', ...)`` / ``jest.mock`` at
  exactly one key path.
- Python ``unittest.mock``: ``m.assert_called_once_with(ARGS)`` / ``assert_called_with`` / ``assert_any_call``
  where ``m`` is a ``patch`` of ``...stripe.X.m`` (decorator, ``with ... as m``, ``m = mocker.patch(...)``).
- PHP: Mockery ``$m->shouldReceive('m')[->once()...]->with(ARGS)`` and PHPUnit
  ``$m->expects($this->once())->method('m')->with(ARGS)`` where ``$m`` is assigned once in the same function
  from an ``alias:``/``overload:`` mock of ``Stripe\\X`` or a mock of a ``Stripe\\Service\\...Service`` class.

A negative expectation (``not_to``, ``.not.``, ``never()``) is never one: after a rename it would assert the
absence of the new key. Matchers that wrap the parameters (``hash_including``, ``expect.objectContaining``,
``equalTo``, ``Mockery::subset``) are blanked to spaces and braces of the same length.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from .fixtokens import _Offsets
from .jsparse import CLOSERS, OPENERS, QUOTES, RUBY_KEYWORD, Mask, Source
from . import phplex
from .phplex import Lex

STRIPEY_ROOT = re.compile(r"(?i)(?:mock_?)?stripe(?:_?(?:mock|client|instance|api))?")
RSPEC = re.compile(
    r"\b(?:expect|allow)\(\s*(?P<recv>(?:::)?Stripe(?:::[A-Z]\w*)+)\s*\)\s*\.\s*to\s+(?:have_received|receive)"
    r"\(\s*:(?P<method>[a-z_]\w*)\s*\)"
    r"(?:\s*\.\s*(?:once|twice|thrice|ordered|exactly\(\s*[1-9]\d*\s*\)\s*\.\s*times?"
    r"|at_least\(\s*(?::once|:twice|[1-9]\d*)\s*\)(?:\s*\.\s*times?)?))*"
    r"\s*\.\s*with\s*\("
)
JEST = re.compile(
    r"\bexpect\(\s*(?P<recv>[A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)\s*\)\s*\.\s*"
    r"(?:toHaveBeenCalledWith|toHaveBeenLastCalledWith|toBeCalledWith|lastCalledWith)\s*\("
)
JS_FACTORY = re.compile(r"\b(?:vi|jest)\s*\.\s*(?:mock|doMock)\(\s*(['\"])stripe\1\s*,")
JS_KEYED = re.compile(r"(?:([A-Za-z_$][\w$]*)|'([\w$]+)'|\"([\w$]+)\")\s*[:=]\s*$")
JS_CLASS = re.compile(r"\bclass(?:\s+[A-Za-z_$][\w$]*)?(?:\s+extends\s+[\w$.]+)?\s*$")
JS_RETURNED = re.compile(r"(?:=>\s*\(|\breturn)\s*$")
PY_ASSERTS = {"assert_called_once_with", "assert_called_with", "assert_any_call"}
COUNT = r"(?:once\(\s*\)|twice\(\s*\)|times\(\s*[1-9]\d*\s*\))"
MOCKERY = re.compile(
    r"(?P<recv>\$[A-Za-z_]\w*)\s*->\s*shouldReceive\(\s*(['\"])(?P<method>\w+)\2\s*\)"
    rf"(?:\s*->\s*(?:{COUNT}|atLeast\(\s*\)\s*->\s*{COUNT}))*\s*->\s*with\s*\("
)
PHPUNIT = re.compile(
    r"(?P<recv>\$[A-Za-z_]\w*)\s*->\s*expects\(\s*(?:\$this\s*->\s*|self::|static::)"
    r"(?:once\(\s*\)|atLeastOnce\(\s*\)|any\(\s*\)|exactly\(\s*[1-9]\d*\s*\))\s*\)\s*->\s*method\(\s*(['\"])(?P<method>\w+)\2\s*\)"
    r"\s*->\s*with\s*\("
)
PHP_MOCK = r"(?:(?:\\?Mockery|\bm)::(?:mock|spy)|\$this\s*->\s*(?:createMock|createStub))\(\s*(?P<cls>[^,()]+?)\s*[,)]"
PHP_USE = re.compile(r"^\s*use\s+\\?([A-Za-z_][\w\\]*)(?:\s+as\s+(\w+))?\s*;", re.M)
RUBY_WRAPPERS = ("a_hash_including(", "hash_including(")
JS_WRAPPERS = ("expect.objectContaining(",)
PHP_WRAPPERS = ("$this->equalTo(", "self::equalTo(", "static::equalTo(", "\\Mockery::subset(", "Mockery::subset(")


@dataclass(frozen=True)
class Expect:
    """One expectation, read as the SDK call ``head`` whose arguments are the test text ``[lo, hi)``."""

    kind: str  # rspec, jest, mock, mockery, builder (:mod:`fixtestbuild`)
    head: str
    lo: int
    hi: int
    before: str  # the virtual call's text before text[lo:hi] (``head``, or ``head + "("`` for one literal)
    after: str = ")"
    subs: tuple[tuple[int, str], ...] = ()  # (position in the test, character it reads as): blanked matchers
    only: tuple[str, str] | None = None  # (path, target): the one migration this expectation may take

    def virtual(self, text: str) -> str:
        body = list(text[self.lo : self.hi])
        for pos, ch in self.subs:
            body[pos - self.lo] = ch
        return self.before + "".join(body) + self.after

    @property
    def shift(self) -> int:
        """Virtual offset minus test offset, for every character of ``[lo, hi)``."""
        return len(self.before) - self.lo


def find(text: str, language: str | None) -> list[Expect]:
    if language == "ruby":
        return _rspec(text)
    if language in ("javascript", "typescript"):
        return _jest(text)
    if language == "python":
        return _python(text)
    if language == "php":
        return _php(text)
    return []  # Go: no expectation form is read yet


def _params(src, text: str, open_idx: int, wrappers: tuple[str, ...], is_literal, kwargs=None) -> tuple[int, tuple] | None:
    """End of the parameters argument of the call at ``open_idx`` (the first literal, after any id arguments)
    and the matcher characters to blank; None when there is none or a matcher is not plainly a wrapper."""
    for a, b in src.args(open_idx):
        i = src.skip_space(a)
        if i >= b:
            continue
        wrap = next((w for w in wrappers if text.startswith(w, i) and (i == 0 or not _ident(text[i - 1]))), None)
        if wrap is not None:
            p = i + len(wrap) - 1
            q = _close(src, p)
            if q is None or text[q] != ")" or text[q + 1 : b].strip():
                return None
            j = src.skip_space(p + 1)
            if is_literal(src, j) is not None and src.skip_space(is_literal(src, j)) == q:
                return b, tuple((x, " ") for x in range(i, p + 1)) + ((q, " "),)
            if kwargs is not None and kwargs.match(text, j):
                return b, tuple((x, " ") for x in range(i, p)) + ((p, "{"), (q, "}"))
            return None
        end = is_literal(src, i)
        if end is not None:
            return (b, ()) if not text[end:b].strip() else None
        if kwargs is not None and kwargs.match(text, i):
            spans = src.args(open_idx)
            return spans[-1][1], ()
    return None


def _close(src, opener: int) -> int | None:
    """Index of the bracket closing ``opener``."""
    if isinstance(src, Lex):
        return src.pairs.get(opener)
    end = src.skip_balanced(opener)
    return end - 1 if end <= src.n and src.text[end - 1 : end] == OPENERS[src.text[opener]] else None


def _brace(src, i: int) -> int | None:
    if i < src.n and src.text[i] == "{":
        q = _close(src, i)
        return q + 1 if q is not None else None
    return None


def _ident(c: str) -> bool:
    return c.isalnum() or c in "_$"


# --- Ruby ------------------------------------------------------------------------------


def _rspec(text: str) -> list[Expect]:
    src, mask, out = Source(text, ruby=True), Mask(text, ruby=True), []
    for m in RSPEC.finditer(text):
        if mask.covers(m.start()):
            continue
        open_idx = m.end() - 1
        found = _params(src, text, open_idx, RUBY_WRAPPERS, _brace, RUBY_KEYWORD)
        if found is not None and _close(src, open_idx) is not None:
            head = f"{m.group('recv')}.{m.group('method')}"
            out.append(Expect("rspec", head, open_idx, found[0], head, ")", found[1]))
    return out


# --- JavaScript / TypeScript ---------------------------------------------------------


def _jest(text: str) -> list[Expect]:
    src, mask, out = Source(text), Mask(text, ruby=False), []
    for m in JEST.finditer(text):
        if mask.covers(m.start()):
            continue
        head = _js_head(text, src, mask, re.sub(r"\s+", "", m.group("recv")))
        open_idx = m.end() - 1
        found = _params(src, text, open_idx, JS_WRAPPERS, _brace) if head else None
        if head and found is not None and _close(src, open_idx) is not None:
            out.append(Expect("jest", head, open_idx, found[0], head, ")", found[1]))
    return out


def _js_head(text: str, src: Source, mask: Mask, recv: str) -> str | None:
    chain = recv.split(".")
    roots = [n for n, seg in enumerate(chain[:-2]) if STRIPEY_ROOT.fullmatch(seg)]
    if roots:  # ``stripe.checkout.sessions.create``, ``mockStripe.paymentIntents.create``
        return "stripe." + ".".join(chain[roots[-1] + 1 :])
    paths: set[tuple[str, ...]] = set()
    value = re.compile(r"(?:([A-Za-z_$][\w$]*)|'([\w$]+)'|\"([\w$]+)\")\s*[:=]\s*" + re.escape(recv) + r"(?![\w$.(\[])")
    for f in JS_FACTORY.finditer(text):
        if mask.covers(f.start()):
            continue
        open_idx = text.index("(", f.start())
        end = src.skip_balanced(open_idx)
        for v in value.finditer(text, open_idx, end):
            if mask.covers(v.start()):
                continue
            keys = _key_path(text, src, open_idx, v.start())
            if keys is None:
                return None  # wired somewhere this reader cannot follow: not a tie
            paths.add((*keys, next(g for g in v.groups() if g)))
    if len(paths) != 1:
        return None
    path = next(iter(paths))
    return "stripe." + ".".join(path) if len(path) >= 2 else None


def _key_path(text: str, src: Source, lo: int, pos: int) -> list[str] | None:
    """Keys of the object literals enclosing ``pos`` inside the mock factory at ``lo``, outermost first; None
    when one of them is a function body, an array or a call (the shape is not a plain client mock)."""
    stack, i = [], lo + 1
    while i < pos:
        j = src.skip_space(i)
        if j != i:
            i = j
            continue
        c = text[i]
        if c in QUOTES:
            i = src.skip_string(i)
            continue
        if c in OPENERS:
            stack.append(i)
        elif c in CLOSERS:
            if not stack:
                return None
            stack.pop()
        i += 1
    keys: list[str] = []
    for o in stack:
        tail = text[max(lo, o - 120) : o]
        if text[o] == "(":
            if tail.rstrip().endswith("=>") and text[src.skip_space(o + 1)] == "{":
                continue
            return None
        if text[o] != "{":
            return None
        keyed = JS_KEYED.search(tail)
        if keyed:
            keys.append(next(g for g in keyed.groups() if g))
        elif not (JS_CLASS.search(tail) or JS_RETURNED.search(tail)):
            return None
    return keys


# --- Python ----------------------------------------------------------------------------


def _python(text: str) -> list[Expect]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    offsets, out = _Offsets(text), []

    def visit(node: ast.AST, inherited: dict[str, str | None]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = _patched(child, inherited)
                for call in _own(child):  # each call once, with the bindings of the function it is written in
                    f = call.func if isinstance(call, ast.Call) else None
                    if not (isinstance(f, ast.Attribute) and f.attr in PY_ASSERTS and isinstance(f.value, ast.Name)):
                        continue
                    head = _stripe_tail(names.get(f.value.id))
                    start = offsets.at(f.end_lineno, f.end_col_offset)
                    if head and text[start : start + 1] == "(":
                        out.append(Expect("mock", head, start, offsets.at(call.end_lineno, call.end_col_offset), head, ""))
                visit(child, names)
            elif not isinstance(child, ast.Lambda):
                visit(child, inherited)

    visit(tree, {})
    return sorted(out, key=lambda e: e.lo)


def _own(fn: ast.AST):
    """The nodes of ``fn`` outside the bodies of the functions, lambdas and classes nested in it (a nested
    definition itself is yielded: it binds its name here)."""
    todo = [n for n in ast.iter_child_nodes(fn) if n not in getattr(fn, "decorator_list", ())]
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            if not isinstance(node, ast.Lambda):
                yield node
            continue
        yield node
        todo += ast.iter_child_nodes(node)


def _patched(fn: ast.FunctionDef | ast.AsyncFunctionDef, inherited: dict[str, str | None] | None = None) -> dict[str, str | None]:
    """Mock names visible in ``fn`` and the dotted target each one patches (None: bound more than one way). A
    closure sees its enclosing function's mocks unless it binds the name itself."""
    out: dict[str, str | None] = {}

    def bind(name: str, dotted: str | None) -> None:
        out[name] = dotted if name not in out or out[name] == dotted else None

    args = fn.args
    params = [a.arg for a in args.posonlyargs + args.args]
    every = params + [a.arg for a in args.kwonlyargs] + [a.arg for a in (args.vararg, args.kwarg) if a]
    if params[:1] in (["self"], ["cls"]):
        params = params[1:]
    injected: list[str | None] = []
    for dec in reversed(fn.decorator_list):  # the bottom decorator's mock is the first argument
        kind, dotted = _patch(dec)
        if kind == "unknown":
            return {}
        if kind == "inject":
            injected.append(dotted)
    if len(injected) > len(params):
        return {}
    for name, dotted in zip(params, injected):
        bind(name, dotted)
    for name in every:
        if name not in out:
            bind(name, None)  # a plain parameter hides an enclosing function's mock of that name
    for node in _own(fn):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                kind, dotted = _patch(item.context_expr)
                if isinstance(item.optional_vars, ast.Name):
                    bind(item.optional_vars.id, dotted if kind == "inject" else None)
        elif isinstance(node, ast.Assign):
            value = node.value
            started = isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "start"
            kind, dotted = _patch(value.func.value, mocker=False) if started else _patch(value, mocker=True, only_mocker=True)
            for t in node.targets:
                if isinstance(t, ast.Name):
                    bind(t.id, dotted if kind == "inject" else None)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)) and isinstance(node.target, ast.Name):
            bind(node.target.id, None)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            for t in ast.walk(node.target):
                if isinstance(t, ast.Name):
                    bind(t.id, None)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind(node.name, None)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for name in node.names:
                bind(name, None)
    return {**{k: v for k, v in (inherited or {}).items() if k not in out}, **out}


def _patch(node: ast.AST, mocker: bool = False, only_mocker: bool = False) -> tuple[str, str | None]:
    """("inject", dotted) for a patch that hands over a mock of ``dotted``; ("none", None) for anything that
    is not a patch or hands over nothing; ("unknown", None) for a patch form this reader does not follow.
    ``mocker``: pytest-mock's ``mocker.patch`` counts (it returns the mock); ``only_mocker``: nothing else does
    (a bare ``patch(...)`` assigned is a patcher, not a mock)."""
    if not isinstance(node, ast.Call):
        return "none", None
    name = _dotted(node.func) or ""
    parts = name.split(".")
    if mocker and parts[:1] == ["mocker"]:
        parts = ["patch", *parts[2:]] if parts[1:2] == ["patch"] else []
    elif only_mocker:
        return "none", None
    if "patch" not in parts:
        return "none", None
    tail = parts[parts.index("patch") + 1 :]
    if any(k.arg == "new" for k in node.keywords):
        return "none", None
    if tail == []:
        if len(node.args) != 1 or not _string(node.args[0]):
            return "unknown", None
        return "inject", node.args[0].value  # type: ignore[attr-defined]
    if tail == ["object"]:
        target = _dotted(node.args[0]) if node.args else None
        if len(node.args) != 2 or target is None or not _string(node.args[1]):
            return "unknown", None
        return "inject", f"{target}.{node.args[1].value}"  # type: ignore[attr-defined]
    if tail == ["dict"]:
        return "none", None
    return "unknown", None


def _string(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _stripe_tail(dotted: str | None) -> str | None:
    """``stripe.PaymentIntent.create`` out of ``app.payments.stripe.PaymentIntent.create``."""
    segs = (dotted or "").split(".")
    at = [n for n, s in enumerate(segs) if s == "stripe"]
    tail = segs[at[-1] :] if at else []
    return ".".join(tail) if len(tail) >= 3 and all(s.isidentifier() for s in tail) else None


# --- PHP -------------------------------------------------------------------------------


def _php(text: str) -> list[Expect]:
    lex = phplex.lex(text)
    if not lex.balanced or lex.uncertain:
        return []
    uses = {(alias or full.rsplit("\\", 1)[-1]): full for full, alias in PHP_USE.findall(text)}
    out = []
    for pattern in (MOCKERY, PHPUNIT):
        for m in pattern.finditer(text):
            if lex.masked(m.start()):
                continue
            head = _php_head(text, lex, m.start(), m.group("recv"), m.group("method"), uses)
            open_idx = m.end() - 1
            found = _params(lex, text, open_idx, PHP_WRAPPERS, _php_array) if head else None
            if head and found is not None:
                out.append(Expect("mockery", head, open_idx, found[0], "<?php " + head, ");", found[1]))
    return sorted(out, key=lambda e: e.lo)


def _php_array(lex: Lex, i: int) -> int | None:
    opener = lex.array_open(i)
    return lex.pairs[opener] + 1 if opener is not None else None


def _php_head(text: str, lex: Lex, at: int, var: str, method: str, uses: dict[str, str]) -> str | None:
    """The SDK call a mock in ``var`` stands for: from its one assignment in the enclosing function."""
    fn = max((m.start() for m in re.finditer(r"\bfunction\b", text[:at]) if not lex.masked(m.start())), default=0)
    region = text[fn:at]
    assigned = [m for m in re.finditer(re.escape(var) + r"\s*=(?![=>])", region) if not lex.masked(fn + m.start())]
    if len(assigned) != 1:
        return None
    mock = re.compile(re.escape(var) + r"\s*=\s*" + PHP_MOCK).match(region, assigned[0].start())
    if not mock:
        return None
    cls, alias = mock.group("cls"), False
    if cls[:1] in "'\"" and cls[-1:] == cls[:1]:
        name = cls[1:-1].replace("\\\\", "\\")
        alias = name.startswith(("alias:", "overload:"))
        if not alias:
            return None
        name = name.split(":", 1)[1]
    elif cls.endswith("::class"):
        name = cls[: -len("::class")].strip()
        if not name.startswith("\\"):
            first, _, rest = name.partition("\\")
            name = uses.get(first, "") + ("\\" + rest if rest else "") if first in uses else ""
    else:
        return None
    segs = name.strip("\\").split("\\")
    if len(segs) < 2 or segs[0] != "Stripe" or not all(re.fullmatch(r"[A-Z]\w*", s) for s in segs[1:]):
        return None
    if segs[1] == "Service":  # ``Stripe\Service\Checkout\SessionService``: ``$stripe->checkout->sessions``
        if alias or not segs[-1].endswith("Service") or len(segs) < 3:
            return None
        parts = [s[:1].lower() + s[1:] for s in segs[2:-1]] + [_plural(segs[-1][: -len("Service")])]
        return "$stripe->" + "->".join(parts) + "->" + method
    return "\\" + "\\".join(segs) + "::" + method if alias else None


def _plural(name: str) -> str:
    low = name[:1].lower() + name[1:]
    if low.endswith("y") and low[-2:-1] not in "aeiou":
        return low[:-1] + "ies"
    return low + ("es" if low.endswith(("s", "x", "ch", "sh")) else "s")
