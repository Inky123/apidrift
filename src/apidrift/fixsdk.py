"""The Stripe SDK version a fixed file runs with, read from the project's lockfiles (never the network).

A renamed or reshaped field only helps once the request goes out under an API version that has it, and the
SDK decides that: stripe-node, -python, -php and -ruby send the API version they were built for unless the
code sets one (before node 12, python 6, php 12, ruby 9 they used the account's default), and stripe-go's
version is fixed per major and its structs only have the fields of that major. So the fix payload carries
the locked version of the SDK for each fixed file's language, from the nearest directory (the file's own,
then each parent up to the checkout root) that resolves it:

- Ruby: Gemfile.lock. PHP: composer.lock. Go: go.mod ``require`` lines (never ``replace``), the major the
  fixed file imports when several are required.
- Node: package-lock.json (the copy Node resolves for the file: the nearest ``<dir>/node_modules/stripe``,
  never a dependency's nested copy), yarn.lock (v1 and berry), pnpm-lock.yaml (the importer holding the
  file), else an exact version in package.json. An alias of another package under the name is skipped.
- Python: poetry.lock, uv.lock, Pipfile.lock, else an exact ``stripe==`` pin in requirements.txt, else in
  another requirements*.txt only when there is no requirements.txt.

A lockfile that still resolves several versions of the SDK reports each. :data:`FIRST_RELEASE` is the evidence
for "supports": per API version, the first release of each SDK whose pinned version it is (CHANGELOG.md of
each repository; stripe-go's import path changes with it). The App renders a sentence from that table, never
from the child's text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

MAX_LOCK_BYTES = 40_000_000
MAX_LOCKS = 10
VERSION_SHAPE = re.compile(r"\d{1,4}(?:\.\d{1,6}){0,3}(?:[-+.][0-9A-Za-z][0-9A-Za-z.+-]{0,29})?")
SDK_BY_SUFFIX = {
    ".py": "stripe-python", ".rb": "stripe-ruby", ".php": "stripe-php", ".go": "stripe-go",
    **{s: "stripe-node" for s in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")},
}
SDKS = tuple(sorted(set(SDK_BY_SUFFIX.values())))
CHANGELOGS = {sdk: f"https://github.com/stripe/{sdk}/blob/master/CHANGELOG.md" for sdk in SDKS}
# The first release of each SDK pinned to the API version (its CHANGELOG.md: "changes the pinned API version").
FIRST_RELEASE: dict[str, dict[str, str]] = {
    "2026-09-30.endive": {
        "stripe-node": "23.0.0", "stripe-python": "16.0.0", "stripe-php": "22.0.0", "stripe-ruby": "20.0.0",
        "stripe-go": "87.0.0",
    },
}
GO_MODULE = "github.com/stripe/stripe-go"


@dataclass(frozen=True)
class Context:
    """What a reader may need about the fixed file: its path relative to the lockfile's directory (pnpm
    importers, npm workspaces) and, for go.mod, its imports."""

    sub: str = ""
    source: str = ""


@dataclass(frozen=True)
class SdkLock:
    sdk: str
    version: str
    file: str
    line: int


def older(sdk: str, version: str, api: str) -> bool | None:
    """Whether ``version`` of ``sdk`` predates its first release for ``api``; None when that is not known
    (a pre-release, an unusual version string, or an API version the table does not hold)."""
    first = FIRST_RELEASE.get(api, {}).get(sdk)
    have, need = _numbers(version), _numbers(first or "")
    if have is None or need is None:
        return None
    return have < need


def _numbers(version: str) -> tuple[int, int, int] | None:
    m = re.fullmatch(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?(\+incompatible)?", version)
    return tuple(int(g or 0) for g in m.groups()[:3]) if m else None  # type: ignore[return-value]


def find_sdks(root: Path, fixed: list[str]) -> list[SdkLock]:
    """The locked SDK version for each fixed file's language, nearest lockfile first (see the module doc).
    Lockfiles are repository bytes: a malformed one means "no lock reported", never a failed check."""
    try:
        return _find_sdks(root, fixed)
    except Exception:  # noqa: BLE001 - any reader failure (RecursionError, MemoryError too) is just "unknown"
        return []


def _find_sdks(root: Path, fixed: list[str]) -> list[SdkLock]:
    root = Path(root).resolve()
    if not fixed or not root.is_dir():
        return []
    out: list[SdkLock] = []
    for rel in sorted(fixed):
        sdk = SDK_BY_SUFFIX.get(Path(rel).suffix.lower())
        here = (root / rel).resolve().parent
        if sdk is None or (here != root and root not in here.parents):
            continue
        for d in [here, *here.parents]:
            found = _read_dir(root, d, sdk, root / rel)
            if found:
                out += [lock for lock in found if lock not in out]
                break
            if d == root:
                break
    return out[:MAX_LOCKS]


def _read_dir(root: Path, d: Path, sdk: str, fixed: Path) -> list[SdkLock]:
    sub = fixed.resolve().relative_to(d).as_posix()
    for name, reader in READERS[sdk]:
        if "*" in name:  # requirements-*.txt only when there is no requirements.txt at all
            paths = [] if (d / "requirements.txt").is_file() else sorted(d.glob(name))
        else:
            paths = [d / name]
        for path in paths:
            text = _text(path)
            if text is None:
                continue
            ctx = Context(sub, (_text(fixed) or "") if sdk == "stripe-go" else "")
            hits = [(v, line) for v, line in reader(text, ctx) if VERSION_SHAPE.fullmatch(v) and len(v) <= 40]
            if hits:
                rel = path.relative_to(root).as_posix()
                first = {v: line for v, line in reversed(hits)}  # one line per version (yarn patch entries repeat it)
                return [SdkLock(sdk, v, rel, first[v]) for v in dict.fromkeys(v for v, _ in hits)]
    return []


def _text(path: Path) -> str | None:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_LOCK_BYTES:
            return None
        return path.read_text("utf-8", errors="replace")
    except OSError:
        return None


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, max(pos, 0)) + 1


def _first_line(text: str, pattern: str) -> int:
    m = re.search(pattern, text)
    return _line(text, m.start()) if m else 1


def _json(text: str) -> dict:
    try:
        data = json.loads(text)
    except (ValueError, RecursionError, MemoryError):
        return {}
    return data if isinstance(data, dict) else {}


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


Hits = list[tuple[str, int]]


def _gemfile_lock(text: str, ctx: Context | None = None) -> Hits:
    return [(m.group(1), _line(text, m.start())) for m in re.finditer(r"(?m)^ {4}stripe \(([^)\s]+)\)[ \t]*$", text)]


def _composer_lock(text: str, ctx: Context | None = None) -> Hits:
    data, out = _json(text), []
    for key in ("packages", "packages-dev"):
        for pkg in _list(data.get(key)):
            if isinstance(pkg, dict) and pkg.get("name") == "stripe/stripe-php" and isinstance(pkg.get("version"), str):
                out.append((pkg["version"].removeprefix("v"), _first_line(text, r'"name"\s*:\s*"stripe/stripe-php"')))
    return out


GO_REQUIRE = re.compile(r"github\.com/stripe/stripe-go(?:/v(\d+))?[ \t]+v(\d[^\s]*)")
GO_IMPORT = re.compile(r'"github\.com/stripe/stripe-go(?:/v(\d+))?(?:/[^"]*)?"')


def _go_mod(text: str, ctx: Context | None = None) -> Hits:
    """``require`` lines only (a ``replace`` is not what is built against); with several stripe-go majors, the one
    the fixed file imports."""
    out: list[tuple[str, int, str]] = []  # (version, line, major)
    block, pos = None, 0
    for raw in text.split("\n"):
        line = raw.split("//", 1)[0].strip()
        if block is None and re.fullmatch(r"(require|replace|exclude|retract)\s*\(", line):
            block = line.split()[0].rstrip("(")
        elif block is not None and line == ")":
            block = None
        else:
            body = line[len("require"):].strip() if block is None and line.startswith("require ") else (
                line if block == "require" else "")
            m = GO_REQUIRE.fullmatch(body) if "=>" not in body else None
            if m:
                out.append((m.group(2), _line(text, pos), m.group(1) or "1"))
        pos += len(raw) + 1
    imported = {m.group(1) or "1" for m in GO_IMPORT.finditer(ctx.source if ctx else "")}
    mine = [h for h in out if h[2] in imported]
    return [(v, n) for v, n, _ in (mine or out)]


def _package_lock(text: str, ctx: Context | None = None) -> Hits:
    """The copy Node resolves for the fixed file: the nearest ``<dir>/node_modules/stripe`` up the file's path,
    else the hoisted ``node_modules/stripe``; never a dependency's nested copy, never an alias of a fork."""
    data = _json(text)
    packages = _dict(data.get("packages"))
    if packages:
        dirs = (ctx.sub if ctx else "").split("/")[:-1]
        keys = [f"{'/'.join(dirs[:k])}/node_modules/stripe" for k in range(len(dirs), 0, -1)] + ["node_modules/stripe"]
        for key in keys:
            pkg = packages.get(key)
            if isinstance(pkg, dict):
                if pkg.get("name") not in (None, "stripe") or not isinstance(pkg.get("version"), str):
                    return []  # "node_modules/stripe" holds another package (an npm alias)
                return [(pkg["version"], _first_line(text, re.escape(json.dumps(key)) + r"\s*:"))]
        return []
    dep = _dict(_dict(data.get("dependencies")).get("stripe"))  # lockfileVersion 1
    v = dep.get("version")
    if isinstance(v, str) and not v.startswith(("npm:", "file:", "git", "http")):
        return [(v, _first_line(text, r'"stripe"\s*:\s*\{'))]
    return []


