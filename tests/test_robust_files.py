"""Odd filesystem entries and encodings a checkout can hold: none may hang, abort or silently empty a scan."""

import os
import socket

import pytest

from apidrift.openapi import load_spec
from apidrift.scanner import _iter_files, scan_repo
from apidrift.sdkmap import SdkIndex
from specs import sdk_doc

PY = "import stripe\nstripe.Charge.create(amount=1, currency='usd')\n"
JS = "const stripe = require('stripe')('sk');\nstripe.charges.create({amount: 1, currency: 'usd'});\n"
PHP = "<?php\n$stripe = new \\Stripe\\StripeClient('sk');\n$stripe->charges->create(['amount' => 1, 'currency' => 'usd']);\n"


@pytest.fixture(scope="module")
def index():
    return SdkIndex.build(load_spec(sdk_doc()))


def write(root, files):
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())


def test_a_fifo_or_socket_named_like_source_is_skipped_not_read(tmp_path, monkeypatch, index):
    write(tmp_path, {"real/real.py": PY, "real/real.js": JS})
    os.mkfifo(tmp_path / "pipe.py")  # reading it would block until the scan's timeout
    monkeypatch.chdir(tmp_path)  # a socket path must be short
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind("sock.php")
        files, skipped, _ = _iter_files(tmp_path)
        assert [p.name for p in files] == ["real.js", "real.py"] and skipped == 2
        result = scan_repo(tmp_path, index)
    assert sorted(u.file for u in result.usages) == ["real/real.js", "real/real.py"]
    assert result.skipped_files == 2 and result.files_scanned == 2


def test_a_utf8_byte_order_mark_is_dropped_before_every_reader(tmp_path, index):
    write(tmp_path, {"src/bom.py": "﻿" + PY, "src/bom.js": "﻿" + JS, "src/bom.php": "﻿" + PHP})
    result = scan_repo(tmp_path, index)
    assert sorted((u.file, u.line, u.path) for u in result.usages) == [
        ("src/bom.js", 2, "/v1/charges"), ("src/bom.php", 3, "/v1/charges"), ("src/bom.py", 2, "/v1/charges"),
    ]
    assert result.unread == {} and all(u.fields == {"amount", "currency"} for u in result.usages)


def test_a_python_file_the_parser_rejects_is_counted_as_unread_not_dropped(tmp_path, index):
    late_nul = (PY + "x = 1\n" * 2000).encode() + b"\x00" * 100 + PY.encode()  # a NUL after the first 8 KB
    write(tmp_path, {"src/latenul.py": late_nul, "src/bad.py": "import stripe\ndef (:\n", "src/ok.py": PY})
    result = scan_repo(tmp_path, index)
    assert [u.file for u in result.usages] == ["src/ok.py"]
    assert result.unread == {"Python (unparsable)": 2} and result.skipped_files == 0
    assert "2 Python (unparsable) files mention Stripe and were not checked" in __import__("apidrift.scanner").scanner.render_scan_text(result)


def test_fixes_in_bom_files_place_their_edits_in_the_bytes_on_disk(tmp_path):
    """The fixer reads the file itself, mark included, and the App re-derives the content from the same bytes:
    the mark stays and the edit lands on the right key. Python's parser rejects the mark, so there the fix is
    refused rather than guessed."""
    from test_fixer import NEW, OLD, changed_lines, plan_for

    js = "﻿const stripe = require('stripe')('sk');\n" f"stripe.paymentIntents.create({{amount: 1, {OLD}: ['card']}});\n"
    py = "﻿import stripe\n" f"stripe.PaymentIntent.create(amount=1, {OLD}=['card'])\n"
    plan = plan_for(tmp_path, {"src/pay.js": js, "src/pay.py": py})
    assert [f.file for f in plan.fixes] == ["src/pay.js"]
    fix = plan.fixes[0]
    assert fix.content.startswith("﻿") and changed_lines(fix) == {
        2: (f"stripe.paymentIntents.create({{amount: 1, {OLD}: ['card']}});", f"stripe.paymentIntents.create({{amount: 1, {NEW}: ['card']}});")
    }
    assert [(u.file, u.why) for u in plan.unfixable] == [("src/pay.py", "not_found")]


STUB_TS = 'const url = "https://api.stripe.com/v1/charges";\nexport const stub = () => fetch(url, { method: "post" });\n'


def test_a_single_file_is_reported_relative_to_its_repository_so_the_test_rules_see_its_directory(tmp_path, index):
    write(tmp_path, {"repo/tests/stub.ts": STUB_TS, "loose/tests/stub.ts": STUB_TS})
    (tmp_path / "repo" / ".git").mkdir()
    in_repo = scan_repo(tmp_path / "repo" / "tests" / "stub.ts", index)
    assert [(u.file, u.language) for u in in_repo.usages] == [("tests/stub.ts", "doc")]
    whole = scan_repo(tmp_path / "repo", index)
    assert [(u.file, u.language) for u in whole.usages] == [("tests/stub.ts", "doc")]  # the same answer either way
    loose = scan_repo(tmp_path / "loose" / "tests" / "stub.ts", index)  # no repository above it: its own directory
    assert [(u.file, u.language) for u in loose.usages] == [("stub.ts", "http")]


def test_a_fix_planned_for_a_single_file_finds_it_under_the_same_base(tmp_path):
    from test_fixer import NEW, OLD, changed_lines, docs
    from apidrift.diff import diff_chain
    from apidrift.fixer import plan_fixes
    from apidrift.impact import assess
    from apidrift.report import filter_changes

    js = "const stripe = require('stripe')('sk');\n" f"stripe.paymentIntents.create({{amount: 1, {OLD}: ['card']}});\n"
    write(tmp_path, {"src/pay.js": js})
    (tmp_path / ".git").mkdir()
    old, new = (load_spec(d) for d in docs())
    idx = SdkIndex.build(old, new)
    file = tmp_path / "src" / "pay.js"
    result = scan_repo(file, idx)
    plan = plan_fixes(result, assess(result.usages, filter_changes(diff_chain([old, new])[0], min_severity="breaking")), idx, file)
    assert [f.file for f in plan.fixes] == ["src/pay.js"] and plan.unfixable == []
    assert list(changed_lines(plan.fixes[0])) == [2] and NEW in plan.fixes[0].content
