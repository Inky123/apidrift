"""Plan the mechanical fix for documented request-field renames, verified file by file.

Only breaking, certain impacts whose change is in :mod:`migrations` are candidates. Edit sites come
from :mod:`fixedit` (call -> argument -> reaching bindings and mutations, never a text search), and
every edited file must pass an oracle before it is offered: the re-scan sees the same calls, the
only field delta is the rename at endpoints in the table, no finding appears or worsens, the fixed
sites no longer carry the change, and exactly the edited tokens are gone. Nothing is written here:
``apidrift fix`` prints diffs and the App pushes the content as a pull request.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import migrations, scanner
from .diff import BREAKING, Change
from .fixedit import Edit, Locator, apply, tokens, variable_uses
from .impact import AFFECTED, POSSIBLE, Impact, assess
from .migrations import Migration
from .scanner import LANGUAGES, ScanResult, Usage, scan_text
from .sdkmap import SdkIndex

FIX_LANGUAGES = {"python", "javascript", "typescript", "ruby"}
WHY = (
    "nested", "not_found", "incomplete_binding", "unsupported_key_form", "oracle_changed_other_calls",
    "oracle_finding_remains", "oracle_new_finding", "leftover_tokens", "not_utf8", "too_large", "cap",
)
NOT_ATTEMPTED = "arguments_not_visible"  # a possible hit: the call may send the field through arguments we cannot read
CAP_FILES, CAP_FILE_BYTES, CAP_TOTAL_BYTES = 25, 1_000_000, 3_000_000
MAX_PINS = 100
PROJECT_MARKERS = scanner.PROJECT_FILES | {"requirements.txt", "Pipfile", "setup.cfg", "Gemfile.lock", "deno.json"}
PIN_RE = re.compile(
    r"""(?:\bapiVersion|\bapi_version|['"]?Stripe-Version['"]?)\s*(?:=>|[:=])\s*['"](\d{4}-\d{2}-\d{2}(?:\.[A-Za-z]\w*)?)['"]"""
)


@dataclass(frozen=True)
class FixSite:
    line: int
    path: str
    target: str
    replacement: str


@dataclass
class Fix:
    file: str
    blob_sha: str
    content: str
    sites: list[FixSite]
    original: str = field(default="", repr=False)  # for diffs; not part of the payload

    def diff(self) -> str:
        lines = difflib.unified_diff(
            self.original.splitlines(keepends=True), self.content.splitlines(keepends=True),
            fromfile=f"a/{self.file}", tofile=f"b/{self.file}",
        )
        return "".join(ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n" for ln in lines)


@dataclass(frozen=True)
class Unfixable:
    file: str
    line: int
    path: str
    target: str
    why: str


@dataclass(frozen=True)
class Pin:
    file: str
    line: int
    version: str


@dataclass
class FixPlan:
    fixes: list[Fix] = field(default_factory=list)
    unfixable: list[Unfixable] = field(default_factory=list)
    pins: list[Pin] = field(default_factory=list)
    migrations: list[Migration] = field(default_factory=list)
    leftovers: list[tuple[str, int]] = field(default_factory=list)
    not_attempted: list[Unfixable] = field(default_factory=list)  # possible hits, why == NOT_ATTEMPTED

    def to_dict(self) -> dict:
        """The ``--fix`` payload: a frozen contract with the App's pull-request step."""
        return {
            "fixes": [
                {
                    "file": f.file, "blob_sha": f.blob_sha, "content": f.content,
                    "sites": [{"line": s.line, "path": s.path, "target": s.target, "replacement": s.replacement} for s in f.sites],
                }
                for f in self.fixes
            ],
            "unfixable": [
                {"file": u.file, "line": u.line, "path": u.path, "target": u.target, "why": u.why} for u in self.unfixable
            ],
            "pins": [{"file": p.file, "line": p.line, "version": p.version} for p in self.pins],
            "migrations": [m.to_dict() for m in self.migrations],
            "leftovers": [{"file": f, "line": line} for f, line in self.leftovers],
            "not_attempted": [
                {"file": u.file, "line": u.line, "path": u.path, "target": u.target, "why": u.why} for u in self.not_attempted
            ],
        }


Candidate = tuple[Impact, Migration]


def plan_fixes(result: ScanResult, impacts: list[Impact], index: SdkIndex, root: Path) -> FixPlan:
    root = Path(root).resolve()
    base = root.parent if root.is_file() else root
    plan = FixPlan()
    by_file: dict[str, list[Candidate]] = {}
    for impact in impacts:
        migration = migrations.lookup(impact.change)
        if migration is None or impact.severity != BREAKING or impact.usage.language not in FIX_LANGUAGES:
            continue
        if impact.certainty == AFFECTED:
            by_file.setdefault(impact.usage.file, []).append((impact, migration))
        else:
            u = impact.usage
            plan.not_attempted.append(Unfixable(u.file, u.line, u.path, migration.target, NOT_ATTEMPTED))
    seen: dict[tuple[str, str, str], Migration] = {}
    for group in by_file.values():
        for _, m in group:
            seen.setdefault((m.path, m.target, m.method), m)
    plan.migrations = [seen[k] for k in sorted(seen)]
    changes = _unique_changes(impacts)
    total = 0
    for file in sorted(by_file):
        group = by_file[file]
        if len(plan.fixes) >= CAP_FILES:
            _drop(plan, group, "cap")
            continue
        try:
            raw = (base / file).read_bytes()
        except OSError:
            _drop(plan, group, "not_found")
            continue
        if len(raw) > CAP_FILE_BYTES or total + len(raw) > CAP_TOTAL_BYTES:
            _drop(plan, group, "too_large")
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            _drop(plan, group, "not_utf8")
            continue
        fix, leftovers = _plan_file(plan, file, text, group, index, changes)
        plan.leftovers += [(file, line) for line in leftovers]
        if fix is not None:
            fix.blob_sha = blob_sha(raw)
            plan.fixes.append(fix)
            total += len(raw)
    plan.pins = find_pins(root, [f.file for f in plan.fixes])
    return plan


def _plan_file(
    plan: FixPlan, file: str, text: str, group: list[Candidate], index: SdkIndex, changes: list[Change]
) -> tuple[Fix | None, list[int]]:
    language = group[0][0].usage.language
    locator = Locator(text, language, index)
    edits: list[Edit] = []
    planned: list[Candidate] = []
    for impact, migration in group:
        why = _candidate_edits(locator, impact.usage, migration, edits)
        if why:
            _drop(plan, [(impact, migration)], why)
        else:
            planned.append((impact, migration))
    if not planned:
        return None, []
    try:
        content = apply(text, edits)
    except ValueError:
        _drop(plan, planned, "not_found")
        return None, []
    why, leftovers = _oracle(file, text, content, language, planned, edits, index, changes)
    if why:
        _drop(plan, planned, why)
        return None, leftovers
    sites = [FixSite(i.usage.line, i.usage.path, m.target, m.replacement) for i, m in planned]
    return Fix(file, "", content, sites, original=text), leftovers


def _candidate_edits(locator: Locator, usage: Usage, migration: Migration, edits: list[Edit]) -> str | None:
    if "." in migration.target or "[]" in migration.target:
        return "nested"
    sites = locator.sites(usage, migration.target, migration.replacement)
    if not sites.found_call:
        return "not_found"
    if sites.unsupported:
        return "unsupported_key_form"
    if sites.incomplete:
        return "incomplete_binding"
    if not sites.edits:
        return "not_found"
    edits += sites.edits
    return None


def _oracle(
    file: str, before: str, after: str, language: str, planned: list[Candidate], edits: list[Edit],
    index: SdkIndex, changes: list[Change],
) -> tuple[str | None, list[int]]:
    """Why the edited file must not be offered (None when every check holds), and the old-name tokens left."""
    u_before, _, _ = scan_text(before, file, index)
    u_after, _, _ = scan_text(after, file, index)
    shape = lambda us: [(u.method, u.path, u.call, u.complete) for u in us]  # noqa: E731
    if shape(u_before) != shape(u_after):
        return "oracle_changed_other_calls", []
    renames = {(m.target, m.replacement) for _, m in planned}
    for ub, ua in zip(u_before, u_after):
        if ub.fields == ua.fields:
            continue
        if not any(
            ub.fields - ua.fields == {t} and ua.fields - ub.fields == {r}
            and migrations.replacement_for(ub.method, ub.path, t) == r
            for t, r in renames
        ):
            return "oracle_changed_other_calls", []
    why = _oracle_impacts(u_before, u_after, planned, changes)
    if why:
        return why, []
    leftovers: list[int] = []
    stray = False
    for target in sorted({t for t, _ in renames}):
        unique = {e for e in edits if e.key == target}
        expected = sum(1 for e in unique if not e.shorthand)
        remaining = tokens(after, language, target)
        if len(tokens(before, language, target)) - len(remaining) != expected:
            stray = True
        # Backstop: any token of the old key left in code means the rename is not known to be complete. The
        # one exception is a variable that happens to carry the key's name, which a shorthand entry leaves
        # behind as the value (``new: payment_method_types``).
        allowed = variable_uses(after, language, remaining) if any(e.shorthand for e in unique) else set()
        stray = stray or any(t.pos not in allowed for t in remaining)
        leftovers += [t.line for t in remaining]
    return ("leftover_tokens" if stray else None), sorted(set(leftovers))


def _oracle_impacts(u_before: list[Usage], u_after: list[Usage], planned: list[Candidate], changes: list[Change]) -> str | None:
    pos_b = {id(u): n for n, u in enumerate(u_before)}
    pos_a = {id(u): n for n, u in enumerate(u_after)}
    kb = {(pos_b[id(i.usage)], i.change.kind, i.change.target): i.certainty for i in assess(u_before, changes)}
    ka = {(pos_a[id(i.usage)], i.change.kind, i.change.target): i.certainty for i in assess(u_after, changes)}
    for key, certainty in ka.items():
        if key not in kb or (kb[key] == POSSIBLE and certainty == AFFECTED):
            return "oracle_new_finding"
    for impact, migration in planned:
        u = impact.usage
        for n, ub in enumerate(u_before):
            if (ub.line, ub.method, ub.path, ub.call) == (u.line, u.method, u.path, u.call):
                if (n, impact.change.kind, migration.target) in ka:
                    return "oracle_finding_remains"
    return None


def _unique_changes(impacts: list[Impact]) -> list[Change]:
    out: dict[tuple, Change] = {}
    for i in impacts:
        out.setdefault(i.change.key(), i.change)
    return list(out.values())


def _drop(plan: FixPlan, group: list[Candidate], why: str) -> None:
    assert why in WHY, why
    for impact, migration in group:
        u = impact.usage
        plan.unfixable.append(Unfixable(u.file, u.line, u.path, migration.target, why))


def blob_sha(raw: bytes) -> str:
    """The git blob id of a file's bytes, so the App can prove the fix came from the exact base content."""
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()


def find_pins(root: Path, fixed: list[str]) -> list[Pin]:
    """API-version pins (``apiVersion:``, ``api_version =``, ``Stripe-Version``) in the source files of the
    projects that hold the fixed files: the nearest ancestor directory with a project file (package.json,
    requirements.txt, Gemfile, ...), else the file's own directory. Reported for the pull request body;
    never edited. A browser bundle's Stripe.js pin elsewhere in the repository is not the server's pin.
    """
    root = Path(root).resolve()
    if not fixed or not root.exists():
        return []
    if root.is_file():
        files, base = [root], root.parent
    else:
        base = root
        files, seen = [], set()
        for project in sorted({_project_root(base, f) for f in fixed}):
            for path in scanner._iter_files(project)[0]:
                if path not in seen:
                    seen.add(path)
                    files.append(path)
    pins: list[Pin] = []
    for path in sorted(files):
        if path.suffix.lower() not in LANGUAGES:
            continue
        text = scanner._read(path)
        if text is None or "stripe" not in text.lower():
            continue
        rel = path.relative_to(base).as_posix()
        for m in PIN_RE.finditer(text):
            pins.append(Pin(rel, text.count("\n", 0, m.start()) + 1, m.group(1)))
            if len(pins) >= MAX_PINS:
                return pins
    return pins


def _project_root(base: Path, rel: str) -> Path:
    here = (base / rel).parent
    for d in [here, *here.parents]:
        if d == base or any((d / marker).exists() for marker in PROJECT_MARKERS):
            return d
        if base not in d.parents:
            break
    return here


# --- Rendering (apidrift fix) ---------------------------------------------------


def render_fix_text(plan: FixPlan, root: str) -> str:
    lines = [f"apidrift fix: {root} (dry run; nothing is written)"]
    if not plan.fixes and not plan.unfixable and not plan.not_attempted:
        lines.append("nothing to fix: no breaking change with a documented rename hits this checkout")
    for fix in plan.fixes:
        lines += ["", fix.diff().rstrip("\n")]
    if plan.fixes:
        n = sum(len(f.sites) for f in plan.fixes)
        lines += ["", f"{len(plan.fixes)} file(s), {n} call site(s) renamed:"]
        lines += [f"  {s.target} -> {s.replacement}  {f.file}:{s.line}  {s.path}" for f in plan.fixes for s in f.sites]
    if plan.pins:
        lines += ["", "API-version pins (merge the rename together with your upgrade; these are not changed):"]
        lines += [f"  {p.file}:{p.line}  {p.version}" for p in plan.pins]
    if plan.unfixable:
        lines += ["", "left for you:"]
        lines += [f"  {u.file}:{u.line}  {u.target}  {u.path}  ({u.why})" for u in plan.unfixable]
    if plan.not_attempted:
        lines += ["", "possibly affected, not attempted (arguments not fully visible):"]
        lines += [f"  {u.file}:{u.line}  {u.target}  {u.path}" for u in plan.not_attempted]
    if plan.leftovers:
        lines += ["", "still mentions the old name:"]
        lines += [f"  {f}:{line}" for f, line in plan.leftovers]
    for m in plan.migrations:
        lines += ["", f"{m.target} -> {m.replacement} (Stripe {m.introduced}): {m.note}", f"  {m.doc}"]
    return "\n".join(lines)
