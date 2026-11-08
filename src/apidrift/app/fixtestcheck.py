"""What the parent checks before it pushes a test the sandboxed child updated, and how it rebuilds the file.

The child sends no content for a test, only edits against the base blob: ``(start, end, text)`` with the line,
the endpoint, the field, the action and the replacement. Nothing of that is trusted. A test file is pushed
only when:

- its path passes :func:`fixcheck.reject`, is a test location (the scanner's test-directory and test-file
  rules), is not a file the code fix pushes, and the child listed every edited line among the tests;
- every edit names a migration in the parent's own table with the same action and replacement, and one the
  accepted code fixes change at every call: not when the parent dropped one of the child's files for that
  field, nor when the child reported a site of it unfixable, unattempted or possibly still sent;
- every edit lies inside an expectation the parent finds itself in the base text, with the same readers the
  child used (:mod:`fixtestfind`, and :mod:`fixtestbuild` over the code fixes the parent rebuilt);
- the test files fit the commit's caps together with the code fixes;
- GitHub reports the path at the base commit with the declared blob sha, and the blob the parent fetches by
  that sha hashes to it (the parent reads the base bytes itself, never the child's copy);
- every edit is the declared action on the declared key, written in one of the few shapes the fixer writes
  (:func:`editshape.shape`): a rename swaps the key's name and nothing else; a deletion removes one ``key: value``
  entry, balanced, with at most one adjacent comma; a rename_first keeps the key's quoting and puts the first
  element of a literal array (or ``v[0]``) in its place; a wrap_type puts the old value, a plain string,
  number or name, inside ``{type: ...}``.

The parent then applies the edits to the base text itself. One bad edit rejects the whole file: it stays in
the pull request's list of tests for a person to check.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from .. import migrations
from ..scanner import LANGUAGES, TEST_DIRS, TEST_FILE
from . import fixcheck, issue
from .editshape import SEPARATORS, shape
from .github import GitHubClient

MAX_FILES, MAX_EDITS, MAX_TEXT = 25, 50, 2000
logger = logging.getLogger("apidrift.fixpr")


def validate(gh: GitHubClient, token: str, full_name: str, base_sha: str, payload: dict, fixes: list[dict]) -> list[dict]:
    """The test files the parent is willing to push, each ``{file, blob_sha, content, edits}``, its content
    built here from the base blob; ``fixes`` are the code fixes :func:`fixcheck.validate` accepted. The code
    fixes and the tests share the commit's caps. A GitHub error raises: no pull request goes out with fewer
    tests than this check would have updated."""
    accepted: list[dict] = []
    total = sum(len(f["content"].encode("utf-8")) for f in fixes)
    for item in claimed(payload, fixes):
        if len(fixes) + len(accepted) >= fixcheck.MAX_FILES:
            logger.info("repo %s: not pushing one test file (over the size caps)", full_name)
            continue
        file, edits = item["file"], item["edits"]
        text, why = fixcheck.base_text(gh, token, full_name, base_sha, file, item["blob_sha"])
        content = fixcheck.apply_edits(text, edits) if why is None else ""
        size = len(content.encode("utf-8"))
        if why is None and total + size > fixcheck.MAX_TOTAL_BYTES:
            why = "over the size caps"
        if why is None:
            why = _check(text, LANGUAGES[PurePosixPath(file).suffix.lower()], edits, file, item["listed"], fixes)
        if why is not None:
            logger.info("repo %s: not pushing one test file (%s)", full_name, why)
            continue
        total += size
        accepted.append({
            "file": file, "blob_sha": item["blob_sha"], "content": content,
            "edits": [{k: e[k] for k in ("line", "path", "target", "replacement", "action")} for e in edits],
        })
    return accepted


def claimed(payload: dict, fixes: list[dict]) -> list[dict]:
    """The child's test fixes that pass the checks needing no network (path, listed lines, edits naming a
    migration the code fixes ``fixes`` change): what an unchanged pull request's fingerprint is taken over."""
    pushed = {f["file"] for f in fixes}
    # A field qualifies only if this pull request changes every call that sends it: not when the parent dropped
    # one of the child's files for it, nor when the child reported a site of it left unchanged.
    raw = payload.get("fixes") if isinstance(payload.get("fixes"), list) else []
    left = {(str(s.get("path")), str(s.get("target"))) for f in raw if isinstance(f, dict) and f.get("file") not in pushed
            for s in (f.get("sites") if isinstance(f.get("sites"), list) else []) if isinstance(s, dict)}
    for key in ("unfixable", "not_attempted", "possibly_remaining"):
        items = payload.get(key) if isinstance(payload.get(key), list) else []
        left |= {(str(u.get("path")), str(u.get("target"))) for u in items if isinstance(u, dict)}
    changed = {(s["path"], s["target"]) for f in fixes for s in f["sites"]} - left
    listed = {(t["file"], t["line"]) for t in payload.get("tests") or [] if isinstance(t, dict)
              and isinstance(t.get("file"), str) and type(t.get("line")) is int}
    items = payload.get("test_fixes")
    out: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or len(out) >= MAX_FILES:
            continue
        file = item.get("file")
        why = fixcheck.reject(file, item.get("blob_sha"), "") or _reject_file(file, pushed, out)
        edits = None if why else _edits(item.get("edits"), changed)
        if why is None and edits:
            out.append({"file": file, "blob_sha": item["blob_sha"], "edits": edits, "listed": listed})
    return out


