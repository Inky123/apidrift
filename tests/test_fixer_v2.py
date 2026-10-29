"""Fixer v2: delete, rename_first (a nested rename) and wrap_type, one fixture per action per language."""

import json
import shutil
from pathlib import Path

import pytest

from apidrift.diff import BREAKING, diff_chain
from apidrift.fixer import FixPlan, plan_fixes, render_fix_text
from apidrift.impact import assess
from apidrift.openapi import load_spec
from apidrift.report import filter_changes
from apidrift.scanner import scan_repo
from apidrift.sdkmap import SdkIndex
from specs import _form, _op, sdk_doc

V2 = Path(__file__).parent / "fixtures" / "fixer" / "v2"
PM = "/v1/payment_methods/{payment_method}"
FC = "/v1/financial_connections/sessions"
SUB = "/v1/subscriptions/{subscription_exposed_id}"
RESUME = "/v1/subscriptions/{subscription}/resume"
PREVIEW = "/v1/invoices/create_preview"
ANCHOR_ENUM = {"type": "string", "enum": ["now", "unchanged"]}


def _anchor_object(timestamp: bool) -> dict:
    props = {"type": {"type": "string", "enum": ["now", "unchanged"] + (["timestamp"] if timestamp else [])}}
    if timestamp:
        props["timestamp"] = {"type": "integer"}
    return {"type": "object", "properties": props, "required": ["type"]}


def _resource(name: str, package: str, ops: list[dict]) -> dict:
    return {
        "type": "object", "properties": {"id": {"type": "string"}},
        "x-stripeResource": {"class_name": name, "in_package": package}, "x-stripeOperations": ops,
    }


