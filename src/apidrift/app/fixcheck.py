"""What the parent checks before it pushes a fix the sandboxed child planned, and how it builds the commit.

Every value in a fix is child output computed from untrusted repository bytes. A fix is pushed only when its
path is a plain relative path in a scanned language outside ``.github/``, its tokens are Stripe field/path
names, every site matches a migration in the parent's own table (same action, same replacement), it fits the
caps, and GitHub confirms the file at the base commit has exactly the ``blob_sha`` the child rewrote. The
pushed content is never the child's: the parent fetches the base blob itself (it must hash to that sha) and
applies the child's edits, each of which must be the documented action of one of the fix's sites on its key,
written the way the fixer writes it (:mod:`editshape`). File modes come from the base tree, never from a
guess. One bad site or edit rejects the whole file: content cannot be pushed in part.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import PurePosixPath

from .. import fixpins, fixsdk, migrations
from ..scanner import LANGUAGES
from . import issue
from .github import GitHubClient, GitHubError

MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES = 25, 1_000_000, 3_000_000
MAX_EDITS, MAX_EDIT_TEXT = 400, 2000
BLOB_MODES = ("100644", "100755")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
logger = logging.getLogger("apidrift.fixpr")


def validate(gh: GitHubClient, token: str, full_name: str, base_sha: str, fixes: object) -> list[dict]:
    """The fixes the parent is willing to push, each ``{file, blob_sha, content, sites, original}``, the content
    rebuilt here from the base blob and the child's edits (:func:`rebuild`); the child's own content is never
    pushed. A GitHub error raises: a check that cannot read every blob pushes nothing (no reduced PR)."""
    accepted: list[dict] = []
    total = 0
    for fix in claimed(fixes):
        if len(accepted) >= MAX_FILES:
            logger.info("repo %s: not pushing one file (over the size caps)", full_name)
            continue
        text, why = base_text(gh, token, full_name, base_sha, fix["file"], fix["blob_sha"])
        content = None
        if why is None:
            content, why = rebuild(text, fix)
        size = len(content.encode("utf-8")) if content is not None else 0
        if why is None and (size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES):
            why = "over the size caps"
        if why is not None:
            logger.info("repo %s: not pushing one file (%s)", full_name, why)
            continue
        total += size
        kept = {k: v for k, v in fix.items() if k != "content_claim"}
        accepted.append({**kept, "content": content, "original": text})
    return accepted


def claimed(fixes: object) -> list[dict]:
    """The child's fixes that pass the checks needing no network, reduced to ``{file, blob_sha, sites, edits}``
    (edits still unchecked): what the fingerprint of an unchanged pull request is computed from."""
    out = []
    for fix in fixes if isinstance(fixes, list) else []:
        if not isinstance(fix, dict):
            continue
        why = reject(fix.get("file"), fix.get("blob_sha"), fix.get("content", "")) or reject_sites(fix.get("sites"))
        if why is None and any(f["file"] == fix["file"] for f in out):
            why = "the file is twice in the plan"
        edits = _edits(fix.get("edits"), {s["target"] for s in fix["sites"]}) if why is None else None
        if why is None and edits is None:
            why = "malformed edits"
        if why is not None:
            logger.info("not pushing one file (%s)", why)
            continue
        out.append({"file": fix["file"], "blob_sha": fix["blob_sha"], "sites": [_site(s) for s in fix["sites"]],
                    "edits": edits, "content_claim": fix.get("content")})
    return out


def rebuild(text: str, fix: dict) -> tuple[str | None, str | None]:
    """The fixed file from the base text and the fix's edits, when every edit is the documented action of one
    of the fix's sites on its key (:func:`editshape.shape`); else (None, why)."""
    from .editshape import shape  # the shapes the fixer writes

    language = LANGUAGES[PurePosixPath(fix["file"]).suffix.lower()]
    by_target: dict[str, list] = {}
    for s in fix["sites"]:
        m = migrations.for_site(s["path"], s["target"])
        if m is not None and m not in by_target.setdefault(s["target"], []):
            by_target[s["target"]].append(m)
    for e in fix["edits"]:
        if e["end"] > len(text) or not any(shape(text, language, e, m, code=True) is not None for m in by_target[e["target"]]):
            return None, "an edit is not a documented rewrite of its key"
    content = apply_edits(text, fix["edits"])
    claim = fix.get("content_claim")
    if isinstance(claim, str) and claim != content:
        return None, "the child's content is not its edits"
    return content, None


def apply_edits(text: str, edits: list[dict]) -> str:
    out = text
    for e in sorted(edits, key=lambda e: e["start"], reverse=True):
        out = out[: e["start"]] + e["text"] + out[e["end"] :]
    return out


