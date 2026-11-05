"""One pass over a PHP file: what is code, and which brackets pair up.

Strings (with ``{$x["k"]}`` interpolation), heredocs and nowdocs, ``//``, ``#`` and ``/* */`` comments
and the HTML outside ``<?php ... ?>`` become opaque spans; the brackets left over are paired. Everything
else in the PHP reader walks these tables instead of re-reading characters, so a quote or bracket inside
a string can never be taken for code. Nothing is executed and no PHP binary is involved.

``uncertain`` is set when the pass cannot be sure it read the file the way PHP would: an unterminated
string or comment, or a heredoc whose closing line differs between PHP before and after 7.3. The fixer
refuses such a file. :func:`same_structure` is the fixer's before/after check: an edit must leave every
span and every bracket pair outside itself exactly where it was.
"""

from __future__ import annotations

import bisect
import re

OPEN_TAG = re.compile(r"<\?(?:php(?=\s|$)|=)", re.I)
HEREDOC = re.compile(r"<<<[ \t]*(?:\"([A-Za-z_]\w*)\"|'([A-Za-z_]\w*)'|([A-Za-z_]\w*))[ \t]*\r?\n")
INTERESTING = re.compile(r"[?#/'\"`<()\[\]{}]")
ARRAY = re.compile(r"array\s*\(", re.I)
NOT_NEWLINE = re.compile(r"[^\n]")
OPENERS = {"(": ")", "[": "]", "{": "}"}
CLOSERS = set(OPENERS.values())
HTML, STRING, COMMENT, HEREDOC_KIND = "html", "string", "comment", "heredoc"


