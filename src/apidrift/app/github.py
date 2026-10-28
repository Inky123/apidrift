"""The GitHub REST calls the App makes: App JWT, scoped installation tokens, repo metadata, tarballs, issues.

Tokens are passed in as arguments and used for one request's headers; the client never stores or logs
them. ``scrub`` removes ``token=`` query values and Authorization headers from error text.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx
import jwt

from .config import Settings

API = "https://api.github.com"
USER_AGENT = "apidrift/0.1 (+https://github.com/Inky123/apidrift)"
PERMISSIONS = {"contents": "read", "issues": "write", "metadata": "read"}
# Only the fix step asks for these, with a second token it revokes as soon as the pull request is up.
FIX_PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}
_SHA_RE = re.compile(r"[0-9a-f]{40}")
JWT_BACKDATE, JWT_LIFETIME = 60, 600
MAX_RATE_LIMIT_WAIT = 15 * 60
_TOKEN_RE = re.compile(r"(token=)[^&\s'\"<>]+", re.I)
_AUTH_RE = re.compile(r"(authorization['\":\s]*)(?:bearer|token)\s+[^\s'\",]+", re.I)

# httpx logs every request URL at INFO; the tarball redirect carries a token in its query string.
logging.getLogger("httpx").setLevel(logging.WARNING)


class GitHubError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimited(GitHubError):
    """GitHub asked us to wait; ``reset_at`` is epoch seconds, already capped at MAX_RATE_LIMIT_WAIT."""

    def __init__(self, reset_at: float) -> None:
        super().__init__(f"rate limited until {int(reset_at)}", 429)
        self.reset_at = reset_at


class TarballTooLarge(GitHubError):
    pass


def scrub(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Error text safe to store: no token query values, no Authorization values, no known secrets."""
    out = _AUTH_RE.sub(r"\1***", _TOKEN_RE.sub(r"\1***", text))
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


