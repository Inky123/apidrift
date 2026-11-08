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
