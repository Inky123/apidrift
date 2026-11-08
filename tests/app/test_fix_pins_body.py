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


def pin(version: str, file: str = "config/stripe.rb", line: int = 3, **extra) -> dict:
    return {"file": file, "line": line, "version": version, **extra}


def test_server_pins_first_and_test_and_browser_pins_on_one_counted_line():
    pins = [
        pin("2023-10-16", "spec/a_spec.rb", 9, kind="server"),  # the path makes it a test whatever the child says
        pin("2020-08-27", "client/card.js", 13, kind="browser"),
        pin("2023-10-16", "config/initializers/stripe.rb", 3, kind="test"),  # not a test path: the server's pin
        pin("2022-11-15", "app/services/tax.rb", 19),
    ]
    lines = section(body([PMT], pins=pins)).split("\n")
    assert lines[2:4] == [
        f"- https://github.com/o/r/blob/{SHA}/config/initializers/stripe.rb#L3 pins `2023-10-16`",
        f"- https://github.com/o/r/blob/{SHA}/app/services/tax.rb#L19 pins `2022-11-15`",
    ]
    assert lines[4] == (
        "Also pinned in tests and browser Stripe.js code, which does not set your server's API version (2): "
        f"https://github.com/o/r/blob/{SHA}/spec/a_spec.rb#L9 (`2023-10-16`), "
        f"https://github.com/o/r/blob/{SHA}/client/card.js#L13 (`2020-08-27`)."
    )
    assert len(lines) == 5


def test_only_test_pins_say_there_is_no_server_pin():
    text = section(body([ANCHOR], pins=[pin("2023-10-16", "test/a.py", 1), pin("2023-10-16", "test/b.py", 2)]))
    assert f"No server-side API-version pin older than {ENDIVE} was found in the scanned files." in text
    assert "Also pinned in tests, which does not set your server's API version (2): " in text


def test_a_deletes_only_pull_splits_them_too():
    text = section(body([PAYTO], pins=[pin("2023-10-16", "client/x.js", 1, kind="browser")]))
    assert text.split("\n")[1] == ("Also pinned in browser Stripe.js code, which does not set your server's API version "
                                   f"(1): https://github.com/o/r/blob/{SHA}/client/x.js#L1 (`2023-10-16`).")


def section(text: str) -> str:
    start = text.index("**Merge together with your upgrade**") if "**Merge together" in text else text.index("**API-version")
    return text[start:text.index("\n\nVerified:")]


def test_only_pins_older_than_the_release_are_listed_as_predating_it():
    text = section(body([PMT], pins=[pin("2025-04-30.basil"), pin("2026-09-30.endive", line=4),
                                     pin("2026-10-28.endive", line=5), pin("2026-09-30.preview", line=6)]))
    assert text.split("\n")[1] == (f"These files pin an API version older than {ENDIVE}, the release that changed these "
                                   "fields: check that the pinned version accepts the new field, or bump them and the "
                                   "Stripe SDK when you merge it:")
    assert "/config/stripe.rb#L3 pins `2025-04-30.basil`" in text and text.count("\n- ") == 1
    assert text.split("\n")[-1].startswith(f"Already pinned to {ENDIVE} or later: ")
    assert "`2026-10-28.endive`" in text and "`2026-09-30.preview`" in text


def test_a_wrap_pin_says_it_rejects_and_no_older_pin_says_so():
    text = section(body([ANCHOR], pins=[pin("2024-09-30.acacia")]))
    assert f"older than {ENDIVE}, which rejects the new shape; bump them" in text
    text = section(body([ANCHOR], pins=[pin("2026-10-28.endive")]))
    assert f"No server-side API-version pin older than {ENDIVE} was found in the scanned files." in text and "rejects" not in text


@pytest.mark.parametrize("bad", [
    pin("2024"), pin("`2023-10-16`"), pin("2023-10-16; beta=v1"), pin("2023-10-16", file="../x.rb"),
    pin("2023-10-16", line=True), {"file": "a.rb", "line": 1}, "2023-10-16",
])
def test_a_pin_the_parent_cannot_compare_is_dropped(bad):
    text = section(body([PMT], pins=[bad]))
    assert f"No server-side API-version pin older than {ENDIVE} was found" in text and "2023" not in text
