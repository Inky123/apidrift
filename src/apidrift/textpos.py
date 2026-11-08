"""Line numbers of positions in one text, without counting the file's newlines again for every position.

``text.count("\\n", 0, pos) + 1`` is linear in ``pos``; asked once per match in a file with thousands of
matches it is quadratic. :class:`LineCounter` serves positions that come in text order (a pattern's
matches) by counting only the stretch since the last one, and :func:`line_of` answers positions in any
order from the newline positions found once. Both give exactly ``text.count("\\n", 0, pos) + 1``.
"""

from __future__ import annotations

import bisect
import re

NEWLINE = re.compile("\n")


class LineCounter:
    """Line numbers of positions asked mostly in increasing order (any order is answered correctly)."""

    __slots__ = ("text", "pos", "count")

    def __init__(self, text: str) -> None:
        self.text, self.pos, self.count = text, 0, 0

    def line(self, pos: int) -> int:
        if pos < 0:
            return self.text.count("\n", 0, pos) + 1
        if pos < self.pos:
            self.pos, self.count = 0, 0
        self.count += self.text.count("\n", self.pos, pos)
        self.pos = pos
        return self.count + 1


def newlines(text: str) -> list[int]:
    """The positions of the newlines, for :func:`line_of`."""
    return [m.start() for m in NEWLINE.finditer(text)]


def line_of(starts: list[int], pos: int) -> int:
    """``text.count("\\n", 0, pos) + 1`` for ``pos >= 0``, given :func:`newlines` of the text."""
    return bisect.bisect_left(starts, pos) + 1
