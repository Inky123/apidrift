import json

import httpx
import pytest
from conftest import NEW_VERSION, THIRD_VERSION, install_payload, third_doc

from apidrift.app import db
from apidrift.app.poller import ETAG_NAME, initial_baseline, latest_version, poll_stripe, snapshots
from apidrift.app.webhooks import handle_event


def client(responder):
    return httpx.Client(transport=httpx.MockTransport(responder))


def test_304_does_nothing(settings, conn):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(304)

    assert poll_stripe(settings, conn, client(respond)) is None
    assert len(seen) == 1 and seen[0].url.host == "raw.githubusercontent.com" and db.queued_jobs(conn) == []


def test_new_body_saves_a_snapshot_and_queues_one_release_job_per_active_repo(settings, conn, fake):
    handle_event(conn, "installation", "d-1", install_payload(fake), settings.data)
    added = {"action": "added", "installation": {"id": 77}, "repositories_added": [{"id": 502, "full_name": "octo/other"}]}
    handle_event(conn, "installation_repositories", "d-2", added, settings.data)
    other = {"action": "created", "installation": {"id": 88, "account": {"login": "x"}}, "repositories": [{"id": 503, "full_name": "x/y"}]}
    handle_event(conn, "installation", "d-3", other, settings.data)
    handle_event(conn, "installation", "d-4", {"action": "suspend", "installation": {"id": 88}}, settings.data)
    while db.claim_job(conn) is not None:
        pass
    body = json.dumps(third_doc()).encode()
    etags = []

    def respond(request):
        etags.append(request.headers.get("if-none-match"))
        if etags[-1] == '"v3"':
            return httpx.Response(304)
        return httpx.Response(200, content=body, headers={"etag": '"v3"'})

    path = poll_stripe(settings, conn, client(respond))
    assert path is not None and path.name.endswith(".json.gz") and "2026-03-01-test" in path.name
    assert latest_version(settings.data) == THIRD_VERSION
    jobs = [j for j in db.queued_jobs(conn) if j["reason"] == "release"]
    assert [j["repo_id"] for j in jobs] == [501, 502]
    assert jobs[1]["not_before"] == db.later(2, base=jobs[0]["not_before"])
    assert (settings.snapshot_dir / ETAG_NAME).read_text() == '"v3"'
    assert poll_stripe(settings, conn, client(respond)) is None  # 304 on the stored ETag
    assert etags == [None, '"v3"'] and len(db.queued_jobs(conn)) == 2


def test_same_content_again_is_not_a_new_snapshot(settings, conn, fake):
    handle_event(conn, "installation", "d-1", install_payload(fake), settings.data)
    while db.claim_job(conn) is not None:
        pass
    body = json.dumps(third_doc()).encode()
    assert poll_stripe(settings, conn, client(lambda r: httpx.Response(200, content=body))) is not None
    while db.claim_job(conn) is not None:
        pass
    assert poll_stripe(settings, conn, client(lambda r: httpx.Response(200, content=body))) is None
    assert db.queued_jobs(conn) == [] and len(snapshots(settings.data)) == 3


def test_http_errors_propagate(settings, conn):
    with pytest.raises(httpx.HTTPStatusError):
        poll_stripe(settings, conn, client(lambda r: httpx.Response(500)))
    assert len(snapshots(settings.data)) == 2


def test_latest_version_and_initial_baseline(data_dir, tmp_path):
    assert latest_version(data_dir) == NEW_VERSION
    assert initial_baseline(data_dir) == snapshots(data_dir)[0].name
    assert latest_version(tmp_path / "empty") is None and initial_baseline(tmp_path / "empty") is None