def _sha(value: object, what: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise GitHubError(f"GitHub returned no sha for the {what}")
    return value


def _rate_limit_reset(headers: httpx.Headers, now: float) -> float | None:
    retry_after = headers.get("retry-after")
    if retry_after and retry_after.strip().isdigit():
        return min(now + int(retry_after), now + MAX_RATE_LIMIT_WAIT)
    if headers.get("x-ratelimit-remaining", "").strip() == "0":
        reset = headers.get("x-ratelimit-reset", "").strip()
        when = float(reset) if reset.replace(".", "", 1).isdigit() else now + 60
        return min(max(when, now + 1), now + MAX_RATE_LIMIT_WAIT)
    return None


class GitHubClient:
    """Thin wrapper over the handful of endpoints the App needs."""

    def __init__(self, settings: Settings, http: httpx.Client | None = None, clock: Callable[[], float] = time.time) -> None:
        self.settings = settings
        self._key = settings.private_key()
        self._clock = clock
        self._http = http or httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True)

    # --- auth -----------------------------------------------------------------

    def app_jwt(self) -> str:
        now = int(self._clock())
        claims = {"iat": now - JWT_BACKDATE, "exp": now - JWT_BACKDATE + JWT_LIFETIME, "iss": self.settings.github_app_id}
        return jwt.encode(claims, self._key, algorithm="RS256")

    def installation_token(self, installation_id: int, repo_id: int, permissions: dict[str, str] = PERMISSIONS) -> str:
        """A token limited to one repository and ``permissions`` (by default the read-only check set)."""
        return self.mint_token(installation_id, repo_id, permissions)[0]

    def mint_token(
        self, installation_id: int, repo_id: int, permissions: dict[str, str] = PERMISSIONS
    ) -> tuple[str, dict[str, str]]:
        """(token, the permissions GitHub says it granted). Asking for more than the installation accepted is a 422."""
        body = {"repository_ids": [repo_id], "permissions": dict(permissions)}
        response = self._request(
            "POST", f"/app/installations/{installation_id}/access_tokens", bearer=self.app_jwt(), json=body
        )
        data = response.json()
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise GitHubError("installation token response had no token")
        granted = data.get("permissions")
        return token, dict(granted) if isinstance(granted, dict) else {}

    def get_installation(self, installation_id: int) -> dict:
        """The installation object (App JWT): ``permissions`` accepted so far, ``html_url`` of its settings page."""
        data = self._request("GET", f"/app/installations/{installation_id}", bearer=self.app_jwt()).json()
        return data if isinstance(data, dict) else {}

    def installation_permissions(self, installation_id: int) -> dict[str, str]:
        granted = self.get_installation(installation_id).get("permissions")
        return dict(granted) if isinstance(granted, dict) else {}

    def revoke_token(self, token: str) -> None:
        """Invalidate an installation token once a job is done with it (``DELETE /installation/token``)."""
        self._request("DELETE", "/installation/token", bearer=token)

    # --- repositories -----------------------------------------------------------

    def get_repo(self, token: str, full_name: str) -> dict:
        """Repository metadata; a renamed or transferred repo answers 301, which is followed, and the
        returned ``full_name`` is the current one (the caller stores it)."""
        return self._request("GET", f"/repos/{full_name}", bearer=token).json()

    def branch_sha(self, token: str, full_name: str, branch: str) -> str:
        data = self._request("GET", f"/repos/{full_name}/branches/{quote(branch, safe='')}", bearer=token).json()
        sha = (data.get("commit") or {}).get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitHubError(f"no commit sha for branch {branch!r} of {full_name}")
        return sha

    def download_tarball(self, token: str, full_name: str, sha: str, dest: Path, max_bytes: int) -> int:
        """Stream the tarball of ``sha`` to ``dest``; raises TarballTooLarge past ``max_bytes``."""
        url = f"{API}/repos/{full_name}/tarball/{sha}"
        total = 0
        with self._http.stream("GET", url, headers=self._headers(token), follow_redirects=True) as response:
            self._check(response, f"tarball of {full_name}")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise TarballTooLarge(f"tarball of {full_name} is {int(declared):,} bytes; the cap is {max_bytes:,}")
            with open(dest, "wb") as fh:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise TarballTooLarge(f"tarball of {full_name} exceeds the {max_bytes:,} byte cap")
                    fh.write(chunk)
        return total

    # --- issues -----------------------------------------------------------------

    def list_bot_issues(self, token: str, full_name: str) -> list[dict]:
        """Issues (not pull requests) created by this App in the repo, newest first."""
        params = {"creator": self.settings.bot_login, "state": "all", "per_page": "100"}
        data = self._request("GET", f"/repos/{full_name}/issues", bearer=token, params=params).json()
        issues = [item for item in data if isinstance(item, dict) and "pull_request" not in item]
        return sorted(issues, key=lambda item: item.get("number", 0), reverse=True)

    def get_issue(self, token: str, full_name: str, number: int) -> dict:
        return self._request("GET", f"/repos/{full_name}/issues/{number}", bearer=token).json()

    def create_issue(self, token: str, full_name: str, title: str, body: str) -> dict:
        return self._request("POST", f"/repos/{full_name}/issues", bearer=token, json={"title": title, "body": body}).json()

    def update_issue(self, token: str, full_name: str, number: int, **fields: str) -> dict:
        return self._request("PATCH", f"/repos/{full_name}/issues/{number}", bearer=token, json=fields).json()

    def comment(self, token: str, full_name: str, number: int, body: str) -> dict:
        path = f"/repos/{full_name}/issues/{number}/comments"
        return self._request("POST", path, bearer=token, json={"body": body}).json()

    # --- git data and pull requests (the fix step; write-scoped token) ----------------

    def get_content_meta(self, token: str, full_name: str, path: str, ref: str) -> dict:
        """``{sha, type}`` of ``path`` at ``ref``; ``type`` is file, dir, symlink or submodule."""
        encoded = "/".join(quote(segment, safe="") for segment in path.split("/"))
        data = self._request("GET", f"/repos/{full_name}/contents/{encoded}", bearer=token, params={"ref": ref}).json()
        if isinstance(data, list):
            return {"sha": None, "type": "dir"}
        return {"sha": data.get("sha"), "type": data.get("type")} if isinstance(data, dict) else {"sha": None, "type": None}

    def get_commit(self, token: str, full_name: str, sha: str) -> dict:
        return self._request("GET", f"/repos/{full_name}/git/commits/{sha}", bearer=token).json()

    def branch_head(self, token: str, full_name: str, branch: str) -> tuple[str, str]:
        """(commit sha, tree sha) at the tip of ``branch``."""
        data = self._request("GET", f"/repos/{full_name}/branches/{quote(branch, safe='')}", bearer=token).json()
        commit = data.get("commit") or {}
        return _sha(commit.get("sha"), f"branch {branch}"), _sha(((commit.get("commit") or {}).get("tree") or {}).get("sha"), f"tree of {branch}")

    def get_tree(self, token: str, full_name: str, sha: str, recursive: bool = True) -> dict:
        params = {"recursive": "1"} if recursive else None
        return self._request("GET", f"/repos/{full_name}/git/trees/{sha}", bearer=token, params=params).json()

    def create_blob(self, token: str, full_name: str, content: str) -> str:
        """Store ``content`` (text; sent as base64 of its UTF-8 bytes) and return the blob sha."""
        body = {"content": base64.b64encode(content.encode("utf-8")).decode("ascii"), "encoding": "base64"}
        return _sha(self._request("POST", f"/repos/{full_name}/git/blobs", bearer=token, json=body).json().get("sha"), "blob")

    def create_tree(self, token: str, full_name: str, base_tree: str, entries: list[dict]) -> str:
        body = {"base_tree": base_tree, "tree": entries}
        return _sha(self._request("POST", f"/repos/{full_name}/git/trees", bearer=token, json=body).json().get("sha"), "tree")

    def create_commit(self, token: str, full_name: str, message: str, tree: str, parents: list[str]) -> str:
        body = {"message": message, "tree": tree, "parents": parents}
        return _sha(self._request("POST", f"/repos/{full_name}/git/commits", bearer=token, json=body).json().get("sha"), "commit")

    def get_ref(self, token: str, full_name: str, ref: str) -> dict | None:
        """``ref`` like ``heads/apidrift/stripe-x``; None when it does not exist."""
        try:
            return self._request("GET", f"/repos/{full_name}/git/ref/{ref}", bearer=token).json()
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise

    def create_ref(self, token: str, full_name: str, ref: str, sha: str) -> dict:
        """``ref`` is fully qualified here: ``refs/heads/...``."""
        return self._request("POST", f"/repos/{full_name}/git/refs", bearer=token, json={"ref": ref, "sha": sha}).json()

    def delete_ref(self, token: str, full_name: str, ref: str) -> None:
        """``ref`` as ``heads/<branch>``."""
        self._request("DELETE", f"/repos/{full_name}/git/refs/{ref}", bearer=token)

    def update_ref(self, token: str, full_name: str, ref: str, sha: str, force: bool = False) -> dict:
        body = {"sha": sha, "force": bool(force)}
        return self._request("PATCH", f"/repos/{full_name}/git/refs/{ref}", bearer=token, json=body).json()

    def create_pull(self, token: str, full_name: str, title: str, body: str, head: str, base: str) -> dict:
        fields = {"title": title, "body": body, "head": head, "base": base}
        return self._request("POST", f"/repos/{full_name}/pulls", bearer=token, json=fields).json()

    def update_pull(self, token: str, full_name: str, number: int, **fields: str) -> dict:
        return self._request("PATCH", f"/repos/{full_name}/pulls/{number}", bearer=token, json=fields).json()

    def get_pull(self, token: str, full_name: str, number: int) -> dict:
        return self._request("GET", f"/repos/{full_name}/pulls/{number}", bearer=token).json()

    def find_pull(self, token: str, full_name: str, head: str) -> dict | None:
        """The open pull request from ``head`` (``owner:branch``), if there is one."""
        params = {"head": head, "state": "all", "per_page": "100"}
        data = self._request("GET", f"/repos/{full_name}/pulls", bearer=token, params=params).json()
        pulls = [p for p in data if isinstance(p, dict) and p.get("state") == "open"] if isinstance(data, list) else []
        return pulls[0] if pulls else None

    # --- plumbing ---------------------------------------------------------------

    @staticmethod
    def _headers(bearer: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }

    def _request(
        self, method: str, path: str, *, bearer: str, json: Any = None, params: dict[str, str] | None = None
    ) -> httpx.Response:
        response = self._http.request(
            method, API + path, headers=self._headers(bearer), json=json, params=params, follow_redirects=True
        )
        self._check(response, f"{method} {path}")
        return response

    def _check(self, response: httpx.Response, what: str) -> None:
        status = response.status_code
        if status in (403, 429):
            reset = _rate_limit_reset(response.headers, self._clock())
            if reset is not None:
                raise RateLimited(reset)
        if status >= 400:
            raise GitHubError(f"GitHub returned {status} for {what}", status)
