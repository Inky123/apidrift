"""The per-repo index of one provider's call sites: what one scan found at one commit, kept so that a release
of that provider's API can be assessed without downloading the code again.

Nothing here is particular to a provider. What a provider's reader may ask its index is declared in its profile
(``Profile.questions``); the Recorder notes those questions with their arguments, and the parent asks the new
release's index the same ones (``indexcheck.answers``).

Three parts. The sandboxed child exports what its scan produced (``Recorder``, ``export``). The parent
validates that export on the way in (``parse``): every string is length-capped and printable, every count is
capped, and an export that breaks any rule is rejected whole, never truncated; the repo then has no index and
takes the full-check path. Storage (``store``, ``get``, ``load``, ``drop``) keeps one header row and at most
MAX_SITES site rows per repo and provider; both go when the repo or its installation is deleted (foreign keys).

Member names read in a file (``Usage.reads``) are kept only as short hashes: impact.py asks nothing of them
but ``name in reads``. So are the literal values a call sends (``Usage.values``, collected by some readers):
``#`` and a hash each, never the value; ``indexcheck`` turns one back into a value only where it hashes like a
value a change lists, which is all impact.py compares it with. An SDK enum member (``~Name``) stays as written.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..profiles import get_profile
from ..scanner import ScanResult, Usage
from . import db
from .issue import valid_path

FORMAT = 3  # 3: one index per provider, holding the questions of the provider's profile with their arguments
MAX_SITES = 10_000  # call sites of one repo, documentation matches included
MAX_FIELDS = 400  # request fields (or hidden prefixes) at one call site
MAX_READS = 200_000  # hashed member names, all files together
MAX_ASKED = 20_000  # questions one scan asked the provider's index
MAX_CHARS = 4_000_000  # all stored text of one repo together
MAX_FILE, MAX_CALL, MAX_FIELD, MAX_URL, MAX_LINE = 1024, 500, 300, 1000, 10_000_000
HASH_CHARS = 12
_METHOD_RE = re.compile(r"[a-z]{1,10}|\?")
_SPEC_PATH_RE = re.compile(r"(?:/[A-Za-z0-9_{}.-]{1,100}){1,16}")
_LANGUAGE_RE = re.compile(r"[a-z][a-z0-9+#-]{0,19}")
_HASH_RE = re.compile(r"[0-9a-f]{%d}" % HASH_CHARS)
_SENT_RE = re.compile(r"#[0-9a-f]{%d}" % HASH_CHARS)
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


def _plain(value: object, inside: bool = False) -> object:
    """A question's argument as JSON: a name or a number, or a list of them (a chain, an operation, a set of
    names, sorted). Anything else raises TypeError, and the question is then noted as untracked."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if not inside and isinstance(value, (list, tuple)):
        return [_plain(item, True) for item in value]
    if not inside and isinstance(value, (set, frozenset)) and all(isinstance(item, str) for item in value):
        return sorted(value)
    raise TypeError(type(value).__name__)


class _Asking:
    """An index, or an object it holds (``go``), seen through a Recorder."""

    def __init__(self, target: object, owner: "Recorder", prefix: str) -> None:
        self._target, self._owner, self._prefix = target, owner, prefix

    def __getattr__(self, name: str):
        if name.startswith("__") or name in ("_target", "_owner", "_prefix"):
            raise AttributeError(name)
        return self._owner._reach(self._target, self._prefix + name)


class Recorder(_Asking):
    """Stands in for a provider's index during a check and notes every question the scanner and the fixer ask it.

    The scan is a function of the code and of these answers, and the fix plan of the code, the impacts and
    these answers. An index that answers them all the same way would therefore find the same call sites and,
    for the same impacts, plan the same fixes. The questions are the provider's (``Profile.questions``), each
    noted with its arguments; anything else that is reached for, or a question asked another way (keywords,
    another number of arguments, an argument that is not plain data), is passed through and noted in
    ``untracked``, and such a check is exported as unusable.
    """

    def __init__(self, index: object, provider: str = "stripe") -> None:
        super().__init__(index, self, "")
        self.provider = provider
        self.questions = get_profile(provider).questions
        self.asked: dict[str, tuple[str, list]] = {}
        self.untracked: set[str] = set()

    @property
    def empty(self) -> bool:
        return self._target.empty

    def _reach(self, holder: object, dotted: str):
        target = getattr(holder, dotted.rpartition(".")[2])
        if dotted in self.questions:
            return self._asker(target, dotted)
        if any(question.startswith(dotted + ".") for question in self.questions):
            return None if target is None else _Asking(target, self, dotted + ".")
        self.untracked.add(dotted)
        return target

    def _asker(self, target, dotted: str):
        def ask(*args, **kwargs):
            try:
                if kwargs or len(args) != self.questions[dotted]:
                    raise TypeError(dotted)
                plain = [_plain(arg) for arg in args]
                self.asked.setdefault(json.dumps([dotted, plain]), (dotted, plain))
            except TypeError:
                self.untracked.add(dotted)
            return target(*args, **kwargs)

        return ask


