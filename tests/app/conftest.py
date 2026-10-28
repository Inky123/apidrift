"""Shared fixtures: no network, a data dir with two Stripe snapshots, an in-memory db, a fake GitHub."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import re
import socket
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/, for ``specs``
from specs import sdk_doc  # noqa: E402

from apidrift import snapshot as snap  # noqa: E402
from apidrift.app import db as dbmod  # noqa: E402
from apidrift.app.config import Settings  # noqa: E402
from apidrift.app import issue  # noqa: E402
from apidrift.app.checkout import extract_tarball  # noqa: E402
from apidrift.app.github import GitHubClient  # noqa: E402
from apidrift.app.webhooks import handle_event  # noqa: E402
from apidrift.providers import PROVIDERS  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO = FIXTURES / "repo"
SECRET = "It's a Secret to Everybody"
OLD_VERSION, NEW_VERSION, THIRD_VERSION = "2026-01-01.test", "2026-02-01.test", "2026-03-01.test"
SHA = "1a57c3b2f5aaf4ecd8de59ab0b1c3c5638827625"
TOKEN = "ghs_testtoken"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any attempt to open a real connection fails the test."""

    def refuse(*args, **kwargs):
        raise RuntimeError("network access is disabled in tests")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


def _body(doc: dict) -> dict:
    return doc["paths"]["/v1/charges"]["post"]["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]


def old_doc() -> dict:
    doc = sdk_doc()
    doc["info"]["version"] = OLD_VERSION
    return doc


def new_doc() -> dict:
    """The old spec with ``description`` removed from POST /v1/charges, which every fixture file sends."""
    doc = old_doc()
    doc["info"]["version"] = NEW_VERSION
    _body(doc)["properties"].pop("description")
    return doc


def third_doc() -> dict:
    """One release later: ``currency`` goes too (a second finding per call site)."""
    doc = new_doc()
    doc["info"]["version"] = THIRD_VERSION
    _body(doc)["properties"].pop("currency")
    _body(doc)["required"] = ["amount"]
    return doc


def save_snapshot(doc: dict, when: datetime) -> Path:
    path = snap.save(PROVIDERS["stripe"], json.dumps(doc).encode(), now=when)
    assert path is not None
    return path


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("APIDRIFT_DATA", str(data))
    save_snapshot(old_doc(), datetime(2026, 1, 1, tzinfo=timezone.utc))
    save_snapshot(new_doc(), datetime(2026, 2, 1, tzinfo=timezone.utc))
    return data


@pytest.fixture(scope="session")
def rsa_key() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, public


@pytest.fixture
def settings(data_dir, rsa_key, tmp_path) -> Settings:
    pem = tmp_path / "app.pem"
    pem.write_bytes(rsa_key[0])
    return Settings(
        github_app_id="12345", github_app_slug="apidrift-test", github_webhook_secret=SECRET,
        github_private_key_path=pem, host="apidrift.test", data=data_dir, poll_minutes=30,
    )


@pytest.fixture
def conn():
    connection = dbmod.connect(":memory:")
    dbmod.migrate(connection)
    yield connection
    connection.close()


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def tarball_of(tree: Path, topdir: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in sorted(p for p in tree.rglob("*") if p.is_file()):
            tar.add(path, arcname=f"{topdir}/{path.relative_to(tree).as_posix()}")
    return buffer.getvalue()


def blob_sha(data: bytes) -> str:
    """The git blob sha1 of ``data``, what GitHub reports as a file's ``sha``."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _fake_sha(*parts: object) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class FakeGitHub:
    """An ``httpx.MockTransport`` handler covering the GitHub endpoints the App uses; records every request.

    Besides issues it models the installation's granted ``permissions`` (token requests for more are a 422,
    as on GitHub), the git data of ``tree`` at ``sha`` (contents, trees, blobs, commits, refs) and pull
    requests, which share the issue number counter and show up in the issues list with a ``pull_request`` key.
    """

    def __init__(self, public_key: bytes) -> None:
        self.public_key = public_key
        self.full_name, self.repo_id, self.installation_id = "octo/shop", 501, 77
        self.bot = "apidrift-test[bot]"
        self.sha = SHA
        self.repo = {"id": 501, "full_name": "octo/shop", "default_branch": "main", "has_issues": True, "archived": False}
        self.tree = REPO
        self.tree_truncated = False
        self.tarball: bytes | None = None
        self.permissions: dict[str, str] = {"contents": "read", "issues": "write", "metadata": "read"}
        self.installation_url = "https://github.com/organizations/octo/settings/installations/77"
        self.dir_shas: dict[str, Path] = {}
        self.issues: dict[int, dict] = {}
        self.pulls: dict[int, dict] = {}
        self.comments: list[tuple[int, str]] = []
        self.refs: dict[str, str] = {}  # "heads/x" -> sha
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict] = {}
        self.commits: dict[str, dict] = {}
        self.revoked = 0
        self.requests: list[httpx.Request] = []
        self.token_requests: list[dict] = []
        self.responses: dict[tuple[str, str], httpx.Response] = {}
        self.codeload_error: Exception | None = None

    @property
    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method in ("POST", "PATCH", "PUT", "DELETE") and r.url.path.startswith("/repos/")]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def grant_writes(self) -> None:
        self.permissions.update(contents="write", pull_requests="write")

    def _next_number(self) -> int:
        return max([*self.issues, *self.pulls], default=0) + 1

    def add_issue(self, body: str, state: str = "open", closed_by: str | None = None, login: str | None = None) -> dict:
        number = self._next_number()
        self.issues[number] = {
            "number": number, "state": state, "title": "old", "body": body,
            "user": {"login": login or self.bot}, "closed_by": {"login": closed_by} if closed_by else None,
        }
        return self.issues[number]

    def close_by_human(self, number: int, login: str = "zach") -> None:
        self.issues[number].update(state="closed", closed_by={"login": login})

    def close_pull_by_human(self, number: int) -> None:
        self.pulls[number].update(state="closed")

    def merge_pull(self, number: int) -> None:
        self.pulls[number].update(state="closed", merged=True, merged_at="2026-10-28T12:00:00Z")

    def tree_sha(self) -> str:
        return _fake_sha("tree", self.sha)

    def _jwt(self, request: httpx.Request) -> None:
        bearer = request.headers.get("authorization", "")[len("Bearer "):]
        jwt.decode(bearer, self.public_key, algorithms=["RS256"], issuer="12345")

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path
        if request.url.host == "codeload.github.com":
            if self.codeload_error is not None:
                raise self.codeload_error
            data = self.tarball if self.tarball is not None else tarball_of(self.tree, f"octo-shop-{self.sha[:7]}")
            return httpx.Response(200, content=data, headers={"content-type": "application/x-gzip"})
        if (method, path) in self.responses:
            return self.responses.pop((method, path))
        if method == "GET" and path == f"/app/installations/{self.installation_id}":
            self._jwt(request)
            return httpx.Response(200, json={
                "id": self.installation_id, "permissions": dict(self.permissions), "html_url": self.installation_url,
                "account": {"login": "octo", "type": "Organization"},
            })
        if method == "POST" and path == f"/app/installations/{self.installation_id}/access_tokens":
            self._jwt(request)
            body = json.loads(request.content)
            self.token_requests.append(body)
            asked = body.get("permissions") or {}
            denied = [k for k, level in asked.items() if level == "write" and self.permissions.get(k) != "write" or k not in self.permissions]
            if denied:
                return httpx.Response(422, json={"message": "The permissions requested are not granted to this installation"})
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z", "permissions": asked})
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        if method == "DELETE" and path == "/installation/token":
            self.revoked += 1
            return httpx.Response(204)
        base = f"/repos/{self.full_name}"
        if path == base and method == "GET":
            return httpx.Response(200, json=self.repo)
        if path == f"{base}/branches/main" and method == "GET":
            return httpx.Response(200, json={"name": "main", "commit": {"sha": self.sha, "commit": {"tree": {"sha": self.tree_sha()}}}})
        if path == f"{base}/tarball/{self.sha}" and method == "GET":
            location = f"https://codeload.github.com/{self.full_name}/legacy.tar.gz/{self.sha}?token=CODELOADSECRET"
            return httpx.Response(302, headers={"location": location})
        if path.startswith(f"{base}/contents/") or path.startswith(f"{base}/git/") or path.startswith(f"{base}/pulls"):
            return self._git(request, method, path[len(base):])
        if path == f"{base}/issues" and method == "GET":
            creator = request.url.params.get("creator")
            found = [i for i in self.issues.values() if i["user"]["login"] == creator]
            found += [{**p, "pull_request": {"merged_at": p["merged_at"]}} for p in self.pulls.values() if p["user"]["login"] == creator]
            return httpx.Response(200, json=sorted(found, key=lambda i: -i["number"]))
        if path == f"{base}/issues" and method == "POST":
            fields = json.loads(request.content)
            created = self.add_issue(fields["body"])
            created["title"] = fields["title"]
            return httpx.Response(201, json=created)
        match = re.fullmatch(rf"{re.escape(base)}/issues/(\d+)(/comments)?", path)
        if match and (int(match.group(1)) in self.issues or int(match.group(1)) in self.pulls):
            number = int(match.group(1))
            issue = self.issues.get(number) or self.pulls[number]
            if match.group(2) and method == "POST":
                self.comments.append((number, json.loads(request.content)["body"]))
                return httpx.Response(201, json={"id": len(self.comments)})
            if method == "GET":
                return httpx.Response(200, json=issue)
            if method == "PATCH" and number in self.issues:
                fields = json.loads(request.content)
                if fields.get("state") == "closed" and issue["state"] == "open":
                    issue["closed_by"] = {"login": self.bot}
                if fields.get("state") == "open":
                    issue["closed_by"] = None
                issue.update(fields)
                return httpx.Response(200, json=issue)
        return httpx.Response(404, json={"message": "Not Found"})

    def _tree_entries(self, directory: Path, recursive: bool) -> list[dict]:
        """Git tree entries of ``directory``: blobs with their modes, and either subtrees or (recursive) all files."""
        entries: list[dict] = []
        for p in sorted(directory.iterdir()):
            rel = p.relative_to(self.tree).as_posix()
            if p.is_dir():
                if recursive:
                    entries += self._tree_entries(p, True)
                else:
                    sha = _fake_sha("dir", rel)
                    self.dir_shas[sha] = p
                    entries.append({"path": p.name, "mode": "040000", "type": "tree", "sha": sha})
            elif p.is_file():
                mode = "100755" if p.stat().st_mode & 0o111 else "100644"
                entries.append({"path": rel if recursive else p.name, "mode": mode, "type": "blob", "sha": blob_sha(p.read_bytes())})
        return entries

    def _git(self, request: httpx.Request, method: str, rel: str) -> httpx.Response:
        """Git data and pull requests under ``/repos/{full_name}``; ``rel`` is the path after that prefix."""
        body = json.loads(request.content) if request.content else {}
        if rel.startswith("/contents/") and method == "GET":
            target = self.tree / unquote(rel[len("/contents/"):])
            if request.url.params.get("ref") != self.sha or not target.exists():
                return httpx.Response(404, json={"message": "Not Found"})
            if target.is_symlink():
                return httpx.Response(200, json={"type": "symlink", "sha": "0" * 40})
            if target.is_dir():
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"type": "file", "sha": blob_sha(target.read_bytes()), "size": target.stat().st_size})
        if (match := re.fullmatch(r"/git/commits/([0-9a-f]{40})", rel)) and method == "GET":
            sha = match.group(1)
            if sha == self.sha:
                return httpx.Response(200, json={"sha": sha, "tree": {"sha": self.tree_sha()}})
            if sha in self.commits:
                return httpx.Response(200, json={"sha": sha, **self.commits[sha]})
        if (match := re.fullmatch(r"/git/trees/([0-9a-f]{40})", rel)) and method == "GET":
            sha, recursive = match.group(1), request.url.params.get("recursive") == "1"
            directory = self.tree if sha == self.tree_sha() else self.dir_shas.get(sha)
            if directory is not None:
                truncated = recursive and self.tree_truncated
                entries = [] if truncated else self._tree_entries(directory, recursive)
                return httpx.Response(200, json={"sha": sha, "truncated": truncated, "tree": entries})
            if sha in self.trees:
                return httpx.Response(200, json={"sha": sha, "truncated": False, **self.trees[sha]})
        if rel == "/git/blobs" and method == "POST":
            data = base64.b64decode(body["content"]) if body.get("encoding") == "base64" else body["content"].encode()
            sha = blob_sha(data)
            self.blobs[sha] = data
            return httpx.Response(201, json={"sha": sha})
        if rel == "/git/trees" and method == "POST":
            sha = _fake_sha("tree", body)
            self.trees[sha] = body
            return httpx.Response(201, json={"sha": sha})
        if rel == "/git/commits" and method == "POST":
            sha = _fake_sha("commit", body)
            self.commits[sha] = {"message": body["message"], "tree": {"sha": body["tree"]}, "parents": [{"sha": p} for p in body["parents"]]}
            return httpx.Response(201, json={"sha": sha})
        if (match := re.fullmatch(r"/git/refs?/(heads/.+)", rel)) and method == "GET":
            ref = match.group(1)
            if ref in self.refs:
                return httpx.Response(200, json={"ref": f"refs/{ref}", "object": {"sha": self.refs[ref], "type": "commit"}})
            return httpx.Response(404, json={"message": "Not Found"})
        if rel == "/git/refs" and method == "POST":
            ref = str(body["ref"])[len("refs/"):]
            if ref in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.refs[ref] = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"], "object": {"sha": body["sha"]}})
        if (match := re.fullmatch(r"/git/refs/(heads/.+)", rel)) and method == "DELETE":
            if self.refs.pop(match.group(1), None) is None:
                return httpx.Response(422, json={"message": "Reference does not exist"})
            return httpx.Response(204)
        if (match := re.fullmatch(r"/git/refs/(heads/.+)", rel)) and method == "PATCH":
            ref = match.group(1)
            if ref not in self.refs:
                return httpx.Response(422, json={"message": "Reference does not exist"})
            self.refs[ref] = body["sha"]
            return httpx.Response(200, json={"ref": f"refs/{ref}", "object": {"sha": body["sha"]}})
        if rel == "/pulls" and method == "POST":
            number = self._next_number()
            branch = str(body["head"]).split(":", 1)[-1]
            self.pulls[number] = {
                "number": number, "state": "open", "title": body["title"], "body": body["body"],
                "head": {"label": body["head"], "ref": branch, "sha": self.refs.get(f"heads/{branch}")},
                "base": {"ref": body["base"]}, "user": {"login": self.bot}, "merged": False, "merged_at": None,
            }
            return httpx.Response(201, json=self.pulls[number])
        if rel == "/pulls" and method == "GET":
            head, state = request.url.params.get("head"), request.url.params.get("state", "open")
            found = [p for p in self.pulls.values() if (not head or p["head"]["label"] == head) and (state == "all" or p["state"] == state)]
            return httpx.Response(200, json=sorted(found, key=lambda p: -p["number"]))
        if (match := re.fullmatch(r"/pulls/(\d+)", rel)) and int(match.group(1)) in self.pulls:
            pull = self.pulls[int(match.group(1))]
            if method == "GET":
                return httpx.Response(200, json=pull)
            if method == "PATCH":
                pull.update(body)
                return httpx.Response(200, json=pull)
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def fake(rsa_key) -> FakeGitHub:
    return FakeGitHub(rsa_key[1])


@pytest.fixture
def gh(settings, fake) -> GitHubClient:
    return GitHubClient(settings, http=httpx.Client(transport=fake.transport()), clock=time.time)


def install_payload(fake: FakeGitHub, action: str = "created") -> dict:
    return {
        "action": action,
        "installation": {"id": fake.installation_id, "account": {"login": "octo"}},
        "repositories": [{"id": fake.repo_id, "full_name": fake.full_name}],
    }


@pytest.fixture
def installed(conn, settings, fake) -> FakeGitHub:
    """A db with the fake's repo installed and its first check queued."""
    handle_event(conn, "installation", "delivery-install", install_payload(fake), settings.data)
    return fake


# --- helpers shared by the adversarial test files ------------------------------------------


def claim(conn):
    job = dbmod.claim_job(conn)
    assert job is not None
    return job


def tar_bytes(entries, compress=True) -> bytes:
    """entries: (name, kind, payload) with kind in file/dir/sym/lnk/chr/blk; payload is data or linkname."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz" if compress else "w") as tar:
        for name, kind, payload in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                data = payload if isinstance(payload, bytes) else (payload or "").encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                continue
            info.type = {
                "dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE, "lnk": tarfile.LNKTYPE,
                "chr": tarfile.CHRTYPE, "blk": tarfile.BLKTYPE,
            }[kind]
            if payload:
                info.linkname = payload
            tar.addfile(info)
    return buffer.getvalue()


def extract(tmp_path, data: bytes, **caps):
    src = tmp_path / "src.tgz"
    src.write_bytes(data)
    return extract_tarball(src, tmp_path / "tree", **caps)


def headers(body, event="installation", delivery="d-1", secret=SECRET):
    return {
        "X-Hub-Signature-256": sign(body, secret), "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery, "Content-Type": "application/json",
    }


def deliveries(conn):
    return conn.execute("SELECT count(*) FROM deliveries").fetchone()[0]


def push_payload(fake, ref="refs/heads/main", **extra):
    payload = {
        "ref": ref, "deleted": False,
        "repository": {"id": fake.repo_id, "full_name": fake.full_name, "default_branch": "main"},
        "installation": {"id": fake.installation_id},
    }
    payload.update(extra)
    return payload


def finding(**overrides):
    base = {
        "file": "src/pay.py", "line": 12, "method": "POST", "path": "/v1/charges", "label": "POST /v1/charges",
        "call": "stripe.Charge.create", "language": "python", "change_kind": "body_removed",
        "change_message": "body field description was removed", "reason": "description is sent here",
        "certainty": "affected",
    }
    base.update(overrides)
    base["url"] = issue.permalink("octo/shop", SHA, base["file"], base["line"])
    return base


def render(items, **overrides):
    kwargs = dict(full_name="octo/shop", sha=SHA, old_version=OLD_VERSION, new_version=NEW_VERSION, call_sites=9, fingerprint="ab" * 32)
    kwargs.update(overrides)
    return issue.render_issue(items, **kwargs)


def impact(file="a.py", line=1, method="post", path="/v1/charges", kind="body_removed", target="description"):
    return {
        "usage": {"file": file, "line": line, "method": method, "path": path, "call": "stripe.Charge.create", "language": "python"},
        "change": {"kind": kind, "target": target, "severity": "breaking", "message": "m"},
        "reason": "r", "certainty": "affected",
    }
