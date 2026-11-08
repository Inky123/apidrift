"""Ruby blocks by indentation: each ``def`` and each line that ends in ``do`` (``do |x|``) opens one that
runs to the next line indented no deeper (that line included when it is the block's ``end``). Blocks are
kept nested: one that would run past the block around it is cut at that block's end. Built once per file;
every lookup is a binary search plus a walk up the enclosing blocks.
"""

from __future__ import annotations

import bisect
import re

SCOPE = re.compile(r"^([ \t]*)(?:(?:private|protected|public)[ \t]+)?(?:def\b|\S[^\n]*\bdo(?:[ \t]*\|[^|\n]*\|)?[ \t]*$)", re.M)
END = re.compile(r"end\b")


class Scopes:
    def __init__(self, text: str) -> None:
        lines = [(m.start(), m.group(0)) for m in re.finditer(r"[^\n]*\n?", text) if m.group(0)]
        index = {s: k for k, (s, _) in enumerate(lines)}
        indents = [_indent(body) for _, body in lines]
        spans: list[tuple[int, int]] = []
        for m in SCOPE.finditer(text):
            k = index.get(m.start())
            if k is None:
                continue
            indent, end = len(m.group(1).expandtabs()), len(text)
            for j in range(k + 1, len(lines)):
                if indents[j] is not None and indents[j] <= indent:
                    s, body = lines[j]
                    end = s + len(body) if END.match(body.strip()) else s
                    break
            spans.append((m.start(), end))
        self.starts: list[int] = []
        self.ends: list[int] = []
        self.parent: list[int] = []
        stack: list[int] = []
        for start, end in spans:
            while stack and self.ends[stack[-1]] <= start:
                stack.pop()
            if stack:
                end = min(end, self.ends[stack[-1]])
            self.parent.append(stack[-1] if stack else -1)
            self.starts.append(start)
            self.ends.append(end)
            stack.append(len(self.starts) - 1)

    def innermost(self, pos: int) -> int | None:
        k = bisect.bisect_right(self.starts, pos) - 1
        while k >= 0 and not pos < self.ends[k]:
            k = self.parent[k]
        return k if k >= 0 else None

    def root(self, pos: int) -> int | None:
        k = self.innermost(pos)
        while k is not None and self.parent[k] >= 0:
            k = self.parent[k]
        return k

    def holds(self, k: int | None, pos: int) -> bool:
        return k is not None and self.starts[k] <= pos < self.ends[k]

    def span(self, k: int) -> tuple[int, int]:
        return self.starts[k], self.ends[k]


def _indent(body: str) -> int | None:
    """Indentation of a line that counts for block ends: None for blank and comment lines."""
    stripped = body.strip()
    if not stripped or stripped.startswith("#"):
        return None
    expanded = body.expandtabs()
    return len(expanded) - len(expanded.lstrip())
