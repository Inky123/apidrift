from apidrift.diff import BREAKING, INFO, WARNING, Change
from apidrift.impact import AFFECTED, POSSIBLE, assess
from apidrift.scanner import Usage


def usage(fields=(), complete=True, method="post", path="/v1/charges", reads=(), line=1):
    return Usage("pay.py", line, method, path, "stripe.Charge.create", frozenset(fields), complete, "python", frozenset(reads))


def change(kind, target=None, severity=BREAKING, method="post", path="/v1/charges", affects=()):
    return Change(severity, kind, "msg", method, path, target=target, affects=affects)


def one(usages, changes):
    impacts = assess(usages, changes)
    assert len(impacts) == 1, impacts
    return impacts[0]


def test_removed_field_hits_calls_that_send_it_or_a_child():
    assert one([usage({"description"})], [change("body_removed", "description")]).certainty == AFFECTED
    assert one([usage({"shipping.address.city"})], [change("body_removed", "shipping")]).certainty == AFFECTED


def test_removed_child_of_a_sent_parent_depends_on_whether_the_contents_are_visible():
    removed = change("body_removed", "line_items[].dynamic_tax_rates")
    opaque = one([usage({"line_items"})], [removed])
    assert opaque.certainty == POSSIBLE and "line_items is sent here" in opaque.reason
    assert assess([usage({"line_items", "line_items[].price"})], [removed]) == []
    assert one([usage({"line_items", "line_items[].dynamic_tax_rates"})], [removed]).certainty == AFFECTED


def test_removed_field_not_sent_is_clean_when_arguments_are_visible():
    assert assess([usage({"amount"})], [change("body_removed", "description")]) == []
    assert one([usage({"amount"}, complete=False)], [change("body_removed", "description")]).certainty == POSSIBLE


def test_new_required_top_level_field_hits_every_caller():
    assert one([usage({"amount"})], [change("body_added_required", "currency")]).certainty == AFFECTED
    assert one([usage({"amount"}, complete=False)], [change("body_added_required", "currency")]).certainty == AFFECTED


def test_new_required_nested_field_only_binds_callers_that_send_the_parent():
    required = change("body_added_required", "billing_cycle_anchor.type")
    assert assess([usage({"items"})], [required]) == []
    assert one([usage({"items"}, complete=False)], [required]).certainty == POSSIBLE
    assert one([usage({"billing_cycle_anchor"})], [required]).certainty == POSSIBLE
    assert one([usage({"billing_cycle_anchor", "billing_cycle_anchor.day_of_month"})], [required]).certainty == AFFECTED
    assert assess([usage({"billing_cycle_anchor", "billing_cycle_anchor.type"})], [required]) == []


def test_now_required_hits_only_callers_that_omit_it():
    assert assess([usage({"currency"})], [change("body_now_required", "currency")]) == []
    assert one([usage({"amount"})], [change("body_now_required", "currency")]).certainty == AFFECTED
    assert one([usage({"amount"}, complete=False)], [change("body_now_required", "currency")]).certainty == POSSIBLE


def test_query_params_match_by_name_and_path_params_are_ignored():
    get = usage({"limit"}, method="get")
    assert one([get], [change("param_removed", "query:limit", method="get")]).certainty == AFFECTED
    assert assess([get], [change("param_removed", "path:charge", method="get")]) == []


def test_relaxations_only_matter_when_the_field_is_sent():
    assert assess([usage({"amount"})], [change("body_added", "tip", severity=INFO)]) == []
    assert one([usage({"tip"})], [change("body_type_widened", "tip", severity=INFO)]).certainty == AFFECTED


def test_endpoint_removal_hits_url_only_usages_with_unknown_method():
    assert one([usage(method="?")], [change("endpoint_removed")]).certainty == AFFECTED
    assert assess([usage(method="get")], [change("endpoint_removed")]) == []


def test_response_field_changes_use_reads_in_the_file():
    removed = change("response_field_removed", "receipt_url", method=None, path=None, affects=("POST /v1/charges",))
    assert one([usage(reads={"receipt_url"})], [removed]).certainty == AFFECTED
    assert one([usage(reads={"amount"})], [removed]).certainty == POSSIBLE
    assert assess([usage(path="/v1/refunds")], [removed]) == []


def test_response_warnings_are_dropped_when_the_field_is_not_read():
    added = change("response_enum_added", "locale", severity=WARNING, method=None, path=None, affects=("POST /v1/charges",))
    assert assess([usage(reads={"amount"})], [added]) == []
    assert one([usage(reads={"locale"})], [added]).certainty == AFFECTED


def test_impacts_sort_by_severity_then_certainty():
    usages = [usage({"a"}, line=1), usage(set(), complete=False, line=2)]
    changes = [change("body_deprecated", "a", severity=WARNING), change("body_removed", "a")]
    impacts = assess(usages, changes)
    assert [(i.severity, i.certainty, i.usage.line) for i in impacts] == [
        (BREAKING, AFFECTED, 1), (BREAKING, POSSIBLE, 2), (WARNING, AFFECTED, 1), (WARNING, POSSIBLE, 2),
    ]


def test_common_member_names_only_make_response_changes_possible():
    added = change("response_enum_added", "type", severity=WARNING, method=None, path=None, affects=("POST /v1/charges",))
    assert one([usage(reads={"type"})], [added]).certainty == POSSIBLE
    assert assess([usage(reads={"amount"})], [added]) == []


def test_documentation_hits_are_listed_separately_and_not_counted():
    import json

    from apidrift.impact import render_impact_json, render_impact_text
    from apidrift.scanner import ScanResult

    code = usage({"amount"})
    doc = Usage("README.md", 3, "?", "/v1/charges", "api.stripe.com/v1/charges", frozenset(), False, "doc")
    result = ScanResult("repo", [code, doc], 2, [])
    impacts = assess([code, doc], [change("endpoint_removed")])
    assert len(impacts) == 2
    text = render_impact_text(result, impacts, "old", "new")
    assert "1 Stripe call sites in 1 files (python 1); 1 hit by this upgrade: 1 breaking" in text
    assert "in documentation / fixtures (not counted): 1\n! README.md:3" in text
    payload = json.loads(render_impact_json(result, impacts, "old", "new"))
    assert payload["call_sites"] == 1 and payload["documentation_matches"] == 1
    assert payload["summary"]["breaking"] == 1 and len(payload["impacts"]) == 2
