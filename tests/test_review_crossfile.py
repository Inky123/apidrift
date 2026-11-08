"""Failing repros for the cross-file review of c14e397. Each asserts the expected (safe) behaviour."""
import time

from test_fixer import plan_for  # noqa: E402
from test_helpers import scan  # noqa: E402

import crosscases_js as J  # noqa: E402
import crosscases_php as P  # noqa: E402
import crosscases_this as T  # noqa: E402
import crosscases_vendor as V  # noqa: E402


def case(mod, label):
    return dict(next(f for l, f in mod.CASES if l == label))


def edited(tmp_path, files):
    plan = plan_for(tmp_path, files)
    return {(f.file, n) for f in plan.fixes for n, (a, b) in enumerate(zip(f.original.splitlines(), f.content.splitlines()), 1) if a != b}


# 1. wrong edits
def test_js_shadowed_import_is_not_the_wrapper(tmp_path):
    assert ("lib/go.js", 3) not in edited(tmp_path, case(J, "shadowed import param"))


def test_js_object_literal_this_is_not_the_class(tmp_path):
    assert edited(tmp_path, case(T, "object literal this")) == set()


def test_js_function_this_is_not_the_class(tmp_path):
    assert edited(tmp_path, case(T, "function this thisArg")) == set()


def test_js_string_di_token_may_bind_another_class(tmp_path):
    assert edited(tmp_path, case(J, "string DI token useClass")) == set()


def test_php_public_property_written_via_brace_name(tmp_path):
    assert edited(tmp_path, case(P, "brace property write")) == set()


# 2. a finding that disappears: the wrapper's own site must stay when a caller is unseen
def wrapper_kept(tmp_path, files, wrapper_file):
    return any(u.file == wrapper_file and "[through" not in u.call for u in scan(tmp_path, files))


def test_php_vendor_parent_may_call_the_method(tmp_path):
    assert wrapper_kept(tmp_path, case(V, "vendor parent absent (app clone)"), "Driver.php")


def test_php_attribute_listener_is_called_by_the_framework(tmp_path):
    assert wrapper_kept(tmp_path, case(P, "attribute listener"), "Driver.php")


def test_js_decorated_handler_is_called_by_the_framework(tmp_path):
    assert wrapper_kept(tmp_path, case(J, "decorated framework entrypoint"), "src/stripe/payment.service.ts")


def test_php_caller_in_inc_file(tmp_path):
    assert wrapper_kept(tmp_path, case(P, "inc file caller"), "Driver.php")


def test_js_installed_node_modules_does_not_change_the_result(tmp_path):
    files = {"src/billing.ts": J.FUNC, "src/go.ts": "import { createIntent } from './billing';\nexport const go = () => createIntent({ amount: 1 });\n"}
    without = sorted(u.call for u in scan(tmp_path / "a", files))
    big = {f"node_modules/p{i}/f{j}.js": "module.exports=1;\n" for i in range(61) for j in range(1001)}
    with_nm = sorted(u.call for u in scan(tmp_path / "b", {**files, **big}))
    assert with_nm == without


# 4. time bound
def test_php_walk_is_not_quadratic_on_a_long_word(tmp_path):
    files = {"Driver.php": P.DRIVER, "Card.php": P.CALLER,
             "database/seeds/logo.php": "<?php\nreturn hex2bin('" + "89504e470d0a1a0a" * 8000 + "');\n"}
    t = time.time()
    scan(tmp_path, files)
    assert time.time() - t < 5
