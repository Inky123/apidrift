"""The GitHub REST calls the App makes: App JWT, scoped installation tokens, repo metadata, tarballs, issues.

Tokens are passed in as arguments and used for one request's headers; the client never stores or logs
them. ``scrub`` removes ``token=`` query values and Authorization headers from error text.
"""

from __future__ import annotations

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

    def installation_token(self, installation_id: int, repo_id: int) -> str:
        """A token limited to one repository and the three permissions the App declares."""
        body = {"repository_ids": [repo_id], "permissions": dict(PERMISSIONS)}
        response = self._request(
            "POST", f"/app/installations/{installation_id}/access_tokens", bearer=self.app_jwt(), json=body
        )
        token = response.json().get("token")
        if not isinstance(token, str) or not token:
            raise GitHubError("installation token response had no token")
        return token

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
