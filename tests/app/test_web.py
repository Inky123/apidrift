import json

import pytest
from conftest import NEW_VERSION, SECRET, install_payload, sign
from starlette.testclient import TestClient

from apidrift.app import db
from apidrift.app.web import APP_DIR, create_app, load_demo


def needs_template(name):
    present = (APP_DIR / "templates" / name).is_file()
    return pytest.mark.skipif(not present, reason=f"templates/{name} is not written yet (owned by the UI implementer)")


@pytest.fixture
def client(settings, conn):
    with TestClient(create_app(settings, conn)) as test_client:
        yield test_client


def headers(body, event="installation", delivery="d-1", secret=SECRET):
    return {
        "X-Hub-Signature-256": sign(body, secret),
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
    }


def deliveries(conn):
    return conn.execute("SELECT count(*) FROM deliveries").fetchone()[0]


# --- pages ----------------------------------------------------------------------


@needs_template("landing.html")
def test_landing_renders_every_demo_finding(client):
    response = client.get("/")
    assert response.status_code == 200 and "text/html" in response.headers["content-type"]
    demo = load_demo()
    assert len(demo["findings"]) >= 4
    for finding in demo["findings"]:
        assert finding["file"] in response.text
    assert "/install" in response.text


@needs_template("installed.html")
def test_installed_page(client):
    response = client.get("/installed?installation_id=1&setup_action=install")
    assert response.status_code == 200 and "Issues disabled" in response.text


@needs_template("privacy.html")
def test_privacy_page(client):
    response = client.get("/privacy")
    assert response.status_code == 200 and "zachzap2@gmail.com" in response.text


# --- plumbing -------------------------------------------------------------------


def test_robots_txt_allows_everything(client):
    response = client.get("/robots.txt")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")
    assert response.text == "User-agent: *\nAllow: /\n"


@needs_template("privacy.html")
def test_each_page_gets_its_own_description(client):
    landing, privacy = client.get("/").text, client.get("/privacy").text
    assert '<meta name="description" content="Stripe changed its API.' in landing
    assert '<meta name="description" content="What apidrift reads, stores and deletes when it checks a repository.">' in privacy
    assert '<meta property="og:title" content="Privacy">' in privacy


def test_healthz_reports_the_latest_stripe_version(client):
    assert client.get("/healthz").json() == {"ok": True, "stripe_latest": NEW_VERSION}


def test_install_redirects_to_github(client):
    response = client.get("/install", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://github.com/apps/apidrift-test/installations/new"


# --- webhook ----------------------------------------------------------------------


def test_bad_signature_is_401_and_leaves_no_row(client, conn, fake):
    body = json.dumps(install_payload(fake)).encode()
    response = client.post("/github/webhook", content=body, headers=headers(body, secret="wrong"))
    assert response.status_code == 401 and "octo/shop" not in response.text
    assert deliveries(conn) == 0 and db.queued_jobs(conn) == []
    missing = {k: v for k, v in headers(body).items() if k != "X-Hub-Signature-256"}
    assert client.post("/github/webhook", content=body, headers=missing).status_code == 401


def test_valid_install_delivery_queues_a_check_and_duplicates_are_ignored(client, conn, fake):
    body = json.dumps(install_payload(fake)).encode()
    response = client.post("/github/webhook", content=body, headers=headers(body))
    assert response.status_code == 200 and response.json()["ok"] is True
    assert [j["repo_id"] for j in db.queued_jobs(conn)] == [501]
    again = client.post("/github/webhook", content=body, headers=headers(body))
    assert again.status_code == 200 and again.json()["actions"] == ["duplicate delivery ignored"]
    assert deliveries(conn) == 1


def test_unhandled_events_get_204_without_a_row(client, conn, fake):
    body = json.dumps({"action": "created"}).encode()
    assert client.post("/github/webhook", content=body, headers=headers(body, event="star")).status_code == 204
    assert deliveries(conn) == 0


def test_oversized_declared_body_is_rejected_before_reading(client, conn):
    body = b"{}"
    extra = {**headers(body), "Content-Length": str(26 * 1024 * 1024)}
    assert client.post("/github/webhook", content=body, headers=extra).status_code == 413
    assert deliveries(conn) == 0


def test_malformed_json_and_missing_headers_are_400(client, conn):
    body = b"{"
    assert client.post("/github/webhook", content=body, headers=headers(body)).status_code == 400
    valid = json.dumps({"action": "created", "installation": {}}).encode()
    assert client.post("/github/webhook", content=valid, headers=headers(valid)).status_code == 400
    no_delivery = {k: v for k, v in headers(valid).items() if k != "X-GitHub-Delivery"}
    assert client.post("/github/webhook", content=valid, headers=no_delivery).status_code == 400
    assert deliveries(conn) == 0
