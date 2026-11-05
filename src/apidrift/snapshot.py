"""Download and store dated, content-addressed snapshots of a provider's spec.

A snapshot is always gzipped JSON. A vendor that publishes JSON is stored byte for byte. A vendor that
publishes YAML (Plaid) is stored as the compact JSON of the parsed document, in the vendor's key order:
one file suffix and one reader for every provider, a load that costs milliseconds instead of a YAML
parse, and a content hash that changes when the document does, not when its comments or wrapping do.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

from .providers import Provider

SUFFIX = ".json.gz"
USER_AGENT = "apidrift/0.1 (+https://github.com/apidrift)"
MAX_EXPANSION = 8  # a YAML source may grow this many times as JSON; aliases that expand further are refused
_LEADING = b"\xef\xbb\xbf \t\r\n"


class _SpecLoader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    """Safe YAML that types plain scalars the way JSON does, which is how OpenAPI tooling reads a spec.

    PyYAML follows YAML 1.1, where ``no``, ``on`` and ``off`` are booleans, ``2020-09-14`` is a date,
    ``0123`` is octal and ``12:30`` is a base-60 number. In a spec those are enum values, versions and
    examples, so only ``true``/``false``, decimal numbers and null are typed; the rest stay strings.
    """


_SpecLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in pairs if not tag.endswith(("bool", "int", "float", "timestamp"))]
    for first, pairs in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_SpecLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))
_SpecLoader.add_implicit_resolver("tag:yaml.org,2002:int", re.compile(r"^[-+]?(?:0|[1-9][0-9]*)$"), list("-+0123456789"))
_SpecLoader.add_implicit_resolver(
    "tag:yaml.org,2002:float",
    re.compile(r"^[-+]?(?:(?:0|[1-9][0-9]*)(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$"),
    list("-+0123456789."),
)


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


def between(provider_name: str | None, old_path: Path, new_path: Path) -> list[Path]:
    """The stored snapshots strictly between two snapshots of ``provider_name``, oldest first.

    Empty when either path is not one of the provider's snapshots (a plain spec file) or they are not in
    chronological order; the caller then falls back to a single diff.
    """
    if not provider_name:
        return []
    snaps = list_snapshots(provider_name)
    resolved = [p.resolve() for p in snaps]
    try:
        i, j = resolved.index(old_path.resolve()), resolved.index(new_path.resolve())
    except ValueError:
        return []
    return snaps[i + 1:j] if i < j else []


def parse(raw: bytes) -> dict:
    """The spec document in ``raw``, which is JSON or YAML. Raises ValueError for anything else."""
    if raw.lstrip(_LEADING)[:1] == b"{":
        doc = json.loads(raw)
    else:
        try:
            doc = yaml.load(raw, Loader=_SpecLoader)  # a SafeLoader: plain data only, never Python objects
        except yaml.YAMLError as exc:
            raise ValueError(f"spec is neither JSON nor YAML: {str(exc)[:200]}") from None
    if not isinstance(doc, dict):
        raise ValueError("spec is not an object")
    return doc


def canonical(raw: bytes) -> tuple[dict, bytes]:
    """``(document, bytes to store)``: JSON is stored as received, YAML as the compact JSON of what it parses to."""
    doc = parse(raw)
    if raw.lstrip(_LEADING)[:1] == b"{":
        return doc, raw
    limit, size, chunks = MAX_EXPANSION * len(raw) + 65536, 0, []
    try:
        for chunk in json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).iterencode(doc):
            size += len(chunk)
            if size > limit:
                raise ValueError("YAML spec expands past the size limit (nested aliases)")
            chunks.append(chunk)
    except TypeError as exc:  # a binary, set or tagged date value, or a mapping key that is not a scalar
        raise ValueError(f"YAML spec holds a value JSON cannot: {exc}") from None
    return doc, "".join(chunks).encode()


def save(provider: Provider, raw: bytes, now: datetime | None = None) -> Path | None:
    """Store ``raw`` as a new snapshot. Returns None when the content is already stored."""
    doc, raw = canonical(raw)
    info = doc.get("info")
    version = str(info.get("version", "unknown")) if isinstance(info, dict) else "unknown"
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
    """The document in a snapshot or in a plain spec file (JSON or YAML, gzipped or not)."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rb") as fh:
        return parse(fh.read())


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
