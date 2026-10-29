"""Fix actions beyond a rename (rename_first, delete, wrap_type): the parent re-derives every site from its own
migration table, and the pull request describes each action in fixed sentences."""

from __future__ import annotations

import pytest
from conftest import SHA
from test_fix import CHARGES, charges_migration, claim, scan  # noqa: F401  (fixtures)

from apidrift import migrations
from apidrift.app import fixcheck, fixpr, prbody
from apidrift.app.checker import run_job

ENDIVE = "2026-09-30.endive"
PMT = {"path": "/v1/payment_intents", "target": "payment_method_types", "replacement": "allowed_payment_method_types", "action": "rename"}
PAYTO = {"path": "/v1/payment_methods/{payment_method}", "target": "payto", "replacement": "", "action": "delete"}
COUNTRIES = {"path": "/v1/financial_connections/sessions", "target": "filters.countries", "replacement": "filters.country", "action": "rename_first"}
ANCHOR = {"path": "/v1/subscriptions/{subscription_exposed_id}", "target": "billing_cycle_anchor", "replacement": "billing_cycle_anchor", "action": "wrap_type"}
PREVIEW = {
    "path": "/v1/invoices/create_preview", "target": "subscription_details.billing_cycle_anchor",
    "replacement": "subscription_details.billing_cycle_anchor", "action": "wrap_type",
}
ALL = (PMT, PAYTO, COUNTRIES, ANCHOR, PREVIEW)


def site(base: dict, line: int = 3, **change) -> dict:
    return {**base, "line": line, **change}


def fix(*sites: dict, file: str = "a.py") -> dict:
    return {"file": file, "blob_sha": "0" * 40, "content": "", "sites": list(sites)}


def render(fixes: list[dict], **payload) -> tuple[str, str]:
    return prbody.render_pr(payload, fixes, full_name="o/r", sha=SHA, version=ENDIVE, issue_number=4, fingerprint="f" * 64)


# --- the parent re-derives each site from its own table --------------------------------------


@pytest.mark.parametrize("base", ALL, ids=lambda b: b["action"] + ":" + b["target"])
def test_every_documented_action_is_admitted(base):
    assert fixcheck.reject_sites([site(base)]) is None


@pytest.mark.parametrize("change", [
    dict(PMT, action="delete", replacement=""),                    # a rename entry claimed as a delete
    dict(PAYTO, action="rename", replacement="payto_v2"),          # a delete claimed as a rename
    dict(COUNTRIES, replacement="filters.region"),                  # not the documented replacement
    dict(COUNTRIES, action="rename"),                               # right names, wrong action
    dict(ANCHOR, replacement=""),                                   # a wrap with its key deleted
    dict(ANCHOR, action="delete", replacement=""),
    dict(PMT, action="drop_table"),                                 # not an action at all
    dict(PMT, action=None),
    dict(PMT, replacement=""),                                      # an empty replacement for a rename
    dict(PMT, path="/v1/charges"),                                  # no documented migration for (path, target)
    dict(PMT, target="a.b.c.d.e.f"),                                # deeper than five segments
    dict(PMT, target="filters..countries"),
    dict(PMT, target="Filters.countries"),
    dict(COUNTRIES, replacement="filters.country "),
    dict(PAYTO, replacement=None),
])
def test_a_site_that_disagrees_with_the_table_is_rejected(change):
    assert fixcheck.reject_sites([site(PMT), site(change)]) is not None


class Meta:
    """Answers the content-meta lookup as if every file is at the scanned blob."""

    def get_content_meta(self, token, full_name, file, sha):
        return {"type": "file", "sha": "0" * 40}


def test_validate_rebuilds_each_site_from_the_table_and_drops_child_extras():
    child = fix(site(ANCHOR, value="{type: 'evil'}", note="@octocat"), site(PAYTO, 9, why="x"), file="sub.js")
    (accepted,) = fixcheck.validate(Meta(), "t", "o/r", SHA, [child])
    assert accepted["sites"] == [
        {"line": 3, "path": ANCHOR["path"], "target": "billing_cycle_anchor", "replacement": "billing_cycle_anchor", "action": "wrap_type"},
        {"line": 9, "path": PAYTO["path"], "target": "payto", "replacement": "", "action": "delete"},
    ]
    assert fixcheck.validate(Meta(), "t", "o/r", SHA, [fix(site(ANCHOR), site(PMT, action="delete", replacement=""))]) == []


def test_a_mislabelled_site_pushes_nothing(scan, installed, conn, gh, settings):
    installed.grant_writes()
    scan.payload["fixes"][0]["sites"][0].update(action="delete", replacement="")  # description is a documented rename
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: nothing to push")
    assert installed.refs == {} and installed.blobs == {}


