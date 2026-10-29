"""Notice Stripe releases: a conditional GET of the spec, a validated new snapshot, one release job per repo.

Every tick records ``last_poll_at``, ``last_poll_status`` and ``latest_snapshot`` in the ``meta`` table,
including 304s, rejections and errors, so an operator can tell a quiet Stripe from a broken poller.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .. import snapshot as snap
from ..openapi import load_spec
from ..providers import get_provider
from ..sdkmap import SdkIndex
from . import db
from .config import Settings

ETAG_NAME = ".etag"
STAGGER_SECONDS = 2
MIN_OPERATION_RATIO = 0.8
VERSION_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
STAMP_FORMAT = "%Y-%m-%dT%H%M%SZ"  # the prefix snapshot.save gives a filename
logger = logging.getLogger("apidrift.poller")
_versions: dict[str, str] = {}
_operation_counts: dict[str, int] = {}


class Rejected(ValueError):
    """The fetched body is not a usable Stripe spec; the message says why."""


def snapshots(data_dir: Path) -> list[Path]:
    """Stored Stripe snapshots under ``data_dir``, oldest first."""
    folder = data_dir / "snapshots" / "stripe"
    return sorted(folder.glob(f"*{snap.SUFFIX}")) if folder.is_dir() else []


def version_of(path: Path) -> str:
    """``info.version`` of a snapshot, cached by filename (the file never changes once written)."""
    if path.name not in _versions:
        _versions[path.name] = str(snap.load(path).get("info", {}).get("version", "unknown"))
    return _versions[path.name]


def operation_count(path: Path) -> int:
    """How many operations a stored snapshot declares, cached by filename."""
    if path.name not in _operation_counts:
        _operation_counts[path.name] = len(load_spec(snap.load(path)).operations)
    return _operation_counts[path.name]


def snapshot_stamp(path: Path) -> str:
    """When a snapshot was saved, as a db timestamp string (parsed from its filename)."""
    stamp = datetime.strptime(path.name.split("_", 1)[0], STAMP_FORMAT).replace(tzinfo=timezone.utc)
    return stamp.strftime(db.TS_FORMAT)


def latest_version(data_dir: Path) -> str | None:
    snaps = snapshots(data_dir)
    return version_of(snaps[-1]) if snaps else None


def baseline_for(snaps: list[Path]) -> Path | None:
    """The newest snapshot whose version differs from the latest's; the latest itself when there is no other.

    Same-version snapshots (Stripe commits to the spec without a release) are skipped, so a baseline is
    always a real previous release. With a single distinct version the baseline equals the latest: the
    first check is a no-op diff and findings start with the next release.
    """
    if not snaps:
        return None
    latest = version_of(snaps[-1])
    for path in reversed(snaps[:-1]):
        if version_of(path) != latest:
            return path
    return snaps[-1]


def initial_baseline(data_dir: Path) -> str | None:
    """The snapshot a newly installed repo is compared from: the OLDEST stored release.

    A finding needs code that still sends a removed or reshaped field, so an older baseline only adds true
    findings: a repo that already moved past a change does not send the old shape and is not flagged. Comparing
    from the previous release instead told every repo installed after a release with no breaking changes of its
    own (2026-10-28.endive after 2026-09-30.endive) that it was clean, even while it still sent fields the
    earlier release removed. Each finding still names the release that introduced it (``diff_chain``)."""
    snaps = snapshots(data_dir)
    return snaps[0].name if snaps else None


def validate(raw: bytes, previous: Path | None) -> str:
    """Return the version of a body that is safe to store as a snapshot; raise Rejected otherwise."""
    try:
        doc = json.loads(raw)
    except ValueError:
        raise Rejected("body is not JSON") from None
    if not isinstance(doc, dict):
        raise Rejected("body is not a JSON object")
    version = (doc.get("info") or {}).get("version") if isinstance(doc.get("info"), dict) else None
    if not isinstance(version, str) or not VERSION_RE.match(version):
        raise Rejected(f"info.version {version!r} is not a dated Stripe version")
    paths = doc.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise Rejected("paths is missing or empty")
    spec = load_spec(doc)
    if SdkIndex.build(spec).empty:
        raise Rejected("no SDK method metadata (x-stripeOperations)")
    if previous is not None:
        floor = int(operation_count(previous) * MIN_OPERATION_RATIO)
        if len(spec.operations) < floor:
            raise Rejected(
                f"{len(spec.operations)} operations is below {MIN_OPERATION_RATIO:.0%} of the previous"
                f" {operation_count(previous)}"
            )
    return version


def _write_etag(etag_file: Path, etag: str | None) -> None:
    if not etag:
        return
    etag_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = etag_file.with_name(ETAG_NAME + ".tmp")
    tmp.write_text(etag, encoding="utf-8")
    os.replace(tmp, etag_file)


def _record(conn: sqlite3.Connection, status: str, snaps: list[Path]) -> None:
    db.set_meta(conn, last_poll_at=db.now(), last_poll_status=status[:500], latest_snapshot=snaps[-1].name if snaps else None)


def poll_stripe(settings: Settings, conn: sqlite3.Connection, http: httpx.Client) -> Path | None:
    """Fetch the spec if it changed; store it when it validates; queue release checks when the version is new.

    Returns the new snapshot path, or None (304, duplicate content or a rejected body). HTTP and transport
    errors propagate after being recorded in ``meta``.
    """
    provider = get_provider("stripe")
    etag_file = settings.snapshot_dir / ETAG_NAME
    headers = {"User-Agent": snap.USER_AGENT}
    if etag_file.is_file():
        headers["If-None-Match"] = etag_file.read_text(encoding="utf-8").strip()
    try:
        response = http.get(provider.spec_url, headers=headers, follow_redirects=True, timeout=120.0)
        if response.status_code == 304:
            logger.info("Stripe poll: 304 unchanged")
            _record(conn, "304", snapshots(settings.data))
            return None
        response.raise_for_status()
    except Exception as exc:
        _record(conn, f"error: {type(exc).__name__}: {exc}"[:500], snapshots(settings.data))
        raise
    before = snapshots(settings.data)
    previous = before[-1] if before else None
    try:
        version = validate(response.content, previous)
    except Rejected as exc:
        logger.warning("Stripe poll: rejected fetched spec (%s); nothing saved, ETag untouched", exc)
        _record(conn, f"rejected: {exc}", before)
        return None
    path = snap.save(provider, response.content)
    _write_etag(etag_file, response.headers.get("etag"))
    if path is None:
        logger.info("Stripe poll: content already stored (version %s)", version)
        _record(conn, f"duplicate of a stored snapshot ({version})", before)
        return None
    previous_version = version_of(previous) if previous else None
    if version == previous_version:
        logger.info("Stripe poll: saved %s (same version %s as before; no checks queued)", path.name, version)
        _record(conn, f"saved {path.name} (same version)", snapshots(settings.data))
        return path
    with db.write(conn):
        repos = db.active_repos(conn)
        for offset, repo in enumerate(repos):
            db.enqueue(conn, "check", repo["id"], "release", not_before=db.later(offset * STAGGER_SECONDS))
    logger.info("Stripe poll: saved %s (version %s -> %s); %d release checks queued", path.name, previous_version, version, len(repos))
    _record(conn, f"saved {path.name}", snapshots(settings.data))
    return path
