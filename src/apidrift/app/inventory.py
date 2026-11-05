"""The per-repo index of Stripe call sites: what one scan found at one commit, kept so that a Stripe release
can be assessed without downloading the code again.

Three parts. The sandboxed child exports what its scan produced (``Recorder``, ``export``). The parent
validates that export on the way in (``parse``): every string is length-capped and printable, every count is
capped, and an export that breaks any rule is rejected whole, never truncated; the repo then has no index and
takes the full-check path. Storage (``store``, ``get``, ``load``, ``drop``) keeps one header row and at most
MAX_SITES site rows per repo; both go when the repo or its installation is deleted (foreign keys).

Member names read in a file (``Usage.reads``) are kept only as short hashes: impact.py asks nothing of them
but ``name in reads``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..scanner import ScanResult, Usage
from ..sdkmap import MAX_CHAIN, Resolved, SdkIndex
from . import db
from .issue import PATH_RE, valid_path

FORMAT = 2  # 2: reads per call site, and the questions asked of the stripe-go names (``SdkIndex.go``)
# What a check may ask ``SdkIndex.go`` and the shape of each question's arguments: s a name, o an (method, path).
GO_QUESTIONS = {"resolve": "sss", "known": "sss", "owns": "ss", "field": "oss", "children": "os", "path_param": "os"}
MAX_SITES = 10_000  # call sites of one repo, documentation matches included
MAX_FIELDS = 400  # request fields (or hidden prefixes) at one call site
MAX_READS = 200_000  # hashed member names, all files together
MAX_ASKED = 20_000  # questions one scan asked the SDK index
MAX_CHARS = 4_000_000  # all stored text of one repo together
MAX_FILE, MAX_CALL, MAX_FIELD, MAX_URL, MAX_LINE = 1024, 500, 300, 1000, 10_000_000
HASH_CHARS = 12
_METHOD_RE = re.compile(r"[a-z]{1,10}|\?")
_LANGUAGE_RE = re.compile(r"[a-z][a-z0-9+#-]{0,19}")
_HASH_RE = re.compile(r"[0-9a-f]{%d}" % HASH_CHARS)
# Modules of the core package that cannot change what a scan finds; every other one (the scanner, the parsers,
# and any parser added later) is part of ``scanner_digest``.
_NOT_SCANNING = frozenset({
    "__init__.py", "cli.py", "diff.py", "guidance.py", "impact.py", "migrations.py", "openapi.py", "providers.py",
    "refs.py", "report.py", "snapshot.py",
})
logger = logging.getLogger("apidrift.index")
_digest: str | None = None


class Invalid(ValueError):
    """An export that breaks a rule. The message names the rule and never quotes repository content."""


def name_hash(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8", "surrogatepass")).hexdigest()[:HASH_CHARS]


class HashedNames(frozenset):
    """Member names kept as short hashes; ``name in reads`` still answers, which is all impact.py asks."""

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and frozenset.__contains__(self, name_hash(name))


def scanner_digest() -> str:
    """A hash over the source of every module that can shape a scan or its export: the core package minus
    _NOT_SCANNING and the fixers (``fix*.py``, ``*fix.py``), plus the tarball extraction and this module. An
    index written under another digest is not used: a newer scanner (a new language, a parser fix) may find
    call sites the stored one lacks. Empty when the sources cannot be read, and an empty digest never matches."""
    global _digest
    if _digest is None:
        here = Path(__file__).resolve()
        core = [
            p for p in here.parents[1].glob("*.py")
            if p.name not in _NOT_SCANNING and not p.name.startswith("fix") and not p.name.endswith("fix.py")
        ]
        hasher = hashlib.sha256()
        try:
            for path in sorted(core) + [here.with_name("checkout.py"), here]:
                hasher.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
            _digest = hasher.hexdigest()[:16] if any(p.name == "scanner.py" for p in core) else ""
        except OSError:
            _digest = ""
    return _digest


# --- the child: record and export ---------------------------------------------------------


class Recorder:
    """Stands in for the SdkIndex during a check and notes every question the scanner and the fixer ask it.

    The scan is a function of the code and of these answers, and the fix plan of the code, the impacts and
    these answers. An SdkIndex that answers them all the same way would therefore find the same call sites
    and, for the same impacts, plan the same fixes. Anything else that is reached for is passed through and
    noted in ``untracked``; such a check is exported as unusable.
    """

    def __init__(self, index: SdkIndex) -> None:
        self._index = index
        self.chains: dict[tuple[str, ...], None] = {}
        self.urls: dict[str, None] = {}
        self.paths: dict[str, None] = {}
        self.go_asked: dict[tuple[str, tuple], None] = {}
        self.untracked: set[str] = set()
        self.go = _GoRecorder(index.go, self) if getattr(index, "go", None) is not None else None

    @property
    def empty(self) -> bool:
        return self._index.empty

    def resolve(self, chain) -> Resolved | None:
        parts = [p for p in chain if p]
        if len(parts) >= 2:  # resolve() looks at the last MAX_CHAIN parts only, and never at a single one
            self.chains[tuple(parts[-MAX_CHAIN:])] = None
        return self._index.resolve(parts)

    def match_path(self, url_path: str) -> str | None:
        self.urls[url_path] = None
        return self._index.match_path(url_path)

    def methods_for(self, path: str) -> list[str]:
        self.paths[path] = None
        return self._index.methods_for(path)

    def __getattr__(self, name: str):
        if name.startswith("__") or name == "_index":
            raise AttributeError(name)
        self.untracked.add(name)
        return getattr(self._index, name)


class _GoRecorder:
    """The same for ``SdkIndex.go``, the stripe-go names the Go reader and fixer ask: each of GO_QUESTIONS is
    noted with its arguments; anything else, or a call in another shape, is passed through and noted as untracked."""

    def __init__(self, go: object, owner: Recorder) -> None:
        self._go, self._owner = go, owner

    def __getattr__(self, name: str):
        if name.startswith("__") or name in ("_go", "_owner"):
            raise AttributeError(name)
        target = getattr(self._go, name)
        if name not in GO_QUESTIONS:
            self._owner.untracked.add("go." + name)
            return target

        def ask(*args, **kwargs):
            try:
                if kwargs or len(args) != len(GO_QUESTIONS[name]):
                    raise TypeError(name)
                self._owner.go_asked[(name, args)] = None
            except TypeError:  # keywords, another arity, an argument that is not a name or an operation
                self._owner.untracked.add("go." + name)
            return target(*args, **kwargs)

        return ask


def export(result: ScanResult, recorder: Recorder) -> dict:
    """The check as plain data for the parent: every call site in scan order with the hashed reads that go with
    it, and the questions the scan and the fixer asked the SDK index."""
    reads: dict[frozenset[str], int] = {}
    sites = [
        {
            "file": u.file, "line": u.line, "method": u.method, "path": u.path, "call": u.call, "language": u.language,
            "complete": u.complete, "fields": sorted(u.fields), "hidden": None if u.hidden is None else sorted(u.hidden),
            "reads": reads.setdefault(u.reads, len(reads)),
        }
        for u in result.usages
    ]
    return {
        "format": FORMAT, "unusable": bool(recorder.untracked), "sites": sites,
        "reads": [sorted({name_hash(n) for n in names}) for names in reads],
        "asked": {
            "chains": [list(c) for c in recorder.chains], "urls": list(recorder.urls), "paths": list(recorder.paths),
            "go": [[name, [list(a) if isinstance(a, tuple) else a for a in args]] for name, args in recorder.go_asked],
        },
    }


# --- the parent: validate ------------------------------------------------------------------


@dataclass(frozen=True)
class Index:
    usages: tuple[Usage, ...]  # scan order; ``reads`` are HashedNames
    reads: tuple[tuple[str, ...], ...]  # the distinct sets of hashes
    site_reads: tuple[int, ...]  # per call site, which of ``reads`` goes with it
    chains: tuple[tuple[str, ...], ...]
    urls: tuple[str, ...]
    paths: tuple[str, ...]
    go: tuple[tuple[str, tuple], ...]  # (question, arguments) asked of ``SdkIndex.go``


class _Budget:
    def __init__(self, limit: int) -> None:
        self.left = limit

    def take(self, *texts: str) -> None:
        self.left -= sum(len(t) for t in texts)
        if self.left < 0:
            raise Invalid("the index is over the size cap")


def _text(value: object, limit: int, what: str, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or not value.isprintable() or not (value or empty):
        raise Invalid(f"{what} is not short printable text")
    return value


def _names(value: object, what: str, empty: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_FIELDS:
        raise Invalid(f"{what}: not a list within the cap")
    names = [_text(item, MAX_FIELD, what, empty) for item in value]
    if len(set(names)) != len(names):
        raise Invalid(f"{what}: duplicates")
    return names


def _operation(method: object, path: object, wild: bool = False) -> tuple[str, str]:
    if not isinstance(method, str) or not _METHOD_RE.fullmatch(method) or (method == "?" and not wild):
        raise Invalid("method is not an HTTP method")
    if not isinstance(path, str) or len(path) > MAX_FIELD or not PATH_RE.fullmatch(path):
        raise Invalid("path is not a Stripe path")
    return method, path


def _usage(site: object, reads: list[HashedNames], budget: _Budget) -> Usage:
    if not isinstance(site, dict):
        raise Invalid("a call site is not an object")
    file = _text(site.get("file"), MAX_FILE, "file")
    line, complete, which = site.get("line"), site.get("complete"), site.get("reads")
    if not valid_path(file):
        raise Invalid("file is not a relative path")
    if isinstance(line, bool) or not isinstance(line, int) or not 1 <= line <= MAX_LINE:
        raise Invalid("line is not a line number")
    method, path = _operation(site.get("method"), site.get("path"), wild=True)
    if not isinstance(complete, bool):
        raise Invalid("complete is not a boolean")
    if isinstance(which, bool) or not isinstance(which, int) or not 0 <= which < len(reads):
        raise Invalid("a call site names no set of reads")
    call, language = _text(site.get("call"), MAX_CALL, "call"), site.get("language")
    if not isinstance(language, str) or not _LANGUAGE_RE.fullmatch(language):
        raise Invalid("language is not a language name")
    fields = _names(site.get("fields"), "fields")
    hidden = None if site.get("hidden") is None else _names(site["hidden"], "hidden", empty=True)
    budget.take(file, call, *fields, *(hidden or ()))
    return Usage(
        file, line, method, path, call, frozenset(fields), complete, language, reads[which],
        None if hidden is None else frozenset(hidden),
    )


def _reads(raw: object) -> tuple[tuple[str, ...], ...]:
    if not isinstance(raw, list) or len(raw) > MAX_SITES:
        raise Invalid("reads is not a list within the cap")
    total = 0
    for hashes in raw:
        if not isinstance(hashes, list) or not all(isinstance(h, str) and _HASH_RE.fullmatch(h) for h in hashes):
            raise Invalid("reads are not name hashes")
        total += len(hashes)
        if total > MAX_READS or len(set(hashes)) != len(hashes):
            raise Invalid("reads are over the cap or repeated")
    return tuple(tuple(hashes) for hashes in raw)


def _go_question(item: object, budget: _Budget) -> tuple[str, tuple]:
    """One question to ``SdkIndex.go``: a known name with arguments of its shape, names and operations only."""
    if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str) or item[0] not in GO_QUESTIONS:
        raise Invalid("a Go question is not one of the known ones")
    shape, args = GO_QUESTIONS[item[0]], item[1]
    if not isinstance(args, list) or len(args) != len(shape):
        raise Invalid("a Go question has the wrong arguments")
    out: list = []
    for kind, arg in zip(shape, args):
        if kind == "s":
            out.append(_text(arg, MAX_FIELD, "a Go name", empty=True))
            budget.take(arg)
        elif not isinstance(arg, list) or len(arg) != 2:
            raise Invalid("a Go question names no operation")
        else:
            out.append(_operation(*arg))
    return item[0], tuple(out)


def _asked(raw: object, budget: _Budget) -> tuple[tuple, tuple, tuple, tuple]:
    kinds = ("chains", "urls", "paths", "go")
    if not isinstance(raw, dict) or not all(isinstance(raw.get(k), list) for k in kinds):
        raise Invalid("asked is not four lists")
    if sum(len(raw[k]) for k in kinds) > MAX_ASKED:
        raise Invalid("too many questions asked of the SDK index")
    chains = []
    for chain in raw["chains"]:
        if not isinstance(chain, list) or not 2 <= len(chain) <= MAX_CHAIN:
            raise Invalid("a call chain is not two to four names")
        chains.append(tuple(_text(part, MAX_FIELD, "a call chain name") for part in chain))
        budget.take(*chains[-1])
    urls = tuple(_text(url, MAX_URL, "a URL path") for url in raw["urls"])
    budget.take(*urls)
    if not all(isinstance(p, str) and len(p) <= MAX_FIELD and PATH_RE.fullmatch(p) for p in raw["paths"]):
        raise Invalid("an asked path is not a Stripe path")
    return tuple(chains), urls, tuple(raw["paths"]), tuple(_go_question(item, budget) for item in raw["go"])


def parse(raw: object) -> Index:
    """The child's export as trusted-shape data, or Invalid. Nothing is repaired or cut to fit."""
    if not isinstance(raw, dict) or isinstance(raw.get("format"), bool) or raw.get("format") != FORMAT:
        raise Invalid("the check carried no index in this format")
    if raw.get("unusable"):
        raise Invalid("the scan could not be recorded")
    sites = raw.get("sites")
    if not isinstance(sites, list) or len(sites) > MAX_SITES:
        raise Invalid("call sites: not a list within the cap")
    budget = _Budget(MAX_CHARS)
    reads = _reads(raw.get("reads"))
    usages = tuple(_usage(site, [HashedNames(hashes) for hashes in reads], budget) for site in sites)
    which = tuple(site["reads"] for site in sites)
    if set(which) != set(range(len(reads))):
        raise Invalid("a set of reads that no call site uses")
    return Index(usages, reads, which, *_asked(raw.get("asked"), budget))


