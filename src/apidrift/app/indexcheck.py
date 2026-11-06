"""Assess a repo's index against a pair of a provider's snapshots in the parent, and admit a new index.

``payload_of`` runs the same ``impact.assess`` a full check runs in its child, on Usage objects rebuilt from
the index, against the same change set (``pair``: baseline -> latest with every snapshot in between, diffed
and indexed as the provider's profile says).

``record`` is the gate for a new index. Everything in it is output of the sandboxed child, so it is stored
only when it validates (``inventory.parse``), names only operations the trusted specs have, and reproduces
here, impact for impact, what the child itself reported for the same snapshots. An index that fails any of
this is dropped and the repo keeps taking the full-check path.
"""

from __future__ import annotations

import dataclasses
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
from ..localize import place, shared_leaves
from ..openapi import Spec, load_spec
from ..profiles import get_profile
from ..report import filter_changes
from ..scanner import Usage
from ..sdkmap import Resolved
from . import inventory

MAX_PAIRS = 8
ISOLATE_ABOVE = 200_000  # bytes of snapshot files in a chain; a longer chain is parsed in a helper process
BUILD_TIMEOUT = 300
RETRY_SECONDS = 600  # after a failed build, how long the chain is reported unavailable without trying again
logger = logging.getLogger("apidrift.index")
_pairs: dict[tuple[str, ...], "Pair | float"] = {}  # keyed by provider, then the chain's file names  # a float is when (monotonic) the build of that chain failed


@dataclass(frozen=True)
class Pair:
    """What a check of baseline -> latest compares against: built once per chain of snapshots and cached."""

    old_version: str
    new_version: str
    versions: tuple[str, ...]
    index: object  # the provider's index (``Profile.build_index``) of the two ends
    changes: tuple[Change, ...]
    provider: str = "stripe"
    ends: tuple[Spec, Spec] | None = None  # kept for a nested provider: its changes are placed per checkout


def build(chain: list[str], provider: str = "stripe") -> Pair:
    """Parse a chain of snapshot files (baseline, everything between, latest) and diff it, exactly as a full
    check (``checker.run_local``, ``apidrift impact``) does for the same two ends."""
    profile = get_profile(provider)
    specs = [load_spec(snap.load(Path(path))) for path in chain]
    changes, versions = diff_chain(specs)
    return Pair(
        specs[0].version, specs[-1].version, tuple(versions), profile.build_index(specs[0], specs[-1]),
        tuple(filter_changes(changes, min_severity=BREAKING)), provider,
        (specs[0], specs[-1]) if profile.nested else None,
    )


_HELPER = (
    "import pickle, sys; from apidrift.app import indexcheck;"
    " sys.stdout.buffer.write(pickle.dumps(indexcheck.build(sys.argv[2:], sys.argv[1])))"
)


def _build_apart(chain: list[str], provider: str = "stripe") -> Pair:
    """``build`` in a helper process. Parsing real Stripe specs takes about 40 MB each, which a long-lived
    process does not give back; the worker keeps only the result (the alias table and the breaking changes).

    The helper runs this same package (the parent's import path), reads nothing but our own snapshot files and
    gets no secrets in its environment; its output is unpickled because it is ours. A helper that dies or
    times out raises here, and the caller falls back to the full check."""
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
    env["PYTHONPATH"] = os.pathsep.join(os.path.abspath(entry) for entry in sys.path if entry)
    done = subprocess.run(
        [sys.executable, "-c", _HELPER, provider, *chain], capture_output=True, timeout=BUILD_TIMEOUT, env=env
    )
    if done.returncode != 0 or not done.stdout:
        raise RuntimeError(f"building the spec pair failed (exit {done.returncode})")
    result = pickle.loads(done.stdout)
    if not isinstance(result, Pair):
        raise RuntimeError("building the spec pair returned something else")
    return result


def pair(snaps: list[Path], baseline: Path, latest: Path, provider: str = "stripe") -> Pair:
    """The provider's index and the breaking changes a full check uses for the same two snapshots."""
    names = [p.name for p in snaps]
    i, j = names.index(baseline.name), names.index(latest.name)
    chain = [baseline, *(snaps[i + 1:j] if i < j else []), latest]
    key = (provider, *(p.name for p in chain))  # snapshot files never change once written
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
            found = _build_apart(paths, provider) if apart else build(paths, provider)
        except Exception:
            _pairs[key] = time.monotonic()
            raise
        _pairs[key] = found
    return found


