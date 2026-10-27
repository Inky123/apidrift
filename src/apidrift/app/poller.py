"""Notice Stripe releases: a conditional GET of the spec, a new snapshot, one release job per repo."""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

import httpx

from .. import snapshot as snap
from ..providers import get_provider
from . import db
from .config import Settings

ETAG_NAME = ".etag"
STAGGER_SECONDS = 2
logger = logging.getLogger("apidrift.poller")
_versions: dict[str, str] = {}


def snapshots(data_dir: Path) -> list[Path]:
    """Stored Stripe snapshots under ``data_dir``, oldest first."""
    folder = data_dir / "snapshots" / "stripe"
    return sorted(folder.glob(f"*{snap.SUFFIX}")) if folder.is_dir() else []


def version_of(path: Path) -> str:
    """``info.version`` of a snapshot, cached by filename (the file never changes once written)."""
    if path.name not in _versions:
        _versions[path.name] = str(snap.load(path).get("info", {}).get("version", "unknown"))
    return _versions[path.name]


def latest_version(data_dir: Path) -> str | None:
    snaps = snapshots(data_dir)
    return version_of(snaps[-1]) if snaps else None


def initial_baseline(data_dir: Path) -> str | None:
    """The snapshot a newly installed repo is compared from: the previous one, or the only one."""
    snaps = snapshots(data_dir)
    if not snaps:
        return None
    return snaps[-2].name if len(snaps) >= 2 else snaps[-1].name


def poll_stripe(settings: Settings, conn: sqlite3.Connection, http: httpx.Client) -> Path | None:
    """Fetch the spec if it changed; on a new snapshot queue a release check per active repo."""
    provider = get_provider("stripe")
    etag_file = settings.snapshot_dir / ETAG_NAME
    headers = {"User-Agent": snap.USER_AGENT}
    if etag_file.is_file():
        headers["If-None-Match"] = etag_file.read_text(encoding="utf-8").strip()
    response = http.get(provider.spec_url, headers=headers, follow_redirects=True, timeout=120.0)
    if response.status_code == 304:
        return None
    response.raise_for_status()
    path = snap.save(provider, response.content)
    etag = response.headers.get("etag")
    if etag:
        etag_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = etag_file.with_name(ETAG_NAME + ".tmp")
        tmp.write_text(etag, encoding="utf-8")
        os.replace(tmp, etag_file)
    if path is None:
        return None
    logger.info("new Stripe snapshot %s", path.name)
    with db.write(conn):
        for offset, repo in enumerate(db.active_repos(conn)):
            db.enqueue(conn, "check", repo["id"], "release", not_before=db.later(offset * STAGGER_SECONDS))
    return path
