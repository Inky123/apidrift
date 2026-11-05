"""Assess a repo's index against a pair of Stripe snapshots in the parent, and admit a new index.

``payload_of`` runs the same ``impact.assess`` a full check runs in its child, on Usage objects rebuilt from
the index, against the same change set (``pair``: baseline -> latest with every snapshot in between).

``record`` is the gate for a new index. Everything in it is output of the sandboxed child, so it is stored
only when it validates (``inventory.parse``), names only operations the trusted specs have, and reproduces
here, impact for impact, what the child itself reported for the same snapshots. An index that fails any of
this is dropped and the repo keeps taking the full-check path.
"""

from __future__ import annotations

import json
import logging
import os
import pickle
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .. import snapshot as snap
from ..diff import BREAKING, Change, diff_chain
from ..impact import assess
from ..openapi import load_spec
from ..report import filter_changes
from ..scanner import Usage
from ..sdkmap import SdkIndex
from . import inventory

MAX_PAIRS = 8
ISOLATE_ABOVE = 200_000  # bytes of snapshot files in a chain; a longer chain is parsed in a helper process
BUILD_TIMEOUT = 300
RETRY_SECONDS = 600  # after a failed build, how long the chain is reported unavailable without trying again
logger = logging.getLogger("apidrift.index")
_pairs: dict[tuple[str, ...], "Pair | float"] = {}  # a float is when (monotonic) the build of that chain failed


@dataclass(frozen=True)
class Pair:
    """What a check of baseline -> latest compares against: built once per chain of snapshots and cached."""

    old_version: str
    new_version: str
    versions: tuple[str, ...]
    index: SdkIndex
    changes: tuple[Change, ...]


def build(chain: list[str]) -> Pair:
    """Parse a chain of snapshot files (baseline, everything between, latest) and diff it, exactly as
    ``checker.run_local`` does for the same two ends."""
    specs = [load_spec(snap.load(Path(path))) for path in chain]
    changes, versions = diff_chain(specs)
    return Pair(
        specs[0].version, specs[-1].version, tuple(versions), SdkIndex.build(specs[0], specs[-1]),
        tuple(filter_changes(changes, min_severity=BREAKING)),
    )


_HELPER = (
    "import pickle, sys; from apidrift.app import indexcheck;"
    " sys.stdout.buffer.write(pickle.dumps(indexcheck.build(sys.argv[1:])))"
)


def _build_apart(chain: list[str]) -> Pair:
    """``build`` in a helper process. Parsing real Stripe specs takes about 40 MB each, which a long-lived
    process does not give back; the worker keeps only the result (the alias table and the breaking changes).

    The helper runs this same package (the parent's import path), reads nothing but our own snapshot files and
    gets no secrets in its environment; its output is unpickled because it is ours. A helper that dies or
    times out raises here, and the caller falls back to the full check."""
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
    env["PYTHONPATH"] = os.pathsep.join(os.path.abspath(entry) for entry in sys.path if entry)
    done = subprocess.run([sys.executable, "-c", _HELPER, *chain], capture_output=True, timeout=BUILD_TIMEOUT, env=env)
    if done.returncode != 0 or not done.stdout:
        raise RuntimeError(f"building the spec pair failed (exit {done.returncode})")
    result = pickle.loads(done.stdout)
    if not isinstance(result, Pair):
        raise RuntimeError("building the spec pair returned something else")
    return result


def pair(snaps: list[Path], baseline: Path, latest: Path) -> Pair:
    """The SDK index and the breaking changes ``checker.run_local`` uses for the same two snapshots."""
    names = [p.name for p in snaps]
    i, j = names.index(baseline.name), names.index(latest.name)
    chain = [baseline, *(snaps[i + 1:j] if i < j else []), latest]
    key = tuple(p.name for p in chain)  # snapshot files never change once written
    found = _pairs.get(key)
    if isinstance(found, float):
        if time.monotonic() - found < RETRY_SECONDS:
            raise RuntimeError("the spec pair could not be built a moment ago")
        found = None
    if found is None:
        paths = [str(p) for p in chain]
        _pairs.pop(key, None)
        while len(_pairs) >= MAX_PAIRS:
            _pairs.pop(next(iter(_pairs)))
        try:
            apart = sum(p.stat().st_size for p in chain) > ISOLATE_ABOVE
            found = _build_apart(paths) if apart else build(paths)
        except Exception:
            _pairs[key] = time.monotonic()
            raise
        _pairs[key] = found
    return found


def answers(index: SdkIndex, chains, urls, paths) -> dict:
    """What ``index`` answers to the questions a scan asked (JSON-shaped, so it compares with a stored copy)."""
    resolved = [(list(chain), index.resolve(chain)) for chain in chains]
    return {
        "chains": [[chain, f"{r.method} {r.path}" if r is not None else None] for chain, r in resolved],
        "urls": [[url, index.match_path(url)] for url in urls],
        "paths": [[path, index.methods_for(path)] for path in paths],
    }


def same_answers(index: SdkIndex, stored: dict) -> bool:
    """Whether ``index`` resolves every recorded question as the index of the scan did: then a scan of the same
    code with it finds the same call sites."""
    asked = [[question for question, _ in stored.get(kind, [])] for kind in ("chains", "urls", "paths")]
    return answers(index, *asked) == stored


def payload_of(usages: tuple[Usage, ...], unread: dict[str, int], pr: Pair) -> dict:
    """The check payload a full check of the same code would produce for ``pr``, minus the fix plan."""
    impacts = assess(list(usages), list(pr.changes))
    return {
        "impacts": json.loads(json.dumps([impact.to_dict() for impact in impacts])),
        "call_sites": sum(1 for u in usages if u.language != "doc"),
        "documentation_matches": sum(1 for u in usages if u.language == "doc"),
        "unread": dict(unread),
        "old_version": pr.old_version, "new_version": pr.new_version, "versions": list(pr.versions),
    }


def record(
    conn: sqlite3.Connection, snaps: list[Path], repo_id: int, job_id: int, sha: str, payload: dict,
    baseline: Path, latest: Path,
) -> str | None:
    """Store the index the child exported with this check, or drop the repo's index. Returns None when it was
    stored, else why not. Never raises: a check must not fail because its index was refused."""
    try:
        index = inventory.parse(payload.get("index"))
        unread = inventory.parse_unread(payload.get("unread") or {})
        pr = pair(snaps, baseline, latest)
        paths = {path for _, path in pr.index.operations}
        for u in index.usages:
            if (u.method, u.path) not in pr.index.operations and not (u.method == "?" and u.path in paths):
                raise inventory.Invalid("a call site names an operation the specs do not have")
        mine = payload_of(index.usages, unread, pr)
        if any(mine[key] != payload.get(key) for key in ("impacts", "call_sites", "old_version", "new_version")):
            raise inventory.Invalid("the index does not reproduce the impacts of its own check")
        inventory.store(
            conn, repo_id, sha, job_id, index, answers(pr.index, index.chains, index.urls, index.paths), unread,
            inventory.impacts_digest(mine["impacts"]), latest.name,
        )
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the repo just has no index
        why = str(exc) if isinstance(exc, inventory.Invalid) else type(exc).__name__
        try:
            inventory.drop(conn, repo_id)
        except sqlite3.Error:
            pass
        level = logging.INFO if "carried no index" in why else logging.WARNING
        logger.log(level, "repo %s: no index kept (%s)", repo_id, why)
        return why
    logger.info("repo %s: index stored, %d call sites", repo_id, len(index.usages))
    return None
