"""Old-key tokens the leftover backstop may keep: PHP reads of a webhook or response payload.

After a rename, a token of the old key left in the file refuses the fix (:func:`fixer._oracle_tokens`),
because it may be the same request built some other way. One kind is provably not that: a key read off an
array that came in from outside the function and is only tested, as in a webhook handler::

    public function handleSetupIntentSucceeded(array $event): void {
        $setup_intent = $event['data']['object'];
        if (!isset($setup_intent['payment_method_types'])) { return; }

Such a token is kept (and listed as kept) only when every condition holds:

1. it is ``$v['key']`` inside a named function, and every assignment of ``$v`` there is a subscript chain
   of a parameter of that function (``$event['data']['object']``) that is itself never written or rebound;
   ``$v`` is never written, aliased, returned or passed whole (its only other uses read a literal key);
2. the read is only tested: a whole argument of ``isset`` / ``empty`` / ``array_key_exists`` /
   ``in_array`` (``in_array`` also with ``?? <literal>``), and that call sits in the condition of an
   ``if`` / ``elseif`` / ``while`` through ``!``, ``&&``, ``||`` and grouping parentheses only, so the
   value is never stored or sent (comparisons are not accepted: ``$a = $v['k'] == 'x'`` would store it);
3. no call of the function in the repository's source passes an array literal, or a local bound to one,
   at that parameter (an argument unpacked or named counts as one), and no test file passes one whose
   text names the key: no literal this fix edits can flow into the payload (tests are not callers that
   send; a test literal without the key holds nothing a fix of the key edits);
4. the function and its callers are readable (balanced, no ``extract``/``eval``/variable variables), and
   the checkout is known. Otherwise the token is a leftover, as before.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import phpcross, phpparse, scanner
from .phphelpers import PhpHelpers, _named
from .phpscope import BIND, INPLACE, READS_ALL

TESTS = ("isset", "empty", "array_key_exists", "in_array")
CONDITIONS = ("if", "elseif", "while")
SUBSCRIPTS = r"(?:\s*\[\s*(?:'[^'\\\n]*'|\"[^\"\\\n$]*\"|\d+)\s*\])+"
DEFAULT = re.compile(r"\s*\?\?\s*(?:\[\s*\]|null\b|true\b|false\b|'[^'\\\n]*'|\"[^\"\\\n$]*\"|-?\d+)", re.I)
MAX_FILES = 400


def php_kept(text: str, tokens, key: str, where: tuple[Path | None, str]) -> set[int]:
    """Positions among ``tokens`` (quoted ``key`` tokens of ``text``) that are tested reads of a payload."""
    root, rel = where
    if root is None or not tokens:
        return set()
    parsed = phpparse.parse(text, lambda chain: None)
    src = parsed.src
    if not src.balanced or src.uncertain:
        return set()
    helpers = PhpHelpers(parsed)
    out: set[int] = set()
    for t in tokens:
        got = _read(parsed, helpers, t.pos - 1, key) if t.quoted else None
        if got is not None and _no_literal_callers(root, got, key):
            out.add(t.pos)
    return out


def _read(parsed, helpers: PhpHelpers, quote: int, key: str):
    """(function name, parameter index) when the string at ``quote`` is a tested read of a payload."""
    src, text, bindings = parsed.src, parsed.src.text, parsed.bindings
    opener = src.back_space(quote)
    if text[opener - 1 : opener] != "[" or (opener - 1) not in src.pairs:
        return None
    close = src.pairs[opener - 1]
    if src.skip_space(quote + len(key) + 2) != close:
        return None
    var = re.search(r"\$([A-Za-z_]\w*)\s*$", src.code[max(0, opener - 80) : opener - 1])
    if var is None or var.group(1) == "this":
        return None
    name, start = var.group(1), opener - 1 - len(var.group(0))
    p = src.back_space(start)
    if text[p - 2 : p] in ("->", "::") or text[p - 1 : p] == "$":
        return None
    d = helpers.enclosing(start)
    if d is None or bindings.dynamic(name, start) or READS_ALL.search(src.code, d.open, d.close):
        return None
    param = _payload(parsed, d, name)
    if param is None or not _tested(src, start, close + 1):
        return None
    return d.name, param


def _payload(parsed, d, name: str) -> int | None:
    """Index of the parameter every binding of ``name`` in ``d`` subscripts; None when one does not."""
    src, bindings, text = parsed.src, parsed.bindings, parsed.src.text
    uses = [u for u in bindings.uses.get(name, ()) if d.open < u.pos < d.close]
    if parsed.scopes.parameter(name, d.open + 1):
        return None
    params, found = _params(src, d), set()
    for u in uses:
        if u.kind == BIND:
            value = src.skip_space(src.skip_space(u.pos + len(name) + 1) + 1)
            m = re.compile(rf"\$([A-Za-z_]\w*){SUBSCRIPTS}\s*;").match(text, value)
            if m is None or m.group(1) not in params:
                return None
            found.add(m.group(1))
        elif u.kind != INPLACE or u.key is None:
            return None  # written, aliased, returned, passed whole, captured
    if len(found) != 1:
        return None
    source = found.pop()
    if any(u.kind != INPLACE for u in bindings.uses.get(source, ()) if d.open < u.pos < d.close):
        return None  # the parameter is rebound, written or passed on
    return params.index(source)


def _params(src, d) -> list[str]:
    paren = src.code.index("(", d.pos)
    out = []
    for a, b in src.args(paren):
        m = re.search(r"(&?)\s*(\.\.\.)?\s*\$([A-Za-z_]\w*)", src.text[a:b])
        out.append("" if m is None or m.group(1) or m.group(2) else m.group(3))
    return out


def _tested(src, start: int, end: int) -> bool:
    """``src.text[start:end]`` is only tested, directly in an ``if`` / ``elseif`` / ``while`` condition."""
    text = src.text
    default = DEFAULT.match(text, end)
    end = default.end() if default else end
    opener = src.enclosing(start)
    if opener < 0 or text[opener] != "(":
        return False
    head = _head(src, opener)
    if head not in TESTS or (default and head != "in_array"):
        return False  # comparisons and every other use are not accepted
    if not any(src.skip_space(a) == start and src.back_space(b) == end for a, b in src.args(opener)):
        return False  # part of a larger argument
    return _in_condition(src, src.back_space(opener) - len(head), src.pairs[opener] + 1)


def _in_condition(src, start: int, end: int) -> bool:
    """The boolean expression at ``[start, end)`` reaches an ``if`` / ``elseif`` / ``while`` condition
    through ``!``, ``&&``, ``||`` and grouping parentheses only (never stored, returned or passed on)."""
    text = src.text
    while True:
        opener = src.enclosing(start)
        if opener < 0 or text[opener] != "(":
            return False
        b = text[: src.back_space(start)].rstrip()
        if b.endswith("!") and not b.endswith("!="):
            b = b[:-1].rstrip()
        after = text[src.skip_space(end) :]
        if not (b.endswith(("(", "&&", "||")) or re.search(r"\b(?:and|or)$", b, re.I)):
            return False
        if not (after.startswith((")", "&&", "||")) or re.match(r"(?:and|or)\b", after, re.I)):
            return False
        head = _head(src, opener)
        if head in CONDITIONS:
            return True
        if head:
            return False  # an argument of some other call: its value goes somewhere
        start, end = opener, src.pairs[opener] + 1


def _head(src, opener: int) -> str:
    m = re.search(r"([A-Za-z_]\w*)\s*$", src.code[max(0, opener - 40) : opener])
    return m.group(1).lower() if m else ""


def _no_literal_callers(root: Path, got: tuple[str, int], key: str) -> bool:
    """No call of the function in the repository's source passes a visible array at the parameter. A test
    file's call counts only when the array it passes names ``key``: a test is not a caller that sends, and
    a literal without the key holds nothing a fix of that key edits."""
    name, index = got
    word = re.compile(rf"(?<![\w$]){re.escape(name)}\s*\(", re.I)
    files = [p for p in scanner._iter_files(root)[0] if p.suffix.lower() == ".php"]
    hits = []
    for path in files:
        text = scanner._read(path)
        if text is not None and word.search(text):
            rel = path.relative_to(root).as_posix()
            hits.append((text, scanner._doc_like(rel, ".php")))
        if len(hits) > MAX_FILES:
            return False
    for text, test in hits:
        parsed = phpparse.parse(text, lambda chain: None)
        src = parsed.src
        ctx = phpcross.Context(parsed, PhpHelpers(parsed))
        for m in word.finditer(text):
            span = src.span_at(m.start())
            if span is not None and span[2] == "comment":
                continue
            if span is not None:
                return False  # in a string or HTML: not readable
            if re.search(r"function\s*&?\s*$", src.code[max(0, m.start() - 40) : m.start()], re.I):
                continue  # its definition
            paren = m.end() - 1
            args = src.args(paren) if paren in src.pairs else None
            if args is None or not src.balanced or src.uncertain:
                return False
            if any(text.startswith("...", src.skip_space(a)) or _named(src, a) for a, _ in args):
                return False
            if index < len(args) and _literal(ctx, *args[index], key if test else None):
                return False
    return True


def _literal(ctx, a: int, b: int, key: str | None = None) -> bool:
    """The argument is an array literal or a local any of whose reaching bindings is one (with ``key``: one
    whose text names the key)."""
    src = ctx.src
    i = src.skip_space(a)
    opener = src.array_open(i)
    if opener is not None:
        return _names(src, opener, key)
    m = phpparse.VARIABLE.match(src.text, i)
    if m is None or src.skip_space(m.end()) < b:
        return False
    return any(r.literal is not None and _names(src, r.literal, key) for r in ctx.parsed.bindings.reaching(m.group(1), i))


def _names(src, opener: int, key: str | None) -> bool:
    return key is None or re.search(rf"(?<![\w$]){re.escape(key)}(?![\w$])", src.text[opener : src.pairs[opener] + 1]) is not None


def kept(after: str, language: str, m, remaining, where) -> set[int]:
    """The leftover backstop's exception for ``m``'s old key in the edited PHP file (positions)."""
    if language != "php" or m.action == "wrap_type":
        return set()
    return php_kept(after, [t for t in remaining if t.quoted], m.target.rsplit(".", 1)[-1], where)


def kept_places(file: str, content: str, planned, root: Path | None) -> list[tuple[str, int]]:
    """(file, line) of every kept read in a fixed PHP file, for the payload and the pull request."""
    from .fixtokens import tokens

    out = set()
    for m in {p[1] for p in planned}:
        if m.action != "wrap_type":
            found = tokens(content, "php", m.target.rsplit(".", 1)[-1])
            at = kept(content, "php", m, found, (root, file))
            out |= {(file, t.line) for t in found if t.pos in at}
    return sorted(out)