def _yarn_stripe(entry: str) -> bool:
    """``stripe@^14``, ``stripe@npm:^14``, a patch of stripe: the real package; ``stripe@npm:@acme/fork@1``
    (an alias of another package), ``stripe@file:``/``link:``/``workspace:``/git: not stripe-node."""
    if not entry.startswith("stripe@"):
        return False
    spec = entry[len("stripe@"):]
    if spec.startswith("npm:"):
        return "@" not in spec[4:] and not spec[4:5].isalpha()
    if spec.startswith("patch:"):
        return spec[6:].startswith(("stripe@npm", "stripe@^", "stripe@~", "stripe@1", "stripe@2", "stripe@3",
                                    "stripe@4", "stripe@5", "stripe@6", "stripe@7", "stripe@8", "stripe@9"))
    return not spec.startswith(("file:", "link:", "workspace:", "portal:", "git", "http", "github:"))


def _yarn_lock(text: str, ctx: Context | None = None) -> Hits:
    out, current = [], None
    for n, line in enumerate(text.split("\n"), 1):
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            entries = [e.strip().strip('"') for e in line.rstrip()[:-1].split(",")]
            current = n if any(_yarn_stripe(e) for e in entries) else None
        elif current is not None:
            m = re.fullmatch(r"\s+version:?\s+\"?([^\"\s]+)\"?\s*", line)
            if m:
                out.append((m.group(1), current))
                current = None
    return out


