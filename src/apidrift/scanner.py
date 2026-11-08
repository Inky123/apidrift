"""Find the Stripe calls in a checkout: which endpoint, which request fields, where.

Python files go through :mod:`pyparse` (``ast``), JavaScript, TypeScript and
Ruby through :mod:`jsparse`, PHP through :mod:`phpparse`, Go (stripe-go) through :mod:`goparse`. Every text file is also
searched for literal ``api.stripe.com/v1/...`` URLs, which identify the
endpoint but not the fields; in documentation, fixtures and tests (mostly
HTTP stubs) those are tagged ``language="doc"`` and reported separately.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import crosstable, goparse, jsparse, phpparse, pyparse
from .sdkmap import Resolved, SdkIndex

SKIP_DIRS = {"node_modules", "vendor", "site-packages", "__pycache__"}  # skipped anywhere
BUILD_DIRS = {"dist", "build", "coverage", "target", "tmp"}  # skipped at the repo root or next to a project file
PROJECT_FILES = {
    "package.json", "pyproject.toml", "setup.py", "Gemfile", "Cargo.toml", "go.mod", "pom.xml", "composer.json",
    "tsconfig.json",
}
SKIP_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "Gemfile.lock"}
LANGUAGES = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".rb": "ruby", ".rake": "ruby",
    ".php": "php",
    ".go": "go",
}
TEXT_EXT = set(LANGUAGES) | {
    ".go", ".java", ".kt", ".cs", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".json", ".toml", ".env",
    ".http", ".rs", ".swift", ".dart", ".ex", ".exs", ".scala", ".vue", ".svelte", ".html", ".erb", ".md", ".txt",
    ".cfg", ".ini", ".conf", ".graphql", ".sql",
}
# Server-side languages whose call sites are not parsed yet: files in them that mention Stripe are counted
# so the report can say so instead of passing them off as clean. Literal api.stripe.com URLs are still
# found. Swift, Kotlin and Dart are left out: those are Stripe's mobile SDKs, which talk to a different,
# publishable-key surface rather than the server endpoints this diff covers.
UNREAD_LANGUAGES = {
    ".java": "Java", ".cs": "C#", ".rs": "Rust", ".ex": "Elixir", ".exs": "Elixir",
    ".scala": "Scala",
}
DOC_EXT = {".md", ".txt", ".yml", ".yaml", ".json", ".env"}
DOC_DIRS = {"docs", "doc", "fixtures", "__fixtures__", "cassettes"}
TEST_DIRS = {"spec", "specs", "test", "tests", "__tests__", "__mocks__", "e2e", "cypress"}
TEST_FILE = re.compile(r"(?:^test_.*\.py|.*_test\.py|.*_spec\.rb|.*_test\.rb|.*_test\.go|.*\.(?:test|spec)\.[cm]?[jt]sx?)$")
MAX_BYTES = 1_500_000
STRIPE_HINT = re.compile(r"stripe", re.I)
URL_RE = re.compile(r"api\.stripe\.com(/v\d+(?:/[^\s'\"`<>)\],;?]+)+)")
VERB_RE = re.compile(r"\b(get|post|delete)\b", re.I)
METHOD_RE = re.compile(r"(?:method\s*[:=]\s*['\"]?|-X\s*['\"]?|--request\s+['\"]?)(get|post|delete)", re.I)
MEMBER_RE = re.compile(r"\.\s*([A-Za-z_]\w*)|\[\s*[\"']([A-Za-z_]\w*)[\"']\s*\]")
PHP_MEMBER_RE = re.compile(r"->\s*([A-Za-z_]\w*)(?!\w|\s*\()|\[\s*[\"']([A-Za-z_]\w*)[\"']\s*\]")
GENERIC_RECEIVERS = {
    "event", "evt", "req", "request", "res", "response", "body", "params", "options", "opts", "ctx", "this", "self",
    "args", "kwargs", "process", "env",
}
GENERIC_NOT_READS = {  # language keywords and library methods: never a response field
    "then", "catch", "finally", "map", "filter", "reduce", "find", "some", "every", "push", "pop", "slice", "splice",
    "concat", "join", "split", "length", "json", "text", "send", "end", "redirect", "render", "prototype",
    "constructor", "bind", "call", "apply", "log", "error", "warn", "info", "stringify", "parse", "assign", "keys",
    "values", "entries", "freeze", "from", "of", "get", "set", "has", "add", "delete", "append", "format", "strip",
    "lower", "upper", "encode", "decode", "read", "write", "close", "open", "exit", "cwd", "argv", "env", "each",
    "to_s", "to_json", "to_h", "nil", "new", "require", "exports", "module", "default", "items", "exists", "path",
    "resolve", "reject", "listen", "use", "route", "post", "put", "patch", "all", "static", "raw", "headers",
    "header", "query", "cookies", "include", "includes", "index", "replace", "trim", "match", "test", "exec", "now",
}
NOT_READS = GENERIC_NOT_READS | {  # ... and the Stripe client surface
    "create", "retrieve", "update", "modify", "list", "del", "search", "confirm", "capture", "cancel", "attach",
    "detach", "construct_event", "api_key", "api_version", "v1", "v2", "checkout", "sessions", "customers",
    "subscriptions", "invoices", "prices", "products", "charges", "refunds", "accounts", "events", "webhooks",
    "payment_intents", "setup_intents", "payment_methods", "billing_portal", "financial_connections",
}


@dataclass(frozen=True)
class Usage:
    file: str
    line: int
    method: str  # http method, or "?" when only the URL was seen
    path: str
    call: str
    fields: frozenset[str]
    complete: bool  # False when spreads, **kwargs or variables hide some fields
    language: str  # python / javascript / typescript / ruby / http, or "doc" for URLs in documentation
    reads: frozenset[str] = frozenset()  # member names read in the same file off a plausible response object
    hidden: frozenset[str] | None = None  # prefixes ("" = top level) where unseen keys may be sent; None: all if not complete
    # (field, value) for plain values the call sends, value None where it is not a visible literal ("~name": an
    # SDK enum member). None: the reader does not collect values, and a sent field is judged without them.
    values: frozenset[tuple[str, str | None]] | None = None

    def hides(self, path: str) -> bool:
        """Whether ``path`` could be sent here without being visible (a spread, ``**kwargs`` or variable)."""
        if self.hidden is None:
            return not self.complete
        return any(path.startswith(prefix) for prefix in self.hidden)

    @property
    def label(self) -> str:
        return f"{self.method.upper()} {self.path}"

    @property
    def where(self) -> str:
        return f"{self.file}:{self.line}"

    def to_dict(self) -> dict:
        return {
            "file": self.file, "line": self.line, "method": self.method, "path": self.path, "call": self.call,
            "fields": sorted(self.fields), "complete": self.complete, "language": self.language,
        }


@dataclass(frozen=True)
class Reader:
    """What is vendor-specific in a scan: which files are worth reading, how one file is read, and what the
    report calls things. The default reads Stripe; :mod:`profiles` holds the other vendors' readers."""

    vendor: str = "Stripe"
    hint: re.Pattern[str] = STRIPE_HINT  # a file that does not match is not read
    scan_text: Callable | None = None  # (text, rel, index) -> (usages, unresolved, unread language); None: scan_text
    others: str = "Stripe.js, webhooks, SDK helpers"  # what calls that reach no endpoint usually are
    others_are: str = "not API endpoints"