def _edits(items: object, targets: set[str]) -> list[dict] | None:
    """``{start, end, text, target}`` edits rebuilt with exactly these keys, sorted; None when one is malformed,
    names a key no site of the fix declares, or overlaps another."""
    if not isinstance(items, list) or not 0 < len(items) <= MAX_EDITS:
        return None
    out = []
    for e in items:
        if not isinstance(e, dict) or any(type(e.get(k)) is not int for k in ("start", "end")):
            return None
        if not isinstance(e.get("text"), str) or len(e["text"]) > MAX_EDIT_TEXT or not 0 <= e["start"] <= e["end"]:
            return None
        if e.get("target") not in targets:
            return None
        out.append({"start": e["start"], "end": e["end"], "text": e["text"], "target": e["target"]})
    out.sort(key=lambda e: (e["start"], e["end"]))
    if any(a["end"] > b["start"] or a["start"] == b["start"] for a, b in zip(out, out[1:])):
        return None
    return out


def base_text(gh: GitHubClient, token: str, full_name: str, base_sha: str, file: str, sha: str) -> tuple[str | None, str | None]:
    """The file's text at the base commit, read by the parent itself: GitHub must name ``sha`` for the path, the
    size must fit, and the blob fetched by that sha must hash to it. GitHub errors raise."""
    meta = _content_meta(gh, token, full_name, file, base_sha)
    if meta.get("type") != "file" or meta.get("sha") != sha:
        return None, "not the file that was scanned"
    size = meta.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size > MAX_FILE_BYTES:
        return None, "over the size caps"
    raw = gh.get_blob(token, full_name, sha)
    if len(raw) != size or hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest() != sha:
        return None, "the blob is not the one named"
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "the file is not UTF-8"


def reject(file: object, blob_sha: object, content: object) -> str | None:
    """Why a fix cannot be pushed on its face, or None."""
    if not isinstance(file, str) or not issue.valid_path(file):
        return "invalid path"
    pure = PurePosixPath(file)
    if file != pure.as_posix() or file.endswith("/") or any(part in ("", ".") for part in pure.parts):
        return "path is not normalised"
    if pure.parts[0].lower() == ".github":
        return "path under .github"
    if pure.suffix.lower() not in LANGUAGES:
        return "not a scanned language"
    if not isinstance(blob_sha, str) or not _SHA_RE.fullmatch(blob_sha):
        return "no blob sha"
    if not isinstance(content, str):
        return "no content"
    return None


def reject_sites(sites: object) -> str | None:
    """Why a fix's sites cannot be pushed, or None. The child's ``action`` and ``replacement`` must be exactly
    what the parent's own migration table says for (path, target): a child cannot invent an edit."""
    if not isinstance(sites, list) or not sites:
        return "no sites"
    for site in sites:
        if not isinstance(site, dict):
            return "malformed site"
        line = site.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            return "malformed site line"
        for key, pattern in (("path", issue.PATH_RE), ("target", issue.FIELD_PATH_RE)):
            value = site.get(key)
            if not isinstance(value, str) or not pattern.fullmatch(value):
                return f"malformed site {key}"
        replacement, action = site.get("replacement"), site.get("action")
        if not isinstance(replacement, str) or (replacement and not issue.FIELD_PATH_RE.fullmatch(replacement)):
            return "malformed site replacement"
        if not isinstance(action, str) or action not in migrations.ACTIONS:
            return "malformed site action"
        migration = migrations.for_site(site["path"], site["target"])
        if migration is None:
            return "no documented migration for this site"
        if action != migration.action or replacement != migration.replacement:
            return "site does not match the documented migration"
    return None


def _site(site: dict) -> dict:
    """A site already checked by :func:`reject_sites`, rebuilt with exactly the keys the parent uses."""
    migration = migrations.for_site(site["path"], site["target"])
    assert migration is not None  # reject_sites admitted it
    return {
        "line": site["line"], "path": site["path"], "target": migration.target,
        "replacement": migration.replacement, "action": migration.action,
    }


LOCKFILE_RE = {  # each SDK's own lockfiles and manifests: a lock is believed only from its ecosystem's file
    "stripe-ruby": re.compile(r"Gemfile\.lock"),
    "stripe-php": re.compile(r"composer\.lock"),
    "stripe-go": re.compile(r"go\.mod"),
    "stripe-node": re.compile(r"package-lock\.json|yarn\.lock|pnpm-lock\.yaml|package\.json"),
    "stripe-python": re.compile(r"poetry\.lock|uv\.lock|Pipfile\.lock|requirements[A-Za-z0-9_.-]{0,40}\.txt"),
}
VENDORED = {"node_modules", "vendor"}  # a dependency's own lockfile is not the project's


PIN_VERSION_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:\.[A-Za-z][A-Za-z0-9_]{0,30})?")
MAX_PINS = 100