class Lex:
    """Opaque spans and bracket pairs of one PHP source, plus the cursor moves built on them."""

    def __init__(self, text: str, *, fragment: bool = False) -> None:
        self.text, self.n = text, len(text)
        self.fragment = fragment  # a piece of PHP code with no open tag, read as code from its first character
        self.spans: list[tuple[int, int, str]] = []  # (start, end, kind), in order, never overlapping
        self.pairs: dict[int, int] = {}  # opening bracket -> its closer
        self.back: dict[int, int] = {}  # closing bracket -> its opener
        self.parent: dict[int, int] = {}  # paired opening bracket -> the paired bracket around it (-1 at top level)
        self.balanced = True
        self.uncertain = False
        self._old_closers: dict[str, int] = {}  # heredoc marker -> start of its last pre-7.3 closing line
        self._run()
        self.starts = [s[0] for s in self.spans]
        self.opens = sorted(self.pairs)
        # The same text with every opaque span blanked out, offsets unchanged: what the patterns that look
        # for code are run on, so a word inside a string or comment is never matched (or even scanned).
        pieces, last = [], 0
        for a, b, _ in self.spans:
            pieces += [text[last:a], NOT_NEWLINE.sub(" ", text[a:b])]
            last = b
        self.code = "".join(pieces) + text[last:]

    # --- the pass ---------------------------------------------------------------------

    def _run(self) -> None:
        text, n = self.text, self.n
        stack: list[int] = []
        around: dict[int, int] = {}  # every opening bracket -> the one open when it was read
        i = 0 if self.fragment else self._html(0)
        while i < n:
            m = INTERESTING.search(text, i)
            if m is None:
                break
            i, c = m.start(), m.group(0)
            if c == "?":
                i = self._html(i) if text.startswith("?>", i) else i + 1
            elif (c == "#" and not text.startswith("#[", i)) or text.startswith("//", i):
                i = self._add(i, self._line_comment(i), COMMENT)
            elif text.startswith("/*", i):
                end = text.find("*/", i + 2)
                self.uncertain = self.uncertain or end < 0
                i = self._add(i, n if end < 0 else end + 2, COMMENT)
            elif c in "'\"`":
                i = self._add(i, self._string(i), STRING)
            elif c == "<":
                h = HEREDOC.match(text, i)
                i = self._add(i, self._heredoc(h), HEREDOC_KIND) if h else i + 1
            elif c in OPENERS:
                around[i] = stack[-1] if stack else -1
                stack.append(i)
                i += 1
            elif c in CLOSERS:
                if stack and OPENERS[text[stack[-1]]] == c:
                    opener = stack.pop()
                    self.pairs[opener], self.back[i] = i, opener
                else:
                    self.balanced = False
                i += 1
            else:
                i += 1
        if stack:
            self.balanced = False
        for opener in self.pairs:  # in an unbalanced file a bracket that never closes is not anyone's parent
            up = around[opener]
            while up >= 0 and up not in self.pairs:
                up = around[up]
            self.parent[opener] = up

    def _add(self, start: int, end: int, kind: str) -> int:
        self.spans.append((start, end, kind))
        return end

    def _html(self, i: int) -> int:
        """From ``i`` outside PHP mode (the start of the file, or a ``?>``): the index just past the next open tag."""
        m = OPEN_TAG.search(self.text, i)
        end = m.end() if m else self.n
        if end > i:
            self.spans.append((i, end, HTML))
        return end

    def _line_comment(self, i: int) -> int:
        nl = self.text.find("\n", i)
        nl = self.n if nl < 0 else nl
        close = self.text.find("?>", i, nl)  # a closing tag ends a one-line comment
        return close if close >= 0 else nl

    def _string(self, i: int) -> int:
        text, quote = self.text, self.text[i]
        j = i + 1
        while j < self.n:
            c = text[j]
            if c == "\\":
                j += 2
            elif c == quote:
                return j + 1
            elif quote != "'" and (text.startswith("{$", j) or text.startswith("${", j)):
                j = self._interpolation(j if c == "{" else j + 1)
            else:
                j += 1
        self.uncertain = True
        return self.n

    def _interpolation(self, j: int) -> int:
        """From the ``{`` of ``{$x['k']}`` or ``${x}``: just past its ``}`` (quotes inside are skipped)."""
        text, depth = self.text, 0
        while j < self.n:
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return j + 1
            elif c in "'\"":
                k = j + 1
                while k < self.n and text[k] != c:
                    k += 2 if text[k] == "\\" else 1
                j = k
            j += 1
        return self.n

    def _heredoc(self, m: re.Match[str]) -> int:
        marker = m.group(1) or m.group(2) or m.group(3)
        close = re.compile(rf"^[ \t]*{marker}(?!\w)", re.M).search(self.text, m.end())
        if close is None:
            self.uncertain = True
            return self.n
        # Before PHP 7.3 the closing marker had to start its line and end it (``EOT;``). When the line found
        # here is not of that form and a later one is, the two rules end the heredoc in different places.
        old = re.compile(rf"^{marker};?\r?$", re.M)
        if marker not in self._old_closers:  # found once per marker, so many heredocs do not rescan the file
            self._old_closers[marker] = max((m.start() for m in old.finditer(self.text, close.start())), default=-1)
        if not old.match(self.text, close.start()) and self._old_closers[marker] > close.end():
            self.uncertain = True
        return close.end()

    # --- lookups ----------------------------------------------------------------------

    def span_at(self, pos: int) -> tuple[int, int, str] | None:
        k = bisect.bisect_right(self.starts, pos) - 1
        return self.spans[k] if k >= 0 and pos < self.spans[k][1] else None

    def masked(self, pos: int) -> int:
        """End of the string, comment, heredoc or HTML that holds ``pos``; 0 when ``pos`` is code."""
        span = self.span_at(pos)
        return span[1] if span else 0

    def kinds_in(self, lo: int, hi: int) -> set[str]:
        """Kinds of the opaque spans that overlap ``[lo, hi)``."""
        k = max(0, bisect.bisect_right(self.starts, lo) - 1)
        out: set[str] = set()
        while k < len(self.spans) and self.spans[k][0] < hi:
            if self.spans[k][1] > lo:
                out.add(self.spans[k][2])
            k += 1
        return out

    def enclosing(self, pos: int) -> int:
        """The innermost opening bracket whose pair holds ``pos`` (-1 at top level)."""
        k = bisect.bisect_left(self.opens, pos) - 1
        opener = self.opens[k] if k >= 0 else -1
        while opener >= 0 and self.pairs[opener] < pos:
            opener = self.parent[opener]
        return opener

    def line(self, pos: int) -> int:
        return self.text.count("\n", 0, pos) + 1

    # --- cursor moves -----------------------------------------------------------------

    def skip_space(self, i: int) -> int:
        text = self.text
        while i < self.n:
            if text[i].isspace():
                i += 1
                continue
            span = self.span_at(i) if text[i] in "#/" else None
            if span is None or span[2] != COMMENT:
                break
            i = span[1]
        return i

    def back_space(self, i: int) -> int:
        """The index just past the last code character before ``i`` (whitespace and comments skipped)."""
        text = self.text
        while i > 0:
            if text[i - 1].isspace():
                i -= 1
                continue
            span = self.span_at(i - 1)
            if span is None or span[2] != COMMENT:
                break
            i = span[0]
        return i

    def skip_scalar(self, i: int, stops: str = ",") -> int:
        """The next stop character or closing bracket at this nesting level (not consumed)."""
        text = self.text
        while i < self.n:
            i = self.skip_space(i)
            if i >= self.n:
                break
            c = text[i]
            end = self.masked(i)
            if end:
                i = end
            elif c in stops or c in CLOSERS:
                return i
            elif c in OPENERS:
                i = self.pairs.get(i, self.n - 1) + 1
            else:
                i += 1
        return i

    def args(self, open_idx: int) -> list[tuple[int, int]]:
        """Spans of the top-level arguments of the call whose ``(`` is at ``open_idx``."""
        close = self.pairs.get(open_idx)
        if close is None:
            return []
        out, start = [], open_idx + 1
        while True:
            stop = min(self.skip_scalar(start), close)
            if self.skip_space(start) < stop:
                out.append((start, stop))
            if stop >= close:
                return out
            start = stop + 1

    def array_open(self, i: int) -> int | None:
        """The opening bracket of an array literal that starts at ``i``: its ``[``, or the ``(`` of ``array(``."""
        if i < self.n and self.text[i] == "[":
            return i if i in self.pairs else None
        m = ARRAY.match(self.text, i)
        return m.end() - 1 if m and (m.end() - 1) in self.pairs else None