@dataclass
class ScanResult:
    root: str
    usages: list[Usage]
    files_scanned: int
    unresolved: list[tuple[str, int, str]]  # stripe-looking calls that matched no endpoint
    skipped_files: int = 0  # oversize or binary files left unread
    skipped_dirs: int = 0  # dependency and build directories left unread
    unread: dict[str, int] = field(default_factory=dict)  # language -> files mentioning Stripe in languages not parsed
    reader: Reader = Reader()  # the vendor the scan was for
    table: crosstable.Table | None = field(default=None, repr=False, compare=False)  # wrappers followed across files

    @property
    def counted(self) -> list[Usage]:
        """Call sites in code; URL matches in documentation and fixtures are reported but not counted."""
        return [u for u in self.usages if u.language != "doc"]

    @property
    def docs(self) -> list[Usage]:
        return [u for u in self.usages if u.language == "doc"]

    def files_with_usages(self) -> int:
        return len({u.file for u in self.counted})

    def by_language(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for u in self.counted:
            counts[u.language] = counts.get(u.language, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def scan_repo(root: Path, index: SdkIndex, reader: Reader | None = None) -> ScanResult:
    """Scan a checkout for one vendor's calls: Stripe's unless ``reader`` (with an index of its own kind) says otherwise."""
    reader = reader or Reader()
    read = reader.scan_text or scan_text
    root = Path(root).resolve()
    if not root.exists():
        raise SystemExit(f"apidrift: {root} does not exist")
    if not (root.is_file() or root.is_dir()):
        raise SystemExit(f"apidrift: {root} is not a file or directory")
    if root.is_file():
        files, base, skipped_files, skipped_dirs = [root], root.parent, 0, 0
    else:
        files, skipped_files, skipped_dirs = _iter_files(root)
        base = root
    # Stripe's own reader follows wrappers across files (crosstable): a first pass notes them, then the
    # files that call them, or define one whose callers are all visible, are read again with the table.
    table = crosstable.Table() if reader.scan_text is None and root.is_dir() else None
    if table is not None:
        read = lambda text, rel, index: scan_text(text, rel, index, table)  # noqa: E731
    per_file: dict[str, tuple[list[Usage], list[tuple[str, int, str]]]] = {}
    unread: dict[str, int] = {}
    for path in files:
        text = _read(path)
        if text is None:
            skipped_files += 1
            continue
        if not reader.hint.search(text):
            continue
        found, missing, unread_name = read(text, path.relative_to(base).as_posix(), index)
        if unread_name:
            unread[unread_name] = unread.get(unread_name, 0) + 1
        per_file[path.relative_to(base).as_posix()] = (found, missing)
    if table is not None:
        crosstable.finish(table, base, files, lambda chain: _resolve(index, chain))
        for rel in sorted(table.rescan):
            text = _read(base / rel)
            if text is not None:
                per_file[rel] = scan_text(text, rel, index, table)[:2]
    usages = [u for found, _ in per_file.values() for u in found]
    unresolved = [entry for _, missing in per_file.values() for entry in missing]
    usages.sort(key=lambda u: (u.file, u.line, u.path))
    return ScanResult(
        str(root), usages, len(per_file), unresolved, skipped_files, skipped_dirs, dict(sorted(unread.items())), reader,
        table,
    )


def scan_text(
    text: str, rel: str, index: SdkIndex, table: crosstable.Table | None = None
) -> tuple[list[Usage], list[tuple[str, int, str]], str | None]:
    """Usages and unresolved calls of one file's text at repo-relative path ``rel``, with no filesystem.

    The third item names the language when the file is in one the scanner cannot parse yet (else None).
    ``table``: the repository's wrappers followed across files (:mod:`crosstable`), from :func:`scan_repo`.
    """
    suffix = os.path.splitext(rel)[1].lower()
    language = LANGUAGES.get(suffix)
    reads = _reads(text, PHP_MEMBER_RE if language == "php" else MEMBER_RE)
    found: list[Usage] = []
    unread_name: str | None = None
    if language == "python":
        found, missing = _scan_python(text, rel, index, reads)
    elif language == "go":
        found, missing, unread_name = _scan_go(text, rel, index)
    elif language:
        found, missing = _scan_dynamic(text, rel, index, reads, language, table)
    else:
        missing = []
        unread_name = UNREAD_LANGUAGES.get(suffix)
    url_language = "doc" if _doc_like(rel, suffix) else "http"
    found += _scan_urls(text, rel, index, reads, {u.line for u in found}, url_language)
    return found, missing, unread_name


def unread_sentence(unread: dict[str, int], vendor: str = "Stripe") -> str:
    """Plain-language note for languages whose call sites are not parsed, e.g. for Go and PHP files."""
    if not unread:
        return ""
    parts = [f"{n} {lang} file{'s' if n != 1 else ''}" for lang, n in unread.items()]
    listed = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    langs = list(unread)
    names = langs[0] if len(langs) == 1 else ", ".join(langs[:-1]) + " or " + langs[-1]
    return f"apidrift does not read {names} call sites yet: {listed} mention {vendor} and were not checked."


def _iter_files(root: Path, suffixes: set[str] | None = None) -> tuple[list[Path], int, int]:
    """The files a scan reads (``suffixes``: of these extensions instead of every text file), with the
    counts of files and directories left out. Every walk over a checkout goes through here, so the same
    files are read on every machine whatever is installed (dependency and build directories are skipped)."""
    suffixes = TEXT_EXT if suffixes is None else suffixes
    files: list[Path] = []
    skipped_files = skipped_dirs = 0
    for dirpath, dirs, names in os.walk(root):
        here, present = Path(dirpath), set(names)
        keep = []
        for d in sorted(dirs):
            if d.startswith("."):
                continue
            if d in SKIP_DIRS or (d in BUILD_DIRS and (here == root or present & PROJECT_FILES)):
                skipped_dirs += 1
                continue
            keep.append(d)
        dirs[:] = keep
        for name in sorted(names):
            path = here / name
            if name in SKIP_NAMES or name.endswith((".min.js", ".map")) or path.suffix.lower() not in suffixes:
                continue
            if path.is_symlink():
                continue
            if path.stat().st_size > MAX_BYTES:
                skipped_files += 1
                continue
            files.append(path)
    return files, skipped_files, skipped_dirs


def _read(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="replace")


def _doc_like(rel: str, suffix: str) -> bool:
    *dirs, name = rel.split("/")
    return suffix in DOC_EXT or any(part in DOC_DIRS or part in TEST_DIRS for part in dirs) or bool(TEST_FILE.match(name))


def _reads(
    text: str, member: re.Pattern[str] = MEMBER_RE, skip: frozenset[str] | set[str] = NOT_READS, vendor: str = "stripe"
) -> frozenset[str]:
    """Member names read off something that could be an API response (not ``req.body.x`` or ``event.type``)."""
    out: set[str] = set()
    for m in member.finditer(text):
        name = m.group(1) or m.group(2)
        if name in skip or name[0].isupper() or vendor in name.lower() or re.search(r"[a-z][A-Z]", name):
            continue
        receiver = _receiver(text, m.start())
        if (receiver.lstrip("$") if member is PHP_MEMBER_RE else receiver) in GENERIC_RECEIVERS:
            continue
        out.add(name)
    return frozenset(out)


def _receiver(text: str, i: int) -> str:
    """Identifier immediately before a member access at ``i`` ("" after ``)`` or ``]``)."""
    j = i
    while j > 0 and text[j - 1] in " \t?!":
        j -= 1
    k = j
    while k > 0 and (text[k - 1].isalnum() or text[k - 1] in "_$"):
        k -= 1
    return text[k:j]


def _resolve(index: SdkIndex, chain: list[str]) -> Resolved | None:
    """``index.resolve`` with one guard: a chain through ``v2`` must land on a ``/v2/`` path."""
    resolved = index.resolve(chain)
    if resolved is not None and "v2" in chain[:-1] and not resolved.path.startswith("/v2/"):
        return None
    return resolved


# --- Python -----------------------------------------------------------------


def _scan_python(text: str, rel: str, index: SdkIndex, reads: frozenset[str]):
    found, missing = pyparse.extract(text, lambda chain: _resolve(index, chain))
    usages = [
        Usage(rel, f.line, f.resolved.method, f.resolved.path, f.call, frozenset(f.fields), f.complete, "python", reads, f.hidden)
        for f in found
    ]
    return usages, [(rel, line, call) for line, call in missing]


# --- Go -----------------------------------------------------------------------


def _scan_go(text: str, rel: str, index: SdkIndex):
    if index.go is None:
        return [], [], None
    try:
        found, missing, reads = goparse.extract(text, index.go)
    except (IndexError, RecursionError):  # source the tokeniser's assumptions do not hold for: say it was not read
        return [], [], "Go"
    usages = [
        Usage(rel, f.line, f.resolved.method, f.resolved.path, f.call, frozenset(f.fields), f.complete, "go", reads, f.hidden)
        for f in found
    ]
    return usages, [(rel, line, call) for line, call in missing], None


# --- JavaScript / TypeScript / Ruby / PHP ---------------------------------------


def _scan_dynamic(text: str, rel: str, index: SdkIndex, reads: frozenset[str], language: str, table=None):
    resolve = lambda chain: _resolve(index, chain)  # noqa: E731
    if language == "php":
        found, missing = phpparse.extract(text, resolve, table, rel)
    else:
        found, missing = jsparse.extract(text, index, ruby=language == "ruby", resolve=resolve, table=table, rel=rel)
    usages = [
        Usage(rel, f.line, f.resolved.method, f.resolved.path, f.call, frozenset(f.fields.paths), f.fields.complete,
              language, reads, frozenset(f.fields.hidden))
        for f in found
    ]
    return usages, [(rel, line, call) for line, call in missing]


# --- Literal URLs -------------------------------------------------------------


def _scan_urls(
    text: str, rel: str, index: SdkIndex, reads: frozenset[str], taken: set[int], language: str = "http"
) -> list[Usage]:
    usages: list[Usage] = []
    for m in URL_RE.finditer(text):
        spec_path = index.match_path(m.group(1))
        if spec_path is None:
            continue
        line = text.count("\n", 0, m.start()) + 1
        if line in taken:
            continue
        method = _method_hint(text, m.start(), m.end(), spec_path, index)
        usages.append(
            Usage(rel, line, method, spec_path, "api.stripe.com" + m.group(1), frozenset(), False, language, reads)
        )
    return usages


def _method_hint(text: str, start: int, end: int, spec_path: str, index: SdkIndex) -> str:
    """HTTP method named near a literal URL: an explicit ``method:`` option, else the last verb before it."""
    before, after = text[max(0, start - 160): start], text[end: end + 160]
    explicit = METHOD_RE.search(before) or METHOD_RE.search(after)
    if explicit:
        method = explicit.group(1).lower()
    else:
        verbs = VERB_RE.findall(before)
        method = verbs[-1].lower() if verbs else "?"
    available = index.methods_for(spec_path)
    if method not in available:
        method = available[0] if len(available) == 1 else "?"
    return method


# --- Rendering ---------------------------------------------------------------


def render_scan_text(result: ScanResult) -> str:
    counted, docs = result.counted, result.docs
    words, vendor = result.reader, result.reader.vendor.lower()
    langs = ", ".join(f"{lang} {n}" for lang, n in result.by_language().items()) or "none"
    summary = (
        f"{len(counted)} {words.vendor} call sites in {result.files_with_usages()} files ({langs}); "
        f"{result.files_scanned} files mention {vendor}; {len(result.unresolved)} other {vendor} calls ({words.others_are})"
    )
    if docs:
        summary += f"; {len(docs)} URLs in documentation, fixtures and tests (not counted)"
    lines = [f"apidrift scan: {result.root}", summary]
    if result.skipped_files or result.skipped_dirs:
        lines.append(
            f"skipped: {result.skipped_files} oversize or binary files, "
            f"{result.skipped_dirs} dependency or build directories"
        )
    if result.unread:
        lines.append("not read: " + unread_sentence(result.unread, words.vendor))
    for u in counted:
        lines.append(f"{u.where}  {u.label}  via {u.call}")
        shown = ", ".join(sorted(u.fields)[:8])
        if len(u.fields) > 8:
            shown += f" (+{len(u.fields) - 8} more)"
        if not u.complete:
            shown += (" " if shown else "") + "(+ fields not visible)"
        if shown:
            lines.append(f"    fields: {shown}")
    if docs:
        lines += ["", "documentation, fixtures and tests (not counted):"]
        lines += [f"  {u.where}  {u.label}  via {u.call}" for u in docs]
    if result.unresolved:
        lines += ["", f"other {vendor} calls ({words.others}), {words.others_are}:"]
        lines += [f"  {f}:{line}  {call}" for f, line, call in result.unresolved[:15]]
        if len(result.unresolved) > 15:
            lines.append(f"  (+{len(result.unresolved) - 15} more)")
    return "\n".join(lines)


def render_scan_json(result: ScanResult) -> str:
    payload = {
        "root": result.root,
        "files_scanned": result.files_scanned,
        "skipped_files": result.skipped_files,
        "skipped_dirs": result.skipped_dirs,
        "unread": result.unread,
        "usages": [u.to_dict() for u in result.usages],
        "unresolved": [{"file": f, "line": line, "call": call} for f, line, call in result.unresolved],
    }
    return json.dumps(payload, indent=2)
