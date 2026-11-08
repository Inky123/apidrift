"""What the parent checks before it pushes a test the sandboxed child updated, and how it rebuilds the file.

The child sends no content for a test, only edits against the base blob: ``(start, end, text)`` with the line,
the endpoint, the field, the action and the replacement. Nothing of that is trusted. A test file is pushed
only when:

- its path passes :func:`fixcheck.reject`, is a test location (the scanner's test-directory and test-file
  rules), is not a file the code fix pushes, and the child listed every edited line among the tests;
- every edit names a migration in the parent's own table with the same action and replacement, and one the
  accepted code fixes change (a test is never updated for a change this pull request does not make);
- GitHub reports the path at the base commit with the declared blob sha, and the blob the parent fetches by
  that sha hashes to it (the parent reads the base bytes itself, never the child's copy);
- every edit is the declared action on the declared key, written in one of the few shapes the fixer writes
  (:func:`_shape`): a rename swaps the key's name and nothing else; a deletion removes one ``key: value``
  entry, balanced, with at most one adjacent comma; a rename_first keeps the key's quoting and puts the first
  element of a literal array (or ``v[0]``) in its place; a wrap_type puts the old value, a plain string,
  number or name, inside ``{type: ...}``.

The parent then applies the edits to the base text itself. One bad edit rejects the whole file: it stays in
the pull request's list of tests for a person to check.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import PurePosixPath

from .. import migrations
from ..scanner import LANGUAGES, TEST_DIRS, TEST_FILE
from . import fixcheck, issue
from .github import GitHubClient, GitHubError, RateLimited

MAX_FILES, MAX_EDITS, MAX_TEXT = 25, 50, 2000
logger = logging.getLogger("apidrift.fixpr")
SEPARATORS = {  # (key form, separator) pairs each language writes a parameter key in
    "ruby": {("bare", ":"), ("symbol", "=>"), ("quoted", "=>"), ("quoted", ":")},
    "javascript": {("bare", ":"), ("quoted", ":")},
    "typescript": {("bare", ":"), ("quoted", ":")},
    "python": {("bare", "="), ("quoted", ":")},
    "php": {("quoted", "=>")},
}
CHAIN = re.compile(r"(?:\$|@{1,2})?[A-Za-z_]\w*(?:(?:\.|->|::|&\.)[A-Za-z_]\w*)*")
SAFE_STRING = re.compile(r"'[^'\\\n]*'|\"[^\"\\\n#${]*\"")
INTEGER = re.compile(r"\d+")
LINE_COMMENT = {"ruby": ("#",), "python": ("#",), "php": ("#", "//"), "javascript": ("//",), "typescript": ("//",)}


def validate(gh: GitHubClient, token: str, full_name: str, base_sha: str, payload: dict, fixes: list[dict]) -> list[dict]:
    """The test files the parent is willing to push, each ``{file, blob_sha, content, edits}``, its content
    built here from the base blob; ``fixes`` are the code fixes :func:`fixcheck.validate` accepted."""
    changed = {(s["path"], s["target"]) for f in fixes for s in f["sites"]}
    pushed = {f["file"] for f in fixes}
    listed = {(t["file"], t["line"]) for t in payload.get("tests") or [] if isinstance(t, dict)
              and isinstance(t.get("file"), str) and type(t.get("line")) is int}
    items = payload.get("test_fixes")
    accepted: list[dict] = []
    total = 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or len(accepted) >= MAX_FILES:
            continue
        file = item.get("file")
        why = fixcheck.reject(file, item.get("blob_sha"), "") or _reject_file(file, pushed, accepted)
        edits = None if why else _edits(item.get("edits"), changed)
        why = why or (None if edits else "malformed edits")
        text = None
        if why is None:
            text, why = _base(gh, token, full_name, base_sha, file, item["blob_sha"])
        if why is None and total + len(text.encode("utf-8")) > fixcheck.MAX_TOTAL_BYTES:
            why = "over the size caps"
        if why is None:
            why = _check(text, LANGUAGES[PurePosixPath(file).suffix.lower()], edits, file, listed)
        if why is not None:
            logger.info("repo %s: not pushing one test file (%s)", full_name, why)
            continue
        content = text
        for e in sorted(edits, key=lambda e: e["start"], reverse=True):
            content = content[: e["start"]] + e["text"] + content[e["end"] :]
        total += len(text.encode("utf-8"))
        accepted.append({
            "file": file, "blob_sha": item["blob_sha"], "content": content,
            "edits": [{k: e[k] for k in ("line", "path", "target", "replacement", "action")} for e in edits],
        })
    return accepted


def is_test(file: str) -> bool:
    *dirs, name = file.split("/")
    return any(d in TEST_DIRS for d in dirs) or bool(TEST_FILE.match(name))


def _reject_file(file: str, pushed: set[str], accepted: list[dict]) -> str | None:
    if not is_test(file):
        return "not a test file"
    if LANGUAGES[PurePosixPath(file).suffix.lower()] not in SEPARATORS:
        return "not a language whose tests are updated"
    if file in pushed or any(a["file"] == file for a in accepted):
        return "the file is already in this pull request"
    return None


def _edits(items: object, changed: set[tuple[str, str]]) -> list[dict] | None:
    """The edits rebuilt with exactly the keys the parent uses; None when one is malformed or not ours."""
    if not isinstance(items, list) or not 0 < len(items) <= MAX_EDITS:
        return None
    out = []
    for e in items:
        if not isinstance(e, dict) or any(type(e.get(k)) is not int for k in ("line", "start", "end")):
            return None
        text, path, target = e.get("text"), e.get("path"), e.get("target")
        if not isinstance(text, str) or len(text) > MAX_TEXT or not 0 <= e["start"] <= e["end"]:
            return None
        if not (isinstance(path, str) and issue.PATH_RE.fullmatch(path) and isinstance(target, str)):
            return None
        m = migrations.for_site(path, target)
        if m is None or (e.get("action"), e.get("replacement")) != (m.action, m.replacement) or (path, target) not in changed:
            return None
        out.append({"line": e["line"], "start": e["start"], "end": e["end"], "text": text, "path": m.path,
                    "target": m.target, "replacement": m.replacement, "action": m.action})
    out.sort(key=lambda e: (e["start"], e["end"]))
    if any(a["end"] > b["start"] or a["start"] == b["start"] for a, b in zip(out, out[1:])):
        return None  # overlapping, or two edits at one place
    return out


def _base(gh: GitHubClient, token: str, full_name: str, base_sha: str, file: str, sha: str) -> tuple[str | None, str | None]:
    meta = fixcheck._content_meta(gh, token, full_name, file, base_sha)
    if meta.get("type") != "file" or meta.get("sha") != sha:
        return None, "not the file that was scanned"
    try:
        raw = gh.get_blob(token, full_name, sha)
    except RateLimited:
        raise
    except GitHubError:
        return None, "the blob could not be read"  # the code fix still goes out; this test stays listed
    if hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest() != sha or len(raw) > fixcheck.MAX_FILE_BYTES:
        return None, "the blob is not the one named"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "the test file is not UTF-8"


def _check(text: str, language: str, edits: list[dict], file: str, listed: set[tuple[str, int]]) -> str | None:
    for e in edits:
        if e["end"] > len(text):
            return "an edit lies outside the file"
        m = migrations.for_site(e["path"], e["target"])
        assert m is not None  # _edits admitted it
        at = _shape(text, language, e, m)
        if at is None:
            return f"an edit is not a plain {m.action} of the key"
        if text.count("\n", 0, at) + 1 != e["line"] or (file, e["line"]) not in listed:
            return "an edit's line is not a listed test line"
    return None


# --- the shapes the fixer writes ----------------------------------------------------------


def _shape(text: str, language: str, e: dict, m: migrations.Migration) -> int | None:
    """Where the edited key is when ``e`` is ``m``'s action written the way the fixer writes it, else None."""
    old, new = m.target.rsplit(".", 1)[-1], m.replacement.rsplit(".", 1)[-1] if m.replacement else ""
    start, end, span, out = e["start"], e["end"], text[e["start"] : e["end"]], e["text"]
    if m.action == "rename":
        if span == old and out == new and _key(text, language, start, old):
            return start
        js = language in ("javascript", "typescript")
        if js and span == old and out == f"{new}: {old}" and _shorthand(text, start, end):
            return start
        return None
    if m.action == "delete":
        return _deletion(text, language, start, end, out, old)
    if m.action == "rename_first":
        key = _entry_key(text, language, start, old)
        if key is None or key[0] != start:
            return None
        name_at, value_at = key[1], key[2]
        head = text[start:name_at] + new + text[name_at + len(old) : value_at]
        value = text[value_at:end]
        return start if out.startswith(head) and _first(text, value_at, end, value, out[len(head) :], language) else None
    if m.action == "wrap_type":
        return start if _wrapped(text, language, start, end, span, out, old, m) else None
    return None


