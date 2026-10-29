import copy

import pytest
from conftest import NEW_VERSION, OLD_VERSION, REPO, SHA

from apidrift.app import issue
from apidrift.app.checker import run_local

FP = "ab" * 32
CHANGELOG = "https://docs.stripe.com/changelog/test"


@pytest.fixture
def payload(data_dir):
    return run_local(REPO, "previous", "latest").payload()


def finding(**overrides):
    base = {
        "file": "src/pay.py", "line": 12, "method": "POST", "path": "/v1/charges", "label": "POST /v1/charges",
        "call": "stripe.Charge.create", "language": "python", "change_kind": "body_removed",
        "change_message": "body field description was removed", "change_target": "description",
        "introduced": NEW_VERSION, "consequence": "Requests that still send it are rejected.",
        "reason": "description is sent here", "certainty": "affected",
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
        assert f["change_target"] == "description" and f["consequence"] == "Requests that still send it are rejected."
        assert f["introduced"] == NEW_VERSION  # falls back to new_version until the payload carries ``introduced``
    assert {f["language"] for f in items} == {"javascript", "python", "ruby"}


def test_findings_take_introduced_from_the_change_when_present(payload):
    stamped = copy.deepcopy(payload)
    for impact in stamped["impacts"]:
        impact["change"]["introduced"] = "2026-01-15.test"
    assert {f["introduced"] for f in issue.findings(stamped, "octo/shop", SHA)} == {"2026-01-15.test"}


def test_golden_issue_body():
    items = [
        finding(),
        finding(line=40, certainty="possible", reason="arguments are not fully visible at this call"),
        finding(file="web/app.js", line=3, call="stripe.charges.create", language="javascript"),
    ]
    title, body = render(items)
    assert title == f"Upgrading to Stripe {NEW_VERSION} breaks 3 call sites"
    assert body == (
        f"Upgrading to Stripe {NEW_VERSION} breaks 3 call sites in octo/shop at 1a57c3b; 1 of them possibly.\n"
        "\n"
        f"**description removed from POST /v1/charges** (introduced in Stripe {NEW_VERSION}, [changelog]({CHANGELOG}))\n"
        "Requests that still send it are rejected.\n"
        f"- https://github.com/octo/shop/blob/{SHA}/src/pay.py#L12 via `stripe.Charge.create`\n"
        f"- https://github.com/octo/shop/blob/{SHA}/src/pay.py#L40 via `stripe.Charge.create`"
        " (possibly affected: arguments are not fully visible at this call)\n"
        f"- https://github.com/octo/shop/blob/{SHA}/web/app.js#L3 via `stripe.charges.create`\n"
        "\n"
        "---\n"
        f"Scanned octo/shop at {SHA} against Stripe {OLD_VERSION} -> {NEW_VERSION}: 9 Stripe call sites; "
        "unaffected call sites are not listed.\n"
        f"<!-- apidrift:fp={FP} -->\n"
    )


def test_title_counts_distinct_call_sites_not_finding_rows():
    items = [finding(), finding(change_kind="body_now_required", change_target="currency", change_message="m")]
    title, body = render(items)
    assert title == f"Upgrading to Stripe {NEW_VERSION} breaks 1 call site"
    assert body.startswith(f"Upgrading to Stripe {NEW_VERSION} breaks 1 call site in octo/shop")
    assert body.count("**") == 4  # two groups
    assert "**currency now required on POST /v1/charges**" in body


def test_findings_spanning_releases_name_the_newest_in_the_title_and_the_span_in_the_body():
    items = [
        finding(introduced="2026-01-15.test"),
        finding(file="b.py", change_kind="body_removed", change_target="currency", introduced="2026-02-01.endive"),
    ]
    title, body = render(items)
    assert title == "Upgrading to Stripe 2026-02-01.endive breaks 2 call sites"
    assert "breaks 2 call sites in octo/shop at 1a57c3b. The changes arrived between Stripe 2026-01-15.test and 2026-02-01.endive." in body
    assert "(introduced in Stripe 2026-01-15.test, [changelog](https://docs.stripe.com/changelog/test))" in body
    assert "(introduced in Stripe 2026-02-01.endive, [changelog](https://docs.stripe.com/changelog/endive))" in body


def test_groups_with_more_than_ten_links_fold_into_details():
    items = [finding(line=n) for n in range(1, 12)]
    _, body = render(items)
    assert "<details><summary>11 call sites</summary>\n\n- https://" in body
    assert body.count("#L") == 11 and "\n\n</details>\n" in body
    _, small = render(items[:10])
    assert "<details>" not in small


def test_body_is_capped_at_fifty_findings_and_keeps_the_marker():
    items = [finding(line=n) for n in range(1, 61)]
    title, body = render(items)
    assert title == f"Upgrading to Stripe {NEW_VERSION} breaks 60 call sites"
    assert body.count("#L") == 50 and "\nand 10 more.\n" in body
    assert "</details>" in body  # the fold is closed even when cut short
    assert issue.marker_in(body) == FP and body.rstrip().endswith(issue.marker(FP))


def test_single_finding_and_supersedes_line():
    title, body = render([finding()], supersedes=4)
    assert title == f"Upgrading to Stripe {NEW_VERSION} breaks 1 call site"
    assert body.startswith(f"Supersedes #4, which was closed before Stripe {NEW_VERSION} added new findings.\n\n")
    assert "breaks 1 call site in octo/shop" in body


def test_fingerprint_is_stable_across_line_shifts_and_hashes_ignore_the_file(payload):
    impacts = issue.breaking(payload)
    shifted = copy.deepcopy(impacts)
    for impact in shifted:
        impact["usage"]["line"] += 7
    assert issue.fingerprint(shifted) == issue.fingerprint(impacts) and len(issue.fingerprint(impacts)) == 64
    assert issue.finding_hashes(shifted) == issue.finding_hashes(impacts)
    assert len(issue.finding_hashes(impacts)) == 1  # three files, one change: one thing a later release can add to
    renamed = copy.deepcopy(impacts)
    renamed[0]["usage"]["file"] = "elsewhere.py"
    assert issue.fingerprint(renamed) != issue.fingerprint(impacts)  # the body changes, so the issue is updated
    assert issue.finding_hashes(renamed) == issue.finding_hashes(impacts)  # but a rename is not a new finding
    more = copy.deepcopy(impacts)
    more[0]["change"]["target"] = "currency"
    assert len(set(issue.finding_hashes(more)) - set(issue.finding_hashes(impacts))) == 1


def test_breaking_keeps_only_breaking_affected_or_possible_in_code(payload):
    noisy = copy.deepcopy(payload)
    warning = copy.deepcopy(noisy["impacts"][0])
    warning["change"]["severity"] = "warning"
    doc = copy.deepcopy(noisy["impacts"][0])
    doc["usage"]["language"] = "doc"
    noisy["impacts"] += [warning, doc]
    assert len(issue.breaking(noisy)) == 3
    assert issue.findings(noisy, "octo/shop", SHA) == issue.findings(payload, "octo/shop", SHA)


def test_paths_are_validated_encoded_and_put_in_code_spans():
    assert issue.permalink("o/r", SHA, "../x.py", 1) is None
    assert issue.permalink("o/r", SHA, "/abs.py", 1) is None
    assert issue.permalink("o/r", SHA, "a/../b.py", 1) is None
    assert issue.permalink("o/r", SHA, "a b/c+d.py", 7) == f"https://github.com/o/r/blob/{SHA}/a%20b/c%2Bd.py#L7"
    assert issue.escape("a<b`c|d\\e") == "a\\<b\\`c\\|d\\\\e"
    items = [finding(file="../x.py", call="stripe`x`|y.create")]
    assert items[0]["url"] is None
    _, body = render(items)
    assert "- `../x.py`:12 via ``stripe`x`|y.create``\n" in body and "blob/" not in body


def test_code_spans_neutralise_mentions_references_autolinks_and_backticks():
    assert issue.code("@octocat/#123/www.evil.com.py") == "`@octocat/#123/www.evil.com.py`"
    assert issue.code("a``b") == "```a``b```"
    assert issue.code("`lead") == "`` `lead ``"
    assert issue.code("trail`") == "`` trail` ``"
    assert issue.code(" padded ") == "`  padded  `"
    assert issue.code("") == "`  `"
    assert issue.code("nl\nx") == "`nl\\x0ax`"
    _, body = render([finding(file="src/@octocat #1 www.evil.com.py", call="`rm -rf`")])
    assert "@octocat" not in body  # a valid path appears only percent-encoded inside its permalink
    assert f"/blob/{SHA}/src/%40octocat%20%231%20www.evil.com.py#L12 via" in body
    assert "via `` `rm -rf` ``" in body
    _, body = render([finding(file="../@octocat #1 www.evil.com.py", call="x")])
    assert "- `../@octocat #1 www.evil.com.py`:12 via `x`\n" in body  # no permalink: the name sits inside a code span


def test_heading_and_consequence_per_change_kind():
    label = "POST /v1/charges"
    assert issue.heading("body_removed", "description", label) == "description removed from POST /v1/charges"
    assert issue.heading("param_removed", "query:limit", "GET /v1/charges") == "limit removed from GET /v1/charges"
    assert issue.heading("body_added_required", "currency", label) == "currency now required on POST /v1/charges"
    assert issue.heading("body_type_changed", "amount", label) == "amount type changed on POST /v1/charges"
    assert issue.heading("body_enum_removed", "mode", label) == "mode values removed on POST /v1/charges"
    assert issue.heading("body_enum_restricted", "reason", label) == "reason restricted on POST /v1/charges"
    assert issue.heading("endpoint_removed", None, label) == "POST /v1/charges removed"
    assert issue.heading("response_field_removed", "receipt_url", label) == "receipt_url removed from the POST /v1/charges response"
    assert issue.heading("schema_removed", None, label) == "POST /v1/charges response changed shape"
    assert issue.heading("body_removed", "a*b", "POST /v1/x_[y]") == "a\\*b removed from POST /v1/x_\\[y\\]"

    def c(kind, **extra):
        return issue.consequence({"kind": kind, "message": "fallback msg", **extra})

    assert c("body_removed") == "Requests that still send it are rejected."
    assert c("param_now_required") == "Requests without it are rejected."
    assert c("body_type_changed", old="string", new="object") == "The value shape changed from string to object."
    assert c("body_type_narrowed") == "The value shape changed."
    assert c("body_enum_removed", old=["a", "b"]) == "These values are no longer accepted: a, b."
    assert c("param_enum_restricted", new=["x"]) == "Only these values are accepted now: x."
    assert c("endpoint_removed") == "The endpoint no longer exists."
    assert c("response_field_removed") == "Responses no longer include it."
    assert c("response_schema_changed") == "The response changed shape."
    assert c("something_else") == "fallback msg"


def test_changelog_url_uses_the_codename_and_falls_back_safely():
    assert issue.changelog_url("2026-09-30.endive") == "https://docs.stripe.com/changelog/endive"
    assert issue.changelog_url("2026-01-01.test") == "https://docs.stripe.com/changelog/test"
    assert issue.changelog_url("unknown") == "https://docs.stripe.com/changelog"
    assert issue.changelog_url("2026-01-01.a/b") == "https://docs.stripe.com/changelog"


def test_marker_round_trip():
    assert issue.marker_in("hello\n" + issue.marker(FP) + "\n") == FP
    assert issue.marker_in("no marker here") is None and issue.marker_in(None) is None


def test_unread_note_and_notice_issue():
    payload = {"unread": {"Go": 2, "PHP": 1}, "call_sites": 7}
    assert issue.unread_note(payload) == (
        "apidrift does not read Go or PHP call sites yet: 2 Go files and 1 PHP file mention Stripe and were not checked."
    )
    assert issue.unread_note({"unread": {}}) == "" and issue.unread_note({}) == ""
    title, body = issue.render_unread_issue("octo/shop", "a" * 40, payload)
    assert title == "apidrift cannot read Go or PHP call sites in this repository yet"
    assert "2 Go files and 1 PHP file mention Stripe" in body and "7 Stripe call sites read" in body
    assert body.rstrip().endswith(issue.UNREAD_MARKER)


def test_findings_issue_footer_carries_the_unread_note():
    _, body = issue.render_issue(
        [], full_name="octo/shop", sha="b" * 40, old_version="2026-01-01.a", new_version="2026-02-01.b",
        call_sites=3, fingerprint="f" * 64, unread="apidrift does not read Go call sites yet: 1 Go file mention Stripe and were not checked.",
    )
    assert "apidrift does not read Go call sites yet" in body
    assert body.index("does not read Go") < body.index(issue.marker("f" * 64))


def _impact(target, kind, certainty, line=13, path="/v1/subscriptions/{subscription_exposed_id}"):
    return {
        "usage": {"file": "server.js", "line": line, "method": "post", "path": path, "language": "javascript"},
        "change": {"severity": "breaking", "kind": kind, "target": target},
        "certainty": certainty,
    }


def test_a_possible_required_child_is_dropped_when_its_parent_is_already_a_finding_at_that_call():
    parent = _impact("billing_cycle_anchor", "body_type_changed", "affected")
    child = _impact("billing_cycle_anchor.type", "body_added_required", "possible")
    elsewhere = _impact("billing_cycle_anchor.type", "body_added_required", "possible", line=99)
    certain_child = _impact("billing_cycle_anchor.type", "body_added_required", "affected")
    payload = {"impacts": [parent, child, elsewhere, certain_child]}
    assert issue.breaking(payload) == [parent, elsewhere, certain_child]


def test_possibly_counts_only_sites_without_a_certain_finding():
    items = [
        finding(),
        finding(change_kind="body_added_required", change_target="description.type", certainty="possible", reason="r"),
        finding(line=40, certainty="possible", reason="r"),
    ]
    _, body = render(items)
    assert "breaks 2 call sites in octo/shop at 1a57c3b; 1 of them possibly." in body
    _, body = render(items[:2])
    assert "breaks 1 call site in octo/shop at 1a57c3b." in body


def test_the_changelog_link_is_the_migration_entry_when_the_table_has_one():
    anchor = finding(path="/v1/subscriptions/{subscription_exposed_id}", label="POST /v1/subscriptions/{subscription_exposed_id}",
                     change_kind="body_type_changed", change_target="billing_cycle_anchor", introduced="2026-09-30.endive")
    _, body = render([anchor, finding()])
    assert "[changelog](https://docs.stripe.com/changelog/endive/2026-09-30/polymorphic-billing-cycle-anchor)" in body
    assert f"[changelog]({issue.changelog_url(NEW_VERSION)})" in body  # no table entry: the release page
    _, body = render([dict(anchor, introduced="2027-01-01.later")])  # table entry from another release: not used
    assert "[changelog](https://docs.stripe.com/changelog/later)" in body
