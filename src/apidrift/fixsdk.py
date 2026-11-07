"""The Stripe SDK version a fixed file runs with, read from the project's lockfiles (never the network).

A renamed or reshaped field only helps once the request goes out under an API version that has it, and the
SDK decides that: stripe-node, -python, -php and -ruby send the API version they were built for unless the
code sets one (before node 12, python 6, php 12, ruby 9 they used the account's default), and stripe-go's
version is fixed per major and its structs only have the fields of that major. So the fix payload carries
the locked version of the SDK for each fixed file's language, from the nearest directory (the file's own,
then each parent up to the checkout root) that resolves it:

- Ruby: Gemfile.lock. PHP: composer.lock. Go: go.mod (the module path's major and the required tag).
- Node: package-lock.json, yarn.lock (v1 and berry), pnpm-lock.yaml, else an exact version in package.json.
- Python: poetry.lock, uv.lock, Pipfile.lock, else an exact ``stripe==`` pin in requirements*.txt.

A lockfile that resolves several versions of the SDK reports each. :data:`FIRST_RELEASE` is the evidence
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
    """The locked SDK version for each fixed file's language, nearest lockfile first (see the module doc)."""
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
            found = _read_dir(root, d, sdk)
            if found:
                out += [lock for lock in found if lock not in out]
                break
            if d == root:
                break
    return out[:MAX_LOCKS]


def _read_dir(root: Path, d: Path, sdk: str) -> list[SdkLock]:
    for name, reader in READERS[sdk]:
        for path in sorted(d.glob(name)) if "*" in name else [d / name]:
            text = _text(path)
            if text is None:
                continue
            hits = [(v, line) for v, line in reader(text) if VERSION_SHAPE.fullmatch(v) and len(v) <= 40]
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
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


Hits = list[tuple[str, int]]


def _gemfile_lock(text: str) -> Hits:
    return [(m.group(1), _line(text, m.start())) for m in re.finditer(r"(?m)^ {4}stripe \(([^)\s]+)\)[ \t]*$", text)]


def _composer_lock(text: str) -> Hits:
    data, out = _json(text), []
    for key in ("packages", "packages-dev"):
        for pkg in data.get(key) or []:
            if isinstance(pkg, dict) and pkg.get("name") == "stripe/stripe-php" and isinstance(pkg.get("version"), str):
                out.append((pkg["version"].removeprefix("v"), _first_line(text, r'"name"\s*:\s*"stripe/stripe-php"')))
    return out


def _go_mod(text: str) -> Hits:
    pattern = r"(?m)^[ \t]*(?:require[ \t]+)?github\.com/stripe/stripe-go(?:/v(\d+))?[ \t]+v(\d[^\s]*)"
    out = [(m.group(2), _line(text, m.start())) for m in re.finditer(pattern, text)]
    return sorted(out, key=lambda h: _numbers(h[0].split("-")[0]) or (0, 0, 0), reverse=True)[:1]  # the newest major


def _package_lock(text: str) -> Hits:
    data, out = _json(text), []
    for key, pkg in (data.get("packages") or {}).items():
        if (key == "node_modules/stripe" or key.endswith("/node_modules/stripe")) and isinstance(pkg, dict):
            out.append((str(pkg.get("version") or ""), _first_line(text, re.escape(json.dumps(key)) + r"\s*:")))
    if not out:  # lockfileVersion 1
        dep = (data.get("dependencies") or {}).get("stripe")
        if isinstance(dep, dict):
            out.append((str(dep.get("version") or ""), _first_line(text, r'"stripe"\s*:\s*\{')))
    return out


def _yarn_lock(text: str) -> Hits:
    out, lines, current = [], text.split("\n"), None
    for n, line in enumerate(lines, 1):
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            entries = [e.strip().strip('"') for e in line.rstrip()[:-1].split(",")]
            current = n if any(e.startswith("stripe@") for e in entries) else None
        elif current is not None:
            m = re.fullmatch(r"\s+version:?\s+\"?([^\"\s]+)\"?\s*", line)
            if m:
                out.append((m.group(1), current))
                current = None
    return out


def _pnpm_lock(text: str) -> Hits:
    pattern = r"""(?m)^[ \t]{2,4}['"]?/?stripe[@/](\d+\.\d+\.\d+[^:'"()\s]*)"""
    return [(m.group(1), _line(text, m.start())) for m in re.finditer(pattern, text)]


def _package_json(text: str) -> Hits:
    data, out = _json(text), []
    for key in ("dependencies", "devDependencies"):
        v = (data.get(key) or {}).get("stripe") if isinstance(data.get(key), dict) else None
        if isinstance(v, str) and re.fullmatch(r"\d+\.\d+\.\d+", v):  # an exact version only, never a range
            out.append((v, _first_line(text, r'"stripe"\s*:')))
    return out


def _toml_lock(text: str) -> Hits:
    pattern = r'\[\[package\]\]\s*\nname\s*=\s*"stripe"\s*\nversion\s*=\s*"([^"]+)"'
    return [(m.group(1), _line(text, m.start())) for m in re.finditer(pattern, text)]


def _pipfile_lock(text: str) -> Hits:
    data, out = _json(text), []
    for key in ("default", "develop"):
        v = ((data.get(key) or {}).get("stripe") or {}).get("version") if isinstance(data.get(key), dict) else None
        if isinstance(v, str) and v.startswith("=="):
            out.append((v[2:], _first_line(text, r'"stripe"\s*:')))
    return out


def _requirements(text: str) -> Hits:
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
        ("requirements*.txt", _requirements),
    ],
}
