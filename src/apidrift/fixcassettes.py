"""Recorded HTTP cassettes whose requests still send a field the fix pull request changes.

A spec replayed from a VCR (Ruby) or vcrpy (Python) cassette keeps passing after the fix as long as the
cassette is matched on method and URI, VCR's default. A cassette matched on the request body stops replaying
until it is re-recorded. Cassettes are never edited (the response in them is Stripe's, recorded); the ones
whose recorded request goes to an endpoint the fix changes and sends the old field are listed for a person.

A cassette is a YAML file with ``http_interactions:`` (VCR) or ``interactions:`` (vcrpy) whose entries are
``- request:`` blocks; a block counts when its ``method`` and ``uri`` path are the changed endpoint and its
body carries the field as a form key (``payment_method_types[0]=card``, ``filters[countries][0]=US``, also
percent-encoded). Nothing else in the file is read.
"""

from __future__ import annotations

import re
from pathlib import Path

from .scanner import _iter_files, _read

MAX_CASSETTES = 50
MARKER = re.compile(r"^(?:http_)?interactions:\s*$", re.M)
REQUEST = re.compile(r"^([ \t]*)-[ \t]+request:[ \t]*$", re.M)
METHOD = re.compile(r"^[ \t]+method:[ \t]*['\"]?([A-Za-z]+)", re.M)
URI = re.compile(r"^[ \t]+uri:[ \t]*['\"]?https?://[^/\s'\"]+(/[^?\s'\"]*)", re.M)


def recorded(root: Path, changes: list[tuple[str, str, str]]) -> list[tuple[str, int]]:
    """``(file, line)`` of the first recorded request body in each cassette that sends ``target`` to
    ``(method, path)`` for one of the changes; at most MAX_CASSETTES files."""
    if not changes:
        return []
    root = Path(root).resolve()
    files, _, _ = _iter_files(root) if root.is_dir() else ([], 0, 0)
    rules = [(method.lower(), _path_re(path), _key_re(target)) for method, path, target in changes]
    out: list[tuple[str, int]] = []
    for path in files:
        if path.suffix.lower() not in (".yml", ".yaml"):
            continue
        text = _read(path)
        if text is None or not MARKER.search(text) or "request:" not in text:
            continue
        rel = path.relative_to(root).as_posix()
        for lo, hi in _requests(text):
            block = text[lo:hi]
            method, uri = METHOD.search(block), URI.search(block)
            if not method or not uri:
                continue
            for want, path_re, key_re in rules:
                hit = key_re.search(block) if method.group(1).lower() == want and path_re.fullmatch(uri.group(1)) else None
                if hit:
                    out.append((rel, text.count("\n", 0, lo + hit.start()) + 1))
                    break
            if out and out[-1][0] == rel:
                break  # one line per cassette: it is re-recorded as a whole
        if len(out) >= MAX_CASSETTES:
            break
    return out


def _requests(text: str) -> list[tuple[int, int]]:
    """Spans of the ``- request:`` blocks: the lines after one that are indented deeper than ``request``."""
    out = []
    for m in REQUEST.finditer(text):
        child = len(m.group(1).expandtabs()) + 2
        lo = i = m.end() + 1
        while i < len(text):
            nl = text.find("\n", i)
            line = text[i : nl if nl >= 0 else len(text)]
            if line.strip() and len(line) - len(line.lstrip()) <= child:
                break
            i = nl + 1 if nl >= 0 else len(text)
        out.append((lo, i))
    return out


def _path_re(path: str) -> re.Pattern[str]:
    return re.compile("".join("[^/]+" if part.startswith("{") else re.escape(part) for part in re.split(r"(\{[^}]+\})", path)))


def _key_re(target: str) -> re.Pattern[str]:
    """The form-encoded key of ``target`` (``a.b`` is ``a[b]``) starting a parameter, then ``=`` or ``[``."""
    first, *rest = target.split(".")
    key = re.escape(first) + "".join(rf"(?:\[|%5B){re.escape(seg)}(?:\]|%5D)" for seg in rest)
    return re.compile(rf"(?:^|[&\s\"':?])({key})(?:=|\[|%5B)", re.I | re.M)
