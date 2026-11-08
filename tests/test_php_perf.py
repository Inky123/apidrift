"""The PHP reader stays linear in long functions: each ``$var->method(`` asks what ``$var`` holds once per function,
not once per call (a 1,500-call function took 124 s in review when every call re-scanned the function)."""

import time

from test_php_objects import _doc

from apidrift.openapi import load_spec
from apidrift.scanner import scan_text
from apidrift.sdkmap import SdkIndex


def _timed(text: str, index) -> float:
    t = time.perf_counter()
    scan_text(text, "big.php", index)
    return time.perf_counter() - t


def test_a_long_function_of_method_calls_is_read_in_linear_time():
    index = SdkIndex.build(load_spec(_doc()))
    body = "".join(f"    $wpdb->query($x{n % 7});\n" for n in range(3000))
    text = "<?php\n// Stripe\nfunction f($wpdb, $xs) {\n" + body + "}\n"
    assert _timed(text, index) < 1.5


def test_a_long_function_with_a_stripe_object_and_many_calls_on_it_stays_fast():
    index = SdkIndex.build(load_spec(_doc()))
    body = "".join(f"    $c->capture(['amount' => {n}]);\n    $wpdb->query($x);\n" for n in range(1000))
    text = "<?php\nfunction f($id, $wpdb) {\n    $c = \\Stripe\\Charge::retrieve($id);\n" + body + "}\n"
    assert _timed(text, index) < 3.0
