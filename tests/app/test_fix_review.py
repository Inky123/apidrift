"""The independent review's repros for the parent's checks, each kept as a test."""

from __future__ import annotations

import hashlib

from test_fix_test_check import Repo, checked
from test_fixer_v2 import plan_v2

from apidrift.app import fixtestcheck
from apidrift import migrations

SHA = "a" * 40


def git_sha(raw: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()


PM_APP = "def detach(id)\n  Stripe::PaymentMethod.update(id, { payto: { pay_id: 'x' }, metadata: { a: '1' } })\nend\n"


def test_r2_a_delete_keeps_only_the_rest_of_the_comma_line(tmp_path):
    test = 'def test_x(update):\n    update.assert_called_once_with(\n        "pm_1",\n        "a",\n        "b",\n' \
           '        payto={"pay_id": "x"}\n    )\n'
    plan = plan_v2(tmp_path, {"app/pm.rb": PM_APP, "tests/test_pm.py": test})
    payload = plan.to_dict()
    site = payload["fixes"][0]["sites"][0]
    start = test.index('"a",') + 3  # the comma after "a": not the one before payto
    out = test[start + 1 : test.index("payto")]
    out = out[: out.rindex("\n") + 1]
    payload["tests"] = [{"file": "tests/test_pm.py", "line": 6}]
    payload["test_fixes"] = [{"file": "tests/test_pm.py", "blob_sha": git_sha(test.encode()), "edits": [{
        "line": 6, "start": start, "end": test.index('"x"}') + 4, "text": out,
        **{k: site[k] for k in ("path", "target", "replacement", "action")}}]}]
    assert checked(tmp_path, payload)[1] == []
    m = migrations.for_site(site["path"], site["target"])
    good = "f(\n    a=1,  # keep\n    payto=2\n)"
    at = good.index(",  # keep")
    e = {"start": at, "end": good.index("payto=2") + len("payto=2") + 1, "text": "  # keep\n"}
    assert fixtestcheck._shape(good, "python", e, m) is not None  # the fixer's own form still passes
