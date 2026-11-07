"""P14b: the locked Stripe SDK version for each fixed file's language, read from lockfiles only."""

import json

import pytest

from apidrift import fixsdk
from apidrift.fixsdk import find_sdks, older

GEMFILE_LOCK = """GEM
  remote: https://rubygems.org/
  specs:
    stripe (19.4.0)
    stripe-ruby-mock (3.1.0)
      stripe (> 5)

DEPENDENCIES
  stripe
"""
COMPOSER_LOCK = json.dumps({"packages": [{"name": "stripe/stripe-php", "version": "v13.13.0"},
                                          {"name": "stripe/other", "version": "v1.0.0"}]}, indent=4)
GO_MOD = """module example.com/x

go 1.22

require (
\tgithub.com/stripe/stripe-go/v76 v76.25.0
\tgithub.com/stripe/stripe-go/v72 v72.122.0 // indirect
)

replace github.com/stripe/stripe-go/v99 => ../fork
"""
PACKAGE_LOCK = json.dumps({"lockfileVersion": 3, "packages": {
    "": {"dependencies": {"stripe": "^14.0.0"}},
    "node_modules/stripe": {"version": "14.25.0"},
    "node_modules/@stripe/stripe-js": {"version": "2.1.0"},
    "node_modules/stripe-mock": {"version": "1.0.0"},
}}, indent=2)
YARN_V1 = """# yarn lockfile v1

"@stripe/stripe-js@^1.0.0":
  version "1.54.0"

stripe-mock@^1:
  version "1.0.0"

stripe@^8.0.0, stripe@^8.100.0:
  version "8.222.0"
  resolved "https://registry.yarnpkg.com/stripe/-/stripe-8.222.0.tgz"
"""
YARN_BERRY = """__metadata:
  version: 6

"stripe@npm:^17.0.0":
  version: 17.7.0
  resolution: "stripe@npm:17.7.0"
"""
PNPM = """lockfileVersion: '9.0'

packages:

  '@stripe/stripe-js@4.0.0':
    resolution: {integrity: sha512-x}

  stripe@16.12.0:
    resolution: {integrity: sha512-y}

  stripe-mock@1.0.0:
    resolution: {integrity: sha512-z}
"""
POETRY = '[[package]]\nname = "stripe-mock"\nversion = "1.0"\n\n[[package]]\nname = "stripe"\nversion = "7.14.0"\n'
PIPFILE = json.dumps({"default": {"stripe": {"version": "==5.5.0"}}})


@pytest.mark.parametrize("reader,text,want", [
    (fixsdk._gemfile_lock, GEMFILE_LOCK, [("19.4.0", 4)]),
    (fixsdk._composer_lock, COMPOSER_LOCK, [("13.13.0", 4)]),
    (fixsdk._go_mod, GO_MOD, [("76.25.0", 6)]),
    (fixsdk._package_lock, PACKAGE_LOCK, [("14.25.0", 9)]),
    (fixsdk._yarn_lock, YARN_V1, [("8.222.0", 9)]),
    (fixsdk._yarn_lock, YARN_BERRY, [("17.7.0", 4)]),
    (fixsdk._pnpm_lock, PNPM, [("16.12.0", 8)]),
    (fixsdk._toml_lock, POETRY, [("7.14.0", 5)]),
    (fixsdk._pipfile_lock, PIPFILE, [("5.5.0", 1)]),
    (fixsdk._requirements, "Django==5.0\nstripe[async]==12.0.1  # pinned\n", [("12.0.1", 2)]),
    (fixsdk._package_json, json.dumps({"dependencies": {"stripe": "14.25.0"}}), [("14.25.0", 1)]),
])
def test_each_lockfile_reader(reader, text, want):
    assert reader(text) == want


@pytest.mark.parametrize("reader,text", [
    (fixsdk._requirements, "stripe>=5.0\nstripe-mock==1.0\n"),  # a range is not a locked version
    (fixsdk._package_json, json.dumps({"dependencies": {"stripe": "^14.25.0", "@stripe/stripe-js": "1.0.0"}})),
    (fixsdk._gemfile_lock, "    stripe-ruby-mock (3.1.0)\n      stripe (> 5)\n"),
    (fixsdk._go_mod, "replace github.com/stripe/stripe-go/v76 => ../fork\n"),
    (fixsdk._composer_lock, "{not json"),
    (fixsdk._pipfile_lock, json.dumps({"default": {"stripe": {"version": "*"}}})),
])
def test_what_is_not_a_locked_sdk_version(reader, text):
    assert reader(text) == []


