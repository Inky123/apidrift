"""Unpack a repository tarball safely: path checks, the ``data`` filter, byte and entry caps.

Nothing in the tree is imported, executed or templated; it is only read by the scanner, which runs
in a separate process.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

MAX_TREE_BYTES = 1 << 30
MAX_ENTRIES = 200_000


class ExtractError(Exception):
    """The archive was rejected; the message never includes entry names."""


def _check_member(member: tarfile.TarInfo) -> None:
    name = member.name
    if not name or name.startswith("/") or ".." in name.split("/"):
        raise ExtractError("archive entry has an absolute or parent-relative path")
    if not (member.isreg() or member.isdir() or member.issym() or member.islnk()):
        raise ExtractError("archive contains a special file")


def _filter(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    """The ``data`` filter, except that a link whose target leaves ``dest`` is dropped instead of
    failing the whole archive (repos do ship such symlinks); escaping files still raise."""
    try:
        return tarfile.data_filter(member, dest)
    except (tarfile.LinkOutsideDestinationError, tarfile.AbsoluteLinkError):
        if member.issym() or member.islnk():
            return None
        raise


def extract_tarball(
    src: Path, dest: Path, max_bytes: int = MAX_TREE_BYTES, max_entries: int = MAX_ENTRIES
) -> Path:
    """Extract ``src`` into ``dest`` and return the single top-level directory inside it."""
    dest.mkdir(parents=True, exist_ok=True)
    members: list[tarfile.TarInfo] = []
    total = 0
    try:
        with tarfile.open(src, "r:gz") as tar:
            for member in tar:
                _check_member(member)
                members.append(member)
                if len(members) > max_entries:
                    raise ExtractError(f"archive has more than {max_entries:,} entries")
                total += member.size
                if total > max_bytes:
                    raise ExtractError(f"archive expands past {max_bytes:,} bytes")
            tar.extractall(dest, members=members, filter=_filter)
    except tarfile.FilterError:
        raise ExtractError("archive entry escapes the destination or is a special file") from None
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise ExtractError(f"archive could not be read: {type(exc).__name__}") from None
    entries = [p for p in dest.iterdir()]
    if len(entries) != 1 or not entries[0].is_dir() or entries[0].is_symlink():
        raise ExtractError("archive does not contain a single top-level directory")
    return entries[0]
