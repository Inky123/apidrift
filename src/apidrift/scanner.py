"""Find the Stripe calls in a checkout: which endpoint, which request fields, where.

Python files are read with ``ast`` (imports resolved, keyword arguments and
dict literals flattened). JavaScript, TypeScript and Ruby go through
:mod:`jsparse`. Every text file is also searched for literal
``api.stripe.com/v1/...`` URLs, which identify the endpoint but not the fields.
"""

from __future__ import annotations

import ast
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import jsparse
from .sdkmap import SdkIndex

SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "coverage", "site-packages", "__pycache__", "target", "tmp"}
SKIP_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock", "Gemfile.lock"}
LANGUAGES = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".rb": "ruby",
}
TEXT_EXT = set(LANGUAGES) | {
    ".go", ".php", ".java", ".kt", ".cs", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".json", ".toml", ".env",
    ".http", ".rs", ".swift", ".dart", ".ex", ".exs", ".scala", ".vue", ".svelte", ".html", ".erb", ".md", ".txt",
    ".cfg", ".ini", ".conf", ".graphql", ".sql",
}
MAX_BYTES = 1_500_000
OPTION_KWARGS = {
    "api_key", "stripe_account", "stripe_version", "idempotency_key", "api_version", "options", "timeout",
    "max_network_retries", "stripe_context",
}
STRIPE_HINT = re.compile(r"stripe", re.I)
URL_RE = re.compile(r"api\.stripe\.com(/v\d+(?:/[^\s'\"`<>)\],;?]+)+)")
VERB_RE = re.compile(r"\b(get|post|delete)\b", re.I)
METHOD_RE = re.compile(r"(?:method\s*[:=]\s*['\"]?|-X\s*['\"]?|--request\s+['\"]?)(get|post|delete)", re.I)
READ_RE = re.compile(r"\.([A-Za-z_]\w*)|\[\s*[\"']([A-Za-z_]\w*)[\"']\s*\]")


@dataclass(frozen=True)
class Usage:
    file: str
    line: int
    method: str  # http method, or "?" when only the URL was seen
    path: str
    call: str
    fields: frozenset[str]
    complete: bool  # False when spreads, **kwargs or variables hide some fields
    language: str
    reads: frozenset[str] = frozenset()  # member names read anywhere in the same file

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