def write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_the_nearest_directory_that_resolves_the_sdk_wins(tmp_path):
    write(tmp_path, {
        "pnpm-lock.yaml": PNPM,
        "packages/api/package.json": json.dumps({"dependencies": {"stripe": "^16.0.0"}}),  # a range: keep looking up
        "packages/api/src/pay.ts": "",
        "Gemfile.lock": GEMFILE_LOCK,
        "billing/Gemfile.lock": GEMFILE_LOCK.replace("19.4.0", "20.1.0"),
        "billing/app/charge.rb": "",
        "app/other.rb": "",
        "go.mod": GO_MOD,
        "cmd/x.go": "",
    })
    got = find_sdks(tmp_path, ["packages/api/src/pay.ts", "billing/app/charge.rb", "app/other.rb", "cmd/x.go"])
    assert [(s.sdk, s.version, s.file, s.line) for s in got] == [
        ("stripe-ruby", "19.4.0", "Gemfile.lock", 4),
        ("stripe-ruby", "20.1.0", "billing/Gemfile.lock", 4),
        ("stripe-go", "76.25.0", "go.mod", 6),
        ("stripe-node", "16.12.0", "pnpm-lock.yaml", 8),
    ]


def test_no_lockfile_no_language_and_no_escape_from_the_root(tmp_path):
    write(tmp_path, {"inner/a.py": "", "inner/requirements.txt": "stripe>=5\n", "notes.md": ""})
    (tmp_path.parent / "requirements.txt").write_text("stripe==1.0.0\n")  # outside the checkout: never read
    assert find_sdks(tmp_path / "inner", ["a.py"]) == []
    assert find_sdks(tmp_path, ["notes.md", "../a.py"]) == []
    (tmp_path.parent / "requirements.txt").unlink()


def test_a_lockfile_with_two_versions_reports_both(tmp_path):
    text = json.dumps({"packages": {"node_modules/stripe": {"version": "8.222.0"},
                                    "node_modules/x/node_modules/stripe": {"version": "14.25.0"}}}, indent=1)
    write(tmp_path, {"package-lock.json": text, "a.js": ""})
    assert [s.version for s in find_sdks(tmp_path, ["a.js"])] == ["8.222.0", "14.25.0"]


@pytest.mark.parametrize("sdk,version,want", [
    ("stripe-ruby", "19.4.0", True), ("stripe-ruby", "19.6.2", True), ("stripe-ruby", "20.0.0", False),
    ("stripe-ruby", "20.1.0", False), ("stripe-php", "13.13.0", True), ("stripe-php", "22.0.0", False),
    ("stripe-go", "76.25.0", True), ("stripe-go", "87.0.0", False), ("stripe-go", "70.15.0+incompatible", True),
    ("stripe-node", "8.222.0", True), ("stripe-node", "23", False), ("stripe-python", "15.4.0", True),
    ("stripe-ruby", "20.0.0-beta.1", None), ("stripe-ruby", "dev-master", None), ("stripe-java", "1.0.0", None),
])
def test_older_than_the_first_endive_release(sdk, version, want):
    assert older(sdk, version, "2026-09-30.endive") is want


def test_an_api_version_the_table_does_not_hold_is_unknown():
    assert older("stripe-ruby", "1.0.0", "2027-01-01.fig") is None


def test_the_plan_carries_the_lock_and_the_dry_run_prints_it(tmp_path):
    from test_fixer_v2 import plan_v2

    from apidrift.fixer import render_fix_text

    plan = plan_v2(tmp_path, {
        "Gemfile.lock": GEMFILE_LOCK,
        "app/sub.rb": "require 'stripe'\n\nStripe::Subscription.update('sub_1', billing_cycle_anchor: 'now')\n",
    })
    assert len(plan.fixes) == 1, plan.to_dict()
    assert plan.to_dict()["sdks"] == [{"sdk": "stripe-ruby", "version": "19.4.0", "file": "Gemfile.lock", "line": 4}]
    assert "  Gemfile.lock:4  stripe-ruby 19.4.0" in render_fix_text(plan, str(tmp_path))
