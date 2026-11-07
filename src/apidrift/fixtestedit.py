"""Update the tests that expect the exact request a fix pull request changes; list every other one.

A test is edited only where the line is the expectation of a request the pull request changes: the same
key, in the literal that is the parameters argument of a recognised expectation of the same SDK call
(:mod:`fixtestfind`), or the literal an RSpec example compares a builder's result with (:mod:`fixtestbuild`).
The edit is made by the fixer's own :class:`fixedit.Locator` on the expectation read as that call, so it is
exactly the documented action the code got (rename, delete, rename_first, wrap_type). A field qualifies
only when the pull request changes every call that sends it to that endpoint: no site of that (endpoint,
field) is left unfixable, unattempted or possibly still sent, so after the merge nothing sends the old key
there. A test that reads the field from a Stripe response, a fixture, a mock's return value, a key built at
run time, a negative expectation or anything not tied to the call is never edited; it stays listed.

Each edited test must pass the checks the code does where they apply: the expectations are found again
with the same calls, the field delta at each edited one is exactly the table's and empty at the others, the
file's own Stripe calls are unchanged, exactly the edited key tokens are gone, the text outside the edits is
untouched, and Python still parses (PHP: :func:`phplex.same_structure`). Any failure drops the whole file.
"""

from __future__ import annotations

import ast
import difflib
from dataclasses import dataclass, field
from pathlib import Path

from . import fixtestbuild, fixtestfind, migrations, phpfix, phplex
from .fixedit import Locator, apply
from .fixhelpers import plain_call
from .fixtokens import tokens
from .fixvalue import Edit, leaf
from .migrations import Migration
from .scanner import LANGUAGES, Usage, scan_text

MAX_TEST_FILES, MAX_TEST_BYTES, MAX_EDITS = 25, 1_000_000, 50


@dataclass(frozen=True)
class TestEdit:
    line: int  # where the key is, 1-based
    path: str
    target: str
    replacement: str
    action: str
    start: int  # code-point offsets into the original test text
    end: int
    text: str

    def to_dict(self) -> dict:
        return {
            "line": self.line, "path": self.path, "target": self.target, "replacement": self.replacement,
            "action": self.action, "start": self.start, "end": self.end, "text": self.text,
        }