def _key(text: str, language: str, at: int, name: str) -> bool:
    """``text[at:]`` starts with ``name`` as a parameter key: bare, quoted or a Ruby symbol, then its separator."""
    if text[at : at + len(name)] != name:
        return False
    before = text[at - 1] if at else ""
    if before in "'\"":
        form, after = "quoted", at + len(name) + 1
        if text[after - 1 : after] != before or (at >= 2 and (_ident(text[at - 2]) or text[at - 2] == ":")):
            return False  # unclosed, a prefixed string (r'', f''), or a Ruby :"symbol"
    elif before == ":" and language == "ruby":
        form, after = "symbol", at + len(name)
        if at >= 2 and (_ident(text[at - 2]) or text[at - 2] == ":"):
            return False
    else:
        form, after = "bare", at + len(name)
        if before and (_ident(before) or before in ".$@#:"):
            return False
        if after < len(text) and (_ident(text[after]) or text[after] in "?!"):
            return False
    if not _opens_entry(text, at - (0 if form == "bare" else 1), language):
        return False
    j = after
    while j < len(text) and text[j] in " \t":
        j += 1
    seps = [s for f, s in SEPARATORS.get(language, set()) if f == form]
    return any(
        text.startswith(s, j) and not text.startswith({":": "::", "=": "==", "=>": "=>>"}[s], j)
        and not (s == "=" and text.startswith("=>", j))
        for s in seps
    )


