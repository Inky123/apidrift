import time

import httpx
import jwt
import pytest
from conftest import SHA, TOKEN

from apidrift.app.github import PERMISSIONS, GitHubError, RateLimited, TarballTooLarge, scrub


def test_jwt_round_trips_against_the_public_key(gh, rsa_key):
    claims = jwt.decode(gh.app_jwt(), rsa_key[1], algorithms=["RS256"], issuer="12345")
    assert claims["exp"] - claims["iat"] == 600
    assert claims["iat"] <= int(time.time()) - 60 and claims["exp"] > time.time()


def test_installation_token_request_is_scoped_to_the_repo(gh, fake):
    assert gh.installation_token(77, 501) == TOKEN
    assert fake.token_requests == [{"repository_ids": [501], "permissions": PERMISSIONS}]
    assert PERMISSIONS == {"contents": "read", "issues": "write", "metadata": "read"}
    assert TOKEN not in repr(vars(gh))  # never kept on the client


def test_scrub_removes_token_query_values_and_authorization_values():
    text = (
        "ConnectError for url 'https://codeload.github.com/o/r/legacy.tar.gz/abc?token=CODELOADSECRET&x=1'"
        " headers {'Authorization': 'Bearer ghs_live', 'authorization: token ghs_other'} ghs_live"
    )
    out = scrub(text, ("ghs_live",))
    for secret in ("CODELOADSECRET", "ghs_live", "ghs_other"):
        assert secret not in out
    assert "token=***&x=1" in out and "Authorization': '***" in out


def test_rate_limit_responses_raise_with_a_capped_reset(gh, fake):
    far = str(int(time.time()) + 3600)
    fake.responses[("GET", "/repos/octo/shop")] = httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": far}, json={})
    with pytest.raises(RateLimited) as caught:
        gh.get_repo(TOKEN, "octo/shop")
    assert time.time() + 14 * 60 < caught.value.reset_at <= time.time() + 15 * 60 + 1
    fake.responses[("GET", "/repos/octo/shop")] = httpx.Response(429, headers={"retry-after": "30"}, json={})
    with pytest.raises(RateLimited) as caught:
        gh.get_repo(TOKEN, "octo/shop")
    assert time.time() + 25 < caught.value.reset_at <= time.time() + 31


def test_other_errors_carry_the_status(gh):
    with pytest.raises(GitHubError) as caught:
        gh.get_issue(TOKEN, "octo/shop", 999)
    assert caught.value.status == 404 and "GET /repos/octo/shop/issues/999" in str(caught.value)
    with pytest.raises(GitHubError) as caught:
        gh.get_repo("wrong-token", "octo/shop")
    assert caught.value.status == 401


def test_download_tarball_follows_the_redirect_without_auth_and_enforces_the_cap(gh, fake, tmp_path):
    dest = tmp_path / "src.tgz"
    size = gh.download_tarball(TOKEN, "octo/shop", SHA, dest, max_bytes=10_000_000)
    assert size > 0 and dest.stat().st_size == size
    codeload = [r for r in fake.requests if r.url.host == "codeload.github.com"]
    assert len(codeload) == 1 and "authorization" not in codeload[0].headers
    with pytest.raises(TarballTooLarge):
        gh.download_tarball(TOKEN, "octo/shop", SHA, dest, max_bytes=16)
    fake.tarball = b"x" * 100
    fake.responses[("GET", f"/repos/octo/shop/tarball/{SHA}")] = httpx.Response(200, content=fake.tarball, headers={"content-length": "100"})
    with pytest.raises(TarballTooLarge):
        gh.download_tarball(TOKEN, "octo/shop", SHA, dest, max_bytes=50)


def test_branch_sha_must_be_a_full_sha(gh, fake):
    assert gh.branch_sha(TOKEN, "octo/shop", "main") == SHA
    fake.responses[("GET", "/repos/octo/shop/branches/main")] = httpx.Response(200, json={"commit": {"sha": "nope"}})
    with pytest.raises(GitHubError):
        gh.branch_sha(TOKEN, "octo/shop", "main")


def test_list_bot_issues_skips_pull_requests_and_other_authors(gh, fake):
    fake.add_issue("first")
    fake.add_issue("a pr")["pull_request"] = {"url": "x"}
    fake.add_issue("by a person", login="zach")
    fake.add_issue("newest")
    assert [i["number"] for i in gh.list_bot_issues(TOKEN, "octo/shop")] == [4, 1]
    listing = [r for r in fake.requests if r.url.path == "/repos/octo/shop/issues"][0]
    assert listing.url.params["creator"] == "apidrift-test[bot]" and listing.url.params["state"] == "all"


def test_issue_writes_go_to_the_expected_endpoints(gh, fake):
    created = gh.create_issue(TOKEN, "octo/shop", "t", "b")
    gh.update_issue(TOKEN, "octo/shop", created["number"], state="closed")
    gh.comment(TOKEN, "octo/shop", created["number"], "Clean at abc")
    assert fake.issues[1]["state"] == "closed" and fake.comments == [(1, "Clean at abc")]
    assert [(r.method, r.url.path) for r in fake.writes] == [
        ("POST", "/repos/octo/shop/issues"), ("PATCH", "/repos/octo/shop/issues/1"), ("POST", "/repos/octo/shop/issues/1/comments"),
    ]


def test_api_client_follows_a_301_for_a_renamed_repo(gh, fake):
    """Reviewer scenario 4: GitHub answers 301 for the old name; every check used to fail until a push."""
    moved = httpx.Response(301, headers={"location": "https://api.github.com/repos/octo/store"}, json={"message": "Moved Permanently"})
    fake.responses[("GET", "/repos/octo/shop")] = moved
    fake.full_name, fake.repo["full_name"] = "octo/store", "octo/store"
    assert gh.get_repo(TOKEN, "octo/shop")["full_name"] == "octo/store"
    followed = [r for r in fake.requests if r.url.path == "/repos/octo/store"]
    assert len(followed) == 1 and followed[0].headers["authorization"] == f"Bearer {TOKEN}"


def test_default_client_follows_redirects(settings):
    from apidrift.app.github import GitHubClient

    assert GitHubClient(settings)._http.follow_redirects is True


def test_revoke_token_deletes_the_installation_token(gh, fake):
    fake.responses[("DELETE", "/installation/token")] = httpx.Response(204)
    gh.revoke_token(TOKEN)
    request = fake.requests[-1]
    assert (request.method, request.url.path, request.headers["authorization"]) == ("DELETE", "/installation/token", f"Bearer {TOKEN}")
    with pytest.raises(GitHubError):  # the fake knows no such route without a canned answer: callers ignore this
        gh.revoke_token(TOKEN)