def _pnpm_lock(text: str, ctx: Context | None = None) -> Hits:
    """The version the importer holding the fixed file depends on (``importers:``, the longest path that
    contains the file); a single-project lockfile's top-level ``dependencies:``; nothing else."""
    lines = text.split("\n")
    importers = _pnpm_importers(lines)
    if importers is not None:
        sub = ctx.sub if ctx else ""
        best = max((k for k in importers if k == "." or sub.startswith(k.rstrip("/") + "/")), key=len, default=None)
        return importers.get(best, []) if best is not None else []
    return _pnpm_deps(lines, 0, len(lines), 0)


def _pnpm_importers(lines: list[str]) -> dict[str, Hits] | None:
    start = next((n for n, line in enumerate(lines) if line.rstrip() == "importers:"), None)
    if start is None:
        return None
    end = next((n for n in range(start + 1, len(lines)) if lines[n][:1] not in ("", " ")), len(lines))
    out: dict[str, Hits] = {}
    keys = [n for n in range(start + 1, end) if re.match(r"  \S.*:\s*$", lines[n])]
    for k, n in enumerate(keys):
        name = lines[n].strip()[:-1].strip().strip("'\"")
        out[name] = _pnpm_deps(lines, n + 1, keys[k + 1] if k + 1 < len(keys) else end, 4)
    return out