def sent_value(value: str | None) -> str | None:
    """A value a call sends, as the index keeps it."""
    return value if value is None or value.startswith("~") else "#" + name_hash(value)


def _sent(usage: Usage) -> list | None:
    return None if usage.values is None else sorted({(f, sent_value(v)) for f, v in usage.values}, key=json.dumps)


def export(result: ScanResult, recorder: Recorder) -> dict:
    """The check as plain data for the parent: every call site in scan order with the hashed reads that go with
    it, and the questions the scan and the fixer asked the provider's index."""
    reads: dict[frozenset[str], int] = {}
    sites = [
        {
            "file": u.file, "line": u.line, "method": u.method, "path": u.path, "call": u.call, "language": u.language,
            "complete": u.complete, "fields": sorted(u.fields), "hidden": None if u.hidden is None else sorted(u.hidden),
            "reads": reads.setdefault(u.reads, len(reads)), "sent": _sent(u),
        }
        for u in result.usages
    ]
    return {
        "format": FORMAT, "provider": recorder.provider, "unusable": bool(recorder.untracked), "sites": sites,
        "reads": [sorted({name_hash(n) for n in names}) for names in reads],
        "asked": [[name, args] for name, args in recorder.asked.values()],
    }


# --- the parent: validate ------------------------------------------------------------------


@dataclass(frozen=True)
class Index:
    provider: str
    usages: tuple[Usage, ...]  # scan order; ``reads`` are HashedNames
    reads: tuple[tuple[str, ...], ...]  # the distinct sets of hashes
    site_reads: tuple[int, ...]  # per call site, which of ``reads`` goes with it
    asked: tuple[tuple[str, list], ...]  # (question, its arguments as JSON) asked of the provider's index


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


def _operation(method: object, path: object) -> tuple[str, str]:
    """A call site's operation, its shape only: that the provider's specs have it is ``indexcheck.record``'s check."""
    if not isinstance(method, str) or not _METHOD_RE.fullmatch(method):
        raise Invalid("method is not an HTTP method")
    if not isinstance(path, str) or len(path) > MAX_FIELD or not _SPEC_PATH_RE.fullmatch(path):
        raise Invalid("path is not an API path")
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
    method, path = _operation(site.get("method"), site.get("path"))
    if not isinstance(complete, bool):
        raise Invalid("complete is not a boolean")
    if isinstance(which, bool) or not isinstance(which, int) or not 0 <= which < len(reads):
        raise Invalid("a call site names no set of reads")
    call, language = _text(site.get("call"), MAX_CALL, "call"), site.get("language")
    if not isinstance(language, str) or not _LANGUAGE_RE.fullmatch(language):
        raise Invalid("language is not a language name")
    fields = _names(site.get("fields"), "fields")
    hidden = None if site.get("hidden") is None else _names(site["hidden"], "hidden", empty=True)
    sent = _values(site.get("sent"), budget)
    budget.take(file, call, *fields, *(hidden or ()))
    return Usage(
        file, line, method, path, call, frozenset(fields), complete, language, reads[which],
        None if hidden is None else frozenset(hidden), sent,
    )


def _values(raw: object, budget: _Budget) -> frozenset[tuple[str, str | None]] | None:
    """``sent``: None, or distinct [field, value] pairs, each value None, a hash (``#``) or an enum member (``~``)."""
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) > MAX_FIELDS:
        raise Invalid("sent values: not a list within the cap")
    pairs = set()
    for item in raw:
        if not isinstance(item, list) or len(item) != 2:
            raise Invalid("a sent value is not a field and a value")
        field, value = _text(item[0], MAX_FIELD, "a sent field"), item[1]
        if value is not None and not (isinstance(value, str) and _SENT_RE.fullmatch(value)):
            value = _text(value, MAX_FIELD, "a sent value")
            if not value.startswith("~"):
                raise Invalid("a sent value is kept in clear")
        budget.take(field, value or "")
        pairs.add((field, value))
    if len(pairs) != len(raw):
        raise Invalid("sent values: duplicates")
    return frozenset(pairs)


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


def _scalar(value: object, budget: _Budget) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -MAX_LINE <= value <= MAX_LINE:
            raise Invalid("a question's number is out of range")
        return value
    budget.take(_text(value, MAX_URL, "a question's argument", empty=True))
    return value


def _question(item: object, questions: dict[str, int], budget: _Budget) -> tuple[str, list]:
    """One question to the provider's index: one it answers, with as many arguments as it takes, each a name,
    a number, or a list of those."""
    if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str) or item[0] not in questions:
        raise Invalid("a question is not one the provider's index answers")
    args = item[1]
    if not isinstance(args, list) or len(args) != questions[item[0]]:
        raise Invalid("a question has the wrong number of arguments")
    out: list = []
    for arg in args:
        if not isinstance(arg, list):
            out.append(_scalar(arg, budget))
        elif len(arg) > MAX_FIELDS or any(isinstance(part, list) for part in arg):
            raise Invalid("a question's list is over the cap or nested")
        else:
            out.append([_scalar(part, budget) for part in arg])
    return item[0], out


