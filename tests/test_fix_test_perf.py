"""The test-file readers are linear and capped: a hostile-but-legal blob (thousands of mock factories,
expectations or methods) costs milliseconds, not minutes, in the child and in the parent alike."""

from __future__ import annotations

import time

import pytest
from test_fix_test_edits import JS_APP
from test_fixer import plan_for

from apidrift import fixtestbuild, fixtestfind

SITE = ("/v1/payment_intents", "payment_method_types", 10**6, "Stripe::PaymentIntent.create")


def js_blob(n: int) -> str:
    factories = "".join(f"vi.mock('stripe', () => ({{ default: class {{ paymentIntents = {{ create: mockCreate{i} }} }} }}));\n"
                        for i in range(n))
    expects = "".join(f"test('t{i}', () => {{ expect(mockCreate{i}).toHaveBeenCalledWith({{ payment_method_types: ['card'] }}); }});\n"
                      for i in range(n))
    return factories + expects


def php_blob(n: int, one_function: bool = False) -> str:
    if one_function:  # every assignment in one region: the old reader rescanned it per expectation
        body = "".join(f"    $m{i} = \\Mockery::mock('alias:Stripe\\\\PaymentIntent');\n"
                       f"    $m{i}->shouldReceive('create')->with(['payment_method_types' => ['card']]);\n" for i in range(n))
        return "<?php\nclass T {\n  function t() {\n" + body + "  }\n}\n"
    body = "".join(f"  function t{i}() {{\n    $m = \\Mockery::mock('alias:Stripe\\\\PaymentIntent');\n"
                   f"    $m->shouldReceive('create')->with(['payment_method_types' => ['card']]);\n  }}\n" for i in range(n))
    return "<?php\nuse Stripe\\PaymentIntent;\nclass T {\n" + body + "}\n"


def ruby_blob(n: int) -> tuple[str, list[tuple[int, int]]]:
    body = "class Svc\n" + "".join(f"  def m{i}\n    1\n  end\n" for i in range(n))
    body += "  def params\n    { payment_method_types: ['card'] }\n  end\n  def go\n    Stripe::PaymentIntent.create(params)\n  end\nend\n"
    k = body.index("payment_method_types")
    return body, [(k, k + len("payment_method_types"))]


def timed(fn):
    start = time.perf_counter()
    result = fn()
    return result, time.perf_counter() - start


@pytest.fixture
def uncapped(monkeypatch):
    monkeypatch.setattr(fixtestfind, "MAX_EXPECTATIONS", 10**9)
    monkeypatch.setattr(fixtestbuild, "MAX_DEFS", 10**9)


def test_jest_factories_are_indexed_once_not_per_expectation(uncapped):
    found, took = timed(lambda: fixtestfind.find(js_blob(1000), "javascript"))  # 199 KB: 120 s before
    assert len(found) == 1000 and all(e.head == "stripe.paymentIntents.create" for e in found)
    assert took < 1.0


def test_php_function_and_assignment_positions_are_indexed_once(uncapped):
    found, took = timed(lambda: fixtestfind.find(php_blob(4000), "php"))  # 631 KB: 23 s before
    assert len(found) == 4000 and all(e.head == "\\Stripe\\PaymentIntent::create" for e in found)
    assert took < 1.0
    found, took = timed(lambda: fixtestfind.find(php_blob(2000, one_function=True), "php"))
    assert len(found) == 2000 and took < 1.0


def test_builders_do_not_join_the_whole_file_per_method(uncapped):
    body, edits = ruby_blob(8000)  # 222 KB: 70 s before
    found, took = timed(lambda: fixtestbuild.builders("app/svc.rb", body, edits, [SITE]))
    assert [b.method for b in found] == ["params"] and took < 1.0
    nested = "".join(" " * i + f"def m{i}\n" for i in range(1500)) + "".join(" " * (1499 - i) + "end\n" for i in range(1500))
    found, took = timed(lambda: fixtestbuild.builders("app/svc.rb", nested, [(0, 3)], [SITE]))
    assert found == [] and took < 1.0


