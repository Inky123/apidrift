"""Wrappers followed across files: a repository-level table the per-file readers consult.

A same-file wrapper (:mod:`phphelpers`) turns its same-file callers into call sites of their own. This
module does the same for callers in other files, in three passes driven by :func:`scanner.scan_repo`:

1. While every file is scanned as usual, the readers note each *candidate*: an SDK call whose parameters
   argument is a parameter of the function around it, used nowhere else there (the reader's own test).
2. :func:`finish` keeps a candidate only when its name resolves unambiguously in the repository (each
   language module says how; uniqueness of the name is always part of it), then finds every use of the
   name in the repository's source files and sorts it: a *visible caller* (the caller's receiver provably
   reaches the wrapper and the argument it forwards is an array/object literal, or a local bound only to
   one), or anything else. A wrapper whose every use is a visible caller is *covered*: its own call site,
   whose hidden argument is exactly its callers' arguments, is dropped.
3. The files with visible callers, and the files of covered wrappers, are scanned again with the table:
   each visible caller becomes a call site at the caller's line, ``[through NAME, FILE:LINE]``.

One hop only: a caller that forwards a parameter of its own is not visible. Test files are not callers
that send (their uses are ignored); vendored directories are never callers, but a definition there of
the same name makes the name ambiguous. Work is bounded by the caps below: past one, nothing more is
followed and the scan is exactly the per-file one. The fixer's oracle re-scans an edited file with the
same table (:func:`scanner.scan_text`), so it sees the same call sites.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .sdkmap import Resolved

MAX_CANDIDATES = 200  # wrapper names per language
MAX_WALK_FILES = 60_000  # files read per language to prove a name unique
MAX_WALK_BYTES = 400_000_000
MAX_PARSE_FILES = 400  # caller files parsed per language
MAX_CALLERS = 2_000  # visible callers kept per language
MIN_NAME = 6  # shorter names (``create``, ``update``) are too common to be one function's
GENERIC = {
    "create", "update", "delete", "remove", "retrieve", "request", "execute", "process", "handle", "dispatch",
    "invoke", "perform", "submit", "charge", "payment", "refund", "cancel", "confirm", "capture", "subscribe",
    "callapi", "apicall", "makerequest", "sendrequest", "post", "patch", "send", "call", "make", "build", "params",
}
THROUGH = " [through {name}, {file}:{line}]"


@dataclass(frozen=True)
class Wrapper:
    """A function or method that hands one of its parameters, unchanged, to one SDK call as its parameters."""

    language: str  # the family: "php", "js", "python"
    name: str
    file: str  # where it is defined (repo-relative)
    line: int  # the SDK call's line
    call: str  # the SDK call as the reader displays it
    resolved: Resolved
    index: int  # which of its parameters is forwarded
    method: bool = False
    owner: str = ""  # the class that defines it ("" for a function), as the language module names it
    scope: str = ""  # PHP: the namespace; JS/Python: the module the definition is exported from

    @property
    def key(self) -> tuple[str, str]:
        return (self.language, self.name.lower() if self.language == "php" else self.name)

    def through(self) -> str:
        return THROUGH.format(name=self.name, file=self.file, line=self.line)


@dataclass
class Table:
    """What every per-file scan needs to know about wrappers defined in other files."""

    collecting: bool = True
    candidates: dict[tuple[str, str], list[Wrapper]] = field(default_factory=dict)
    followed: dict[tuple[str, str], Wrapper] = field(default_factory=dict)
    covered: set[tuple[str, str]] = field(default_factory=set)
    callers: dict[tuple[str, str], list[tuple[str, int]]] = field(default_factory=dict)  # visible callers
    unaccounted: dict[tuple[str, str], list[tuple[str, int]]] = field(default_factory=dict)  # other uses
    facts: dict[str, object] = field(default_factory=dict)  # language -> what its module learnt from the walk
    rescan: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)  # why a candidate was not followed
    base: Path | None = None  # the checkout (module paths of JS imports are resolved against it)

    def add(self, w: Wrapper) -> None:
        if self.collecting:
            self.candidates.setdefault(w.key, []).append(w)

    def wrappers(self, language: str) -> list[Wrapper]:
        return [w for (lang, _), w in sorted(self.followed.items()) if lang == language]

    def drops(self, language: str, file: str, line: int, call: str, path: str) -> bool:
        """The SDK call at ``file:line`` is a covered wrapper's own: its callers are the call sites."""
        return any(
            w.file == file and w.line == line and w.call == call and w.resolved.path == path
            for w in self.wrappers(language) if w.key in self.covered
        )

    def note(self, w: Wrapper, why: str) -> None:
        self.notes.append(f"{w.file}:{w.line} {w.name}: {why}")


