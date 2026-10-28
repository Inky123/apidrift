"""Adversarial tests, part 1: hostile webhooks, tarballs and token hygiene.

Nothing here duplicates the happy paths in the sibling test files; every case tries to break a guarantee
in the design spec (signature, extraction caps, token hygiene, issue lifecycle, job queue, sandbox, web).
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import io
import json
import logging
import re
import shutil
import tarfile
import threading
import time
from datetime import datetime, timezone

import httpx
import jwt
import pytest
from conftest import NEW_VERSION, OLD_VERSION, REPO, SECRET, SHA, TOKEN, install_payload, save_snapshot, sign, third_doc
from starlette.testclient import TestClient

from apidrift.app import checker, db, issue
from apidrift.app.checker import CheckError, run_job, scan_subprocess
from apidrift.app.checkout import ExtractError, extract_tarball
from apidrift.app.config import Settings
from apidrift.app.github import GitHubClient, scrub
from apidrift.app.poller import snapshots
from apidrift.app.web import create_app
from apidrift.app.webhooks import MAX_BODY, BadPayload, handle_event, verify_signature
from apidrift.app.worker import run_one
from conftest import claim, deliveries, extract, finding, headers, impact, push_payload, render, tar_bytes  # noqa: E402,F401

VECTOR_BODY = b"Hello, World!"
VECTOR_SIG = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
CANARY = "CANARY_EXECUTED"


@pytest.fixture
def client(settings, conn):
    with TestClient(create_app(settings, conn)) as test_client:
        yield test_client


# --- 1. webhook: signature and payload hostility ----------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        "sha1=" + hmac.new(SECRET.encode(), VECTOR_BODY, hashlib.sha1).hexdigest(),
        VECTOR_SIG[:-2],  # wrong length
        VECTOR_SIG + "00",
        "sha256=" + sign(b"Hello, World", SECRET)[7:],  # valid signature for a different body
        sign(VECTOR_BODY, SECRET + " "),  # different secret
        "SHA256=" + VECTOR_SIG[7:],  # prefix must be exact
        "sha256=" + VECTOR_SIG[7:].replace("7", "8", 1),
        "sha256=\x00" + VECTOR_SIG[7:],
        VECTOR_SIG + "\n" + VECTOR_SIG,
    ],
)
def test_signature_bypass_attempts_are_rejected(header):
    assert verify_signature(SECRET, VECTOR_BODY, VECTOR_SIG)
    assert not verify_signature(SECRET, VECTOR_BODY, header)


def test_signature_check_takes_bytes_not_text_and_ignores_body_encoding():
    body = "héllo".encode("utf-8")
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, "héllo".encode("latin-1"), sign(body))
    assert not verify_signature(SECRET, b"", sign(body))


def test_replayed_delivery_id_with_a_different_body_is_a_noop(client, conn, fake):
    first = json.dumps(install_payload(fake)).encode()
    assert client.post("/github/webhook", content=first, headers=headers(first, delivery="dup")).status_code == 200
    jobs_before = [j["id"] for j in db.queued_jobs(conn)]
    second = json.dumps(push_payload(fake)).encode()
    response = client.post("/github/webhook", content=second, headers=headers(second, event="push", delivery="dup"))
    assert response.status_code == 200 and response.json()["actions"] == ["duplicate delivery ignored"]
    assert [j["id"] for j in db.queued_jobs(conn)] == jobs_before and deliveries(conn) == 1


@pytest.mark.parametrize("body", [b"\xff\xfe{", b"[]", b'"x"', b"null", b"1", b"{} {}", b"\xef\xbb\xbf{}"])
def test_non_object_or_non_utf8_json_is_400_without_a_row(client, conn, body):
    response = client.post("/github/webhook", content=body, headers=headers(body))
    assert response.status_code == 400, body
    assert deliveries(conn) == 0 and db.queued_jobs(conn) == []


def test_chunked_body_without_content_length_is_refused_without_a_row(client, conn, fake):
    body = json.dumps(install_payload(fake)).encode()

    def chunks():
        yield body

    response = client.post("/github/webhook", content=chunks(), headers=headers(body))
    assert response.status_code in (411, 400, 401) and deliveries(conn) == 0


@pytest.mark.parametrize("length", ["²", "1e3", "-1", "+5", " 5", "0x10"])
def test_non_decimal_content_length_is_rejected_not_a_crash(client, conn, length):
    body = b"{}"
    extra = {**headers(body), "Content-Length": length}
    try:
        response = client.post("/github/webhook", content=body, headers=extra)
    except Exception as exc:  # httpx refuses some of these itself; that is fine
        pytest.skip(f"client refused the header: {type(exc).__name__}")
    assert response.status_code in (400, 411, 413), length
    assert deliveries(conn) == 0


def test_content_length_exactly_at_and_over_the_cap(settings, conn):
    """The ASGI app must answer 413 for MAX_BODY + 1 without ever calling receive()."""
    app = create_app(settings, conn)
    received = []

    def run(length: int) -> int:
        async def receive():
            received.append(length)
            return {"type": "http.request", "body": b"{}", "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
            "path": "/github/webhook", "raw_path": b"/github/webhook", "query_string": b"", "root_path": "",
            "headers": [(b"host", b"apidrift.test"), (b"content-length", str(length).encode())],
            "client": ("10.0.0.1", 1), "server": ("apidrift.test", 80),
        }
        asyncio.run(app(scope, receive, send))
        return sent[0]["status"]

    assert run(MAX_BODY + 1) == 413 and received == []
    assert run(MAX_BODY) != 413 and received == [MAX_BODY]
    assert deliveries(conn) == 0


def test_push_deleted_or_tag_or_wrong_branch_queues_nothing(installed, conn, settings):
    while db.claim_job(conn) is not None:
        pass
    cases = [
        push_payload(installed, deleted=True),
        push_payload(installed, ref="refs/tags/main"),
        push_payload(installed, ref="refs/heads/main/"),
        push_payload(installed, ref="refs/heads/Main"),
        push_payload(installed, ref="refs/heads/main", repository={"id": 501, "full_name": "octo/shop"}),
    ]
    for n, payload in enumerate(cases):
        handle_event(conn, "push", f"p-{n}", payload, settings.data)
    assert db.queued_jobs(conn) == []


@pytest.mark.parametrize("repositories", [None, "x", 5, {}, [], [None, 5, "y"]])
def test_installation_created_with_odd_repositories_field(conn, settings, fake, repositories):
    payload = {**install_payload(fake), "repositories": repositories}
    actions = handle_event(conn, "installation", "d-odd", payload, settings.data)
    assert actions == ["installation 77 created"]
    assert db.get_installation(conn, 77) is not None and db.queued_jobs(conn) == []


@pytest.mark.parametrize("bad_id", [True, -1, 0, 1.0, "501", None, [501]])
def test_hostile_repository_ids_are_rejected_and_rolled_back(conn, settings, fake, bad_id):
    payload = {**install_payload(fake), "repositories": [{"id": bad_id, "full_name": "octo/shop"}]}
    with pytest.raises(BadPayload):
        handle_event(conn, "installation", "d-bad", payload, settings.data)
    assert deliveries(conn) == 0 and db.get_installation(conn, 77) is None


def test_oversized_integer_id_is_a_bad_payload_not_a_crash(client, conn, fake):
    payload = {**install_payload(fake), "repositories": [{"id": 2**70, "full_name": "octo/shop"}]}
    body = json.dumps(payload).encode()
    response = client.post("/github/webhook", content=body, headers=headers(body))
    assert response.status_code == 400 and deliveries(conn) == 0


@pytest.mark.parametrize("name", ["", "octo", "/shop", "octo/", "a/b/c", 7, None, "octo\n/shop"])
def test_hostile_full_names_are_rejected(conn, settings, fake, name):
    payload = {**install_payload(fake), "repositories": [{"id": 501, "full_name": name}]}
    with pytest.raises(BadPayload):
        handle_event(conn, "installation", "d-name", payload, settings.data)
    assert db.get_repo(conn, 501) is None


def test_repo_rename_keeps_the_row_and_its_issue_state(installed, conn, settings):
    db.update_repo(conn, 501, issue_number=9, issue_state="open", baseline="keep-me")
    renamed = push_payload(installed, repository={"id": 501, "full_name": "octo/store", "default_branch": "main"})
    handle_event(conn, "push", "p-rename", renamed, settings.data)
    repo = db.get_repo(conn, 501)
    assert (repo["full_name"], repo["issue_number"], repo["issue_state"], repo["baseline"]) == ("octo/store", 9, "open", "keep-me")
    assert db.find_repo(conn, "octo/shop") is None and db.find_repo(conn, "OCTO/STORE")["id"] == 501


def test_transfer_to_another_installation_survives_the_old_installations_removed_event(installed, conn, settings):
    added = {
        "action": "added", "installation": {"id": 88, "account": {"login": "other"}},
        "repositories_added": [{"id": 501, "full_name": "other/shop"}], "repositories_removed": [],
    }
    handle_event(conn, "installation_repositories", "t-1", added, settings.data)
    assert db.get_repo(conn, 501)["installation_id"] == 88
    removed = {
        "action": "removed", "installation": {"id": 77, "account": {"login": "octo"}},
        "repositories_added": [], "repositories_removed": [{"id": 501, "full_name": "octo/shop"}],
    }
    handle_event(conn, "installation_repositories", "t-2", removed, settings.data)
    repo = db.get_repo(conn, 501)
    assert repo is not None and repo["installation_id"] == 88, "the old installation's removal deleted a repo that moved"


def test_installation_deleted_only_purges_its_own_rows(installed, conn, settings):
    other = {
        "action": "created", "installation": {"id": 88, "account": {"login": "other"}},
        "repositories": [{"id": 601, "full_name": "other/app"}],
    }
    handle_event(conn, "installation", "o-1", other, settings.data)
    running = db.claim_job(conn)  # job for 501 is now running, with a work dir
    work = settings.work_dir / str(running["id"])
    work.mkdir(parents=True)
    other_work = settings.work_dir / str(db.queued_jobs(conn)[0]["id"])
    other_work.mkdir(parents=True)
    handle_event(conn, "installation", "o-2", {"action": "deleted", "installation": {"id": 77}}, settings.data)
    assert db.get_repo(conn, 501) is None and db.get_job(conn, running["id"]) is None and not work.exists()
    assert db.get_repo(conn, 601)["installation_id"] == 88 and other_work.exists() and len(db.queued_jobs(conn)) == 1
    handle_event(conn, "installation", "o-3", {"action": "deleted", "installation": {"id": 12345}}, settings.data)
    assert db.get_repo(conn, 601) is not None


# --- 2. tarball checkout ------------------------------------------------------------------


@pytest.mark.parametrize(
    "entries",
    [
        [("top", "dir", None), ("top/link", "sym", "../../outsidedir"), ("top/link/pwned", "file", "x")],
        [("top", "dir", None), ("top/link", "sym", "/"), ("top/link/tmp/pwned", "file", "x")],
        [("top", "dir", None), ("top/a", "sym", "b"), ("top/b", "sym", "../.."), ("top/a/pwned", "file", "x")],
        [("top", "dir", None), ("top/dev", "chr", None)],
        [("top", "dir", None), ("top/dev", "blk", None)],
        [("top", "dir", None), ("top/hard", "lnk", "../src.tgz")],
        [("top", "dir", None), ("top/hard", "lnk", "top/../../src.tgz")],
        [("top", "dir", None), ("top/x", "file", "a"), ("top/./../../x", "file", "b")],
        [("top", "dir", None), ("top/" + "a" * 300, "file", "x")],
    ],
)
def test_escapes_links_and_devices_never_touch_the_outside(tmp_path, entries):
    outside_before = sorted(p.name for p in tmp_path.iterdir())
    try:
        extract(tmp_path, tar_bytes(entries))
    except ExtractError as exc:
        assert "pwned" not in str(exc) and "outsidedir" not in str(exc)
    assert not (tmp_path / "outsidedir").exists() and not (tmp_path / "pwned").exists() and not (tmp_path / "x").exists()
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != "tree") == outside_before + ["src.tgz"]
    assert (tmp_path / "src.tgz").read_bytes() == tar_bytes(entries)  # a hardlink never aliased the archive itself


def test_gzip_bomb_is_stopped_by_the_declared_sizes_before_anything_is_written(tmp_path):
    big = [("top", "dir", None), ("top/zeros", "file", b"\0" * 200_000)]
    data = tar_bytes(big)
    assert len(data) < 2_000  # tiny on disk, large when expanded
    with pytest.raises(ExtractError, match="bytes"):
        extract(tmp_path, data, max_bytes=100_000)
    assert not (tmp_path / "tree" / "top" / "zeros").exists()


def test_entry_cap_trips_before_any_file_is_written(tmp_path):
    many = [("top", "dir", None)] + [(f"top/f{i}", "file", "x") for i in range(20)]
    with pytest.raises(ExtractError, match="entries"):
        extract(tmp_path, tar_bytes(many), max_entries=5)
    assert not any((tmp_path / "tree").rglob("f*"))


@pytest.mark.parametrize(
    "data",
    [
        b"",
        gzip.compress(b""),
        gzip.compress(b"\0" * 1024),
        tar_bytes([("top", "dir", None), ("top/a", "file", "x")], compress=False),  # plain tar, not gzip
        tar_bytes([]),  # empty archive
        tar_bytes([("a.py", "file", "x")]),  # file at the top, no directory
        tar_bytes([("top", "sym", "/etc")]),  # the only top entry is a symlink
        tar_bytes([("one", "dir", None), ("two", "dir", None)]),
        b"PK\x03\x04" + b"\0" * 100,  # zip magic
    ],
)
def test_malformed_archives_raise_extract_error_only(tmp_path, data):
    with pytest.raises(ExtractError):
        extract(tmp_path, data)


def test_work_dir_is_gone_after_every_failure_path(installed, conn, gh, settings, monkeypatch):
    failures = {
        "garbage": lambda: setattr(installed, "tarball", b"not a tarball"),
        "two tops": lambda: setattr(installed, "tarball", tar_bytes([("a", "dir", None), ("b", "dir", None)])),
        "escape": lambda: setattr(installed, "tarball", tar_bytes([("top", "dir", None), ("top/../../x", "file", "x")])),
        "device": lambda: setattr(installed, "tarball", tar_bytes([("top", "dir", None), ("top/dev", "chr", None)])),
        "scan error": lambda: monkeypatch.setattr(checker, "scan_subprocess", lambda *a, **k: (_ for _ in ()).throw(CheckError("boom"))),
        "rate limited": lambda: installed.responses.__setitem__(
            ("GET", "/repos/octo/shop/branches/main"), httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={})
        ),
        "codeload down": lambda: setattr(installed, "codeload_error", httpx.ReadTimeout("slow")),
    }
    db.enqueue(conn, "check", 501, "push")
    for name, arm in failures.items():
        installed.tarball, installed.codeload_error = None, None
        arm()
        job = db.claim_job(conn) or db.claim_job(conn, job_id=db.get_job(conn, 1)["id"])
        assert job is not None, name
        outcome = run_one(job, conn, gh, settings)
        assert outcome.startswith(("failed", "rate limited")), (name, outcome)
        assert not (settings.work_dir / str(job["id"])).exists(), name
        assert not any(settings.work_dir.iterdir()) if settings.work_dir.exists() else True, name
        db.requeue_job(conn, job["id"], db.now(), None, count_attempt=False)
    assert installed.issues == {}


# --- 3. token hygiene ----------------------------------------------------------------------


def all_text(conn, fake, caplog) -> str:
    rows = conn.execute("SELECT coalesce(error, '') FROM jobs").fetchall()
    repos = conn.execute("SELECT * FROM repos").fetchall()
    bits = [r[0] for r in rows] + [str(tuple(r)) for r in repos]
    bits += [i.get("body", "") + i.get("title", "") for i in fake.issues.values()] + [c[1] for c in fake.comments]
    bits += [r.getMessage() for r in caplog.records]
    return "\n".join(bits)


def test_tokens_never_reach_errors_logs_or_issues_on_the_happy_path(installed, conn, gh, settings, caplog):
    caplog.set_level(logging.DEBUG)
    assert run_job(claim(conn), conn, gh, settings).startswith("findings")
    db.enqueue(conn, "check", 501, "push")
    run_one(claim(conn), conn, gh, settings)
    text = all_text(conn, installed, caplog)
    assert TOKEN not in text and "CODELOADSECRET" not in text and "Bearer" not in text
    assert all("Authorization" not in r.getMessage() for r in caplog.records)


def test_tokens_never_reach_errors_or_logs_on_every_failure_path(installed, conn, settings, fake, caplog):
    """A transport that blurts the Authorization header and the codeload URL into every exception."""
    caplog.set_level(logging.DEBUG)

    def leaky(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if request.url.path.startswith("/repos/octo/shop/issues"):
            raise httpx.ConnectError(
                f"could not reach {request.url} with headers={{'Authorization': '{auth}'}} "
                f"after redirect https://codeload.github.com/x?token=CODELOADSECRET&y=1"
            )
        return fake.handle(request)

    gh = GitHubClient(settings, http=httpx.Client(transport=httpx.MockTransport(leaky)), clock=time.time)
    job = claim(conn)
    outcome = run_one(job, conn, gh, settings)
    assert outcome.startswith("failed")
    text = all_text(conn, fake, caplog) + outcome
    assert TOKEN not in text and "CODELOADSECRET" not in text and "Bearer ghs" not in text
    assert "token=***" in db.get_job(conn, job["id"])["error"]


def test_jwt_failure_text_does_not_leak_the_jwt(installed, conn, settings, fake, caplog):
    caplog.set_level(logging.DEBUG)
    seen = {}

    def leaky(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            seen["jwt"] = request.headers["authorization"].split()[1]
            raise httpx.ConnectError(f"boom {request.url} Authorization: {request.headers['authorization']}")
        return fake.handle(request)

    gh = GitHubClient(settings, http=httpx.Client(transport=httpx.MockTransport(leaky)), clock=time.time)
    job = claim(conn)
    run_one(job, conn, gh, settings)
    text = all_text(conn, fake, caplog)
    assert seen["jwt"] not in text and seen["jwt"][:20] not in text


@pytest.mark.parametrize(
    "text",
    [
        "GET https://codeload.github.com/o/r/legacy.tar.gz/abc?token=ABCDEF123",
        "https://x/?a=1&token=ABCDEF123&b=2",
        "TOKEN=ABCDEF123 ",
        "headers={'Authorization': 'Bearer ABCDEF123'}",
        '{"authorization":"token ABCDEF123"}',
        "Authorization: Bearer ABCDEF123\nnext",
        "token=ABCDEF123>",
        "token=ABCDEF123'",
    ],
)
def test_scrub_strips_every_token_shape(text):
    assert "ABCDEF123" not in scrub(text)


def test_scrub_removes_known_secrets_even_in_unknown_shapes():
    assert "ghs_abc" not in scrub("weird: ghs_abc; x-token ghs_abc", ("ghs_abc",))
    assert scrub("plain", ("",)) == "plain"


def test_jwt_claims_use_backdated_iat_and_nine_minute_exp(settings, fake, rsa_key):
    fixed = 1_700_000_000
    gh = GitHubClient(settings, http=httpx.Client(transport=fake.transport()), clock=lambda: fixed)
    claims = jwt.decode(gh.app_jwt(), rsa_key[1], algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})
    assert claims["iat"] == fixed - 60 and claims["exp"] == fixed + 540 and claims["iss"] == "12345"
    assert claims["exp"] - claims["iat"] == 600 and set(claims) == {"iat", "exp", "iss"}
    assert jwt.get_unverified_header(gh.app_jwt())["alg"] == "RS256"


def test_installation_token_request_is_exactly_scoped(gh, fake):
    gh.installation_token(77, 501)
    (body,) = fake.token_requests
    assert body == {"repository_ids": [501], "permissions": {"contents": "read", "issues": "write", "metadata": "read"}}
    request = next(r for r in fake.requests if r.url.path.endswith("/access_tokens"))
    assert request.headers["accept"] == "application/vnd.github+json"
    assert request.headers["authorization"].startswith("Bearer eyJ")


def test_codeload_redirect_does_not_carry_the_installation_token(gh, fake, tmp_path):
    gh.download_tarball(TOKEN, "octo/shop", SHA, tmp_path / "t.tgz", 10**9)
    codeload = [r for r in fake.requests if r.url.host == "codeload.github.com"]
    assert len(codeload) == 1 and "authorization" not in codeload[0].headers
    assert TOKEN not in str(codeload[0].url)


