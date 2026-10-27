import pytest
from conftest import SECRET, install_payload, sign

from apidrift.app import db
from apidrift.app.webhooks import BadPayload, handle_event, verify_signature

VECTOR_BODY = b"Hello, World!"
VECTOR_SIG = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"


def push_payload(fake, ref="refs/heads/main", deleted=False, installation_id=None):
    return {
        "ref": ref,
        "deleted": deleted,
        "repository": {"id": fake.repo_id, "full_name": fake.full_name, "default_branch": "main"},
        "installation": {"id": installation_id or fake.installation_id},
    }


def drain(conn):
    while db.claim_job(conn) is not None:
        pass


def deliveries(conn):
    return conn.execute("SELECT count(*) FROM deliveries").fetchone()[0]


# --- signatures ---------------------------------------------------------------


def test_github_test_vector_passes():
    assert verify_signature(SECRET, VECTOR_BODY, VECTOR_SIG)
    assert sign(VECTOR_BODY) == VECTOR_SIG


def test_flipped_byte_is_rejected():
    body = bytearray(VECTOR_BODY)
    body[0] ^= 1
    assert not verify_signature(SECRET, bytes(body), VECTOR_SIG)
    assert not verify_signature(SECRET, VECTOR_BODY, VECTOR_SIG[:-1] + "0")


@pytest.mark.parametrize("header", [None, "", "sha1=757107ea", "sha256=", "757107ea0eb2509f", "sha256=zzé"])
def test_missing_or_malformed_header_is_rejected(header):
    assert not verify_signature(SECRET, VECTOR_BODY, header)


def test_empty_secret_never_verifies():
    assert not verify_signature("", VECTOR_BODY, sign(VECTOR_BODY, ""))


# --- installation events --------------------------------------------------------


def test_install_creates_rows_and_queues_one_check(conn, settings, fake):
    actions = handle_event(conn, "installation", "d-1", install_payload(fake), settings.data)
    assert actions[0] == "installation 77 created" and "check 1 queued" in actions[1]
    repo = db.get_repo(conn, fake.repo_id)
    assert repo["full_name"] == "octo/shop" and repo["installation_id"] == 77
    assert repo["baseline"].startswith("2026-01-01T")  # the previous snapshot
    assert [(j["repo_id"], j["reason"]) for j in db.queued_jobs(conn)] == [(501, "install")]


def test_duplicate_delivery_is_a_noop(conn, settings, fake):
    handle_event(conn, "installation", "d-1", install_payload(fake), settings.data)
    drain(conn)
    assert handle_event(conn, "installation", "d-1", install_payload(fake), settings.data) == ["duplicate delivery ignored"]
    assert db.queued_jobs(conn) == [] and deliveries(conn) == 1


def test_installation_deleted_purges_rows_and_work_dirs(installed, conn, settings):
    job = db.queued_jobs(conn)[0]
    work = settings.work_dir / str(job["id"])
    work.mkdir(parents=True)
    (work / "src.tgz").write_bytes(b"x")
    payload = {"action": "deleted", "installation": {"id": 77, "account": {"login": "octo"}}}
    assert handle_event(conn, "installation", "d-del", payload, settings.data) == ["installation 77 deleted"]
    assert db.get_installation(conn, 77) is None and db.get_repo(conn, 501) is None
    assert db.get_job(conn, job["id"]) is None and not work.exists()


def test_suspend_and_unsuspend(installed, conn, settings):
    drain(conn)
    handle_event(conn, "installation", "d-s", {"action": "suspend", "installation": {"id": 77}}, settings.data)
    assert db.active_repos(conn) == []
    actions = handle_event(conn, "installation", "d-u", {"action": "unsuspend", "installation": {"id": 77}}, settings.data)
    assert actions == ["installation 77 unsuspended, 1 checks queued"]
    assert [r["id"] for r in db.active_repos(conn)] == [501]


def test_repositories_added_and_removed(installed, conn, settings):
    added = {"action": "added", "installation": {"id": 77}, "repositories_added": [{"id": 502, "full_name": "octo/other"}]}
    handle_event(conn, "installation_repositories", "d-a", added, settings.data)
    assert db.get_repo(conn, 502)["full_name"] == "octo/other"
    assert [j["repo_id"] for j in db.queued_jobs(conn)] == [501, 502]
    removed = {"action": "removed", "installation": {"id": 77}, "repositories_removed": [{"id": 502}]}
    assert handle_event(conn, "installation_repositories", "d-r", removed, settings.data) == ["1 repos removed"]
    assert db.get_repo(conn, 502) is None and db.get_repo(conn, 501) is not None
    assert [j["repo_id"] for j in db.queued_jobs(conn)] == [501]


# --- push ---------------------------------------------------------------------


def test_push_to_feature_branch_is_ignored(installed, conn, settings):
    drain(conn)
    actions = handle_event(conn, "push", "d-p", push_payload(installed, ref="refs/heads/feature"), settings.data)
    assert actions == ["push: not the default branch, ignored"] and db.queued_jobs(conn) == []


def test_branch_deletion_and_unknown_installation_are_ignored(installed, conn, settings):
    drain(conn)
    assert handle_event(conn, "push", "d-1", push_payload(installed, deleted=True), settings.data) == ["push: branch deleted, ignored"]
    assert handle_event(conn, "push", "d-2", push_payload(installed, installation_id=999), settings.data) == ["push: unknown installation, ignored"]
    assert db.queued_jobs(conn) == []


def test_two_pushes_queue_one_job(installed, conn, settings):
    drain(conn)
    assert handle_event(conn, "push", "d-1", push_payload(installed), settings.data) == ["push: check 2 queued"]
    assert handle_event(conn, "push", "d-2", push_payload(installed), settings.data) == ["push: a check is already queued"]
    jobs = db.queued_jobs(conn)
    assert len(jobs) == 1 and jobs[0]["reason"] == "push"


# --- everything else ------------------------------------------------------------


def test_unhandled_event_does_nothing(conn, settings):
    assert handle_event(conn, "issues", "d-x", {"action": "opened"}, settings.data) == []
    assert deliveries(conn) == 0


def test_bad_payload_raises_and_records_nothing(conn, settings):
    with pytest.raises(BadPayload):
        handle_event(conn, "installation", "d-bad", {"action": "created", "installation": {}}, settings.data)
    assert deliveries(conn) == 0 and db.queued_jobs(conn) == []
