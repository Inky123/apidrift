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

importers:

  .:
    devDependencies:
      stripe-mock:
        specifier: ^1.0.0
        version: 1.0.0

  packages/api:
    dependencies:
      '@stripe/stripe-js':
        specifier: ^4.0.0
        version: 4.0.0
      stripe:
        specifier: ^16.0.0
        version: 16.12.0(@types/node@20.1.0)

  packages/web:
    dependencies:
      stripe:
        specifier: ^8.0.0
        version: 8.222.0

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
    (fixsdk._go_mod, GO_MOD, [("76.25.0", 6), ("72.122.0", 7)]),  # without the file's imports: both
    (fixsdk._package_lock, PACKAGE_LOCK, [("14.25.0", 9)]),
    (fixsdk._yarn_lock, YARN_V1, [("8.222.0", 9)]),
    (fixsdk._yarn_lock, YARN_BERRY, [("17.7.0", 4)]),
    (fixsdk._pnpm_lock, PNPM, []),  # no importer holds a file at the root
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
        ("stripe-go", "72.122.0", "go.mod", 7),
        ("stripe-node", "16.12.0", "pnpm-lock.yaml", 16),
    ]


def test_no_lockfile_no_language_and_no_escape_from_the_root(tmp_path):
    write(tmp_path, {"inner/a.py": "", "inner/requirements.txt": "stripe>=5\n", "notes.md": ""})
    (tmp_path.parent / "requirements.txt").write_text("stripe==1.0.0\n")  # outside the checkout: never read
    assert find_sdks(tmp_path / "inner", ["a.py"]) == []
    assert find_sdks(tmp_path, ["notes.md", "../a.py"]) == []
    (tmp_path.parent / "requirements.txt").unlink()


@pytest.mark.parametrize("name,text,fixed", [
    # review item 7: each of these used to raise and fail the whole check
    ("package-lock.json", '{"packages": [1]}', "a.js"),
    ("package-lock.json", '{"dependencies": [1]}', "a.js"),
    ("composer.lock", '{"packages": 5}', "a.php"),
    ("Pipfile.lock", '{"default": {"stripe": "==1.0"}}', "a.py"),
    ("package-lock.json", "[" * 200000 + "]" * 200000, "a.js"),
    ("package.json", '{"dependencies": ["stripe"]}', "a.js"),
    ("composer.lock", '{"packages": [{"name": "stripe/stripe-php", "version": {"v": 1}}]}', "a.php"),
    ("package-lock.json", '{"packages": {"node_modules/stripe": {"version": [14]}}}', "a.js"),
])
def test_a_malformed_lockfile_reports_no_lock_and_never_raises(tmp_path, name, text, fixed):
    write(tmp_path, {name: text, fixed: ""})
    assert find_sdks(tmp_path, [fixed]) == []


def test_any_reader_failure_is_no_lock(tmp_path, monkeypatch):
    write(tmp_path, {"Gemfile.lock": GEMFILE_LOCK, "a.rb": ""})
    monkeypatch.setitem(fixsdk.READERS, "stripe-ruby", [("Gemfile.lock", lambda *a: 1 / 0)])
    assert find_sdks(tmp_path, ["a.rb"]) == []


def test_one_version_twice_in_a_lockfile_is_one_lock(tmp_path):
    """fxa's yarn.lock lists stripe@npm:22.3.1 and its patched copy stripe@patch:...22.3.1."""
    text = YARN_BERRY.replace("17.7.0", "22.3.1") + '\n"stripe@patch:stripe@npm%3A22.3.1#~/p.patch":\n  version: 22.3.1\n'
    write(tmp_path, {"yarn.lock": text, "a.ts": ""})
    assert [(s.version, s.line) for s in find_sdks(tmp_path, ["a.ts"])] == [("22.3.1", 4)]


@pytest.mark.parametrize("sub,want", [
    ("packages/api/src/pay.ts", [("16.12.0", 16)]), ("packages/web/a.js", [("8.222.0", 22)]),
    ("packages/apiary/a.js", []), ("a.js", []),
])
def test_pnpm_uses_the_importer_that_holds_the_fixed_file(sub, want):
    """Review item 9: a pnpm workspace resolves stripe per importer; packages: lists every version anywhere."""
    assert fixsdk._pnpm_lock(PNPM, fixsdk.Context(sub)) == want


