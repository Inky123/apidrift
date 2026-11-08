"""Whether the :mod:`jsparse` cursor reads one object/hash literal exactly as JS or Ruby would.

The cursor steps over strings, comments and brackets but has no idea of regex literals (``/'/``, ``/}/``),
JSX text, Ruby heredocs (``<<~EOS``), percent literals (``%q(...)``), character literals (``?'``) or
``$'``-style globals: inside one it can see a quote, bracket, comma or key that is not there, so a key it
reports may sit inside a regex, a value may end at a comma inside one, and the literal may end early at a
``}`` inside one, hiding a later key or spread. The re-scan oracle reads the file with the same cursor, so
it cannot catch that; the edit is not attempted instead (``unsupported_value``).

So the whole literal, from its opener to the closer the cursor found, is checked token by token, and
anything that is not positively known to read the same is refused:

- a ``/`` is division only right after a token that ends a value: a number, a string, ``]``, a name that
  is not a keyword, or a ``)`` whose ``(`` does not follow a keyword (``if (x) /re/``, ``for await (...)``);
  in Ruby also not at a line start and not ``name /x`` (a command call's regex argument);
- a ``<`` only as a comparison or shift with a space or ``=`` after it (``a < b``, ``a <= b``, ``a << b``);
- a Ruby ``%`` only as modulo after a value; ``?`` only followed by a space (a ternary);
- interpolations (``${...}``, ``#{...}``) are checked the same way;
- the edited entry itself (key, value, separator) is stricter for a rewrite of its value: no ``/``, ``<``
  or percent literal at all.

Because every misreadable construct starts before anything it could hide, a literal that passes is read
correctly up to its real closer: the cursor's closer is the real one.
"""

from __future__ import annotations

import re

from .jsparse import IDENT, QUOTES, Source

# Words after which a ``/`` starts a regex, or a ``(`` opens a statement head rather than a value.
JS_KEYWORDS = {
    "await", "break", "case", "catch", "class", "const", "continue", "debugger", "default", "delete", "do", "else",
    "export", "extends", "finally", "for", "function", "if", "import", "in", "instanceof", "let", "new", "of",
    "return", "static", "switch", "throw", "try", "typeof", "var", "void", "while", "with", "yield", "async",
}
RUBY_KEYWORDS = {
    "alias", "and", "begin", "break", "case", "class", "def", "defined", "do", "else", "elsif", "end", "ensure",
    "for", "if", "in", "module", "next", "not", "or", "redo", "rescue", "retry", "return", "then", "undef",
    "unless", "until", "when", "while", "yield", "puts", "p", "print", "raise", "fail", "require", "include",
    "extend", "lambda", "proc", "loop",
}
RUBY_PERCENT = re.compile(r"%[qQwWiIrsx]?[^\w\s=]")
NUMBER = re.compile(r"\d[\w.]*")


def unsafe_entry(src: Source, lo: int, key_start: int, entry_end: int, hi: int, *, strict: bool) -> bool:
    """``lo``: the literal's opener (or first Ruby keyword argument); ``key_start``/``entry_end``: the edited
    entry; ``hi``: the closer the cursor found. ``strict``: the edit rewrites the value (not just the key).
    True when the edit must not be made."""
    return (
        _unsafe(src, lo, key_start, strict=False)
        or _unsafe(src, key_start, entry_end, strict=strict)
        or _unsafe(src, entry_end, hi, strict=False)
    )


def _unsafe(src: Source, lo: int, hi: int, *, strict: bool) -> bool:
    text, ruby = src.text, src.ruby
    keywords = RUBY_KEYWORDS if ruby else JS_KEYWORDS
    prev, word, opens = "op", "", []  # prev: the last token ended a "value", was a "name", or an "op"
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
            if strict or not _division(text, i, prev, ruby):
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
        elif ruby and c == "%":
            if RUBY_PERCENT.match(text, i) and (strict or not _division(text, i, prev, ruby)):
                return True  # %q(...), %w[...], %r{...}: their contents are not tokens
            i, prev = i + 1, "op"
        elif ruby and c == "?" and text[i + 1 : i + 2] not in (" ", "\t", "\n", "\r"):
            return True  # ``?'`` is a character literal
        elif ruby and c == "$" and not re.match(r"\$[A-Za-z_]", text[i : i + 2]):
            return True  # ``$'``, ``$/``: special globals
        elif ruby and text.startswith("=begin", i) and _line_start(text, i):
            return True
        elif c in "([{":
            opens.append(c == "(" and last in keywords)  # a statement head, not a value
            i, prev = i + 1, "op"
        elif c in ")]}":
            head = opens.pop() if opens else True
            i, prev = i + 1, ("op" if c == "}" or head else "value")
        elif m := IDENT.match(text, i):
            word = m.group(0)
            i, prev = m.end(), ("op" if word in keywords else "name")
            if ruby and i < hi and text[i] in "?!" and text[i + 1 : i + 2] != "=":  # ``empty?``: a method name
                i, prev = i + 1, "op"
        elif m := NUMBER.match(text, i):
            i, prev = m.end(), "value"
        else:
            i, prev = i + 1, "op"
    return False


def _division(text: str, i: int, prev: str, ruby: bool) -> bool:
    """A ``/`` (or Ruby ``%``) that JS and Ruby can only read as an operator: it follows a value."""
    if prev not in ("value", "name"):
        return False
    if ruby and (_line_start(text, i) or (prev == "name" and _argument_like(text, i))):
        return False  # a new Ruby line, or ``foo /x/`` / ``foo %q(x)``: a literal
    return True


def _argument_like(text: str, i: int) -> bool:
    """Ruby ``foo /x/``: a space before and none after makes it the method's argument."""
    return i > 0 and text[i - 1] in " \t" and text[i + 1 : i + 2] not in (" ", "\t", "\n", "\r", "")


def _line_start(text: str, i: int) -> bool:
    j = i - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    return j < 0 or text[j] in "\r\n"


HEREDOC = re.compile(r"<<([~-]?)([\"'`]?)([A-Za-z_]\w*)\2")


def ruby_heredocs(text: str) -> list[tuple[int, int]]:
    """(start, end) of every Ruby heredoc body: text the scanner may read as code but that is a string, so
    nothing in it may be edited. Every ``<<WORD`` outside a body already found counts, even one in a string
    or comment, or an ``a <<B`` append: reading too much as a heredoc only refuses edits."""
    out: list[tuple[int, int]] = []
    last_line = -1
    for m in HEREDOC.finditer(text):
        if any(a <= m.start() < b for a, b in out):
            continue
        nl = text.find("\n", m.end())
        line_of = text.rfind("\n", 0, m.start())
        # a second opener on the same line (``f(<<~A, <<~B)``): its body follows the first one's
        start = out[-1][1] if out and line_of == last_line else (nl + 1 if nl >= 0 else len(text))
        last_line = line_of
        squiggly, word, i = m.group(1) in ("~", "-"), m.group(3), start
        end = len(text)
        while i < len(text):
            j = text.find("\n", i)
            line = text[i : j if j >= 0 else len(text)].rstrip("\r")
            if (line.strip() if squiggly else line) == word:
                end = j + 1 if j >= 0 else len(text)
                break
            if j < 0:
                break
            i = j + 1
        out.append((start, end))
    return out


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
