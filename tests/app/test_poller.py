import json
import logging
from datetime import datetime, timezone

import httpx
import pytest
from conftest import NEW_VERSION, OLD_VERSION, THIRD_VERSION, install_payload, new_doc, save_snapshot, third_doc

from apidrift.app import db
from apidrift.app.poller import ETAG_NAME, Rejected, initial_baseline, latest_version, poll_stripe, snapshots, validate
from apidrift.app.webhooks import handle_event
from apidrift.app.worker import tick


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


# --- validation (reviewer scenario 6) -----------------------------------------------


def drain(conn):
    while db.claim_job(conn) is not None:
        pass


def bad_bodies():
    no_paths = {"openapi": "3.0.0", "info": {"title": "oops"}}
    bad_version = third_doc(); bad_version["info"]["version"] = "v3"
    empty_paths = third_doc(); empty_paths["paths"] = {}
    no_sdk = third_doc()
    for schema in no_sdk["components"]["schemas"].values():
        schema.pop("x-stripeOperations", None)
    shrunk = third_doc()
    keep = list(shrunk["paths"])[:1]
    shrunk["paths"] = {k: shrunk["paths"][k] for k in keep}
    return [
        pytest.param(b"<html>cdn error</html>", "not JSON", id="html"),
        pytest.param(b"[]", "not a JSON object", id="list"),
        pytest.param(json.dumps(no_paths).encode(), "info.version", id="no version"),
        pytest.param(json.dumps(bad_version).encode(), "info.version 'v3'", id="undated version"),
        pytest.param(json.dumps(empty_paths).encode(), "paths is missing or empty", id="empty paths"),
        pytest.param(json.dumps(no_sdk).encode(), "no SDK method metadata", id="no sdk metadata"),
        pytest.param(json.dumps(shrunk).encode(), "below 80%", id="operations shrank"),
    ]


@pytest.mark.parametrize("body,reason", bad_bodies())
def test_unusable_bodies_are_rejected_without_a_snapshot_or_etag(settings, conn, fake, caplog, body, reason):
    """Scenario 6: a valid-JSON body without paths used to become 'latest' and rewrite every open issue."""
    handle_event(conn, "installation", "d-1", install_payload(fake), settings.data)
    drain(conn)
    (settings.snapshot_dir / ETAG_NAME).write_text('"keep"')
    with caplog.at_level(logging.WARNING, logger="apidrift.poller"):
        path = poll_stripe(settings, conn, client(lambda r: httpx.Response(200, content=body, headers={"etag": '"bad"'})))
    assert path is None and len(snapshots(settings.data)) == 2 and latest_version(settings.data) == NEW_VERSION
    assert (settings.snapshot_dir / ETAG_NAME).read_text() == '"keep"'
    assert db.queued_jobs(conn) == []
    warning = [r for r in caplog.records if r.levelno == logging.WARNING][0].message
    assert "rejected" in warning and reason in warning
    assert db.get_meta(conn, "last_poll_status").startswith("rejected: ") and reason in db.get_meta(conn, "last_poll_status")


def test_validate_accepts_a_real_release_and_the_shrink_floor_is_relative_to_the_previous(data_dir):
    latest = snapshots(data_dir)[-1]
    assert validate(json.dumps(third_doc()).encode(), latest) == THIRD_VERSION
    assert validate(json.dumps(third_doc()).encode(), None) == THIRD_VERSION
    with pytest.raises(Rejected, match="operations is below"):
        shrunk = third_doc()
        shrunk["paths"] = dict(list(shrunk["paths"].items())[:1])
        validate(json.dumps(shrunk).encode(), latest)