def _answer(value: object) -> object:
    """An index's answer as JSON, so it compares with a stored copy. Raises TypeError for anything else."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Resolved):
        return {"op": [value.method, value.path], "key": value.key}
    if isinstance(value, (list, tuple)):
        return [_answer(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_answer(item) for item in value)
    raise TypeError(f"an answer of type {type(value).__name__}")


def ask(index: object, provider: str, name: str, args: list) -> object:
    """Ask ``index`` one recorded question: only one of the provider's (``Profile.questions``), with as many
    arguments as it takes; a list argument is passed as a tuple, as the readers pass an operation."""
    if get_profile(provider).questions.get(name) != len(args):
        raise ValueError("not a question the provider's index answers")
    *owners, method = name.split(".")
    holder = index
    for owner in owners:
        holder = getattr(holder, owner)
        if holder is None:
            return None
    return _answer(getattr(holder, method)(*[tuple(arg) if isinstance(arg, list) else arg for arg in args]))


def answers(index: object, provider: str, asked) -> list:
    """``[[question, arguments, answer]]`` for every question a check asked."""
    return [[name, list(args), ask(index, provider, name, list(args))] for name, args in asked]


def same_answers(index: object, provider: str, stored: list) -> bool:
    """Whether ``index`` answers every recorded question as the index of the check did: then a scan of the same
    code with it finds the same call sites, and the fixer plans the same fixes for the same impacts."""
    try:
        return answers(index, provider, [(name, args) for name, args, _ in stored]) == stored
    except (TypeError, ValueError, AttributeError):
        return False


def with_values(usages, changes) -> list[Usage]:
    """The usages with each stored value hash (``#...``) that hashes like a value one of ``changes`` lists put back
    as that value. impact.py only asks whether a value sent is among the values a change lists (and names it when
    it is), so it judges the restored usages as it judges the scanned ones; a hash left as it is matches nothing."""
    listed: dict[str, str] = {}
    for change in changes:
        for side in (change.old, change.new):
            if isinstance(side, (list, tuple)):
                for value in side:
                    listed.setdefault(inventory.sent_value(str(value)) or "", str(value))
    return [
        u if not u.values else dataclasses.replace(u, values=frozenset((f, listed.get(v, v) if v else v) for f, v in u.values))
        for u in usages
    ]


def payload_of(usages: tuple[Usage, ...], unread: dict[str, int], pr: Pair) -> dict:
    """The check payload a full check of the same code would produce for ``pr``, minus the fix plan."""
    changes, shared = list(pr.changes), None
    if pr.ends is not None:  # as ``apidrift impact`` does for a nested provider: on the operations this code calls
        called = {u.path for u in usages}
        changes, shared = place(changes, *pr.ends, called), shared_leaves(*pr.ends, called)
    impacts = assess(with_values(usages, changes), changes, shared)
    return {
        "impacts": json.loads(json.dumps([impact.to_dict() for impact in impacts])),
        "call_sites": sum(1 for u in usages if u.language != "doc"),
        "documentation_matches": sum(1 for u in usages if u.language == "doc"),
        "unread": dict(unread),
        "old_version": pr.old_version, "new_version": pr.new_version, "versions": list(pr.versions),
    }


def record(
    conn: sqlite3.Connection, snaps: list[Path], repo_id: int, job_id: int, sha: str, payload: dict,
    baseline: Path, latest: Path, provider: str = "stripe",
) -> str | None:
    """Store the index the child exported with this check, or drop the repo's index for ``provider``. Returns
    None when it was stored, else why not. Never raises: a check must not fail because its index was refused."""
    try:
        index = inventory.parse(payload.get("index"), provider)
        unread = inventory.parse_unread(payload.get("unread") or {})
        pr = pair(snaps, baseline, latest, provider)
        paths = {path for _, path in pr.index.operations}
        for u in index.usages:
            if (u.method, u.path) not in pr.index.operations and not (u.method == "?" and u.path in paths):
                raise inventory.Invalid("a call site names an operation the specs do not have")
        mine = payload_of(index.usages, unread, pr)
        if any(mine[key] != payload.get(key) for key in ("impacts", "call_sites", "old_version", "new_version")):
            raise inventory.Invalid("the index does not reproduce the impacts of its own check")
        inventory.store(
            conn, repo_id, sha, job_id, index, answers(pr.index, provider, index.asked), unread,
            inventory.impacts_digest(mine["impacts"]), latest.name,
        )
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the repo just has no index
        why = str(exc) if isinstance(exc, inventory.Invalid) else type(exc).__name__
        try:
            inventory.drop(conn, repo_id, provider)
        except sqlite3.Error:
            pass
        level = logging.INFO if "carried no index" in why else logging.WARNING
        logger.log(level, "repo %s: no index kept (%s)", repo_id, why)
        return why
    logger.info("repo %s: index stored, %d call sites", repo_id, len(index.usages))
    return None
