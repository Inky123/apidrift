"""Parse Python source that is newer than the Python apidrift runs on, without moving any node.

Python 3.14 (PEP 758) accepts ``except A, B:`` without brackets; older parsers reject the whole file, so every
Stripe call in it went unseen (polar: 12 files, its whole Stripe service). When the plain parse fails, each such
clause whose colon ends its line is read as ``except(A, B):`` -- the space after ``except`` becomes ``(`` and a
``)`` goes before the colon -- so every position the fixer edits by (line and column) is the same as in the file
on disk. Nothing else is rewritten: a file that still does not parse is not read.
"""

from __future__ import annotations

import ast
import io
import tokenize

OPEN, CLOSE = "([{", ")]}"


UNPARSABLE = (SyntaxError, ValueError, RecursionError, MemoryError)  # MemoryError: too deeply nested to parse


def parse(text: str) -> ast.Module | None:
    """The module, or None when it cannot be read (the caller then reads nothing in it)."""
    try:
        return ast.parse(text)
    except UNPARSABLE:
        pass
    patched = bare_except_tuples(text)
    if patched is None:
        return None
    try:
        return ast.parse(patched)
    except UNPARSABLE:
        return None


def bare_except_tuples(text: str) -> str | None:
    """The text with each ``except A, B:`` read as ``except(A, B):``, or None when there is none."""
    try:  # lines end at \n, \r\n or \r only, as the parser counts them (not at \f or \x1c)
        tokens = list(tokenize.generate_tokens(io.StringIO(text, newline="").readline))
    except (tokenize.TokenError, *UNPARSABLE):
        return None
    lines = io.StringIO(text, newline="").readlines()
    changes: list[tuple[int, int, int]] = []  # (line, column of the space after except, column of the colon)
    for i, tok in enumerate(tokens):
        if tok.type != tokenize.NAME or tok.string != "except":
            continue
        j = i + 1
        if j < len(tokens) and tokens[j].string == "*":  # except* A, B:
            j += 1
        if j >= len(tokens) or tokens[j].start[0] != tok.start[0] or tokens[j].start[1] == tok.end[1]:
            continue  # nothing after it on the line, or no space to turn into "("
        depth, comma, colon = 0, False, None
        for k in range(j, len(tokens)):
            t = tokens[k]
            if t.type in (tokenize.NEWLINE, tokenize.NL) or t.start[0] != tok.start[0]:
                break
            if t.type == tokenize.NAME and t.string == "as" and depth == 0:
                break  # ``except A, B as e`` is an error in every version
            if t.type == tokenize.OP:
                if t.string in OPEN:
                    depth += 1
                elif t.string in CLOSE:
                    depth -= 1
                elif t.string == "," and depth == 0:
                    comma = True
                elif t.string == ":" and depth == 0:
                    colon = k
                    break
        if not comma or colon is None:
            continue
        after = tokens[colon + 1] if colon + 1 < len(tokens) else None
        if after is not None and after.type not in (tokenize.NEWLINE, tokenize.NL, tokenize.COMMENT, tokenize.ENDMARKER):
            continue  # code after the colon on the same line would move
        start = tokens[j - 1].end[1] if tokens[j - 1].string == "*" else tok.end[1]
        if start >= tokens[j].start[1]:
            continue
        changes.append((tok.start[0], start, tokens[colon].start[1]))
    if not changes:
        return None
    for line, space, colon in changes:  # token columns count characters, as str indexing does
        raw = lines[line - 1]
        lines[line - 1] = raw[:space] + "(" + raw[space + 1 : colon] + ")" + raw[colon:]
    return "".join(lines)
