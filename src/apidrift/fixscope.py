"""Whether the text around one edited entry can be misread by the :mod:`jsparse` cursor.

The cursor steps over strings, comments and brackets but has no idea of regex literals (``/'/``, ``/}/``),
JSX text, Ruby heredocs (``<<~EOS``) or percent literals (``%r{...}``, ``%q(...)``): inside one it can see
a quote, bracket, comma or key that is not there, so a key it reports may sit inside a regex, and a value
it measures may end at a comma inside one. The re-scan oracle reads the file with the same cursor, so it
cannot catch that misreading; the edit is not attempted instead (``unsupported_value``).

The cursor reads left to right, so what decides the edit is the text from the literal's opener up to the
end of the edited entry:

- the entry itself (key, value and the separator after it): no ``/``, ``<`` or percent literal at all,
  outside strings and comments (interpolations included);
- the siblings before it: a ``/`` only where it can only be division (after a name that is not a keyword,
  a number, ``]``, or a ``)`` that does not close an ``if``/``while``/``for`` head; in Ruby also not
  ``name /x``), a ``<`` only as a comparison or shift with a space after it (``a < b``, ``a <= b``,
  ``a << b``);
- the siblings after it cannot change how the entry was read, but a misread there could hide a second
  entry of the same key from the edit: when they hold such text, the key's name must not appear in them.
"""

from __future__ import annotations

import re

from .jsparse import IDENT, QUOTES, Source

JS_KEYWORDS = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do", "else", "await",
    "yield", "export", "default", "extends",
}
RUBY_KEYWORDS = {
    "if", "elsif", "unless", "when", "while", "until", "and", "or", "not", "return", "then", "do", "else", "in",
    "case", "yield", "puts", "p", "print",
}
CONTROL = {"if", "while", "for", "with", "switch", "catch", "elsif", "unless", "until", "when"}
RUBY_PERCENT = re.compile(r"%[qQwWiIrsx]?[^\w\s=]")
NUMBER = re.compile(r"\d[\w.]*")


def unsafe_entry(src: Source, lo: int, key_start: int, entry_end: int, hi: int, leaf: str) -> bool:
    """``lo``: the literal's opener (or first Ruby keyword argument); ``key_start``/``entry_end``: the edited
    entry; ``hi``: the literal's closer; ``leaf``: the key's bare name. True when the edit must not be made."""
    if _unsafe(src, lo, key_start, strict=False) or _unsafe(src, key_start, entry_end, strict=True):
        return True
    return _unsafe(src, entry_end, hi, strict=False) and re.search(rf"(?<![\w$]){re.escape(leaf)}(?![\w$])", src.text[entry_end:hi]) is not None


def _unsafe(src: Source, lo: int, hi: int, *, strict: bool) -> bool:
    text, ruby = src.text, src.ruby
    keywords = RUBY_KEYWORDS if ruby else JS_KEYWORDS
    prev, word, opens = "op", "", []  # prev: the last token was a "value" end, a "name", or an "op"
    i = lo
    while i < hi:
        j = src.skip_space(i)
        if j > i:
            i = j
            continue
        c, last, word = text[i], word, ""  # ``last``: the name just before this token, if it is one
        if c in QUOTES:
            end = src.skip_string(i)
            if _unsafe_interpolations(src, i, min(end, hi), strict=strict):
                return True
            i, prev = end, "value"
        elif c == "/":
            if strict or not _division(text, i, prev, last, ruby):
                return True
            i, prev = i + 1, "op"
        elif c == "<":
            if strict:
                return True
            if text.startswith("<<", i) and text[i + 2 : i + 3] in (" ", "\t"):
                i += 2
            elif text[i + 1 : i + 2] in (" ", "\t", "\n", "\r", "="):
                i += 1
            else:
                return True  # a generic, JSX, a heredoc: not a comparison
            prev = "op"
        elif ruby and c == "%" and RUBY_PERCENT.match(text, i) and (
            prev == "op" or _line_start(text, i) or (prev == "name" and _argument_like(text, i))
        ):
            return True  # %q(...), %w[...], %i[...]: their contents are not tokens
        elif ruby and text.startswith("%r", i):
            return True
        elif c in "([{":
            opens.append(c == "(" and last in CONTROL)
            i, prev = i + 1, "op"
        elif c in ")]}":
            control = opens.pop() if opens else True
            i, prev = i + 1, ("op" if c == "}" or control else "value")
        elif m := IDENT.match(text, i):
            word = m.group(0)
            i, prev = m.end(), ("op" if word in keywords else "name")
            if ruby and i < hi and text[i] in "?!":  # ``empty?``: a method name, then an operator may follow
                i, prev = i + 1, "op"
        elif m := NUMBER.match(text, i):
            i, prev = m.end(), "value"
        else:
            i, prev = i + 1, "op"
    return False


def _division(text: str, i: int, prev: str, word: str, ruby: bool) -> bool:
    """A ``/`` that both JS and Ruby can only read as division at this point."""
    if ruby and _line_start(text, i):
        return False  # a Ruby line may start a new expression: ``/x/`` there is a regex
    if prev == "value":
        return True
    if prev != "name" or word in CONTROL:
        return False
    return not ruby or not _argument_like(text, i)


def _argument_like(text: str, i: int) -> bool:
    """Ruby ``foo /x/`` / ``foo %q(x)``: a space before and none after makes it the method's argument."""
    return i > 0 and text[i - 1] in " \t" and text[i + 1 : i + 2] not in (" ", "\t", "\n", "\r", "")


def _line_start(text: str, i: int) -> bool:
    j = i - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    return j < 0 or text[j] in "\r\n"


def _unsafe_interpolations(src: Source, i: int, end: int, *, strict: bool) -> bool:
    """Interpolations (``${...}``, Ruby ``#{...}``) in the string at ``i`` hold code: scan it the same way."""
    text, quote = src.text, src.text[i]
    mark = "${" if quote == "`" else ("#{" if src.ruby and quote == '"' else None)
    if mark is None:
        return False
    k = i + 1
    while k < end:
        if text[k] == "\\":
            k += 2
        elif text.startswith(mark, k):
            close = src.skip_balanced(k + 1)
            if _unsafe(src, k + 2, close - 1, strict=strict):
                return True
            k = close
        else:
            k += 1
    return False