def test_builders_read_the_same_methods_as_before():
    """The one-pass ``_defs`` keeps the rules: the end is the first later non-blank line at the method's indent
    or less (a comment there counts), recorded only when it is that method's ``end``."""
    lines = (
        "module A\n  class B\n    def one\n      1\n    end\n\n    def two\n      def inner\n        2\n      end\n    end\n"
        "    def three\n      3\n    # a comment at the method's indent ends the search\n    end\n"
        "    def four\n      4\n  end\n  end\nend\n"
    ).splitlines()
    assert fixtestbuild._defs(lines) == [("one", 3, 5, "A::B"), ("two", 7, 11, "A::B"), ("inner", 8, 10, "A::B")]


def test_changed_lines_come_from_the_edits_not_a_diff():
    text = "a\nbb\nccc\ndddd\n"
    assert fixtestbuild._changed_lines(text, [(0, 1)]) == {1}
    assert fixtestbuild._changed_lines(text, [(2, 2)]) == {2}  # an insertion, on its line
    assert fixtestbuild._changed_lines(text, [(3, 10)]) == {2, 3, 4}  # ``b\nccc\nd``
    assert fixtestbuild._changed_lines(text, [(5, 9)]) == {3}  # a whole line with its newline: that line only
    assert fixtestbuild._changed_lines(text, [(5, 9), (0, 1)]) == {1, 3}


def test_past_the_cap_the_readers_refuse_instead_of_working():
    with pytest.raises(fixtestfind.Overloaded):
        fixtestfind.find(js_blob(fixtestfind.MAX_EXPECTATIONS + 1), "javascript")
    with pytest.raises(fixtestfind.Overloaded):
        fixtestfind.find(php_blob(fixtestfind.MAX_EXPECTATIONS + 1), "php")
    with pytest.raises(fixtestfind.Overloaded):
        fixtestfind.find("RSpec.describe X do\n" + "  expect(Stripe::Charge).to receive(:create).with(a: 1)\n" * 600 + "end\n", "ruby")
    with pytest.raises(fixtestfind.Overloaded):
        fixtestfind.find("def test_x(m):\n" + "    m.assert_called_once_with(a=1)\n" * 600, "python")
    body, edits = ruby_blob(fixtestbuild.MAX_DEFS + 1)
    with pytest.raises(fixtestfind.Overloaded):
        fixtestbuild.builders("app/svc.rb", body, edits, [SITE])
    assert len(fixtestfind.find(js_blob(fixtestfind.MAX_EXPECTATIONS - 2), "javascript")) == fixtestfind.MAX_EXPECTATIONS - 2


def test_the_child_lists_an_over_cap_test_file_instead_of_editing_it(tmp_path):
    noise = "".join(f"  expect(db.save{i}).toHaveBeenCalledWith({{ id: {i} }});\n" for i in range(fixtestfind.MAX_EXPECTATIONS))
    test = ("test('pays', () => {\n" + noise
            + "  expect(stripe.paymentIntents.create).toHaveBeenCalledWith({ payment_method_types: ['card'], amount: 1 });\n});\n")
    plan = plan_for(tmp_path / "big", {"src/pay.js": JS_APP, "src/pay.test.js": test})
    assert [f.file for f in plan.fixes] == ["src/pay.js"]
    assert plan.test_fixes == [] and ("src/pay.test.js", fixtestfind.MAX_EXPECTATIONS + 2) in plan.tests
    smaller = test.replace(noise, noise[: noise.index(f"save{fixtestfind.MAX_EXPECTATIONS // 2}")])
    plan = plan_for(tmp_path / "ok", {"src/pay.js": JS_APP, "src/pay.test.js": smaller})
    assert [t.file for t in plan.test_fixes] == ["src/pay.test.js"]
