"""Adversarial tests, part 2: issue content and lifecycle, the job queue, the scan sandbox and the web app.

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

CANARY = "CANARY_EXECUTED"



@pytest.fixture
def client(settings, conn):
    with TestClient(create_app(settings, conn)) as test_client:
        yield test_client


# --- 4. issue content and lifecycle --------------------------------------------------------


HOSTILE_PATHS = [
    "src/<script>alert(1)</script>.py",
    "src/`rm -rf /`.py",
    "src/a|b|c.py",
    "../../etc/passwd",
    "/etc/passwd",
    "src/..",
    "src/../x.py",
    "src/ünïcödé/日本語.py",
    "src/" + "a" * 300 + ".py",
    "src/a b.py",
    "src/%2e%2e/x.py",
    "src/#L1.py",
    "src/a?b=1.py",
    "src/a\\b.py",
    "src/[link](https://evil).py",
    "src/x.py\n- https://github.com/evil/evil/blob/x/y#L1",
]


@pytest.mark.parametrize("path", HOSTILE_PATHS)
def test_hostile_file_names_are_neutralised_in_body_and_permalink(path):
    item = finding(file=path, call="stripe.`x`|<b>.create")
    title, body = render([item])
    assert body.count("<!-- apidrift:fp=") == 1
    prose = re.sub(r"(`+)(?:(?!\1).)*\1", "", body)  # inside a code span nothing renders, so '<' is inert there
    unescaped = [m.start() for m in re.finditer(r"(?<!\\)<", prose)]
    assert unescaped == [prose.index("<!-- apidrift:fp=")], "a '<' outside the marker survived unescaped"
    assert not re.search(r"(?<!\\)`rm", body) and not re.search(r"(?<!\\)\|b", body)
    assert "\n- https://github.com/evil" not in body, "a newline in a file name injected a fake finding line"
    urls = re.findall(r"https://github\.com/\S+", prose)  # a URL inside a code span is text, never a link
    for url in urls:
        assert url.startswith(f"https://github.com/octo/shop/blob/{SHA}/"), url
        tail = url[len(f"https://github.com/octo/shop/blob/{SHA}/"):]
        assert re.fullmatch(r"[A-Za-z0-9%._~\-/]+#L\d+", tail), url
        assert "/../" not in tail and not tail.startswith("/")
    if item["url"] is None:
        assert issue.valid_path(path) is False
    assert "https://evil" not in prose and "https://github.com/evil" not in prose
    assert title == f"Upgrading to Stripe {NEW_VERSION} breaks 1 call site"


def test_call_names_cannot_break_out_of_the_finding_line():
    item = finding(call="stripe.`Charge`|<b>.create\\x")
    _, body = render([item])
    line = next(ln for ln in body.splitlines() if " via " in ln)
    after = line.split(" via ", 1)[1]
    assert after.startswith("``stripe.`Charge`|<b>.create\\x``"), after  # a code span whose fence outruns any backtick run


def test_fifty_one_findings_show_fifty_and_the_overflow_line():
    items = [finding(file=f"f{i:03}.py", line=i) for i in range(51)]
    title, body = render(items)
    assert body.count("https://github.com/") == 50 and "and 1 more." in body
    assert title.endswith("breaks 51 call sites")
    assert body.rstrip().endswith("-->") and "f050.py" not in body and "f049.py" in body


def test_capped_body_with_long_unicode_paths_fits_github_limit():
    items = [finding(file=f"src/{'ü' * 290}{i:03}.py", line=i) for i in range(50)]
    _, body = render(items)
    assert len(body) <= 65536, f"GitHub rejects bodies over 65536 chars; this one is {len(body)}"


def test_fingerprint_ignores_lines_and_order_but_not_added_findings():
    base = [impact(file="a.py", line=1), impact(file="b.py", line=7)]
    shifted = [impact(file="b.py", line=70), impact(file="a.py", line=10)]
    assert issue.fingerprint(base) == issue.fingerprint(shifted)
    assert issue.fingerprint(base) == issue.fingerprint(base + [impact(file="a.py", line=99)])  # same key, more lines
    assert issue.fingerprint(base) != issue.fingerprint(base + [impact(file="c.py")])
    assert issue.fingerprint(base) != issue.fingerprint(base + [impact(file="a.py", target="currency")])
    assert issue.fingerprint(base) != issue.fingerprint(base + [impact(file="a.py", method="get")])
    assert issue.fingerprint(base) != issue.fingerprint([impact(file="a.py", target="description\n('b.py'")])
    # finding hashes drive 'gained': the same change in a renamed or extra file is not a new finding
    assert set(issue.finding_hashes(base)) == set(issue.finding_hashes(base + [impact(file="c.py")]))
    assert set(issue.finding_hashes(base)) < set(issue.finding_hashes(base + [impact(file="a.py", target="currency")]))


def test_marker_is_not_fooled_by_near_misses():
    fp = "a" * 64
    assert issue.marker_in(f"<!-- apidrift:fp={fp} -->") == fp
    assert issue.marker_in(f"<!-- apidrift:fp={fp[:63]} -->") is None
    assert issue.marker_in(f"<!-- apidrift:fp={fp.upper()} -->") is None
    assert issue.marker_in(f"\\<!-- apidrift:fp={fp} -->") == fp  # escaped text still carries it; adoption is by creator anyway


def test_findings_stay_within_baseline_when_breaking_findings_remain(installed, conn, gh, settings):
    before = db.get_repo(conn, 501)["baseline"]
    assert run_job(claim(conn), conn, gh, settings).startswith("findings")
    assert db.get_repo(conn, 501)["baseline"] == before
    installed.close_by_human(1)
    db.enqueue(conn, "check", 501, "push")
    assert "silent" in run_job(claim(conn), conn, gh, settings)
    assert db.get_repo(conn, 501)["baseline"] == before


@pytest.mark.parametrize("field", ["has_issues", "archived"])
def test_skipped_repos_keep_their_baseline_and_issue_untouched(installed, conn, gh, settings, field):
    run_job(claim(conn), conn, gh, settings)
    installed.repo[field] = field == "archived"
    writes = len(installed.writes)
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).startswith("skipped")
    repo = db.get_repo(conn, 501)
    assert repo["baseline"] == snapshots(settings.data)[0].name and repo["issue_number"] == 1
    assert len(installed.writes) == writes and installed.issues[1]["state"] == "open"


def test_human_reopen_is_noticed_and_the_issue_is_maintained_again(installed, conn, gh, settings):
    run_job(claim(conn), conn, gh, settings)
    installed.close_by_human(1)
    db.enqueue(conn, "check", 501, "push")
    assert "silent" in run_job(claim(conn), conn, gh, settings)
    installed.issues[1].update(state="open", closed_by=None)
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings) == "findings: #1 unchanged"
    assert db.get_repo(conn, 501)["issue_state"] == "open"


def test_close_with_null_closed_by_is_treated_as_a_human_close(installed, conn, gh, settings):
    run_job(claim(conn), conn, gh, settings)
    installed.issues[1].update(state="closed", closed_by=None)
    db.enqueue(conn, "check", 501, "push")
    assert "silent" in run_job(claim(conn), conn, gh, settings) and installed.issues[1]["state"] == "closed"


def test_deleted_issue_leads_to_a_new_one_not_a_crash(installed, conn, gh, settings):
    run_job(claim(conn), conn, gh, settings)
    installed.issues.clear()
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).startswith("findings: opened #")
    assert db.get_repo(conn, 501)["issue_state"] == "open" and len(installed.issues) == 1


def test_adopted_closed_by_user_issue_still_gets_a_fresh_issue_after_a_release_adds_findings(installed, conn, gh, settings):
    installed.add_issue("old " + issue.marker("1" * 64), state="closed", closed_by="zach")
    assert "silent" in run_job(claim(conn), conn, gh, settings)
    save_snapshot(third_doc(), datetime(2026, 3, 1, tzinfo=timezone.utc))
    db.enqueue(conn, "check", 501, "release")
    outcome = run_job(claim(conn), conn, gh, settings)
    assert outcome == "findings: opened #2 superseding #1", outcome
    assert installed.issues[2]["body"].startswith("Supersedes #1,")


def test_adopted_closed_by_us_issue_is_reopened(installed, conn, gh, settings):
    installed.add_issue("ours " + issue.marker("1" * 64), state="closed", closed_by=installed.bot)
    assert run_job(claim(conn), conn, gh, settings) == "findings: reopened #1"
    assert installed.issues[1]["state"] == "open" and f"blob/{SHA}/" in installed.issues[1]["body"]


def test_repo_renamed_on_github_uses_the_new_name_in_permalinks(installed, conn, gh, settings):
    installed.responses[("GET", "/repos/octo/shop")] = httpx.Response(200, json={**installed.repo, "full_name": "octo/store"})
    installed.full_name = "octo/store"
    installed.repo["full_name"] = "octo/store"
    assert run_job(claim(conn), conn, gh, settings) == "findings: opened #1"
    assert "https://github.com/octo/store/blob/" in installed.issues[1]["body"] and "octo/shop" not in installed.issues[1]["body"]
    assert db.get_repo(conn, 501)["full_name"] == "octo/store"


# --- 5. jobs --------------------------------------------------------------------------------


def test_two_workers_on_separate_connections_claim_each_job_once(tmp_path):
    path = tmp_path / "q.db"
    setup = db.connect(path)
    db.migrate(setup)
    db.upsert_installation(setup, 1, "octo")
    for i in range(1, 61):
        db.upsert_repo(setup, i, 1, f"octo/r{i}", None)
        db.enqueue(setup, "check", i, "push")
    setup.close()
    claimed: dict[str, list[int]] = {"a": [], "b": []}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        conn = db.connect(path)
        barrier.wait()
        while (job := db.claim_job(conn)) is not None:
            claimed[name].append(job["id"])
        conn.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in claimed]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    ids = claimed["a"] + claimed["b"]
    assert sorted(ids) == list(range(1, 61)) and len(set(ids)) == 60


def test_not_before_is_honoured_by_the_claim_query(installed, conn):
    db.claim_job(conn)
    db.enqueue(conn, "check", 501, "release", not_before=db.later(3600))
    assert db.claim_job(conn) is None
    assert db.claim_job(conn, at=db.later(3599)) is None
    assert db.claim_job(conn, at=db.later(3601)) is not None


def test_backoff_schedule_doubles_each_attempt(installed, conn, gh, settings):
    job_id = db.queued_jobs(conn)[0]["id"]
    for attempt in range(1, 5):
        installed.responses[("GET", "/repos/octo/shop")] = httpx.Response(502, json={})
        before = db.now()
        run_one(db.claim_job(conn, job_id=job_id), conn, gh, settings)
        row = db.get_job(conn, job_id)
        expected = 60 * 2**attempt
        assert db.later(expected - 2, before) <= row["not_before"] <= db.later(expected + 2), attempt
        assert row["status"] == "queued" and row["started_at"] is None


def test_burst_of_pushes_yields_one_queued_job_per_running_one(installed, conn, settings):
    for n in range(100):
        handle_event(conn, "push", f"burst-{n}", push_payload(installed), settings.data)
    assert len(db.queued_jobs(conn)) == 1
    running = db.claim_job(conn)
    for n in range(100, 150):
        handle_event(conn, "push", f"burst-{n}", push_payload(installed), settings.data)
    queued = db.queued_jobs(conn)
    assert len(queued) == 1 and queued[0]["id"] != running["id"]
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2


def test_requeue_of_a_job_whose_repo_vanished_is_harmless(installed, conn, settings):
    job = db.claim_job(conn)
    handle_event(conn, "installation", "gone", {"action": "deleted", "installation": {"id": 77}}, settings.data)
    assert db.requeue_job(conn, job["id"], db.now(), "x", count_attempt=True) == "gone"
    db.finish_job(conn, job["id"], "done")
    assert db.requeue_running(conn) == 0


# --- 6. scan subprocess -------------------------------------------------------------------


def test_planted_modules_pth_and_startup_files_are_never_executed(data_dir, tmp_path):
    tree = tmp_path / "planted"
    shutil.copytree(REPO, tree)
    canary = tmp_path / "canary"
    bomb = f"import pathlib; pathlib.Path({str(canary)!r}).write_text('{CANARY}')\n"
    for name in ["json.py", "apidrift.py", "sitecustomize.py", "usercustomize.py", "conftest.py", "setup.py",
                 "__init__.py", "__main__.py", "os.py", "re.py", "stripe.py", "ast.py"]:
        (tree / name).write_text(bomb)
    (tree / "evil.pth").write_text(f"import sys; exec({bomb!r})\n")
    (tree / "pyproject.toml").write_text("[build-system]\nrequires = ['x']\n")
    (tree / "apidrift").mkdir()
    (tree / "apidrift" / "__init__.py").write_text(bomb)
    (tree / "apidrift" / "cli.py").write_text(bomb)
    old, new = (p.name for p in snapshots(data_dir))
    payload = scan_subprocess(tree, old, new, data_dir, max_mem_mb=0)
    assert not canary.exists()
    assert len(issue.breaking(payload)) == 3 and payload["new_version"] == NEW_VERSION
    assert (data_dir / "canary").exists() is False and not list(data_dir.glob("**/canary"))


def test_deeply_nested_brackets_do_not_hang_or_sink_the_whole_scan(data_dir, tmp_path):
    tree = tmp_path / "nested"
    shutil.copytree(REPO, tree)
    depth = 700_000
    (tree / "deep.py").write_text("x = " + "(" * depth + ")" * depth + "\n")
    (tree / "deep.js").write_text("const x = " + "{a:" * (depth // 2) + "1" + "}" * (depth // 2) + ";\n")
    (tree / "deep.rb").write_text("x = " + "[" * depth + "]" * depth + "\n")
    (tree / "deep2.js").write_text("stripe.charges.create(" + "f(" * depth + ")" * depth + ")\n")
    assert (tree / "deep.py").stat().st_size >= 1_400_000
    old, new = (p.name for p in snapshots(data_dir))
    timeout = 60
    started = time.monotonic()
    payload = scan_subprocess(tree, old, new, data_dir, max_mem_mb=0, timeout=timeout)
    assert time.monotonic() - started < timeout
    assert len(issue.breaking(payload)) == 3, "one unparsable file must not hide the real findings"


# --- 7. web ----------------------------------------------------------------------------------


def test_install_redirect_ignores_host_and_forwarded_headers(client):
    hostile = {"Host": "evil.example", "X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "javascript",
               "X-Forwarded-For": "1.2.3.4", "Referer": "https://evil.example/"}
    response = client.get("/install", headers=hostile, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://github.com/apps/apidrift-test/installations/new"
    assert "evil" not in response.text


@pytest.mark.parametrize("query", [
    "installation_id=<script>alert(1)</script>&setup_action=install",
    "installation_id=%3Cscript%3Ealert(1)%3C/script%3E",
    "installation_id=1&setup_action=\"'><img src=x onerror=alert(1)>",
    "installation_id=" + "9" * 5000,
])
def test_installed_page_reflects_nothing_from_the_query(client, query):
    response = client.get(f"/installed?{query}")
    assert response.status_code == 200
    assert "alert(1)" not in response.text and "onerror" not in response.text and "9" * 50 not in response.text


def test_healthz_shape_with_and_without_snapshots(client, settings, tmp_path, conn):
    body = client.get("/healthz").json()
    assert set(body) == {"ok", "stripe_latest"} and body["ok"] is True and body["stripe_latest"] == NEW_VERSION
    bare = Settings(**{**settings.__dict__, "data": tmp_path / "empty"})
    with TestClient(create_app(bare, conn)) as empty_client:
        assert empty_client.get("/healthz").json() == {"ok": True, "stripe_latest": None}
        assert empty_client.get("/").status_code == 200


@pytest.mark.parametrize("method,path", [
    ("HEAD", "/"), ("HEAD", "/healthz"), ("HEAD", "/install"), ("HEAD", "/privacy"), ("HEAD", "/installed"),
    ("OPTIONS", "/"), ("OPTIONS", "/github/webhook"), ("GET", "/github/webhook"), ("HEAD", "/github/webhook"),
    ("PUT", "/github/webhook"), ("DELETE", "/"), ("GET", "/static/../app.db"), ("GET", "/static/%2e%2e/web.py"),
    ("GET", "/static/"), ("GET", "/nope"), ("POST", "/"), ("POST", "/healthz"),
])
def test_other_methods_and_paths_never_crash(client, conn, method, path):
    response = client.request(method, path, follow_redirects=False)
    assert response.status_code < 500, (method, path, response.status_code)
    if path == "/github/webhook":
        assert response.status_code in (405, 404)
    assert deliveries(conn) == 0
