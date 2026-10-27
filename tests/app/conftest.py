"""Shared fixtures: no network, a data dir with two Stripe snapshots, an in-memory db, a fake GitHub."""

from __future__ import annotations

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


class FakeGitHub:
    """An ``httpx.MockTransport`` handler covering the GitHub endpoints the App uses; records every request."""

    def __init__(self, public_key: bytes) -> None:
        self.public_key = public_key
        self.full_name, self.repo_id, self.installation_id = "octo/shop", 501, 77
        self.bot = "apidrift-test[bot]"
        self.sha = SHA
        self.repo = {"id": 501, "full_name": "octo/shop", "default_branch": "main", "has_issues": True, "archived": False}
        self.tree = REPO
        self.tarball: bytes | None = None
        self.issues: dict[int, dict] = {}
        self.comments: list[tuple[int, str]] = []
        self.requests: list[httpx.Request] = []
        self.token_requests: list[dict] = []
        self.responses: dict[tuple[str, str], httpx.Response] = {}
        self.codeload_error: Exception | None = None

    @property
    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method in ("POST", "PATCH") and r.url.path.startswith("/repos/")]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def add_issue(self, body: str, state: str = "open", closed_by: str | None = None, login: str | None = None) -> dict:
        number = max(self.issues, default=0) + 1
        self.issues[number] = {
            "number": number, "state": state, "title": "old", "body": body,
            "user": {"login": login or self.bot}, "closed_by": {"login": closed_by} if closed_by else None,
        }
        return self.issues[number]

    def close_by_human(self, number: int, login: str = "zach") -> None:
        self.issues[number].update(state="closed", closed_by={"login": login})

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
        if method == "POST" and path == f"/app/installations/{self.installation_id}/access_tokens":
            bearer = request.headers.get("authorization", "")[len("Bearer "):]
            jwt.decode(bearer, self.public_key, algorithms=["RS256"], issuer="12345")
            self.token_requests.append(json.loads(request.content))
            return httpx.Response(201, json={"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"message": "Bad credentials"})
        base = f"/repos/{self.full_name}"
        if path == base and method == "GET":
            return httpx.Response(200, json=self.repo)
        if path == f"{base}/branches/main" and method == "GET":
            return httpx.Response(200, json={"name": "main", "commit": {"sha": self.sha}})
        if path == f"{base}/tarball/{self.sha}" and method == "GET":
            location = f"https://codeload.github.com/{self.full_name}/legacy.tar.gz/{self.sha}?token=CODELOADSECRET"
            return httpx.Response(302, headers={"location": location})
        if path == f"{base}/issues" and method == "GET":
            creator = request.url.params.get("creator")
            found = [i for i in self.issues.values() if i["user"]["login"] == creator]
            return httpx.Response(200, json=sorted(found, key=lambda i: -i["number"]))
        if path == f"{base}/issues" and method == "POST":
            fields = json.loads(request.content)
            created = self.add_issue(fields["body"])
            created["title"] = fields["title"]
            return httpx.Response(201, json=created)
        match = re.fullmatch(rf"{re.escape(base)}/issues/(\d+)(/comments)?", path)
        if match and int(match.group(1)) in self.issues:
            issue = self.issues[int(match.group(1))]
            if match.group(2) and method == "POST":
                self.comments.append((issue["number"], json.loads(request.content)["body"]))
                return httpx.Response(201, json={"id": len(self.comments)})
            if method == "GET":
                return httpx.Response(200, json=issue)
            if method == "PATCH":
                fields = json.loads(request.content)
                if fields.get("state") == "closed" and issue["state"] == "open":
                    issue["closed_by"] = {"login": self.bot}
                if fields.get("state") == "open":
                    issue["closed_by"] = None
                issue.update(fields)
                return httpx.Response(200, json=issue)
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