def mask_spans(text: str) -> list[tuple[int, int]]:
    """Strings, heredocs, comments and inline HTML of a PHP file, for the fixer's token count. Text with no
    open tag is the replacement text of an edit, which is code."""
    return [(a, b) for a, b, _ in Lex(text, fragment=OPEN_TAG.search(text) is None).spans]


def same_structure(before: str, after: str, edits) -> bool:
    """Both texts read cleanly, and the edits changed nothing about how the rest of the file is read: every
    opaque span and bracket pair that lies outside the edits is found again at its shifted place, and
    anything new sits wholly inside the text an edit wrote. There is no ``php -l`` to ask."""
    a, b = Lex(before), Lex(after)
    if not (a.balanced and b.balanced) or a.uncertain or b.uncertain:
        return False
    ranges = sorted({(e.start, e.end, len(e.text)) for e in edits})
    placed, shift = [], 0
    for s, e, size in ranges:
        placed.append((s + shift, s + shift + size))
        shift += size - (e - s)

    def inside(lo: int, hi: int) -> bool:
        return any(s <= lo and hi <= e for s, e, _ in ranges)

    def place(p: int) -> int | None:
        moved = 0
        for s, e, size in ranges:
            if p <= s:
                break
            if p < e:
                return None
            moved += size - (e - s)
        return p + moved

    def expect(items):
        """``(lo, hi, tag)`` items of the old text mapped into the new one; None when an edit cuts one."""
        out = set()
        for lo, hi, tag in items:
            if inside(lo, hi):
                continue
            x, y = place(lo), place(hi)
            if x is None or y is None:
                return None
            out.add((x, y, tag))
        return out

    def kept(old, new) -> bool:
        want = expect(old)
        if want is None or not want <= set(new):
            return False
        return all(item in want or any(s <= item[0] and item[1] <= e for s, e in placed) for item in new)

    pairs = lambda lex: [(o, c + 1, "") for o, c in lex.pairs.items()]  # noqa: E731
    return kept(a.spans, b.spans) and kept(pairs(a), pairs(b))
