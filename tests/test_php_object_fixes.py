"""PHP fixes through the reader's new shapes: calls on returned objects, keys written later, ``array_merge``,
several reaching literals, ``apply_filters`` and string-dispatch wrappers. Each ends as the exact expected file,
a refusal with its reason, or "not attempted".
"""

import json
from pathlib import Path

from specs import _form, _op

from apidrift.diff import BREAKING, diff_chain
from apidrift.fixer import plan_fixes
from apidrift.impact import assess
from apidrift.openapi import load_spec
from apidrift.report import filter_changes
from apidrift.scanner import scan_repo
from apidrift.sdkmap import SdkIndex
from test_fixer import NEW, OLD, docs
from test_php_objects import API, FORWARD, vendored

H = "<?php\n"
CONFIRM = "/v1/payment_intents/{intent}/confirm"


def _docs():
    old, new = docs()
    for doc in (old, new):
        props = {"amount": {"type": "integer"}, NEW: {"type": "array"}}
        if doc is old:
            props[OLD] = {"type": "array"}
        doc["paths"]["/v1/payment_intents/{intent}"] = {"get": {"responses": {"200": {}}}}
        doc["paths"][CONFIRM] = {"post": {"requestBody": _form(props), "responses": {"200": {}}}}
        doc["components"]["schemas"]["payment_intent"]["x-stripeOperations"] += [
            _op("retrieve", "retrieve", "get", "/v1/payment_intents/{intent}"),
            _op("confirm", "custom", "post", CONFIRM),
        ]
    return old, new


def outcome(root: Path, files: dict[str, str], name: str = "app/Pay.php"):
    for f, text in files.items():
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text(text)
    old, new = (load_spec(d) for d in _docs())
    index = SdkIndex.build(old, new)
    changes = filter_changes(diff_chain([old, new])[0], min_severity=BREAKING)
    result = scan_repo(root, index)
    plan = plan_fixes(result, assess(result.usages, changes), index, root)
    if plan.fixes:
        assert len(plan.fixes) == 1 and plan.fixes[0].file == name, plan.to_dict()
        return "fixed", plan.fixes[0].content, sorted(u.why for u in plan.unfixable)
    if plan.unfixable:
        return "refused", sorted(u.why for u in plan.unfixable), []
    return ("not attempted", [u.line for u in plan.not_attempted], []) if plan.not_attempted else ("nothing", None, [])


def renamed(src: str) -> str:
    return src.replace(f"'{OLD}'", f"'{NEW}'")


def test_a_confirm_on_a_retrieved_intent_is_fixed(tmp_path):
    src = H + "function f($id) {\n  $pi = \\Stripe\\PaymentIntent::retrieve($id);\n  $pi->confirm(['" + OLD + "' => ['card']]);\n}\n"
    assert outcome(tmp_path, {"app/Pay.php": src}) == ("fixed", renamed(src), [])


def test_a_confirm_on_an_unknown_object_is_left_alone(tmp_path):
    src = H + "function f($pi) {\n  $pi->confirm(['" + OLD + "' => ['card']]);\n}\n"
    assert outcome(tmp_path, {"app/Pay.php": src})[0] == "nothing"


def test_a_later_literal_write_no_longer_hides_the_break_and_the_literal_is_fixed(tmp_path):
    src = H + (
        "function f($m) {\n"
        "  $d = ['amount' => 1, '" + OLD + "' => $m];\n"
        "  $d['metadata']['k'] = 'v';\n"
        "  return \\Stripe\\PaymentIntent::create($d);\n"
        "}\n"
    )
    assert outcome(tmp_path, {"app/Pay.php": src}) == ("fixed", renamed(src), [])


def test_every_reaching_literal_is_fixed(tmp_path):
    src = H + (
        "function f($a) {\n"
        "  if ($a) { $p = ['" + OLD + "' => ['card']]; } else { $p = ['" + OLD + "' => ['sepa_debit'], 'amount' => 1]; }\n"
        "  return \\Stripe\\PaymentIntent::create($p);\n"
        "}\n"
    )
    assert outcome(tmp_path, {"app/Pay.php": src}) == ("fixed", renamed(src), [])


def test_array_merge_of_a_literal_and_a_local_literal_is_fixed_in_both(tmp_path):
    src = H + (
        "function f() {\n"
        "  $extra = ['" + OLD + "' => ['card']];\n"
        "  return \\Stripe\\PaymentIntent::create(array_merge(['amount' => 1, '" + OLD + "' => ['link']], $extra));\n"
        "}\n"
    )
    assert outcome(tmp_path, {"app/Pay.php": src}) == ("fixed", renamed(src), [])