def test_a_documented_delete_opens_a_pull_described_by_the_parent(scan, installed, conn, gh, settings):
    installed.grant_writes()
    scan.payload["fixes"][0]["sites"] = [site(PAYTO, 5)]
    scan.payload["migrations"] = [dict(PAYTO, method="post", note="cc @octocat", doc="javascript:x", introduced="2099-01-01.x")]
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: opened #2")
    pull = installed.pulls[2]
    assert pull["title"] == f"Stripe {ENDIVE}: remove payto (1 call site)"
    assert "**remove `payto` from POST `/v1/payment_methods/{payment_method}`**" in pull["body"]
    assert "@octocat" not in pull["body"] and "javascript:" not in pull["body"]
    assert installed.commits[installed.refs["heads/apidrift/stripe-" + ENDIVE]]["message"] == f"Stripe {ENDIVE}: remove payto"


# --- the pull request body ------------------------------------------------------------------


def _note(base: dict) -> str:
    found = migrations.for_site(base["path"], base["target"])
    assert found is not None
    return found.note


def test_each_action_has_its_own_heading_and_the_tables_note():
    fixes = [fix(*(site(b, i + 1) for i, b in enumerate(ALL)))]
    title, body = render(fixes, migrations=[dict(PMT, note="child note @zach", doc="https://evil")])
    assert title == f"Stripe {ENDIVE}: 4 migrations (5 call sites)"
    for heading in (
        "**`payment_method_types` → `allowed_payment_method_types` on POST `/v1/payment_intents`**",
        "**`filters.countries` → `filters.country` on POST `/v1/financial_connections/sessions`**",
        "**remove `payto` from POST `/v1/payment_methods/{payment_method}`**",
        "**`billing_cycle_anchor` becomes an object with `type` on POST `/v1/subscriptions/{subscription_exposed_id}`**",
        "**`subscription_details.billing_cycle_anchor` becomes an object with `type` on POST `/v1/invoices/create_preview`**",
    ):
        assert heading in body, heading
    for base in ALL:
        assert prbody._note(_note(base)) in body
    assert f"[Changelog]({migrations.PAYTO_DOC})" in body and f"[Changelog]({migrations.ANCHOR_DOC})" in body
    assert "child note" not in body and "https://evil" not in body  # the child's migrations list is ignored
    assert "Stripe rejects the new shape." in body
    assert f"5 call sites now send what Stripe {ENDIVE} expects; every other Stripe call is unchanged." in body


@pytest.mark.parametrize(("base", "phrase"), [
    (PMT, "rename payment_method_types"), (COUNTRIES, "rename filters.countries"), (PAYTO, "remove payto"),
    (ANCHOR, "wrap billing_cycle_anchor"), (PREVIEW, "wrap billing_cycle_anchor"),
])
def test_one_migration_names_itself_in_the_title_and_the_commit(base, phrase):
    fixes = [fix(site(base, 1), site(base, 2))]
    assert render(fixes)[0] == f"Stripe {ENDIVE}: {phrase} (2 call sites)"
    assert fixpr.commit_message(ENDIVE, fixes) == f"Stripe {ENDIVE}: {phrase}"


def test_the_same_change_on_several_endpoints_is_one_phrase():
    resume = dict(ANCHOR, path="/v1/subscriptions/{subscription}/resume")
    fixes = [fix(site(ANCHOR, 1), site(resume, 2), site(PREVIEW, 3))]
    assert render(fixes)[0] == f"Stripe {ENDIVE}: wrap billing_cycle_anchor (3 call sites)"
    assert fixpr.commit_message(ENDIVE, [fix(site(PAYTO), site(PMT, 4))]) == f"Stripe {ENDIVE}: 2 migrations"


def test_title_never_carries_an_unvalidated_token():
    assert prbody.phrase("delete", "@zach") == "remove a removed field"
    assert prbody.phrase("wrap_type", "x.@y") == "wrap a removed field"
    assert prbody.pr_title("@octocat", {("wrap_type", "a.b")}, 1) == "Stripe the next release: wrap b (1 call site)"


def test_left_for_you_lists_possibly_remaining_and_the_new_reasons():
    payload = {
        "possibly_remaining": [
            {"file": "a.py", "line": 8, "path": PAYTO["path"], "target": "payto"},
            {"file": "zz.js", "line": 1, "path": PAYTO["path"], "target": "payto"},     # not a site this PR changes
            {"file": "a.py", "line": 3, "path": "/v1/x", "target": "@octocat"},        # not in the table
            "junk",
        ],
        "unfixable": [
            {"file": "a.py", "line": 1, "why": "nested_array"}, {"file": "b.py", "line": 2, "why": "unsupported_value"},
            {"file": "c.py", "line": 3, "why": "would_empty_literal"}, {"file": "d.py", "line": 4, "why": "syntax"},
        ],
    }
    _, body = render([fix(site(PAYTO, 8), site(PAYTO, 12))], **payload)
    possibly = f"- `payto` possibly still sent through a spread or a variable apidrift cannot see: https://github.com/o/r/blob/{SHA}/a.py#L8"
    assert possibly + "\n" in body and "zz.js" not in body and "@octocat" not in body
    assert f"1 call site now sends what Stripe {ENDIVE} expects (1 may still send it through a spread; see below);" in body
    for why, file in (
        ("nested_array", "a.py#L1"), ("unsupported_value", "b.py#L2"), ("would_empty_literal", "c.py#L3"), ("syntax", "d.py#L4"),
    ):
        assert f"- Not changed, {prbody.UNFIXABLE_WHY[why]}: https://github.com/o/r/blob/{SHA}/{file}" in body
    assert "- Not changed, the edited file would not parse, so apidrift left it alone: " in body