@dataclass
class TestFix:
    file: str
    blob_sha: str
    content: str
    edits: list[TestEdit]
    original: str = field(default="", repr=False)

    def to_dict(self) -> dict:
        """No content: the App rebuilds it from the base blob and these edits, after checking each one."""
        return {"file": self.file, "blob_sha": self.blob_sha, "edits": [e.to_dict() for e in self.edits]}

    def diff(self) -> str:
        lines = difflib.unified_diff(
            self.original.splitlines(keepends=True), self.content.splitlines(keepends=True),
            fromfile=f"a/{self.file}", tofile=f"b/{self.file}",
        )
        return "".join(ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n" for ln in lines)


def plan_tests(plan, impacts, index, base: Path) -> tuple[list[TestFix], list[tuple[str, int]]]:
    """The test files to update for ``plan`` (a :class:`fixer.FixPlan`), and the plan's test lines with the
    edited ones included (a wrap_type key is listed only where it is edited)."""
    eligible = _eligible(plan)
    files = list(dict.fromkeys(f for f, _ in plan.tests))
    if not eligible or not files:
        return [], plan.tests
    sources = _builders(plan, impacts, eligible)
    out: list[TestFix] = []
    for file in files:
        if len(out) >= MAX_TEST_FILES:
            break
        fix = _plan_file(Path(base), file, eligible, index, sources)
        if fix is not None:
            out.append(fix)
    edited = {(f.file, e.line) for f in out for e in f.edits}
    return out, sorted(set(plan.tests) | edited)


def _eligible(plan) -> dict[tuple[str, str], Migration]:
    """(path, target) the plan changes at every call that sends it."""
    fixed = {(s.path, s.target) for f in plan.fixes for s in f.sites}
    left = {(u.path, u.target) for u in [*plan.unfixable, *plan.not_attempted]}
    left |= {(str(p.get("path")), str(p.get("target"))) for p in plan.possibly_remaining}
    out = {}
    for key in sorted(fixed - left):
        m = migrations.for_site(*key)
        if m is not None:
            out[key] = m
    return out


def _builders(plan, impacts, eligible) -> list[fixtestbuild.Builder]:
    out: list[fixtestbuild.Builder] = []
    for fix in plan.fixes:
        if LANGUAGES.get(Path(fix.file).suffix.lower()) != "ruby":
            continue
        sites = []
        for s in fix.sites:
            calls = {plain_call(i.usage.call) for i in impacts
                     if (i.usage.file, i.usage.line, i.usage.path) == (fix.file, s.line, s.path)}
            if (s.path, s.target) in eligible and len(calls) == 1:
                sites.append((s.path, s.target, s.line, calls.pop()))
        if sites:
            out += fixtestbuild.builders(fix.file, fix.original, fix.content, sites)
    return out


def _plan_file(base: Path, file: str, eligible, index, sources) -> TestFix | None:
    language = LANGUAGES.get(Path(file).suffix.lower())
    try:
        raw = (base / file).read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if len(raw) > MAX_TEST_BYTES or language is None:
        return None
    expects = _expects(text, file, language, sources)
    edits: list[tuple[Edit, Migration, int]] = []  # with the index of the expectation
    for n, ex in enumerate(expects):
        found = _expect_edits(ex, text, file, language, eligible, index)
        edits += [(e, m, n) for e, m in found or ()]
    if not edits or len(edits) > MAX_EDITS:
        return None
    try:
        content = apply(text, [e for e, _, _ in edits])
    except ValueError:
        return None
    if not _verify(text, content, file, language, expects, edits, index, sources):
        return None
    from .fixer import blob_sha  # the fixer imports this module

    out = []
    for e, m, _ in sorted(edits, key=lambda x: x[0].start):
        keys = [t.pos for t in tokens(text, language, leaf(m.target)) if e.start <= t.pos < e.end]
        line = text.count("\n", 0, keys[0] if keys else e.start) + 1
        out.append(TestEdit(line, m.path, m.target, m.replacement, m.action, e.start, e.end, e.text))
    return TestFix(file, blob_sha(raw), content, out, original=text)


def _expects(text: str, file: str, language: str, sources) -> list[fixtestfind.Expect]:
    found = fixtestfind.find(text, language)
    if language == "ruby":
        found += fixtestbuild.find(text, file, sources)
    return found


def _usage(ex: fixtestfind.Expect, text: str, file: str, index) -> tuple[str, Usage | None]:
    virtual = ex.virtual(text)
    usages = [u for u in scan_text(virtual, "virtual" + Path(file).suffix.lower(), index)[0] if u.line == 1]
    usages = [u for u in usages if u.language not in ("http", "doc")]
    return virtual, usages[0] if len(usages) == 1 else None


def _sends(m: Migration, fields: frozenset[str]) -> bool:
    return m.target in fields or (m.action == "delete" and bool(migrations.expected_delta(m, fields)[0]))


def _expect_edits(ex, text: str, file: str, language: str, eligible, index) -> list[tuple[Edit, Migration]] | None:
    """The edits of one expectation, in test offsets; None when it names a qualifying field it cannot edit."""
    virtual, u = _usage(ex, text, file, index)
    if u is None:
        return None
    wanted = [m for key, m in eligible.items() if (key[0], m.method) == (u.path, u.method) and ex.only in (None, key)]
    wanted = [m for m in wanted if _sends(m, u.fields)]
    if not wanted:
        return None
    locator = (phpfix.Locator if language == "php" else Locator)(virtual, language, index)
    blanked = {p for p, _ in ex.subs}
    out = []
    for m in wanted:
        sites = locator.sites(u, m)
        if not sites.found_call or sites.incomplete or sites.unsupported or sites.why or not sites.edits:
            return None
        for e in sites.edits:
            s, t = e.start - ex.shift, e.end - ex.shift
            if not (ex.lo <= s <= t <= ex.hi) or any(s <= p < t for p in blanked) or virtual[e.start : e.end] != text[s:t]:
                return None
            out.append((Edit(s, t, e.text, e.key, e.shorthand, e.timestamp), m))
    return out


def _verify(text: str, content: str, file: str, language: str, expects, edits, index, sources) -> bool:
    if language == "python":
        try:
            ast.parse(content)
        except (SyntaxError, ValueError):
            return False
    if language == "php" and not phplex.same_structure(text, content, [e for e, _, _ in edits]):
        return False
    after = _expects(content, file, language, sources)
    if [(x.kind, x.head, x.only) for x in expects] != [(x.kind, x.head, x.only) for x in after]:
        return False
    for n, (xb, xa) in enumerate(zip(expects, after)):
        _, ub = _usage(xb, text, file, index)
        _, ua = _usage(xa, content, file, index)
        mine = [(e, m) for e, m, k in edits if k == n]
        if ub is None or ua is None:
            if mine or (ub is None) != (ua is None):
                return False
            continue
        removed, added = set(), set()
        for e, m in mine:
            r, a = migrations.expected_delta(m, ub.fields, timestamp=e.timestamp)
            removed, added = removed | r, added | a
        if (ub.path, ub.method) != (ua.path, ua.method) or ub.fields - ua.fields != removed or ua.fields - ub.fields != added:
            return False
    shape = lambda us: [(u.method, u.path, u.call, u.complete, u.fields) for u in us]  # noqa: E731
    if shape(scan_text(text, file, index)[0]) != shape(scan_text(content, file, index)[0]):
        return False
    for name in {leaf(m.target) for _, m, _ in edits}:
        found, left = tokens(text, language, name), tokens(content, language, name)
        expected = sum(
            sum(1 for t in found if e.start <= t.pos < e.end) - len(tokens(e.text, language, name))
            for e, m, _ in edits if leaf(m.target) == name
        )
        if len(found) - len(left) != expected:
            return False
    return True


def render_text(fixes: list[TestFix], tests: list[tuple[str, int]]) -> list[str]:
    """The ``apidrift fix`` lines for tests: the diffs, the updated lines, then the lines left for a person."""
    lines: list[str] = []
    for fix in fixes:
        lines += ["", fix.diff().rstrip("\n")]
    edited = {(f.file, e.line) for f in fixes for e in f.edits}
    if edited:
        lines += ["", "tests updated to expect the new request:"]
        lines += [f"  {f}:{line}" for f, line in sorted(edited)]
    rest = [t for t in tests if t not in edited]
    if rest:
        lines += ["", "tests that still name the old field (update those that expect the old request):"]
        lines += [f"  {f}:{line}" for f, line in rest]
    return lines
