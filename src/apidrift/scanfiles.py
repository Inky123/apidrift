"""Which files a scan reads, and how one is read: the same files on every machine, whatever is installed.

Every walk over a checkout goes through :func:`iter_files` (the scan, the cross-file table, the fixer's
test and cassette searches), so dependency and build directories, symlinks, oversize and binary files are
left out everywhere alike.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

SKIP_DIRS = {"node_modules", "vendor", "site-packages", "__pycache__"}  # skipped anywhere
BUILD_DIRS = {"dist", "build", "coverage", "target", "tmp"}  # skipped at the repo root or next to a project file
PROJECT_FILES = {
    "package.json", "pyproject.toml", "setup.py", "Gemfile", "Cargo.toml", "go.mod", "pom.xml", "composer.json",
    "tsconfig.json",
}
SKIP_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "Gemfile.lock"}
MAX_BYTES = 1_500_000


def iter_files(root: Path, suffixes: set[str]) -> tuple[list[Path], int, int]:
    """The files of these extensions a scan reads under ``root``, with the counts of files and directories
    left out."""
    files: list[Path] = []
    skipped_files = skipped_dirs = 0
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
            if name in SKIP_NAMES or name.endswith((".min.js", ".map")) or path.suffix.lower() not in suffixes:
                continue
            try:
                st = path.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:  # a FIFO or socket would block the read
                skipped_files += 1
                continue
            files.append(path)
    return files, skipped_files, skipped_dirs


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