def v2_docs() -> tuple[dict, dict]:
    """Old and new specs shaped like Stripe's 2026-09-30 changes on the five v2 endpoints."""
    old = sdk_doc()
    paths, schemas = old["paths"], old["components"]["schemas"]
    post = lambda props: {"post": {"requestBody": _form(props), "responses": {"200": {}}}}  # noqa: E731
    paths[PM] = post({
        "payto": {"type": "object", "properties": {"pay_id": {"type": "string"}}},
        "metadata": {"type": "object"}, "billing_details": {"type": "object", "properties": {"name": {"type": "string"}}},
    })
    paths[FC] = post({
        "account_holder": {"type": "object", "properties": {"type": {"type": "string"}, "customer": {"type": "string"}}},
        "permissions": {"type": "array", "items": {"type": "string"}},
        "filters": {"type": "object", "properties": {"countries": {"type": "array", "items": {"type": "string"}}}},
    })
    paths[SUB] = post({"billing_cycle_anchor": dict(ANCHOR_ENUM), "proration_behavior": {"type": "string"}})
    paths[RESUME] = post({"billing_cycle_anchor": dict(ANCHOR_ENUM), "proration_behavior": {"type": "string"}})
    paths[PREVIEW] = post({
        "customer": {"type": "string"},
        "subscription_details": {"type": "object", "properties": {
            "billing_cycle_anchor": {"anyOf": [{"type": "string", "enum": ["now", "unchanged"]}, {"type": "integer"}]},
        }},
    })
    schemas["payment_method"] = _resource("PaymentMethod", "", [_op("update", "update", "post", PM)])
    schemas["financial_connections.session"] = _resource("Session", "FinancialConnections", [_op("create", "create", "post", FC)])
    schemas["subscription"] = _resource("Subscription", "", [
        _op("update", "update", "post", SUB), _op("resume", "custom", "post", RESUME),
    ])
    schemas["invoice"] = _resource("Invoice", "", [_op("create_preview", "custom", "post", PREVIEW)])
    new = json.loads(json.dumps(old))
    new["info"]["version"] = "2026-09-30.endive"
    body = lambda path: new["paths"][path]["post"]["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]  # noqa: E731
    body(PM)["properties"].pop("payto")
    body(FC)["properties"]["filters"]["properties"] = {"country": {"type": "string"}}
    body(SUB)["properties"]["billing_cycle_anchor"] = _anchor_object(False)
    body(RESUME)["properties"]["billing_cycle_anchor"] = _anchor_object(False)
    body(PREVIEW)["properties"]["subscription_details"]["properties"]["billing_cycle_anchor"] = _anchor_object(True)
    return old, new


def specs():
    old, new = (load_spec(d) for d in v2_docs())
    index = SdkIndex.build(old, new)
    return index, filter_changes(diff_chain([old, new])[0], min_severity=BREAKING)


def plan_v2(root: Path, files: dict[str, str | bytes] | None = None) -> FixPlan:
    for name, text in (files or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    index, changes = specs()
    result = scan_repo(root, index)
    return plan_fixes(result, assess(result.usages, changes), index, root)


def findings(root: Path) -> dict[tuple, str]:
    """Breaking findings keyed by (file, call ordinal in the file, path, kind, target): lines shift on deletes."""
    index, changes = specs()
    result = scan_repo(root, index)
    lines = {}
    for u in result.usages:
        lines.setdefault(u.file, set()).add(u.line)
    order = {f: sorted(v) for f, v in lines.items()}
    return {
        (i.usage.file, order[i.usage.file].index(i.usage.line), i.usage.path, i.change.kind, i.change.target): i.certainty
        for i in assess(result.usages, changes)
    }


FIXTURES = sorted(p.name for p in V2.iterdir() if p.is_file())
ACTIONS = {"delete": ("delete", ""), "rename_first": ("rename_first", "filters.country"), "wrap_type": ("wrap_type", None)}


@pytest.mark.parametrize("name", FIXTURES)
def test_every_v2_fixture_yields_the_expected_file_and_the_change_is_gone(tmp_path, name):
    before, after = tmp_path / "before", tmp_path / "after"
    before.mkdir()
    shutil.copy(V2 / name, before / name)
    plan = plan_v2(before)
    assert plan.unfixable == [] and plan.not_attempted == [] and len(plan.fixes) == 1, plan.to_dict()
    fix = plan.fixes[0]
    assert fix.content == (V2 / "expected" / name).read_text(encoding="utf-8")
    action, replacement = ACTIONS[name.split(".")[0]]
    for site in plan.to_dict()["fixes"][0]["sites"]:
        assert site["action"] == action
        assert site["replacement"] == (site["target"] if replacement is None else replacement)
    # Applying the content: every planned change (and the wrap's required .type) is gone, nothing new appears.
    shutil.copytree(before, after)
    (after / name).write_text(fix.content, encoding="utf-8")
    old, new = findings(before), findings(after)
    assert all(k in old and not (old[k] == "possible" and v == "affected") for k, v in new.items()), new
    remaining = {k: v for k, v in new.items() if k[3] in ("body_removed", "body_type_changed", "body_added_required")}
    if plan.possibly_remaining:  # only the deletion behind a spread: still possible, never affected
        assert list(remaining.values()) == ["possible"] and [k[3:] for k in remaining] == [("body_removed", "payto")]
    else:
        assert remaining == {}, remaining
    assert len(plan.possibly_remaining) == (1 if name == "delete.js" else 0)


def test_possibly_remaining_names_the_deletion_behind_a_spread(tmp_path):
    shutil.copy(V2 / "delete.js", tmp_path / "delete.js")
    data = plan_v2(tmp_path).to_dict()
    assert data["possibly_remaining"] == [{"file": "delete.js", "line": 30, "path": PM, "target": "payto"}]
    assert {"fixes", "unfixable", "pins", "migrations", "leftovers", "not_attempted", "possibly_remaining"} == set(data)


def test_fix_text_uses_action_specific_wording(tmp_path):
    for name in ("delete.py", "rename_first.py", "wrap_type.py"):
        shutil.copy(V2 / name, tmp_path / name)
    text = render_fix_text(plan_v2(tmp_path), str(tmp_path))
    assert "payto removed on POST /v1/payment_methods/{payment_method}:" in text
    assert "filters.countries renamed to filters.country, keeping its first element on POST" in text
    assert "billing_cycle_anchor wrapped in {type: …} on POST /v1/subscriptions/{subscription_exposed_id}:" in text
    assert "subscription_details.billing_cycle_anchor wrapped in {type: …} on POST /v1/invoices/create_preview:" in text
    assert " -> " not in text and "renamed:" not in text
