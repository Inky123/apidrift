import copy

import pytest
from conftest import NEW_VERSION, OLD_VERSION, REPO, SHA

from apidrift.app import issue
from apidrift.app.checker import run_local

FP = "ab" * 32


@pytest.fixture
def payload(data_dir):
    return run_local(REPO, "previous", "latest").payload()


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
    kwargs = dict(full_name="octo/shop", sha=SHA, old_version=OLD_VERSION, new_version=NEW_VERSION, call_sites=9, fingerprint=FP)
    kwargs.update(overrides)
    return issue.render_issue(items, **kwargs)


def test_findings_have_exactly_the_contract_keys(payload):
    items = issue.findings(payload, "octo/shop", SHA)
    assert [tuple(f) for f in items] == [issue.FINDING_KEYS] * 3
    assert [f["file"] for f in items] == ["server.js", "server.py", "server.rb"]
    for f in items:
        assert f["url"] == f"https://github.com/octo/shop/blob/{SHA}/{f['file']}#L{f['line']}"
        assert f["label"] == "POST /v1/charges" and f["certainty"] == "affected" and f["change_kind"] == "body_removed"
    assert {f["language"] for f in items} == {"javascript", "python", "ruby"}


def test_golden_issue_body():
    items = [
        finding(),
        finding(line=40, certainty="possible", reason="arguments are not fully visible at this call"),
        finding(file="web/app.js", line=3, call="stripe.charges.create", language="javascript"),
    ]
    title, body = render(items)
    assert title == f"Stripe {NEW_VERSION} breaks 3 Stripe call sites"
    assert body == (
        f"Stripe {NEW_VERSION} affects 3 Stripe call sites in octo/shop at 1a57c3b; 1 of them possibly.\n"
        "\n"
        "**src/pay.py**\n"
        f"- https://github.com/octo/shop/blob/{SHA}/src/pay.py#L12\n"
        "  `POST /v1/charges` via stripe.Charge.create: body field description was removed (description is sent here)\n"
        f"- https://github.com/octo/shop/blob/{SHA}/src/pay.py#L40\n"
        "  possibly affected: `POST /v1/charges` via stripe.Charge.create: body field description was removed"
        " (arguments are not fully visible at this call)\n"
        "\n"
        "**web/app.js**\n"
        f"- https://github.com/octo/shop/blob/{SHA}/web/app.js#L3\n"
        "  `POST /v1/charges` via stripe.charges.create: body field description was removed (description is sent here)\n"
        "\n"
        "---\n"
        f"Scanned octo/shop at {SHA} against Stripe {OLD_VERSION} -> {NEW_VERSION}: 9 Stripe call sites; "
        "unaffected call sites are not listed.\n"
        f"<!-- apidrift:fp={FP} -->\n"
    )


def test_body_is_capped_at_fifty_findings_and_keeps_the_marker():
    items = [finding(line=n) for n in range(1, 61)]
    title, body = render(items)
    assert title == f"Stripe {NEW_VERSION} breaks 60 Stripe call sites"
    assert body.count("#L") == 50 and "\nand 10 more.\n" in body
    assert issue.marker_in(body) == FP and body.rstrip().endswith(issue.marker(FP))


def test_single_finding_and_supersedes_line():
    title, body = render([finding()], supersedes=4)
    assert title == f"Stripe {NEW_VERSION} breaks 1 Stripe call site"
    assert body.startswith(f"Supersedes #4, which was closed before Stripe {NEW_VERSION} added new findings.\n\n")
    assert "affects 1 Stripe call site in octo/shop" in body


def test_fingerprint_is_stable_across_line_shifts(payload):
    impacts = issue.breaking(payload)
    shifted = copy.deepcopy(impacts)
    for impact in shifted:
        impact["usage"]["line"] += 7
    assert issue.fingerprint(shifted) == issue.fingerprint(impacts) and len(issue.fingerprint(impacts)) == 64
    assert issue.finding_hashes(shifted) == issue.finding_hashes(impacts) and len(issue.finding_hashes(impacts)) == 3
    renamed = copy.deepcopy(impacts)
    renamed[0]["usage"]["file"] = "elsewhere.py"
    assert issue.fingerprint(renamed) != issue.fingerprint(impacts)
    assert len(set(issue.finding_hashes(renamed)) - set(issue.finding_hashes(impacts))) == 1


def test_breaking_keeps_only_breaking_affected_or_possible(payload):
    noisy = copy.deepcopy(payload)
    warning = copy.deepcopy(noisy["impacts"][0])
    warning["change"]["severity"] = "warning"
    noisy["impacts"].append(warning)
    assert len(issue.breaking(noisy)) == 3


def test_paths_are_validated_encoded_and_escaped():
    assert issue.permalink("o/r", SHA, "../x.py", 1) is None
    assert issue.permalink("o/r", SHA, "/abs.py", 1) is None
    assert issue.permalink("o/r", SHA, "a/../b.py", 1) is None
    assert issue.permalink("o/r", SHA, "a b/c+d.py", 7) == f"https://github.com/o/r/blob/{SHA}/a%20b/c%2Bd.py#L7"
    assert issue.escape("a<b`c|d\\e") == "a\\<b\\`c\\|d\\\\e"
    items = [finding(file="../x.py", call="stripe`x`|y.create")]
    assert items[0]["url"] is None
    _, body = render(items)
    assert "- ../x.py:12\n" in body and "via stripe\\`x\\`\\|y.create:" in body and "blob/" not in body


def test_marker_round_trip():
    assert issue.marker_in("hello\n" + issue.marker(FP) + "\n") == FP
    assert issue.marker_in("no marker here") is None and issue.marker_in(None) is None
