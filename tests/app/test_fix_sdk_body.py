"""P14b in the pull request: the parent checks every locked-SDK entry the child reports and says, in its own
sentences and from its own table, whether that release is older than the first one for the needed API version."""

from __future__ import annotations

import pytest
from conftest import SHA

from apidrift.app import fixcheck, prbody

ENDIVE = "2026-09-30.endive"
PMT = {"path": "/v1/payment_intents", "target": "payment_method_types", "replacement": "allowed_payment_method_types",
       "action": "rename", "line": 3}
PAYTO = {"path": "/v1/payment_methods/{payment_method}", "target": "payto", "replacement": "", "action": "delete", "line": 3}


def fix(site: dict, file: str) -> dict:
    return {"file": file, "blob_sha": "0" * 40, "content": "", "sites": [site]}


def body(fixes: list[dict], sdks: list) -> str:
    return prbody.render_pr({"sdks": sdks}, fixes, full_name="o/r", sha=SHA, version=ENDIVE, issue_number=4,
                            fingerprint="f" * 64)[1]


def lock(sdk: str, version: str, file: str = "Gemfile.lock", line: int = 120) -> dict:
    return {"sdk": sdk, "version": version, "file": file, "line": line}


def test_an_older_ruby_sdk_gets_one_plain_sentence_with_the_changelog():
    text = body([fix(PMT, "app/pay.rb")], [lock("stripe-ruby", "19.4.0")])
    assert "**Stripe SDK version**" in text
    assert (f"- https://github.com/o/r/blob/{SHA}/Gemfile.lock#L120 locks `stripe-ruby` `19.4.0`, "
            "which is older than [stripe-ruby 20.0.0](https://github.com/stripe/stripe-ruby/blob/master/CHANGELOG.md#20-0-0), "
            "its first release for Stripe 2026-09-30.endive. Upgrade it in this change, or make sure every request it "
            "sends uses API version 2026-09-30.endive or later.\n") in text


def test_go_says_the_import_path_changes_and_node_typescript_may_not_compile():
    text = body([fix(PMT, "pay.go")], [lock("stripe-go", "76.25.0", "go.mod", 6)])
    assert "`github.com/stripe/stripe-go/v87`" in text and "apidrift does not change import paths" in text
    text = body([fix(PMT, "pay.ts")], [lock("stripe-node", "14.25.0", "package-lock.json", 9)])
    assert "older than [stripe-node 23.0.0]" in text and "TypeScript types may not have the new field names or shapes" in text
    assert "TypeScript" not in body([fix(PMT, "pay.js")], [lock("stripe-node", "14.25.0", "package-lock.json", 9)])


def test_a_current_or_unreadable_version_is_said_without_a_claim():
    text = body([fix(PMT, "a.py")], [lock("stripe-python", "16.1.0", "poetry.lock", 5)])
    assert "locks `stripe-python` `16.1.0`, a release built for Stripe 2026-09-30.endive or later." in text
    text = body([fix(PMT, "a.py")], [lock("stripe-python", "16.0.0b1", "poetry.lock", 5)])
    assert ("locks `stripe-python` `16.0.0b1`; it must be a release built for Stripe API version 2026-09-30.endive "
            "or later (stripe-python 16.0.0 or later).") in text


def test_deletions_only_and_no_locks_add_nothing():
    assert "Stripe SDK version" not in body([fix(PAYTO, "a.rb")], [lock("stripe-ruby", "1.0.0")])
    assert "Stripe SDK version" not in body([fix(PMT, "a.rb")], [])


@pytest.mark.parametrize("entry", [
    lock("stripe-go", "76.25.0", "go.mod"),  # no Go file is fixed
    lock("stripe-java", "1.0.0"),
    lock("stripe-ruby", "19.4.0](https://evil)"),
    lock("stripe-ruby", "1" * 41),
    lock("stripe-ruby", "19.4.0", "../Gemfile.lock"),
    lock("stripe-ruby", "19.4.0", "/etc/Gemfile.lock"),
    lock("stripe-ruby", "19.4.0", "app/pay.rb"),  # not a lockfile or manifest
    lock("stripe-ruby", "19.4.0", "a/./Gemfile.lock"),
    lock("stripe-ruby", "19.4.0", line=True),
    lock("stripe-ruby", "19.4.0", line=0),
    {"sdk": "stripe-ruby", "version": 19, "file": "Gemfile.lock", "line": 1},
    "stripe-ruby 19.4.0",
    # review item 8: the lockfile must be the SDK's own ecosystem's, and never a vendored dependency's
    lock("stripe-ruby", "19.4.0", "package-lock.json"),
    lock("stripe-ruby", "19.4.0", "composer.lock"),
    lock("stripe-ruby", "19.4.0", "vendor/bundle/x/Gemfile.lock"),
    lock("stripe-ruby", "19.4.0", "node_modules/x/Gemfile.lock"),
])
def test_the_parent_drops_anything_it_cannot_check(entry):
    fixes = [fix(PMT, "app/pay.rb")]
    assert fixcheck.sdk_locks([entry], fixes) == []
    assert "Stripe SDK version" not in body(fixes, [entry])


@pytest.mark.parametrize("sdk,file,source", [
    ("stripe-ruby", "Gemfile.lock", "a.rb"), ("stripe-php", "composer.lock", "a.php"), ("stripe-go", "go.mod", "a.go"),
    ("stripe-node", "package-lock.json", "a.ts"), ("stripe-node", "yarn.lock", "a.js"),
    ("stripe-node", "pnpm-lock.yaml", "a.js"), ("stripe-node", "package.json", "a.js"),
    ("stripe-python", "poetry.lock", "a.py"), ("stripe-python", "uv.lock", "a.py"),
    ("stripe-python", "Pipfile.lock", "a.py"), ("stripe-python", "requirements-prod.txt", "a.py"),
])
def test_each_sdk_is_believed_from_its_own_lockfiles(sdk, file, source):
    fixes = [fix(PMT, source)]
    assert fixcheck.sdk_locks([lock(sdk, "1.0.0", file)], fixes) == [lock(sdk, "1.0.0", file)]
    others = {"stripe-ruby": "go.mod", "stripe-php": "Gemfile.lock", "stripe-go": "package.json",
              "stripe-node": "poetry.lock", "stripe-python": "yarn.lock"}
    assert fixcheck.sdk_locks([lock(sdk, "1.0.0", others[sdk])], fixes) == []


def test_the_list_is_capped_and_deduplicated():
    fixes = [fix(PMT, "app/pay.rb")]
    many = [lock("stripe-ruby", f"19.{n}.0", f"d{n}/Gemfile.lock") for n in range(30)]
    assert len(fixcheck.sdk_locks(many + many, fixes)) == 10
    assert fixcheck.sdk_locks([lock("stripe-ruby", "19.4.0")] * 3, fixes) == [lock("stripe-ruby", "19.4.0")]
    assert fixcheck.sdk_locks("nope", fixes) == []