def test_pnpm_single_project_lockfiles():
    v5 = "lockfileVersion: 5.4\n\ndependencies:\n  stripe: 14.1.0\n  stripe-mock: 1.0.0\n\npackages:\n\n  /stripe/8.0.0:\n"
    assert fixsdk._pnpm_lock(v5, fixsdk.Context("a.js")) == [("14.1.0", 4)]
    v6 = "lockfileVersion: '6.0'\n\ndependencies:\n  stripe:\n    specifier: ^14\n    version: 14.1.0\n"
    assert fixsdk._pnpm_lock(v6, fixsdk.Context("a.js")) == [("14.1.0", 4)]
    alias = "lockfileVersion: '6.0'\n\ndependencies:\n  stripe:\n    specifier: npm:@acme/stripe@1\n    version: /@acme/stripe@1.0.0\n"
    assert fixsdk._pnpm_lock(alias, fixsdk.Context("a.js")) == []


def test_go_mod_require_only_and_the_major_the_file_imports():
    two = "module x\nrequire (\n\tgithub.com/stripe/stripe-go/v72 v72.0.0\n\tgithub.com/stripe/stripe-go/v76 v76.1.0\n)\n"
    src = 'import (\n\tstripe "github.com/stripe/stripe-go/v76"\n\t"github.com/stripe/stripe-go/v76/paymentintent"\n)\n'
    assert fixsdk._go_mod(two, fixsdk.Context("a.go", src)) == [("76.1.0", 4)]
    replace = ("module x\n\nrequire github.com/stripe/stripe-go/v76 v76.0.0\n\nreplace (\n"
               "\tgithub.com/stripe/stripe-go/v82 v82.0.0 => ../fork\n)\n")
    assert fixsdk._go_mod(replace, fixsdk.Context("a.go", "")) == [("76.0.0", 3)]
    assert fixsdk._go_mod("require (\n\tgithub.com/stripe/stripe-go v70.15.0+incompatible\n)\n") == [("70.15.0+incompatible", 2)]


@pytest.mark.parametrize("lock", [
    {"packages": {"node_modules/stripe": {"name": "@acme/stripe-fork", "version": "1.2.0"}}},
    {"dependencies": {"stripe": {"version": "npm:@acme/stripe-fork@1.2.0"}}},
])
def test_an_aliased_fork_is_not_stripe_node(lock):
    assert fixsdk._package_lock(json.dumps(lock), fixsdk.Context("a.js")) == []


@pytest.mark.parametrize("header,want", [
    ('"stripe@npm:@acme/stripe@^1":', []), ('"stripe@npm:stripe-fork@^1":', []), ('"stripe@file:../stripe":', []),
    ('"stripe@npm:^14.0.0":', [("14.0.0", 1)]), ("stripe@^14:", [("14.0.0", 1)]),
])
def test_yarn_aliases_are_not_stripe_node(header, want):
    assert fixsdk._yarn_lock(header + '\n  version: 14.0.0\n') == want


def test_package_lock_uses_the_copy_node_resolves_for_the_file(tmp_path):
    lock = json.dumps({"packages": {
        "node_modules/stripe": {"version": "14.0.0"},
        "node_modules/@payloadcms/plugin-stripe/node_modules/stripe": {"version": "10.17.0"},
        "apps/web/node_modules/stripe": {"version": "18.0.0"},
    }}, indent=1)
    assert [v for v, _ in fixsdk._package_lock(lock, fixsdk.Context("a.js"))] == ["14.0.0"]
    assert [v for v, _ in fixsdk._package_lock(lock, fixsdk.Context("apps/web/src/a.ts"))] == ["18.0.0"]


def test_requirements_txt_wins_and_others_only_without_it(tmp_path):
    write(tmp_path, {"requirements-legacy.txt": "stripe==2.60.0\n", "requirements.txt": "stripe==12.0.0\n", "a.py": ""})
    assert [(s.version, s.file) for s in find_sdks(tmp_path, ["a.py"])] == [("12.0.0", "requirements.txt")]
    (tmp_path / "requirements.txt").write_text("stripe>=12\n")  # present without an exact pin: still no other file
    assert find_sdks(tmp_path, ["a.py"]) == []
    (tmp_path / "requirements.txt").unlink()
    assert [(s.version, s.file) for s in find_sdks(tmp_path, ["a.py"])] == [("2.60.0", "requirements-legacy.txt")]


def test_a_lockfile_with_a_nested_copy_reports_only_the_resolved_one(tmp_path):
    text = json.dumps({"packages": {"node_modules/stripe": {"version": "8.222.0"},
                                    "node_modules/x/node_modules/stripe": {"version": "14.25.0"}}}, indent=1)
    write(tmp_path, {"package-lock.json": text, "a.js": ""})
    assert [s.version for s in find_sdks(tmp_path, ["a.js"])] == ["8.222.0"]


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