@dataclass
class ScanResult:
    root: str
    usages: list[Usage]
    files_scanned: int
    unresolved: list[tuple[str, int, str]]  # stripe-looking calls that matched no endpoint

    def files_with_usages(self) -> int:
        return len({u.file for u in self.usages})

    def by_language(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for u in self.usages:
            counts[u.language] = counts.get(u.language, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def scan_repo(root: Path, index: SdkIndex) -> ScanResult:
    root = Path(root).resolve()
    files, base = ([root], root.parent) if root.is_file() else (list(_iter_files(root)), root)
    usages: list[Usage] = []
    unresolved: list[tuple[str, int, str]] = []
    scanned = 0
    for path in files:
        text = _read(path)
        if text is None or not STRIPE_HINT.search(text):
            continue
        scanned += 1
        rel = path.relative_to(base).as_posix()
        reads = frozenset(a or b for a, b in READ_RE.findall(text))
        language = LANGUAGES.get(path.suffix.lower())
        found: list[Usage] = []
        if language == "python":
            found, missing = _scan_python(text, rel, index, reads)
        elif language:
            found, missing = _scan_dynamic(text, rel, index, reads, language)
        else:
            missing = []
        unresolved += missing
        found += _scan_urls(text, rel, index, reads, {u.line for u in found})
        usages += found
    usages.sort(key=lambda u: (u.file, u.line, u.path))
    return ScanResult(str(root), usages, scanned, unresolved)


def _iter_files(root: Path):
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            path = Path(dirpath) / name
            if name in SKIP_NAMES or name.endswith((".min.js", ".map")) or path.suffix.lower() not in TEXT_EXT:
                continue
            if path.is_symlink() or path.stat().st_size > MAX_BYTES:
                continue
            yield path


def _read(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="replace")


# --- Python -----------------------------------------------------------------


def _scan_python(text: str, rel: str, index: SdkIndex, reads: frozenset[str]):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return [], []
    aliases = _import_aliases(tree)
    assigned = _assigned_dicts(tree)
    usages: list[Usage] = []
    unresolved: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        chain = _chain(node.func)
        if not chain:
            continue
        if chain[0] in aliases:
            chain = aliases[chain[0]].split(".") + chain[1:]
        if not _stripe_like(chain):
            continue
        call = ".".join(chain)
        resolved = index.resolve(chain)
        if resolved is None:
            unresolved.append((rel, node.lineno, call))
            continue
        fields, complete = _py_fields(node, assigned)
        usages.append(
            Usage(rel, node.lineno, resolved.method, resolved.path, call, frozenset(fields), complete, "python", reads)
        )
    return usages, unresolved


def _stripe_like(chain: list[str]) -> bool:
    return any("stripe" in part.lower() for part in chain) or any(part in ("v1", "v2") for part in chain[1:-1])


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "stripe" or alias.name.startswith("stripe."):
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else "stripe"
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "stripe":
            for alias in node.names:
                if alias.name != "*":
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


def _assigned_dicts(tree: ast.AST) -> dict[str, ast.expr]:
    """Variable name -> the last dict literal (or ``dict(...)`` call) assigned to it."""
    out: dict[str, ast.expr] = {}
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        value = node.value
        if _as_dict(value, {}) is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                out[target.id] = value
    return out


def _chain(node: ast.expr) -> list[str] | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return [node.id] + parts[::-1]
    if isinstance(node, ast.Call):
        inner = _chain(node.func)
        return inner + parts[::-1] if inner else None
    return None


def _as_dict(node: ast.expr | None, assigned: dict[str, ast.expr]):
    """Return a node whose keys we can read: a dict literal, ``dict(k=v)`` call, or a variable bound to one."""
    if isinstance(node, ast.Dict):
        return node
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict" and not node.args:
        return node
    if isinstance(node, ast.Name) and node.id in assigned:
        return assigned[node.id]
    return None


def _py_fields(call: ast.Call, assigned: dict[str, ast.expr]) -> tuple[set[str], bool]:
    fields: set[str] = set()
    complete = True
    for kw in call.keywords:
        if kw.arg is None or kw.arg == "params":
            node = _as_dict(kw.value, assigned)
            complete = _dict_fields(node, "", fields, assigned) if node is not None else False
            continue
        if kw.arg in OPTION_KWARGS:
            continue
        fields.add(kw.arg)
        complete &= _nested(kw.value, kw.arg, fields, assigned)
    for arg in call.args:
        if isinstance(arg, ast.Starred):
            complete = False
        elif isinstance(arg, ast.Dict):
            complete &= _dict_fields(arg, "", fields, assigned)
    return fields, complete


def _dict_fields(node: ast.expr, prefix: str, fields: set[str], assigned: dict[str, ast.expr]) -> bool:
    complete = True
    if isinstance(node, ast.Call):  # dict(amount=1, currency="usd")
        pairs = [(kw.arg, kw.value) for kw in node.keywords]
    else:
        pairs = [(k.value if isinstance(k, ast.Constant) else None, v) for k, v in zip(node.keys, node.values)]
    for key, value in pairs:
        if not isinstance(key, str):
            complete = False
            continue
        path = prefix + key
        fields.add(path)
        complete &= _nested(value, path, fields, assigned)
    return complete


def _nested(value: ast.expr, path: str, fields: set[str], assigned: dict[str, ast.expr]) -> bool:
    node = _as_dict(value, assigned)
    if node is not None:
        return _dict_fields(node, path + ".", fields, assigned)
    if isinstance(value, (ast.List, ast.Tuple)):
        complete = True
        for item in value.elts:
            inner = _as_dict(item, assigned)
            if inner is not None:
                complete &= _dict_fields(inner, path + "[].", fields, assigned)
            elif isinstance(item, ast.Starred):
                complete = False
        return complete
    return True


# --- JavaScript / TypeScript / Ruby -------------------------------------------


def _scan_dynamic(text: str, rel: str, index: SdkIndex, reads: frozenset[str], language: str):
    found, missing = jsparse.extract(text, index, ruby=language == "ruby")
    usages = [
        Usage(rel, f.line, f.resolved.method, f.resolved.path, f.call, frozenset(f.fields.paths), f.fields.complete,
              language, reads)
        for f in found
    ]
    return usages, [(rel, line, call) for line, call in missing]


# --- Literal URLs -------------------------------------------------------------


def _scan_urls(text: str, rel: str, index: SdkIndex, reads: frozenset[str], taken: set[int]) -> list[Usage]:
    usages: list[Usage] = []
    for m in URL_RE.finditer(text):
        spec_path = index.match_path(m.group(1))
        if spec_path is None:
            continue
        line = text.count("\n", 0, m.start()) + 1
        if line in taken:
            continue
        method = _method_hint(text, m.start(), m.end(), spec_path, index)
        usages.append(Usage(rel, line, method, spec_path, "api.stripe.com" + m.group(1), frozenset(), False, "http", reads))
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
    langs = ", ".join(f"{lang} {n}" for lang, n in result.by_language().items()) or "none"
    lines = [
        f"apidrift scan: {result.root}",
        f"{len(result.usages)} Stripe call sites in {result.files_with_usages()} files ({langs}); "
        f"{result.files_scanned} files mention stripe; {len(result.unresolved)} other stripe calls (not API endpoints)",
    ]
    for u in result.usages:
        lines.append(f"{u.where}  {u.label}  via {u.call}")
        shown = ", ".join(sorted(u.fields)[:8])
        if len(u.fields) > 8:
            shown += f" (+{len(u.fields) - 8} more)"
        if not u.complete:
            shown += (" " if shown else "") + "(+ fields not visible)"
        if shown:
            lines.append(f"    fields: {shown}")
    if result.unresolved:
        lines += ["", "other stripe calls (Stripe.js, webhooks, SDK helpers), not API endpoints:"]
        lines += [f"  {f}:{line}  {call}" for f, line, call in result.unresolved[:15]]
        if len(result.unresolved) > 15:
            lines.append(f"  (+{len(result.unresolved) - 15} more)")
    return "\n".join(lines)


def render_scan_json(result: ScanResult) -> str:
    payload = {
        "root": result.root,
        "files_scanned": result.files_scanned,
        "usages": [u.to_dict() for u in result.usages],
        "unresolved": [{"file": f, "line": line, "call": call} for f, line, call in result.unresolved],
    }
    return json.dumps(payload, indent=2)