def test_unknown_reasons_render_one_fixed_sentence_with_capped_links():
    hostile = ["@octocat ping", "see #1", "www.evil.example", "x" * 5000]
    payload = {
        "unfixable": [{"file": f"f{i}.py", "line": i + 1, "why": hostile[i % 4]} for i in range(120)],
        "not_attempted": [{"file": "n.py", "line": 1, "why": "@octocat"}, {"file": "m.py", "line": 2, "why": "#7"}],
    }
    _, body = render([fix(site(PMT))], **payload)
    for text in ("@octocat", "#1 ", "#7", "www.evil", "xxxx"):
        assert text not in body, text
    (line,) = [ln for ln in body.splitlines() if prbody.UNKNOWN_UNFIXABLE in ln]
    assert line.endswith(" and 70 more") and line.count("https://") == prbody.MAX_PR_LINKS
    assert f"- Not attempted ({prbody.UNKNOWN_NOT_ATTEMPTED}): " in body and body.count("- Not attempted") == 1


def test_a_deletes_only_pull_does_not_claim_it_must_wait_for_the_upgrade():
    pins = [{"file": "a.py", "line": 1, "version": "2024-06-20"}]
    _, body = render([fix(site(PAYTO))], pins=pins)
    assert body.startswith(f"Ready for Stripe {ENDIVE}. Removing these fields is safe on your current API version too, "
                           "so this can merge any time.\n")
    assert "**API-version pins found**" in body and "does not depend on it" in body
    assert "Merge together" not in body and "rejects the new shape" not in body and "predates" not in body
    _, mixed = render([fix(site(PAYTO), site(ANCHOR, 4))], pins=pins)
    assert "rejects the new shape." in mixed and "**Merge together with your upgrade**" in mixed
    assert "These files pin an API version that predates this change;" in mixed and "can merge any time" not in mixed


def test_one_note_per_migration_and_its_endpoints_sit_together():
    confirm = dict(PMT, path="/v1/payment_intents/{intent}/confirm")
    resume = dict(ANCHOR, path="/v1/subscriptions/{subscription}/resume")
    _, body = render([fix(site(PMT, 1), site(ANCHOR, 2), site(confirm, 3), site(resume, 4), site(PREVIEW, 5))])
    assert body.count(prbody._note(migrations.PAYMENT_METHOD_TYPES_NOTE)) == 1
    assert body.count("Stripe now takes an object with a type instead of a string.") == 1  # subscriptions + resume
    headings = [ln for ln in body.splitlines() if ln.startswith("**`")]
    pmt = [i for i, h in enumerate(headings) if "payment_method_types" in h]
    assert len(headings) == 5 and pmt[1] - pmt[0] == 1  # one migration's endpoints are adjacent


def test_object_literals_in_a_note_are_code_spans():
    preview = migrations.for_site(PREVIEW["path"], PREVIEW["target"])
    assert preview is not None and "{type: 'timestamp'" in preview.note
    _, body = render([fix(site(PREVIEW))])
    assert "a timestamp becomes `{type: 'timestamp', timestamp: …}`." in body
    assert prbody._note("x {a: [b](c)} [d](e)") == "x `{a: [b](c)}` \\[d\\]\\(e\\)"


def test_pin_note_is_action_neutral():
    _, body = render([fix(site(ANCHOR))], pins=[{"file": "a.py", "line": 1, "version": "2024-06-20"}])
    assert "These files pin an API version that predates this change;" in body and "rename" not in body


def test_every_table_entry_is_reachable_by_its_site():
    real = {key: m for key, m in migrations._BY_SITE.items() if m is not CHARGES}  # CHARGES is the test spec's entry
    assert len(real) == len(migrations.MIGRATIONS)
    assert all(migrations.for_site(m.path, m.target) is m for m in migrations.MIGRATIONS)


# --- fingerprint --------------------------------------------------------------------------


def test_fingerprint_covers_the_action_and_keeps_old_rename_fingerprints():
    rename = [fix(site(PMT))]
    old_formula = fixpr.hashlib.sha256(
        (repr(("a.py", "0" * 40, PMT["target"], PMT["replacement"])) + f"\n{ENDIVE}\n#4").encode()
    ).hexdigest()
    assert fixpr.fingerprint(rename, ENDIVE, 4) == old_formula  # an open PR from before actions stays "unchanged"
    relabelled = [fix(site(PMT, action="rename_first"))]
    assert fixpr.fingerprint(relabelled, ENDIVE, 4) != old_formula