def single(table: Table, language: str) -> dict[tuple[str, str], Wrapper]:
    """The candidates of ``language`` whose name only one SDK-forwarding function has, past the name rules."""
    out: dict[tuple[str, str], Wrapper] = {}
    for key, ws in sorted(table.candidates.items()):
        if key[0] != language:
            continue
        w = ws[0]
        if len(ws) != 1:
            table.note(w, "several wrappers of this name")
        elif len(w.name) < MIN_NAME or w.name.lower() in GENERIC:
            table.note(w, "name too common to follow")
        elif len(out) >= MAX_CANDIDATES:
            table.note(w, "cap")
        else:
            out[key] = w
    return out


def walk(base: Path, suffixes: set[str], skip=None, needles: tuple[bytes, ...] = ()):
    """Every file of the language under ``base``, vendored and test directories included (a definition
    anywhere makes a name ambiguous), as (path, text); ``skip(rel)`` leaves a file unread, and with
    ``needles`` a file holding none of them is read but not yielded. Yields None once a cap is passed: the
    caller must then follow nothing."""
    files = size = 0
    for dirpath, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in (".git", ".hg", ".svn"))
        for name in sorted(names):
            path = Path(dirpath) / name
            if path.suffix.lower() not in suffixes or path.is_symlink():
                continue
            if skip is not None and skip(path.relative_to(base).as_posix()):
                continue
            files += 1
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            size += len(raw)
            if files > MAX_WALK_FILES or size > MAX_WALK_BYTES:
                yield None
                return
            if not needles or any(n in raw for n in needles):
                yield path, raw.decode("utf-8", errors="replace")


def composer_library(base: Path) -> bool:
    """A Composer package others install (the default type is ``library``): its methods have callers outside."""
    try:
        composer = json.loads((base / "composer.json").read_text(errors="replace"))
    except (OSError, ValueError):
        return False
    return isinstance(composer, dict) and composer.get("type", "library") == "library"


def record(table: Table, w: Wrapper, file: str, line: int, visible: bool) -> None:
    into = table.callers if visible else table.unaccounted
    into.setdefault(w.key, []).append((file, line))
    if visible:
        table.rescan.add(file)


def settle(table: Table, language: str) -> None:
    """Covered wrappers: at least one visible caller and no other use. Past the caller cap, nothing of the
    language is followed (the scan is then the per-file one)."""
    if sum(len(v) for k, v in table.callers.items() if k[0] == language) > MAX_CALLERS:
        for key, w in list(table.followed.items()):
            if key[0] == language:
                table.note(w, "cap: too many callers")
                del table.followed[key]
        table.rescan = {f for f in table.rescan if f not in {f for k, v in table.callers.items() for f, _ in v if k[0] == language}}
        return
    for key, w in table.followed.items():
        if key[0] == language and table.callers.get(key) and not table.unaccounted.get(key):
            table.covered.add(key)
            table.rescan.add(w.file)


def finish(table: Table, base: Path, sources: list[Path], resolve) -> None:
    """Pass 2: decide what is followed. ``sources`` are the files the scan reads (vendored and build
    directories already left out); ``resolve`` is the scan's chain resolver."""
    from . import jscross, phpcross

    table.collecting, table.base = False, base
    phpcross.finish(table, base, sources, resolve)
    jscross.finish(table, base, sources, resolve)
