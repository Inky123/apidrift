"""Download and store dated, content-addressed snapshots of a provider's spec."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .providers import Provider

SUFFIX = ".json.gz"
USER_AGENT = "apidrift/0.1 (+https://github.com/apidrift)"


def data_dir() -> Path:
    return Path(os.environ.get("APIDRIFT_DATA", "data"))


def snapshot_dir(provider_name: str) -> Path:
    return data_dir() / "snapshots" / provider_name


def fetch(url: str, timeout: float = 120.0) -> bytes:
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha_of(path: Path) -> str:
    """The 12-character content hash embedded in a snapshot filename."""
    return path.name[: -len(SUFFIX)].rsplit("_", 1)[-1]


def list_snapshots(provider_name: str) -> list[Path]:
    folder = snapshot_dir(provider_name)
    return sorted(folder.glob(f"*{SUFFIX}")) if folder.exists() else []


def save(provider: Provider, raw: bytes, now: datetime | None = None) -> Path | None:
    """Store ``raw`` as a new snapshot. Returns None when the content is already stored."""
    doc = json.loads(raw)
    version = str(doc.get("info", {}).get("version", "unknown"))
    sha = digest(raw)[:12]
    if any(sha_of(p) == sha for p in list_snapshots(provider.name)):
        return None
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H%M%SZ")
    safe_version = re.sub(r"[^A-Za-z0-9-]+", "-", version).strip("-") or "unknown"
    folder = snapshot_dir(provider.name)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stamp}_{safe_version}_{sha}{SUFFIX}"
    tmp = path.with_name(path.name + ".tmp")  # written whole, then renamed: a full disk never leaves a truncated snapshot
    try:
        with gzip.open(tmp, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def load(path: Path) -> dict:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as fh:
        return json.load(fh)


def resolve(provider_name: str | None, ref: str) -> Path:
    """Turn a CLI argument into a path: a file, a snapshot name or prefix, ``latest`` or ``previous``."""
    candidate = Path(ref)
    if candidate.is_file():
        return candidate
    if provider_name:
        snaps = list_snapshots(provider_name)
        if ref == "latest" and snaps:
            return snaps[-1]
        if ref == "previous" and len(snaps) >= 2:
            return snaps[-2]
        matches = [s for s in snaps if s.name == ref or s.name.startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise SystemExit(f"{ref!r} matches several snapshots: {', '.join(m.name for m in matches)}")
    raise SystemExit(f"no snapshot or file matches {ref!r}")
