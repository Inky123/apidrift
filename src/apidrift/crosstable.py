"""Wrappers followed across files: a repository-level table the per-file readers consult.

A same-file wrapper (:mod:`phphelpers`) turns its same-file callers into call sites of their own. This
module does the same for callers in other files, in three passes driven by :func:`scanner.scan_repo`:

1. While every file is scanned as usual, the readers note each *candidate*: an SDK call whose parameters
   argument is a parameter of the function around it, used nowhere else there (the reader's own test).
2. :func:`finish` keeps a candidate only when its name resolves unambiguously in the repository (each
   language module says how; uniqueness of the name is always part of it), then finds every use of the
   name in the repository's source files and sorts it: a *visible caller* (the caller's receiver provably
   reaches the wrapper and the argument it forwards is an array/object literal, or a local bound only to
   one), or anything else.
3. The files with visible callers are scanned again with the table: each visible caller becomes a call
   site at the caller's line, ``[through NAME, FILE:LINE]``.

The wrapper's own call site is never dropped, even when every use found is a visible caller: a framework,
a parent class outside the checkout, an attribute or decorator, an event listener, or a file this walk
does not read may call it too. It stays as it is (possible, arguments not visible) beside its callers.

One hop only: a caller that forwards a parameter of its own is not visible. Test files are not callers
that send (their uses are ignored). Every walk reads exactly the files :func:`scanner._iter_files` lists
(dependency, vendored and build directories left out, symlinks and files over the size cap skipped), so a
scan finds the same callers on every machine whatever is installed. Work is bounded by the caps below:
past one, nothing more is followed and the scan is exactly the per-file one. The fixer's oracle re-scans
an edited file with the same table (:func:`scanner.scan_text`), so it sees the same call sites.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import httpwrap
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
    callers: dict[tuple[str, str], list[tuple[str, int]]] = field(default_factory=dict)  # visible callers
    unaccounted: dict[tuple[str, str], list[tuple[str, int]]] = field(default_factory=dict)  # other uses
    facts: dict[str, object] = field(default_factory=dict)  # language -> what its module learnt from the walk
    rescan: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)  # why a candidate was not followed
    base: Path | None = None  # the checkout (module paths of JS imports are resolved against it)
    index: object = field(default=None, repr=False)  # the scan's SDK index (Go reads params types through it)
    http: httpwrap.Registry = field(default_factory=lambda: httpwrap.Registry(), repr=False)  # raw-HTTP wrappers

    def add(self, w: Wrapper) -> None:
        if self.collecting:
            self.candidates.setdefault(w.key, []).append(w)

    def wrappers(self, language: str) -> list[Wrapper]:
        return [w for (lang, _), w in sorted(self.followed.items()) if lang == language]

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


def names_pattern(names: list[str], before: str) -> str:
    """``(?<![before])(?:n1|n2|...)`` for the escaped ``names``, written ``n1(?<![before]n1)|n2(?<![before]n2)``:
    the same test on the same character, but the engine now scans for the names' first letters instead of
    trying the look-behind at every position (20x faster on a large checkout)."""
    return "|".join(f"{n}(?<![{before}]{n})" for n in names)


def walk(base: Path, suffixes: set[str], skip=None, needles: tuple[bytes, ...] = ()):
    """Every file of the language the scan would read under ``base`` (:func:`scanner._iter_files`: the same
    files on every machine), as (path, text); ``skip(rel)`` leaves a file unread, and with ``needles`` a
    file holding none of them is read but not yielded. Binary files are left out. Yields None once a cap is
    passed: the caller must then follow nothing."""
    from . import scanner

    files = size = 0
    for path in scanner._iter_files(base, suffixes)[0]:
        if skip is not None and skip(scanner.rel_path(path, base)):
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
        if b"\x00" in raw[:8192]:
            continue
        if not needles or any(n in raw for n in needles):
            yield path, raw.decode("utf-8", errors="replace")


WP_HEADER = re.compile(rb"^[ \t/*#@]*Plugin Name\s*:", re.M)


def php_package(base: Path, rel: str) -> str | None:
    """Why the PHP file at ``rel`` belongs to a package others install or extend (None: an application).

    The nearest ``composer.json`` above it decides: anything but ``"type": "project"`` (the default type
    is ``library``; a ``wordpress-plugin`` such as GiveWP has add-ons that call and subclass it), and an
    unreadable one too. A directory on the way up holding a PHP file with a WordPress ``Plugin Name:``
    header is a plugin whatever Composer says."""
    here = (base / rel).parent
    composer_seen = False
    while True:
        if not composer_seen and (here / "composer.json").is_file():
            composer_seen = True
            try:
                composer = json.loads((here / "composer.json").read_text(errors="replace"))
            except (OSError, ValueError):
                composer = None
            if not isinstance(composer, dict) or composer.get("type", "library") != "project":
                return "a Composer library: callers outside the repository"
        try:
            heads = [p for p in sorted(here.iterdir()) if p.suffix.lower() == ".php" and p.is_file() and not p.is_symlink()]
        except OSError:
            heads = []
        for p in heads:
            try:
                with p.open("rb") as f:
                    if WP_HEADER.search(f.read(8192)):
                        return "a WordPress plugin: callers outside the repository"
            except OSError:
                continue
        if here == base or base not in here.parents:
            return None
        here = here.parent


def record(table: Table, w: Wrapper, file: str, line: int, visible: bool) -> None:
    into = table.callers if visible else table.unaccounted
    into.setdefault(w.key, []).append((file, line))
    if visible:
        table.rescan.add(file)


def over_cap(table: Table, language: str) -> bool:
    """More visible callers than :data:`MAX_CALLERS` already recorded for ``language``: :func:`settle` will
    drop everything of it, so the files still to read cannot change the result (their callers would only be
    dropped too, and the files they would add to ``rescan`` removed again)."""
    return sum(len(v) for k, v in table.callers.items() if k[0] == language) > MAX_CALLERS


def settle(table: Table, language: str) -> None:
    """Past the caller cap, nothing of the language is followed (the scan is then the per-file one)."""
    if sum(len(v) for k, v in table.callers.items() if k[0] == language) > MAX_CALLERS:
        for key, w in list(table.followed.items()):
            if key[0] == language:
                table.note(w, "cap: too many callers")
                del table.followed[key]
        table.rescan = {f for f in table.rescan if f not in {f for k, v in table.callers.items() for f, _ in v if k[0] == language}}


def finish(table: Table, base: Path, sources: list[Path], resolve) -> None:
    """Pass 2: decide what is followed. ``sources`` are the files the scan reads (vendored and build
    directories already left out); ``resolve`` is the scan's chain resolver."""
    from . import gocross, jscross, phpcross, pycross

    table.collecting, table.base = False, base
    phpcross.finish(table, base, sources, resolve)
    jscross.finish(table, base, sources, resolve)
    pycross.finish(table, base, sources, resolve)
    gocross.finish(table, base, sources, resolve)
    from .scanner import _read

    table.http.finish(lambda rel: _read(base / rel))  # raw-HTTP wrappers: their files and callers are read again
    table.rescan |= table.http.rescan