def test_array_merge_with_a_parameter_is_refused(tmp_path):
    src = H + "function f($params) {\n  return \\Stripe\\PaymentIntent::create(array_merge(['" + OLD + "' => ['card']], $params));\n}\n"
    assert outcome(tmp_path, {"app/Pay.php": src})[:2] == ("refused", ["incomplete_binding"])


def test_a_filtered_variable_is_a_certain_hit_but_refused(tmp_path):
    src = H + (
        "function f($o) {\n"
        "  $p = ['amount' => 1, '" + OLD + "' => ['card']];\n"
        "  $p = apply_filters('my_hook', $p, $o);\n"
        "  return \\Stripe\\PaymentIntent::create($p);\n"
        "}\n"
    )
    assert outcome(tmp_path, {"app/Pay.php": src})[:2] == ("refused", ["incomplete_binding"])


CALLER = H + "// Stripe\nfunction pay($id) {\n  return edds_api_request('PaymentIntent', 'create', ['amount' => 1, '" + OLD + "' => ['card']]);\n}\n"


def _dispatch_files(write=""):
    return {**vendored(), "src/API.php": API.replace("@WRITE@", write),
            "inc/functions.php": FORWARD, "app/Pay.php": CALLER}


def test_a_dispatched_call_whose_wrapper_passes_its_arguments_unchanged_is_fixed(tmp_path):
    assert outcome(tmp_path, _dispatch_files()) == ("fixed", renamed(CALLER), [])


def test_a_dispatched_call_whose_wrapper_writes_into_the_parameters_is_not_edited(tmp_path):
    assert outcome(tmp_path / "a", _dispatch_files("$args[0] = $more;"))[:2] == ("refused", ["incomplete_binding"])
    assert outcome(tmp_path / "b", _dispatch_files("$args[0]['" + OLD + "'] = ['card'];"))[:2] == ("refused", ["incomplete_binding"])


def test_a_dispatched_call_whose_wrapper_adds_another_key_is_still_fixed(tmp_path):
    assert outcome(tmp_path, _dispatch_files("$args[0]['metadata']['via'] = 'edd';")) == ("fixed", renamed(CALLER), [])


HELPER = H + (
    "// Stripe\nfunction pay($p) {\n  return edds_api_request('PaymentIntent', 'create', $p);\n}\n"
    "function go() {\n  return pay(['amount' => 1, '" + OLD + "' => ['card']]);\n}\n"
)


def test_a_key_the_wrapper_writes_reaches_the_callers_of_a_same_file_helper_and_is_not_edited(tmp_path):
    files = _dispatch_files("$args[0]['" + OLD + "'] = ['card'];")
    files["app/Pay.php"] = HELPER
    assert outcome(tmp_path, files)[:2] == ("refused", ["incomplete_binding", "incomplete_binding"])


def test_a_callers_fields_through_a_same_file_helper_include_what_the_wrapper_adds(tmp_path):
    from test_php_objects import _doc
    from apidrift.openapi import load_spec

    files = _dispatch_files("$args[0]['metadata']['via'] = 'edd';")
    files["app/Pay.php"] = HELPER
    for f, text in files.items():
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text(text)
    result = scan_repo(tmp_path, SdkIndex.build(load_spec(_doc())))
    via = [u for u in result.usages if "[through pay" in u.call]
    assert [sorted(u.fields) for u in via] == [["amount", "metadata", "metadata.via", OLD]]


def test_a_helper_caller_is_still_fixed_when_the_wrapper_adds_another_key(tmp_path):
    files = _dispatch_files("$args[0]['metadata']['via'] = 'edd';")
    files["app/Pay.php"] = HELPER
    assert outcome(tmp_path, files)[0] == "fixed"


def test_a_wrapper_that_hands_its_arguments_to_a_by_reference_helper_is_not_edited(tmp_path):
    files = _dispatch_files("$this->defaults($args);")
    files["src/API.php"] = files["src/API.php"].replace(
        "  }\n}\n", "  }\n  private function defaults(array &$args) { $args[0]['" + OLD + "'] = ['card']; }\n}\n"
    )
    assert outcome(tmp_path, files)[:2] == ("refused", ["incomplete_binding"])


def test_a_write_after_the_call_that_only_reaches_later_code_is_neither_a_finding_nor_edited(tmp_path):
    src = H + (
        "function f($xs) {\n"
        "  foreach ($xs as $x) {\n"
        "    $d = ['amount' => 1];\n"
        "    \\Stripe\\PaymentIntent::create($d);\n"
        "    $d['" + OLD + "'] = ['card'];\n"
        "    log_it($d);\n"
        "  }\n"
        "}\n"
    )
    assert outcome(tmp_path, {"app/Pay.php": src})[0] == "nothing"
