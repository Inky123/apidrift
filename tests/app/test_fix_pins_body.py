"""The merge advice in the fix pull request says only what the evidence supports: an older version rejects a
field that became an object; a renamed field needs a check or an upgrade."""

from __future__ import annotations

import pytest
from conftest import SHA

from apidrift.app import prbody, prpins

ENDIVE = "2026-09-30.endive"
PMT = {"path": "/v1/payment_intents", "target": "payment_method_types", "replacement": "allowed_payment_method_types",
       "action": "rename", "line": 3}
COUNTRIES = {"path": "/v1/financial_connections/sessions", "target": "filters.countries", "replacement": "filters.country",
             "action": "rename_first", "line": 4}
ANCHOR = {"path": "/v1/subscriptions/{subscription_exposed_id}", "target": "billing_cycle_anchor",
          "replacement": "billing_cycle_anchor", "action": "wrap_type", "line": 5}
PAYTO = {"path": "/v1/payment_methods/{payment_method}", "target": "payto", "replacement": "", "action": "delete", "line": 6}
REJECTS = "on your current pinned version Stripe rejects the new shape."
RENAMED = (f"Stripe changed these fields in {ENDIVE}, so an older pinned version may not accept the new names; "
           "check it, or upgrade.")


def body(sites: list[dict], file: str = "a.py", **payload) -> str:
    fixes = [{"file": file, "blob_sha": "0" * 40, "content": "", "sites": sites}]
    return prbody.render_pr(payload, fixes, full_name="o/r", sha=SHA, version=ENDIVE, issue_number=4,
                            fingerprint="f" * 64)[1]


def first_line(text: str) -> str:
    return text.split("\n", 1)[0]


def test_a_wrap_only_pull_says_the_old_version_rejects_it():
    assert first_line(body([ANCHOR])) == (f"Ready for Stripe {ENDIVE}. Merge this together with your API-version and "
                                          f"SDK upgrade; {REJECTS}")


@pytest.mark.parametrize("sites", [[PMT], [COUNTRIES], [PMT, PAYTO]])
def test_a_rename_never_claims_a_rejection(sites):
    line = first_line(body(sites))
    assert RENAMED in line and "rejects" not in line and "rejected" not in line


def test_a_mixed_pull_names_the_rejection_only_for_the_wrapped_fields():
    line = first_line(body([PMT, ANCHOR]))
    assert RENAMED in line and line.endswith("The fields that become an object are rejected on your current pinned version.")


def test_deletes_only_is_unchanged():
    assert first_line(body([PAYTO])).endswith(prpins.DELETES_ONLY)