def test_same_version_content_change_is_saved_but_queues_no_checks(settings, conn, fake):
    """Scenario 6: a docs-only commit to stripe/openapi at the same version is not a release."""
    handle_event(conn, "installation", "d-1", install_payload(fake), settings.data)
    drain(conn)
    doc = new_doc()
    doc["info"]["description"] = "a docs-only commit"
    path = poll_stripe(settings, conn, client(lambda r: httpx.Response(200, content=json.dumps(doc).encode(), headers={"etag": '"d"'})))
    assert path is not None and len(snapshots(settings.data)) == 3 and db.queued_jobs(conn) == []
    assert (settings.snapshot_dir / ETAG_NAME).read_text() == '"d"'
    assert db.get_meta(conn, "last_poll_status") == f"saved {path.name} (same version)"
    # and the next real release still fans out
    body = json.dumps(third_doc()).encode()
    assert poll_stripe(settings, conn, client(lambda r: httpx.Response(200, content=body))) is not None
    assert [j["reason"] for j in db.queued_jobs(conn)] == ["release"]


def test_initial_baseline_skips_same_version_snapshots(data_dir):
    """Scenario 6: new installs after a same-version snapshot used to diff NEW -> NEW' and come out clean."""
    first = snapshots(data_dir)[0].name
    doc = new_doc()
    doc["info"]["description"] = "docs-only"
    save_snapshot(doc, datetime(2026, 2, 2, tzinfo=timezone.utc))
    assert len(snapshots(data_dir)) == 3 and initial_baseline(data_dir) == first
    assert OLD_VERSION.replace(".", "-") in first


def test_initial_baseline_with_a_single_distinct_version_is_the_latest(tmp_path, monkeypatch):
    data = tmp_path / "one"
    monkeypatch.setenv("APIDRIFT_DATA", str(data))
    save_snapshot(new_doc(), datetime(2026, 2, 1, tzinfo=timezone.utc))
    doc = new_doc()
    doc["info"]["description"] = "docs-only"
    second = save_snapshot(doc, datetime(2026, 2, 2, tzinfo=timezone.utc))
    assert initial_baseline(data) != second.name  # the oldest stored snapshot, here the same version: a no-op diff


def test_initial_baseline_is_the_oldest_release_not_the_previous_one(data_dir):
    """A release with no breaking changes of its own must not hide the breaking changes of the one before it."""
    oldest = snapshots(data_dir)[0].name
    third = new_doc()
    third["info"]["version"] = "2026-03-01.test"
    save_snapshot(third, datetime(2026, 3, 1, tzinfo=timezone.utc))
    assert len(snapshots(data_dir)) == 3 and initial_baseline(data_dir) == oldest


def test_every_tick_records_its_outcome_in_meta(settings, conn, caplog):
    assert db.all_meta(conn) == {}
    (settings.snapshot_dir / ETAG_NAME).write_text('"keep"')
    with caplog.at_level(logging.INFO):
        tick(settings, conn, gh=None, http=client(lambda r: httpx.Response(304)), poll=True)
    meta = db.all_meta(conn)
    assert meta["last_poll_status"] == "304" and meta["last_poll_at"] <= db.now()
    assert meta["latest_snapshot"] == snapshots(settings.data)[-1].name
    assert "Stripe poll: 304 unchanged" in [r.message for r in caplog.records]
    with caplog.at_level(logging.ERROR):
        tick(settings, conn, gh=None, http=client(lambda r: httpx.Response(503)), poll=True)
    assert db.get_meta(conn, "last_poll_status").startswith("error: HTTPStatusError")
    tick(settings, conn, gh=None, http=client(lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("t"))), poll=True)
    assert db.get_meta(conn, "last_poll_status").startswith("error: ReadTimeout")
    assert sum("Stripe poll failed" in r.message for r in caplog.records) == 2
    assert (settings.snapshot_dir / ETAG_NAME).read_text() == '"keep"'
    body = json.dumps(third_doc()).encode()
    tick(settings, conn, gh=None, http=client(lambda r: httpx.Response(200, content=body)), poll=True)
    latest = snapshots(settings.data)[-1]
    assert db.get_meta(conn, "last_poll_status") == f"saved {latest.name}" and db.get_meta(conn, "latest_snapshot") == latest.name
    tick(settings, conn, gh=None, http=client(lambda r: httpx.Response(200, content=body)), poll=True)
    assert db.get_meta(conn, "last_poll_status").startswith("duplicate of a stored snapshot")