def _opens_entry(text: str, token: int, language: str) -> bool:
    """The code before a key token ends an opener or a comma (Ruby: or a paren-less call's name)."""
    j = token
    while True:
        while j > 0 and text[j - 1].isspace():
            j -= 1
        line_start = text.rfind("\n", 0, j) + 1
        comment = _trailing_comment(text[line_start:j], language)
        if comment is None:
            break
        j = line_start + comment  # a comment ending the line before: the code before it counts
    c = text[j - 1] if j else ""
    return c in "{[(," and c != "" or (language == "ruby" and c != "" and (_ident(c) or c == "|") and j < token)


def _trailing_comment(line: str, language: str) -> int | None:
    """Where a line comment without quotes in it starts, when one ends ``line``."""
    marks = "|".join(re.escape(c) for c in LINE_COMMENT.get(language, ()))
    m = re.search(rf"(?:{marks})[^'\"`\n]*$", line) if marks else None
    return m.start() if m and m.start() < len(line) else None


def _entry_key(text: str, language: str, at: int, name: str) -> tuple[int, int, int] | None:
    """For an entry starting at ``at``: (token start, name start, value start) when its key is ``name``."""
    if text[at : at + 1] in "'\"" or (text[at : at + 1] == ":" and language == "ruby"):
        name_at = at + 1
    else:
        name_at = at
    if not _key(text, language, name_at, name):
        return None
    j = name_at + len(name) + (1 if text[at] in "'\"" else 0)
    while j < len(text) and text[j] in " \t":
        j += 1
    j += 2 if text.startswith("=>", j) else 1
    while j < len(text) and text[j] in " \t\r\n":
        j += 1
    return at, name_at, j


def _shorthand(text: str, start: int, end: int) -> bool:
    before = text[:start].rstrip()
    after = text[end:].lstrip()
    return before[-1:] in ("{", ",") and after[:1] in (",", "}") and not (start and _ident(text[start - 1]))


def _deletion(text: str, language: str, start: int, end: int, out: str, name: str) -> int | None:
    """One ``key: value`` entry and at most one comma next to it; ``out`` may only keep the rest of the line
    before it (the fixer's own-line deletion of a last entry)."""
    lo, comma = start, False
    if out:
        comments = "|".join(re.escape(c) for c in LINE_COMMENT[language])
        rest_of_line = re.fullmatch(rf"[ \t]*(?:(?:{comments})[^\n]*)?\r?\n", out)
        if not (text[start : start + 1] == "," and text[start + 1 : start + 1 + len(out)] == out and rest_of_line):
            return None  # only the rest of the comma's own line (spaces, a comment, the line break) may be kept
        lo, comma = start + 1 + len(out), True
        if text[lo:end].lstrip(" \t")[:1] in ("", "\n", "\r"):
            return None  # the entry must start the next line
    i = _skip_space(text, lo, end)
    if text[i : i + 1] == ",":
        if comma:
            return None
        i, comma = _skip_space(text, i + 1, end), True
    key = _entry_key(text, language, i, name)
    if key is None or key[2] >= end:
        return None
    stop = _value_end(text, key[2], end, language)
    if stop is None or not text[key[2] : stop].strip():
        return None
    rest = text[stop:end]
    if rest.startswith(","):
        if comma:
            return None
        comma, rest = True, rest[1:]
    elif not re.match(r"\s*[)\]},]", text[end:]):
        return None  # the value did not end where the entry ends
    comments = "|".join(re.escape(c) for c in LINE_COMMENT[language])
    if not re.fullmatch(rf"[ \t]*(?:(?:{comments})[^\n]*)?\s*", rest):
        return None
    return key[1]


