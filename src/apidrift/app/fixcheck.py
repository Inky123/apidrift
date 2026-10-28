"""What the parent checks before it pushes a fix the sandboxed child planned, and how it builds the commit.

Every value in a fix is child output computed from untrusted repository bytes. A fix is pushed only when its
path is a plain relative path in a scanned language outside ``.github/``, its tokens are Stripe field/path
names, its sites are well-formed, it fits the caps, and GitHub confirms the file at the base commit has
exactly the ``blob_sha`` the child rewrote. File modes come from the base tree, never from a guess.
"""

from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath

from ..scanner import LANGUAGES
from . import issue
from .github import GitHubClient, GitHubError

MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES = 25, 1_000_000, 3_000_000
BLOB_MODES = ("100644", "100755")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
logger = logging.getLogger("apidrift.fixpr")


def validate(gh: GitHubClient, token: str, full_name: str, base_sha: str, fixes: object) -> list[dict]:
    """The fixes the parent is willing to push, each reduced to ``{file, blob_sha, content, sites}``."""
    accepted: list[dict] = []
    total = 0
    for fix in fixes if isinstance(fixes, list) else []:
        if not isinstance(fix, dict):
            continue
        why = reject(fix.get("file"), fix.get("blob_sha"), fix.get("content")) or reject_sites(fix.get("sites"))
        size = 0
        if why is None:
            try:
                size = len(fix["content"].encode("utf-8"))
            except UnicodeEncodeError:
                why = "content is not encodable text"
        if why is None and (len(accepted) >= MAX_FILES or size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES):
            why = "over the size caps"
        if why is None:
            meta = _content_meta(gh, token, full_name, fix["file"], base_sha)
            if meta.get("type") != "file" or meta.get("sha") != fix["blob_sha"]:
                why = "not the file that was scanned"
        if why is not None:
            logger.info("repo %s: not pushing one file (%s)", full_name, why)
            continue
        total += size
        accepted.append({
            "file": fix["file"], "blob_sha": fix["blob_sha"], "content": fix["content"],
            "sites": [{"line": s["line"], "path": s["path"], "target": s["target"], "replacement": s["replacement"]} for s in fix["sites"]],
        })
    return accepted


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
    if not isinstance(sites, list) or not sites:
        return "no sites"
    for site in sites:
        if not isinstance(site, dict):
            return "malformed site"
        line = site.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            return "malformed site line"
        for key, pattern in (("path", issue.PATH_RE), ("target", issue.FIELD_RE), ("replacement", issue.FIELD_RE)):
            value = site.get(key)
            if not isinstance(value, str) or not pattern.fullmatch(value):
                return f"malformed site {key}"
    return None


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
