"""A fix pull request names the tests that still expect the old field, so they are updated in the same change."""

from apidrift.app.prbody import render_pr
from apidrift.fixtests import stale_tests


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_test_files_naming_a_renamed_field_are_listed_with_their_lines(tmp_path):
    write(tmp_path, "spec/services/pay_spec.rb", "it do\n  expect(x).to eq(\n    payment_method_types: [:card],\n  )\nend\n")
    write(tmp_path, "src/__tests__/pay.test.ts", "expect(body).toEqual({ payment_method_types: ['card'] })\n")
    write(tmp_path, "internal/pay_test.go", "want := stripe.PaymentIntentParams{PaymentMethodTypes: nil}\n")
    write(tmp_path, "tests/test_pay.py", "assert params['payment_method_types'] == ['card']\n")
    write(tmp_path, "app/pay.rb", "payment_method_types: [:card]\n")  # not a test: the fixer's own business
    write(tmp_path, "spec/fixtures/stripe/pi.json", '{"payment_method_types": ["card"]}\n')  # Stripe's payload, not code
    write(tmp_path, "spec/other_spec.rb", "allowed_payment_method_types: [:card]\nmy_payment_method_types = 1\n")
    found = stale_tests(tmp_path, [("rename", "payment_method_types")], set())
    assert found == [
        ("internal/pay_test.go", 1), ("spec/services/pay_spec.rb", 3), ("src/__tests__/pay.test.ts", 1),
        ("tests/test_pay.py", 1),
    ]


def test_edited_files_wrap_type_and_nested_targets(tmp_path):
    write(tmp_path, "spec/a_spec.rb", "billing_cycle_anchor: 'now'\n")
    write(tmp_path, "spec/b_spec.rb", "filters: { countries: ['US'] }\n")
    assert stale_tests(tmp_path, [("wrap_type", "billing_cycle_anchor")], set()) == []  # the key itself stays
    assert stale_tests(tmp_path, [("rename_first", "filters.countries")], set()) == [("spec/b_spec.rb", 1)]
    assert stale_tests(tmp_path, [("rename_first", "filters.countries")], {"spec/b_spec.rb"}) == []
    assert stale_tests(tmp_path, [], set()) == []


def test_the_list_is_capped(tmp_path):
    write(tmp_path, "spec/many_spec.rb", "payment_method_types\n" * 80)
    assert len(stale_tests(tmp_path, [("delete", "payment_method_types")], set())) == 50


def test_the_pull_request_names_the_tests_to_update():
    payload = {
        "fixes": [{"file": "app/pay.rb", "content": "x", "sites": [
            {"line": 3, "path": "/v1/payment_intents", "target": "payment_method_types",
             "replacement": "allowed_payment_method_types", "action": "rename"}]}],
        "migrations": [], "pins": [], "unfixable": [], "not_attempted": [], "leftovers": [],
        "tests": [{"file": "spec/pay_spec.rb", "line": 7}],
    }
    def render(p):
        return render_pr(p, p["fixes"], full_name="octo/shop", sha="a" * 40, version="2026-09-30.endive",
                         issue_number=1, fingerprint="f" * 64)[1]

    body = render(payload)
    assert "Tests that name the old field" in body and "fails until it is updated here" in body
    assert "spec/pay_spec.rb#L7" in body
    assert "Tests that name" not in render({**payload, "tests": []})