def parse(raw: object, provider: str = "stripe") -> Index:
    """The child's export as trusted-shape data, or Invalid. Nothing is repaired or cut to fit."""
    if not isinstance(raw, dict) or isinstance(raw.get("format"), bool) or raw.get("format") != FORMAT:
        raise Invalid("the check carried no index in this format")
    if raw.get("provider") != provider:
        raise Invalid("the index is for another provider")
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
    asked = raw.get("asked")
    if not isinstance(asked, list) or len(asked) > MAX_ASKED:
        raise Invalid("asked is not a list within the cap")
    questions = get_profile(provider).questions
    return Index(provider, usages, reads, which, tuple(_question(item, questions, budget) for item in asked))


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
    provider: str
    usages: tuple[Usage, ...]
    answers: list  # [[question, arguments, answer]] as ``indexcheck.answers`` gives them
    unread: dict[str, int]
    impacts: str  # ``impacts_digest`` of the check that wrote the index: what the repo's issue was last written from


def impacts_digest(impacts: list[dict]) -> str:
    """A hash of a check's whole impact list (every certainty, documentation matches included)."""
    return hashlib.sha256(json.dumps(impacts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def store(
    conn: sqlite3.Connection, repo_id: int, sha: str, job_id: int | None, index: Index, answers: list,
    unread: dict[str, int], impacts: str, checked: str,
) -> None:
    """Replace the repo's index for ``index.provider`` with ``index`` (validated), marked as checked against
    snapshot ``checked``."""
    extras = json.dumps(
        {"reads": ["".join(hashes) for hashes in index.reads], "site_reads": list(index.site_reads), "answers": answers,
         "unread": unread, "impacts": impacts},
        separators=(",", ":"),
    )
    rows = [
        (repo_id, index.provider, seq, u.file, u.line, u.method, u.path, u.call, u.language, int(u.complete), json.dumps(sorted(u.fields)),
         None if u.hidden is None else json.dumps(sorted(u.hidden)), None if u.values is None else json.dumps(sorted(map(list, u.values), key=json.dumps)))
        for seq, u in enumerate(index.usages)
    ]
    ts = db.now()
    with db.write(conn):
        _delete(conn, repo_id, index.provider)
        conn.execute(
            "INSERT INTO repo_index (repo_id, provider, sha, format, scanner, job_id, extras, written_at, checked,"
            " checked_at, checked_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'full')",
            (repo_id, index.provider, sha, FORMAT, scanner_digest(), job_id, extras, ts, checked, ts),
        )
        conn.executemany("INSERT INTO index_sites VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def get(conn: sqlite3.Connection, repo_id: int, provider: str = "stripe") -> db.Row | None:
    """The index header of a repo for one provider: sha, format, scanner, job_id, written_at, checked, checked_at,
    checked_by."""
    return conn.execute("SELECT * FROM repo_index WHERE repo_id = ? AND provider = ?", (repo_id, provider)).fetchone()


def sites(conn: sqlite3.Connection, repo_id: int, provider: str = "stripe") -> list[db.Row]:
    return conn.execute(
        "SELECT * FROM index_sites WHERE repo_id = ? AND provider = ? ORDER BY seq", (repo_id, provider)
    ).fetchall()


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
                None if row["sent"] is None else frozenset(tuple(pair) for pair in json.loads(row["sent"])),
            )
            for row in sites(conn, header["repo_id"], header["provider"])
        )
        return Stored(header["provider"], usages, extras["answers"], extras["unread"], str(extras["impacts"]))
    except (ValueError, KeyError, TypeError, AttributeError):
        logger.warning("repo %s: the stored index does not read back", header["repo_id"])
        return None


def _delete(conn: sqlite3.Connection, repo_id: int, provider: str | None) -> None:
    for table in ("index_sites", "repo_index"):
        if provider is None:
            conn.execute(f"DELETE FROM {table} WHERE repo_id = ?", (repo_id,))
        else:
            conn.execute(f"DELETE FROM {table} WHERE repo_id = ? AND provider = ?", (repo_id, provider))


def drop(conn: sqlite3.Connection, repo_id: int, provider: str | None = None) -> None:
    """Drop the repo's index for ``provider``, or for every provider."""
    with db.write(conn):
        _delete(conn, repo_id, provider)


def mark_checked(conn: sqlite3.Connection, repo_id: int, snapshot: str, how: str = "index", provider: str = "stripe") -> None:
    with db.write(conn):
        conn.execute(
            "UPDATE repo_index SET checked = ?, checked_at = ?, checked_by = ? WHERE repo_id = ? AND provider = ?",
            (snapshot, db.now(), how, repo_id, provider),
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
