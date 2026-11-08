"""The files a scan reads: one walk over a checkout, shared by everything that reads it.

:func:`iter_files` lists the files :func:`scanner.scan_repo` reads (dependency, vendored and build
directories left out, symlinks and files over the size cap skipped), so every pass over a checkout (the
cross-file tables, the raw-HTTP registry, the fixer's caller searches, pins and tests) sees the same files
on every machine. Within one scan or fix planning (:func:`phpvendor.within_repo`) the checkout is walked
once per extension set and the list is served again: nothing writes to a checkout while one runs.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import phpvendor


def iter_files(root: Path, suffixes: set[str] | None = None) -> tuple[list[Path], int, int]:
    """The files a scan reads (``suffixes``: of these extensions instead of every text file), with the
    counts of files and directories left out."""
    key = ("files", str(root), None if suffixes is None else frozenset(suffixes))
    files, skipped_files, skipped_dirs = phpvendor.memo(key, lambda: _walk(root, suffixes))
    return list(files), skipped_files, skipped_dirs


def rel_path(path: Path, base: Path) -> str:
    """``path.relative_to(base).as_posix()`` for a path the walk built under ``base``, without pathlib's cost."""
    s, b = str(path), str(base)
    if len(s) > len(b) and s.startswith(b) and s[len(b)] == os.sep:
        rel = s[len(b) + 1 :]
        return rel if os.sep == "/" else rel.replace(os.sep, "/")
    return path.relative_to(base).as_posix()


def _walk(root: Path, suffixes: set[str] | None) -> tuple[list[Path], int, int]:
    from . import scanner  # the rules live with the scanner

    suffixes = scanner.TEXT_EXT if suffixes is None else suffixes
    files: list[Path] = []
    skipped_files = skipped_dirs = 0
    for dirpath, dirs, names in os.walk(root):
        here, present = Path(dirpath), set(names)
        keep = []
        for d in sorted(dirs):
            if d.startswith("."):
                continue
            if d in scanner.SKIP_DIRS or (d in scanner.BUILD_DIRS and (here == root or present & scanner.PROJECT_FILES)):
                skipped_dirs += 1
                continue
            keep.append(d)
        dirs[:] = keep
        for name in sorted(names):
            path = here / name
            if name in scanner.SKIP_NAMES or name.endswith((".min.js", ".map")) or path.suffix.lower() not in suffixes:
                continue
            if path.is_symlink():
                continue
            if path.stat().st_size > scanner.MAX_BYTES:
                skipped_files += 1
                continue
            files.append(path)
    return files, skipped_files, skipped_dirs


def read(path: Path) -> str | None:
    """A file's text, or None when it cannot be read or is binary."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="replace")