def _first(text: str, lo: int, hi: int, value: str, new: str, language: str) -> bool:
    """``new`` is the first element of the array literal ``value``, or ``value[0]`` (in the fixer's forms)."""
    if value.startswith("[") or (language == "php" and value[:6].lower() == "array("):
        opener = lo if value.startswith("[") else lo + len("array")
        closer = {"[": "]", "(": ")"}[text[opener]]
        if text[hi - 1 : hi] != closer or _value_end(text, opener + 1, hi - 1, language, commas=False) is None:
            return False  # the literal is not the whole value
        i = _skip_space(text, opener + 1, hi - 1)
        stop = _value_end(text, i, hi - 1, language)
        return stop is not None and stop > i and new == text[i:stop].rstrip()
    if not CHAIN.fullmatch(value):
        return False
    return new in (f"{value}[0]", f"{value}[0] ?? null", f"{value}[0] if {value} else None")


def _wrapped(text: str, language: str, start: int, end: int, value: str, new: str, name: str, m) -> bool:
    if language in ("javascript", "typescript") and value == name and new == f"{name}: {{ type: {name} }}":
        return m.wrap_any and _shorthand(text, start, end)
    key = re.search(rf"(?:['\"]|:)?{re.escape(name)}['\"]?[ \t]*(?:=>|:|=)\s*$", text[max(0, start - 200) : start])
    if key is None:
        return False
    key_at = start - (len(text[max(0, start - 200) : start]) - key.start())
    entry = _entry_key(text, language, key_at, name)
    if entry is None or entry[2] != start:
        return False
    is_int = bool(INTEGER.fullmatch(value))
    content = value[1:-1] if SAFE_STRING.fullmatch(value) else (value[1:] if language == "ruby" and re.fullmatch(r":\w+", value) else None)
    if is_int:
        ok = m.timestamps
    elif content is not None:
        ok = content in m.wrap_values
    else:
        ok = m.wrap_any and bool(CHAIN.fullmatch(value)) and value not in ("nil", "null", "None", "true", "false")
    forms = _wrap_forms(language, value)
    return ok and (new in forms["timestamp"] if is_int else new in forms["plain"])


def _wrap_forms(language: str, v: str) -> dict[str, set[str]]:
    qs = ("'", '"')
    if language == "python":
        return {"plain": {f"{{{q}type{q}: {v}}}" for q in qs},
                "timestamp": {f"{{{q}type{q}: {q}timestamp{q}, {q}timestamp{q}: {v}}}" for q in qs}}
    if language == "php":
        return {"plain": {f"[{q}type{q} => {v}]" for q in qs},
                "timestamp": {f"[{q}type{q} => {q}timestamp{q}, {q}timestamp{q} => {v}]" for q in qs}}
    return {"plain": {f"{{ type: {v} }}"}, "timestamp": {f"{{ type: {q}timestamp{q}, timestamp: {v} }}" for q in qs}}


# --- a small balanced reader for the removed and kept text ----------------------------------


def _skip_space(text: str, i: int, hi: int) -> int:
    while i < hi and text[i].isspace():
        i += 1
    return i


def _value_end(text: str, i: int, hi: int, language: str, commas: bool = True) -> int | None:
    """Index of the first top-level ``,`` in ``[i, hi)`` (``hi`` when none, or when not ``commas``); None when
    a string, comment or bracket there is unbalanced."""
    quotes = "'\"`" if language in ("javascript", "typescript") else "'\""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    while i < hi:
        c = text[i]
        if c in quotes:
            j = i + 1
            while j < hi and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            if j >= hi:
                return None
            i = j + 1
            continue
        if any(text.startswith(x, i) for x in LINE_COMMENT[language]):
            nl = text.find("\n", i, hi)
            if nl < 0:
                return None
            i = nl
            continue
        if text.startswith("/*", i) and language != "ruby" and language != "python":
            close = text.find("*/", i + 2, hi)
            if close < 0:
                return None
            i = close + 2
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif c in ")]}":
            if not stack or stack.pop() != c:
                return None
        elif c == "," and not stack and commas:
            return i
        i += 1
    return None if stack else hi


def _ident(c: str) -> bool:
    return c.isalnum() or c in "_$"