def parse_unread(raw: object) -> dict[str, int]:
    """``payload["unread"]`` (language -> files not parsed) with the same strictness."""
    if not isinstance(raw, dict) or len(raw) > 50:
        raise Invalid("unread is not a small object")
    for name, count in raw.items():
        _text(name, 40, "an unread language")
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= MAX_LINE:
            raise Invalid("an unread count is not a count")
    return dict(raw)


# --- the parent: store ----------------------------------------------------------------------


@dataclass(frozen=True)
class Stored:
    usages: tuple[Usage, ...]
    answers: dict  # {"chains": [[parts, "post /v1/x" | None]], "urls": [[url, path | None]], "paths": [[path, methods]]}
    unread: dict[str, int]
    impacts: str  # ``impacts_digest`` of the check that wrote the index: what the repo's issue was last written from


def impacts_digest(impacts: list[dict]) -> str:
    """A hash of a check's whole impact list (every certainty, documentation matches included)."""
    return hashlib.sha256(json.dumps(impacts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def store(
    conn: sqlite3.Connection, repo_id: int, sha: str, job_id: int | None, index: Index, answers: dict,
    unread: dict[str, int], impacts: str, checked: str,
) -> None:
    """Replace the repo's index with ``index`` (validated), marked as checked against snapshot ``checked``."""
    extras = json.dumps(
        {"reads": ["".join(hashes) for hashes in index.reads], "site_reads": list(index.site_reads), "answers": answers,
         "unread": unread, "impacts": impacts},
        separators=(",", ":"),
    )
    rows = [
        (repo_id, seq, u.file, u.line, u.method, u.path, u.call, u.language, int(u.complete), json.dumps(sorted(u.fields)),
         None if u.hidden is None else json.dumps(sorted(u.hidden)))
        for seq, u in enumerate(index.usages)
    ]
    ts = db.now()
    with db.write(conn):
        conn.execute("DELETE FROM index_sites WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM repo_index WHERE repo_id = ?", (repo_id,))
        conn.execute(
            "INSERT INTO repo_index (repo_id, sha, format, scanner, job_id, extras, written_at, checked, checked_at, checked_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'full')",
            (repo_id, sha, FORMAT, scanner_digest(), job_id, extras, ts, checked, ts),
        )
        conn.executemany("INSERT INTO index_sites VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def get(conn: sqlite3.Connection, repo_id: int) -> db.Row | None:
    """The index header of a repo: sha, format, scanner, job_id, written_at, checked, checked_at, checked_by."""
    return conn.execute("SELECT * FROM repo_index WHERE repo_id = ?", (repo_id,)).fetchone()


def sites(conn: sqlite3.Connection, repo_id: int) -> list[db.Row]:
    return conn.execute("SELECT * FROM index_sites WHERE repo_id = ? ORDER BY seq", (repo_id,)).fetchall()


def load(conn: sqlite3.Connection, header: db.Row) -> Stored | None:
    """The stored index as Usage objects in scan order; None when a row does not read back."""
    try:
        extras = json.loads(header["extras"])
        reads = [HashedNames(text[i:i + HASH_CHARS] for i in range(0, len(text), HASH_CHARS)) for text in extras["reads"]]
        which = extras["site_reads"]
        usages = tuple(
            Usage(
                row["file"], row["line"], row["method"], row["path"], row["call"], frozenset(json.loads(row["fields"])),
                bool(row["complete"]), row["language"], reads[which[row["seq"]]],
                None if row["hidden"] is None else frozenset(json.loads(row["hidden"])),
            )
            for row in sites(conn, header["repo_id"])
        )
        return Stored(usages, extras["answers"], extras["unread"], str(extras["impacts"]))
    except (ValueError, KeyError, TypeError, AttributeError):
        logger.warning("repo %s: the stored index does not read back", header["repo_id"])
        return None


def drop(conn: sqlite3.Connection, repo_id: int) -> None:
    with db.write(conn):
        conn.execute("DELETE FROM index_sites WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM repo_index WHERE repo_id = ?", (repo_id,))


def mark_checked(conn: sqlite3.Connection, repo_id: int, snapshot: str, how: str = "index") -> None:
    with db.write(conn):
        conn.execute(
            "UPDATE repo_index SET checked = ?, checked_at = ?, checked_by = ? WHERE repo_id = ?",
            (snapshot, db.now(), how, repo_id),
        )


def later_job(conn: sqlite3.Connection, repo_id: int, job_id: int | None) -> bool:
    """Whether a check other than a queued one came after the job that wrote the index. Every completed check
    rewrites or drops the index, so such a job failed, is running, or ended without looking at the code."""
    return conn.execute(
        "SELECT 1 FROM jobs WHERE repo_id = ? AND kind = 'check' AND status != 'queued' AND id > ? LIMIT 1",
        (repo_id, job_id or 0),
    ).fetchone() is not None


def drop_superseded(conn: sqlite3.Connection) -> int:
    """Drop every index a later finished check did not rewrite (it failed, or was skipped). Run before old
    jobs are pruned, so the fact outlives the job row."""
    with db.write(conn):
        stale = (
            "SELECT i.repo_id FROM repo_index i JOIN jobs j ON j.repo_id = i.repo_id WHERE j.kind = 'check'"
            " AND j.status IN ('done', 'failed') AND j.id > coalesce(i.job_id, 0)"
        )
        conn.execute(f"DELETE FROM index_sites WHERE repo_id IN ({stale})")
        return conn.execute(f"DELETE FROM repo_index WHERE repo_id IN ({stale})").rowcount


# --- the operator's view --------------------------------------------------------------------


def summary(conn: sqlite3.Connection) -> dict:
    """Counts for ``apidrift status``: plain types only."""
    head = conn.execute(
        "SELECT count(*) AS repos, coalesce(sum(checked_by = 'index'), 0) AS by_index FROM repo_index"
    ).fetchone()
    body = conn.execute(
        "SELECT coalesce(sum(language != 'doc'), 0) AS call_sites, coalesce(sum(language = 'doc'), 0) AS doc_sites,"
        " count(DISTINCT CASE WHEN language != 'doc' THEN method || ' ' || path END) AS endpoints FROM index_sites"
    ).fetchone()
    return {**dict(head), **dict(body)}


def render(full_name: str, header: db.Row, rows: list[db.Row]) -> str:
    """One repo's index as text, grouped by endpoint. Every value was validated as printable before storage."""
    counted = [r for r in rows if r["language"] != "doc"]
    groups: dict[tuple[str, str], list[db.Row]] = {}
    for row in counted:
        groups.setdefault((row["path"], row["method"]), []).append(row)
    lines = [
        f"{full_name} at {header['sha'][:12]}: {len(counted)} Stripe call sites on {len(groups)} endpoints",
        f"indexed {header['written_at']} by job {header['job_id']}; last checked against {header['checked']}"
        f" ({'from the index' if header['checked_by'] == 'index' else 'by a full check'}, {header['checked_at']})",
    ]
    for (path, method), group in sorted(groups.items()):
        lines += ["", f"{method.upper()} {path}  ({len(group)})"]
        for row in group:
            fields = json.loads(row["fields"])
            shown = ", ".join(fields[:8]) + (f" (+{len(fields) - 8} more)" if len(fields) > 8 else "")
            if not row["complete"]:
                shown += (" " if shown else "") + "(+ fields not visible)"
            lines.append(f"  {row['file']}:{row['line']}  {row['call']}  [{row['language']}]" + (f"  fields: {shown}" if shown else ""))
    docs = [r for r in rows if r["language"] == "doc"]
    if docs:
        lines += ["", f"documentation, fixtures and tests (not counted): {len(docs)}"]
        lines += [f"  {r['file']}:{r['line']}  {r['method'].upper()} {r['path']}  {r['call']}" for r in docs]
    return "\n".join(lines)
