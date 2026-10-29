"""Plan the mechanical fix for documented request-field migrations, verified file by file.

Only breaking, certain impacts whose change is in :mod:`migrations` are candidates (a rename, a rename
to the first element, a deletion, or a value wrapped in ``{type: ...}``). Edit sites come from
:mod:`fixedit` (call -> argument -> reaching bindings and mutations, never a text search), and every
edited file must pass an oracle before it is offered: the re-scan sees the same calls, the field delta
at each planned call is exactly the one the table predicts and empty everywhere else, no finding appears
or worsens, the fixed sites no longer carry the change, and exactly the edited tokens are gone. Nothing
is written here: ``apidrift fix`` prints diffs and the App pushes the content as a pull request.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import migrations, scanner
from .diff import BREAKING, Change
from .fixedit import Edit, Locator, apply
from .fixtokens import accessed, tokens, variable_uses
from .fixvalue import leaf
from .impact import AFFECTED, POSSIBLE, Impact, assess
from .migrations import Migration
from .scanner import LANGUAGES, ScanResult, Usage, scan_text
from .sdkmap import SdkIndex

FIX_LANGUAGES = {"python", "javascript", "typescript", "ruby"}
WHY = (
    "nested", "not_found", "incomplete_binding", "unsupported_key_form", "oracle_changed_other_calls",
    "oracle_finding_remains", "oracle_new_finding", "leftover_tokens", "not_utf8", "too_large", "cap",
    "nested_array", "unsupported_value", "would_empty_literal", "syntax",
)
NOT_VISIBLE = "arguments are not fully visible at this call"  # impact.py's reason at an incomplete call
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
    replacement: str  # "" for delete; the target itself for wrap_type
    action: str = "rename"


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
    possibly_remaining: list[dict] = field(default_factory=list)  # deleted keys a spread may still send

    def to_dict(self) -> dict:
        """The ``--fix`` payload: a frozen contract with the App's pull-request step."""
        return {
            "fixes": [
                {
                    "file": f.file, "blob_sha": f.blob_sha, "content": f.content,
                    "sites": [
                        {"line": s.line, "path": s.path, "target": s.target, "replacement": s.replacement, "action": s.action}
                        for s in f.sites
                    ],
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
            "possibly_remaining": [dict(p) for p in self.possibly_remaining],
        }


Candidate = tuple[Impact, Migration]
Planned = tuple[Impact, Migration, bool]  # the bool: a wrap_type edit wrote the timestamp form


def plan_fixes(result: ScanResult, impacts: list[Impact], index: SdkIndex, root: Path) -> FixPlan:
    root = Path(root).resolve()
    base = root.parent if root.is_file() else root
    plan = FixPlan()
    by_file: dict[str, list[Candidate]] = {}
    fixable = [
        (i, m) for i in impacts
        if (m := migrations.lookup(i.change)) is not None and i.severity == BREAKING and i.usage.language in FIX_LANGUAGES
    ]
    planned_keys: set[tuple[int, Migration]] = set()  # one candidate per (call, migration): one edit per site
    for impact, m in fixable:
        if impact.certainty == AFFECTED and (id(impact.usage), m) not in planned_keys:
            planned_keys.add((id(impact.usage), m))
            by_file.setdefault(impact.usage.file, []).append((impact, m))
    listed: set[tuple[str, int, str, str]] = set()
    for impact, m in fixable:
        u = impact.usage
        if impact.certainty == AFFECTED or (id(u), m) in planned_keys:
            continue  # e.g. the possible ``<target>.type`` companion of a planned wrap_type
        if (u.file, u.line, u.path, m.target) not in listed:
            listed.add((u.file, u.line, u.path, m.target))
            plan.not_attempted.append(Unfixable(u.file, u.line, u.path, m.target, NOT_ATTEMPTED))
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
    planned: list[Planned] = []
    for impact, migration in group:
        why, found = _candidate_edits(locator, impact.usage, migration)
        if why:
            _drop(plan, [(impact, migration)], why)
        else:
            edits += found
            planned.append((impact, migration, any(e.timestamp for e in found)))
    if not planned:
        return None, []
    pairs = [(i, m) for i, m, _ in planned]
    try:
        content = apply(text, edits)
    except ValueError:
        _drop(plan, pairs, "not_found")
        return None, []
    if language == "python" and not _parses(content):
        _drop(plan, pairs, "syntax")  # checked by the real parser, not the reader that placed the edits
        return None, []
    why, leftovers, possibly = _oracle(file, text, content, language, planned, edits, index, changes)
    if why:
        _drop(plan, pairs, why)
        return None, leftovers
    plan.possibly_remaining += [
        {"file": file, "line": i.usage.line, "path": i.usage.path, "target": m.target} for i, m in possibly
    ]
    sites = [FixSite(i.usage.line, i.usage.path, m.target, m.replacement, m.action) for i, m in pairs]
    return Fix(file, "", content, sites, original=text), leftovers


def _candidate_edits(locator: Locator, usage: Usage, migration: Migration) -> tuple[str | None, list[Edit]]:
    if "[]" in migration.target:
        return "nested_array", []
    sites = locator.sites(usage, migration)
    if not sites.found_call:
        return "not_found", []
    if sites.unsupported:
        return "unsupported_key_form", []
    if sites.incomplete:
        return "incomplete_binding", []
    if sites.why:
        return sites.why, []
    if not sites.edits:
        return ("unsupported_value" if sites.leave else "not_found"), []
    return None, sites.edits


def _oracle(
    file: str, before: str, after: str, language: str, planned: list[Planned], edits: list[Edit],
    index: SdkIndex, changes: list[Change],
) -> tuple[str | None, list[int], list[Candidate]]:
    """Why the edited file must not be offered (None when every check holds), the old-name tokens left,
    and the planned deletions a call's hidden arguments may still send."""
    u_before, _, _ = scan_text(before, file, index)
    u_after, _, _ = scan_text(after, file, index)
    shape = lambda us: [(u.method, u.path, u.call, u.complete) for u in us]  # noqa: E731
    if shape(u_before) != shape(u_after):
        return "oracle_changed_other_calls", [], []
    at = _planned_at(u_before, planned)
    for n, (ub, ua) in enumerate(zip(u_before, u_after)):
        removed: set[str] = set()
        added: set[str] = set()
        for _, m, stamp in at.get(n, ()):
            r, a = migrations.expected_delta(m, ub.fields, timestamp=stamp)
            removed |= r
            added |= a
        if ub.fields - ua.fields != removed or ua.fields - ub.fields != added:
            return "oracle_changed_other_calls", [], []
    why, possibly = _oracle_impacts(u_before, u_after, at, changes)
    if why:
        return why, [], []
    why, leftovers = _oracle_tokens(before, after, language, planned, edits)
    return why, leftovers, possibly


def _planned_at(u_before: list[Usage], planned: list[Planned]) -> dict[int, list[Planned]]:
    out: dict[int, list[Planned]] = {}
    for p in planned:
        u = p[0].usage
        for n, ub in enumerate(u_before):
            if (ub.line, ub.method, ub.path, ub.call) == (u.line, u.method, u.path, u.call):
                out.setdefault(n, []).append(p)
    return out


def _oracle_impacts(
    u_before: list[Usage], u_after: list[Usage], at: dict[int, list[Planned]], changes: list[Change]
) -> tuple[str | None, list[Candidate]]:
    pos_b = {id(u): n for n, u in enumerate(u_before)}
    pos_a = {id(u): n for n, u in enumerate(u_after)}
    kb = {(pos_b[id(i.usage)], i.change.kind, i.change.target): (i.certainty, i.reason) for i in assess(u_before, changes)}
    ka = {(pos_a[id(i.usage)], i.change.kind, i.change.target): (i.certainty, i.reason) for i in assess(u_after, changes)}
    for key, (certainty, _) in ka.items():
        if key not in kb or (kb[key][0] == POSSIBLE and certainty == AFFECTED):
            return "oracle_new_finding", []
    possibly: list[Candidate] = []
    for n, entries in at.items():
        for impact, m, _ in entries:
            keys = {(n, impact.change.kind, impact.change.target), (n, m.kind, m.target)}
            if m.action == "wrap_type":
                keys.add((n, "body_added_required", m.target + ".type"))
            for key in keys:
                if key not in ka:
                    continue
                certainty, reason = ka[key]
                if m.action == "delete" and not u_after[n].complete and certainty == POSSIBLE and reason == NOT_VISIBLE:
                    if (impact, m) not in possibly:
                        possibly.append((impact, m))  # a spread may still send it: reported, not hidden
                    continue
                return "oracle_finding_remains", []
    return None, possibly


def _oracle_tokens(before: str, after: str, language: str, planned: list[Planned], edits: list[Edit]) -> tuple[str | None, list[int]]:
    """Every edit removes exactly the leaf-key tokens it covers (minus those its new text carries), the
    text outside the edits is byte-identical, and no token of a renamed or deleted key is left behind."""
    identical, placed = _layout(before, after, edits)
    stray = not identical
    leftovers: list[int] = []
    by_target = {m.target: m for _, m, _ in planned}
    for target in sorted(by_target):
        m, name = by_target[target], leaf(target)
        mine = {e for e in edits if e.key == target}
        found = tokens(before, language, name)
        remaining = tokens(after, language, name)
        expected = sum(
            sum(1 for t in found if e.start <= t.pos < e.end) - len(tokens(e.text, language, name)) for e in mine
        )
        if len(found) - len(remaining) != expected:
            stray = True
        if m.action == "wrap_type":
            # The key stays, so its count is unchanged; but a read of it elsewhere (``p.billing_cycle_anchor``,
            # ``p['billing_cycle_anchor']``) would now see the object instead of the string it compared.
            stray = stray or any(accessed(after, language, name, t) for t in remaining)
            continue
        # Backstop: any token of the old key left in code means the edit is not known to be complete. The
        # exceptions: a value carried into the new text (``country: countries[0]``), and a variable that
        # happens to carry the key's name, which a shorthand entry leaves behind (``new: payment_method_types``)
        # or which rename_first keeps as the value (``def f(countries)`` ... ``country: countries[0]``).
        carried = {t.pos for t in remaining for e in mine if placed[e][0] <= t.pos < placed[e][1]}
        loose = m.action == "rename_first" or any(e.shorthand for e in mine)
        allowed = variable_uses(after, language, remaining) if loose else set()
        stray = stray or any(t.pos not in allowed and t.pos not in carried for t in remaining)
        if m.action == "rename_first":  # the old value is kept on purpose: its variables are not leftovers
            remaining = [t for t in remaining if t.pos not in allowed and t.pos not in carried]
        leftovers += [t.line for t in remaining]
    return ("leftover_tokens" if stray else None), sorted(set(leftovers))


def _parses(text: str) -> bool:
    try:
        ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    return True


def _layout(before: str, after: str, edits: list[Edit]) -> tuple[bool, dict[Edit, tuple[int, int]]]:
    """Whether ``after`` equals ``before`` outside the edits, and where each edit's new text landed."""
    placed: dict[Edit, tuple[int, int]] = {}
    pos, last, same = 0, 0, True
    for e in sorted(set(edits), key=lambda e: e.start):
        seg = before[last : e.start]
        same = same and after[pos : pos + len(seg)] == seg
        pos += len(seg)
        placed[e] = (pos, pos + len(e.text))
        pos += len(e.text)
        last = e.end
    return same and after[pos:] == before[last:], placed


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


def describe(m: Migration) -> str:
    """What a migration does to a call, in words (``apidrift fix`` output)."""
    if m.action == "delete":
        return f"{m.target} removed"
    if m.action == "rename_first":
        return f"{m.target} renamed to {m.replacement}, keeping its first element"
    if m.action == "wrap_type":
        return f"{m.target} wrapped in {{type: …}}"
    return f"{m.target} renamed to {m.replacement}"


def render_fix_text(plan: FixPlan, root: str) -> str:
    lines = [f"apidrift fix: {root} (dry run; nothing is written)"]
    if not plan.fixes and not plan.unfixable and not plan.not_attempted:
        lines.append("nothing to fix: no breaking change with a documented migration hits this checkout")
    for fix in plan.fixes:
        lines += ["", fix.diff().rstrip("\n")]
    if plan.fixes:
        n = sum(len(f.sites) for f in plan.fixes)
        lines += ["", f"{len(plan.fixes)} file(s), {n} call site(s) changed:"]
        for m in plan.migrations:
            here = [(f.file, s.line) for f in plan.fixes for s in f.sites if (s.path, s.target) == (m.path, m.target)]
            if here:
                lines.append(f"  {describe(m)} on {m.method.upper()} {m.path}:")
                lines += [f"    {file}:{line}" for file, line in here]
    if plan.pins:
        lines += ["", "API-version pins (merge the fix together with your upgrade; these are not changed):"]
        lines += [f"  {p.file}:{p.line}  {p.version}" for p in plan.pins]
    if plan.possibly_remaining:
        lines += ["", "removed here, but possibly still sent through arguments that are not visible:"]
        lines += [f"  {p['file']}:{p['line']}  {p['target']}  {p['path']}" for p in plan.possibly_remaining]
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
        lines += ["", f"{describe(m)} (Stripe {m.introduced}): {m.note}", f"  {m.doc}"]
    return "\n".join(lines)