def is_test(file: str) -> bool:
    *dirs, name = file.split("/")
    return any(d in TEST_DIRS for d in dirs) or bool(TEST_FILE.match(name))


def _reject_file(file: str, pushed: set[str], accepted: list[dict]) -> str | None:
    if not is_test(file):
        return "not a test file"
    if LANGUAGES[PurePosixPath(file).suffix.lower()] not in SEPARATORS:
        return "not a language whose tests are updated"
    if file in pushed or any(a["file"] == file for a in accepted):
        return "the file is already in this pull request"
    return None


def _edits(items: object, changed: set[tuple[str, str]]) -> list[dict] | None:
    """The edits rebuilt with exactly the keys the parent uses; None when one is malformed or not ours."""
    if not isinstance(items, list) or not 0 < len(items) <= MAX_EDITS:
        return None
    out = []
    for e in items:
        if not isinstance(e, dict) or any(type(e.get(k)) is not int for k in ("line", "start", "end")):
            return None
        text, path, target = e.get("text"), e.get("path"), e.get("target")
        if not isinstance(text, str) or len(text) > MAX_TEXT or not 0 <= e["start"] <= e["end"]:
            return None
        if not (isinstance(path, str) and issue.PATH_RE.fullmatch(path) and isinstance(target, str)):
            return None
        m = migrations.for_site(path, target)
        if m is None or (e.get("action"), e.get("replacement")) != (m.action, m.replacement) or (path, target) not in changed:
            return None
        out.append({"line": e["line"], "start": e["start"], "end": e["end"], "text": text, "path": m.path,
                    "target": m.target, "replacement": m.replacement, "action": m.action})
    out.sort(key=lambda e: (e["start"], e["end"]))
    if any(a["end"] > b["start"] or a["start"] == b["start"] for a, b in zip(out, out[1:])):
        return None  # overlapping, or two edits at one place
    return out


def _check(
    text: str, language: str, edits: list[dict], file: str, listed: set[tuple[str, int]], fixes: list[dict],
) -> str | None:
    expects = _expectations(text, language, file, fixes)
    if expects is None:
        return "the test file could not be read"
    for e in edits:
        if e["end"] > len(text):
            return "an edit lies outside the file"
        m = migrations.for_site(e["path"], e["target"])
        assert m is not None  # _edits admitted it
        at = shape(text, language, e, m)
        if at is None:
            return f"an edit is not a plain {m.action} of the key"
        if text.count("\n", 0, at) + 1 != e["line"] or (file, e["line"]) not in listed:
            return "an edit's line is not a listed test line"
        if not any(lo <= e["start"] and e["end"] <= hi for lo, hi in expects):
            return "an edit is not inside an expectation of the call"
    return None


def _expectations(text: str, language: str, file: str, fixes: list[dict]) -> list[tuple[int, int]] | None:
    """The argument spans of the expectations the parent finds itself in the base text (:mod:`fixtestfind`,
    and for Ruby the builder specs of the code it accepted, :mod:`fixtestbuild`); None when reading fails."""
    from .. import fixtestbuild, fixtestfind  # the readers the child used, run here on the bytes GitHub gave

    try:
        found = fixtestfind.find(text, language)
        if language == "ruby":
            sources = []
            for fix in fixes:
                if LANGUAGES[PurePosixPath(fix["file"]).suffix.lower()] == "ruby":
                    sites = [(s["path"], s["target"], s["line"], "") for s in fix["sites"]]
                    sources += fixtestbuild.builders(fix["file"], fix["original"], fix["content"], sites)
            found += fixtestbuild.find(text, file, sources)
    except Exception:  # noqa: BLE001 - untrusted bytes: a reader failure only means this test is not updated
        return None
    return [(x.lo, x.hi) for x in found]
