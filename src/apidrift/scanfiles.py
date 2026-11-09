"""Which files a scan reads, and how one is read: the same files on every machine, whatever is installed.

Every pass over a checkout goes through :func:`iter_files` (the scan, the cross-file tables, the raw-HTTP
registry, the fixer's caller, test and cassette searches, pins), so dependency and build directories,
symlinks, oversize and binary files, and anything that is not a regular file are left out everywhere alike.
Within one scan or fix planning (:func:`phpvendor.within_repo`) the checkout is walked once and the list is
served again: nothing writes to a checkout while one runs.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from . import phpvendor

SKIP_DIRS = {"node_modules", "vendor", "site-packages", "__pycache__"}  # skipped anywhere
BUILD_DIRS = {"dist", "build", "coverage", "target", "tmp"}  # skipped at the repo root or next to a project file
PROJECT_FILES = {
    "package.json", "pyproject.toml", "setup.py", "Gemfile", "Cargo.toml", "go.mod", "pom.xml", "composer.json",
    "tsconfig.json",
}
SKIP_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "Gemfile.lock"}
MAX_BYTES = 1_500_000


def iter_files(root: Path, suffixes: set[str] | None = None) -> tuple[list[Path], int, int]:
    """The files a scan reads (``suffixes``: of these extensions instead of every text file), with the
    counts of files and directories left out."""
    from . import scanner  # the text extensions live with the scanner's language tables

    full = phpvendor.memo(("files", str(root)), lambda: _walk(root, scanner.TEXT_EXT))
    if suffixes is None:
        files, skipped_files, skipped_dirs, _ = full
        return list(files), skipped_files, skipped_dirs
    if set(suffixes) <= scanner.TEXT_EXT:  # the full walk's list, filtered: the skip rules do not depend on the set
        files, _, skipped_dirs, left_out = full
        return ([p for p in files if p.suffix.lower() in suffixes], sum(1 for s in left_out if s in suffixes), skipped_dirs)
    files, skipped_files, skipped_dirs, _ = phpvendor.memo(("files", str(root), frozenset(suffixes)), lambda: _walk(root, suffixes))
    return list(files), skipped_files, skipped_dirs


def rel_path(path: Path, base: Path) -> str:
    """``path.relative_to(base).as_posix()`` for a path the walk built under ``base``, without pathlib's cost."""
    s, b = str(path), str(base)
    if len(s) > len(b) and s.startswith(b) and s[len(b)] == os.sep:
        rel = s[len(b) + 1 :]
        return rel if os.sep == "/" else rel.replace(os.sep, "/")
    return path.relative_to(base).as_posix()


def _walk(root: Path, suffixes: set[str]) -> tuple[list[Path], int, int, list[str]]:
    """(files, files left out, directories skipped, the suffix of each file left out). A file is left out when
    it is over the size cap or not a regular file (a FIFO or socket would block the read)."""
    files: list[Path] = []
    left_out: list[str] = []
    skipped_dirs = 0
    for dirpath, dirs, names in os.walk(root):
        here, present = Path(dirpath), set(names)
        keep = []
        for d in sorted(dirs):
            if d.startswith("."):
                continue
            if d in SKIP_DIRS or (d in BUILD_DIRS and (here == root or present & PROJECT_FILES)):
                skipped_dirs += 1
                continue
            keep.append(d)
        dirs[:] = keep
        for name in sorted(names):
            path = here / name
            suffix = path.suffix.lower()
            if name in SKIP_NAMES or name.endswith((".min.js", ".map")) or suffix not in suffixes:
                continue
            try:
                st = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
                left_out.append(suffix)
                continue
            files.append(path)
    return files, len(left_out), skipped_dirs, left_out


def repo_root(file: Path) -> Path:
    """What a single file's path is reported relative to: the nearest directory above it holding a ``.git``
    entry, else its own directory. So ``tests/stub.ts`` keeps its directory and the documentation and test
    rules see it, as they would in a scan of the whole checkout."""
    for parent in file.parents:
        if (parent / ".git").exists():
            return parent
    return file.parent


BOM = b"\xef\xbb\xbf"


def read(path: Path) -> str | None:
    """The file's text, or None when it cannot be read or is binary (a NUL in its first 8 KB). A UTF-8 byte
    order mark is dropped: Python's parser rejects it and the other readers would see it as a character
    (lines are unchanged; the fixer reads the file itself and places its edits in what it read)."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    return raw[3:].decode("utf-8", errors="replace") if raw.startswith(BOM) else raw.decode("utf-8", errors="replace")