def _pnpm_deps(lines: list[str], lo: int, hi: int, base: int) -> Hits:
    """``stripe`` under the ``dependencies:``-like sections indented ``base`` in lines[lo:hi]: ``stripe: 14.1.0``
    (lockfile v5) or ``stripe:`` with an indented ``version: 14.1.0(...)`` (v6+)."""
    out, section = [], False
    pad = " " * base
    for n in range(lo, hi):
        line = lines[n]
        if line.startswith(pad) and not line[base:base + 1].isspace() and line.rstrip().endswith(":"):
            section = line.strip()[:-1] in ("dependencies", "devDependencies", "optionalDependencies")
            continue
        if not section or not line.startswith(pad + "  ") or line[base + 2:base + 3].isspace():
            continue
        m = re.fullmatch(r"\s*['\"]?stripe['\"]?:\s*(.*?)\s*", line)
        if not m:
            continue
        value = m.group(1)
        if not value:  # v6+: the version line follows, indented further
            for j in range(n + 1, min(n + 4, hi)):
                v = re.fullmatch(r"\s+version:\s*['\"]?([^'\"\s(]+).*", lines[j])
                if v:
                    value = v.group(1)
                    break
        value = value.split("(")[0].strip("'\"")
        if value and value[0].isdigit():  # never link:, npm:@acme/fork@1, file:
            out.append((value, n + 1))
    return out


def _package_json(text: str, ctx: Context | None = None) -> Hits:
    data, out = _json(text), []
    for key in ("dependencies", "devDependencies"):
        v = _dict(data.get(key)).get("stripe")
        if isinstance(v, str) and re.fullmatch(r"\d+\.\d+\.\d+", v):  # an exact version only, never a range
            out.append((v, _first_line(text, r'"stripe"\s*:')))
    return out


def _toml_lock(text: str, ctx: Context | None = None) -> Hits:
    pattern = r'\[\[package\]\]\s*\nname\s*=\s*"stripe"\s*\nversion\s*=\s*"([^"]+)"'
    return [(m.group(1), _line(text, m.start())) for m in re.finditer(pattern, text)]


def _pipfile_lock(text: str, ctx: Context | None = None) -> Hits:
    data, out = _json(text), []
    for key in ("default", "develop"):
        v = _dict(_dict(data.get(key)).get("stripe")).get("version")
        if isinstance(v, str) and v.startswith("=="):
            out.append((v[2:], _first_line(text, r'"stripe"\s*:')))
    return out


def _requirements(text: str, ctx: Context | None = None) -> Hits:
    pattern = r"(?mi)^stripe(?:\[[^\]\n]*\])?[ \t]*==[ \t]*([0-9][^\s;#,]*)"
    return [(m.group(1), _line(text, m.start())) for m in re.finditer(pattern, text)]


READERS = {
    "stripe-ruby": [("Gemfile.lock", _gemfile_lock)],
    "stripe-php": [("composer.lock", _composer_lock)],
    "stripe-go": [("go.mod", _go_mod)],
    "stripe-node": [
        ("package-lock.json", _package_lock), ("yarn.lock", _yarn_lock), ("pnpm-lock.yaml", _pnpm_lock),
        ("package.json", _package_json),
    ],
    "stripe-python": [
        ("poetry.lock", _toml_lock), ("uv.lock", _toml_lock), ("Pipfile.lock", _pipfile_lock),
        ("requirements.txt", _requirements), ("requirements*.txt", _requirements),
    ],
}