def pin_list(items: object) -> list[dict]:
    """The child's API-version pins the parent will mention, rebuilt from checked values: a path
    :func:`issue.valid_path` admits, a line number, and a dated version string (``2024-06-20``,
    ``2025-04-30.basil``) that the parent can compare with its own table."""
    out: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or len(out) >= MAX_PINS:
            continue
        file, line, version = item.get("file"), item.get("line"), item.get("version")
        if not isinstance(file, str) or not issue.valid_path(file) or file != PurePosixPath(file).as_posix():
            continue
        if isinstance(line, bool) or not isinstance(line, int) or not 1 <= line <= 10_000_000:
            continue
        if not isinstance(version, str) or not PIN_VERSION_RE.fullmatch(version):
            continue
        # A test path is re-derived here from the path alone; "browser" is the child's reading of the file's text.
        kind = "test" if fixpins.test_path(file) else ("browser" if item.get("kind") == "browser" else "server")
        entry = {"file": file, "line": line, "version": version, "kind": kind}
        if entry not in out:
            out.append(entry)
    return out


def sdk_locks(items: object, fixes: list[dict]) -> list[dict]:
    """The child's locked SDK versions the parent will mention, rebuilt from checked values only: a known SDK
    whose language an accepted fix with a non-delete site is written in, a short plain version string, a
    lockfile or manifest path that :func:`issue.valid_path` admits, and a line number."""
    wanted = {
        fixsdk.SDK_BY_SUFFIX.get(PurePosixPath(str(fix.get("file"))).suffix.lower()) for fix in fixes
        if any(isinstance(s, dict) and s.get("action") != "delete" for s in fix.get("sites") or [])
    }
    out: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or len(out) >= fixsdk.MAX_LOCKS:
            continue
        sdk, version, file, line = item.get("sdk"), item.get("version"), item.get("file"), item.get("line")
        if not isinstance(sdk, str) or sdk not in fixsdk.SDKS or sdk not in wanted:
            continue
        if not isinstance(version, str) or len(version) > 40 or not fixsdk.VERSION_SHAPE.fullmatch(version):
            continue
        if not isinstance(file, str) or not issue.valid_path(file) or file != PurePosixPath(file).as_posix():
            continue
        parts = PurePosixPath(file).parts
        if not LOCKFILE_RE[sdk].fullmatch(parts[-1]) or any(p in ("", ".", "..") or p in VENDORED for p in parts):
            continue
        if isinstance(line, bool) or not isinstance(line, int) or not 1 <= line <= 10_000_000:
            continue
        entry = {"sdk": sdk, "version": version, "file": file, "line": line}
        if entry not in out:
            out.append(entry)
    return out


def _content_meta(gh: GitHubClient, token: str, full_name: str, file: str, base_sha: str) -> dict:
    """``{sha, type}`` of ``file`` at the base commit; a path GitHub does not know is simply not a file."""
    try:
        return gh.get_content_meta(token, full_name, file, base_sha)
    except GitHubError as exc:
        if exc.status == 404:
            return {"sha": None, "type": None}
        raise


# --- building the commit -----------------------------------------------------------------


def commit(gh: GitHubClient, token: str, full_name: str, base_sha: str, fixes: list[dict], message: str) -> str:
    """blobs -> tree on top of the base commit's tree (modes preserved) -> commit whose parent is the base."""
    base_tree = str((gh.get_commit(token, full_name, base_sha).get("tree") or {}).get("sha") or "")
    if not _SHA_RE.fullmatch(base_tree):
        raise GitHubError(f"no tree for commit {base_sha[:7]}")
    modes = file_modes(gh, token, full_name, base_tree, [fix["file"] for fix in fixes])
    entries = []
    for fix in fixes:
        mode = modes.get(fix["file"])
        if mode not in BLOB_MODES:
            raise GitHubError("a file to fix is not a regular file in the base tree")
        blob = gh.create_blob(token, full_name, fix["content"])
        entries.append({"path": fix["file"], "mode": mode, "type": "blob", "sha": blob})
    tree = gh.create_tree(token, full_name, base_tree, entries)
    return gh.create_commit(token, full_name, message, tree, [base_sha])


def file_modes(gh: GitHubClient, token: str, full_name: str, base_tree: str, files: list[str]) -> dict[str, str]:
    """The git mode of each file in the base tree. When the recursive listing is truncated, walk the tree one
    level at a time along each file's path instead; a path that cannot be resolved raises (no guessing)."""
    listing = gh.get_tree(token, full_name, base_tree, recursive=True)
    if not listing.get("truncated"):
        return {
            str(e.get("path")): str(e.get("mode") or "") for e in listing.get("tree") or []
            if isinstance(e, dict) and e.get("type") == "blob"
        }
    modes: dict[str, str] = {}
    levels: dict[str, list[dict]] = {}
    for file in files:
        parts = PurePosixPath(file).parts
        sha = base_tree
        for index, part in enumerate(parts):
            if sha not in levels:
                levels[sha] = [e for e in gh.get_tree(token, full_name, sha, recursive=False).get("tree") or [] if isinstance(e, dict)]
            entry = next((e for e in levels[sha] if e.get("path") == part), None)
            last = index == len(parts) - 1
            if entry is None or entry.get("type") != ("blob" if last else "tree"):
                raise GitHubError("could not read a file's mode from the base tree")
            if last:
                modes[file] = str(entry.get("mode") or "")
            else:
                sha = str(entry.get("sha") or "")
    return modes
