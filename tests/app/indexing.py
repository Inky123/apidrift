"""Shared by the index tests: Stripe releases to poll, and helpers to drive a repo through checks and releases."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from conftest import FIXTURES, NEW_VERSION, REPO, THIRD_VERSION, _body, new_doc, save_snapshot

from apidrift.app import db
from apidrift.app.poller import poll_stripe, snapshots
from apidrift.app.worker import drain

TREES = {"plain": REPO, "possible": FIXTURES / "repo_possible", "docs": FIXTURES / "repo_docs", "fixes": FIXTURES / "repo_fixes"}
FOURTH_VERSION, FIFTH_VERSION = "2026-04-01.test", "2026-05-01.test"
_OP = {"method_on": "service", "operation": "post"}


def _unrelated(doc: dict) -> None:
    """A breaking change no fixture's code is hit by in a way an issue shows: a refund reason is withdrawn."""
    refunds = doc["paths"]["/v1/charges/{charge}/refunds"]["post"]["requestBody"]["content"]
    refunds["application/x-www-form-urlencoded"]["schema"]["properties"]["reason"]["enum"] = ["duplicate"]


def _currency(doc: dict) -> None:
    _body(doc)["properties"].pop("currency")
    _body(doc)["required"] = ["amount"]


def _response(doc: dict) -> None:
    charge = doc["components"]["schemas"]["charge"]["properties"]
    charge.pop("receipt_url")
    charge["billing_details"]["properties"].pop("email")


def _endpoint_removed(doc: dict) -> None:
    """GET /v1/charges/{charge} goes away; the baseline still has it, so calls to it still resolve."""
    doc["paths"]["/v1/charges/{charge}"].pop("get")
    ops = doc["components"]["schemas"]["charge"]["x-stripeOperations"]
    ops[:] = [op for op in ops if op["method_name"] != "retrieve"]


def _required(doc: dict) -> None:
    _body(doc)["properties"]["shipping"]["required"] = ["name"]


def _restored(doc: dict) -> None:
    _body(doc)["properties"]["description"] = {"type": "string"}


def _repick(doc: dict) -> None:
    """A second ``create`` on the Charge resource at a path that sorts first: ``Charge.create`` (Python, Ruby)
    now resolves to it, ``charges.create`` (JavaScript) does not."""
    doc["paths"]["/v1/billing"] = {"post": json.loads(json.dumps(doc["paths"]["/v1/charges"]["post"]))}
    doc["components"]["schemas"]["charge"]["x-stripeOperations"].append(
        {"method_name": "create", "method_type": "create", "path": "/v1/billing", **_OP}
    )


def _refunds(doc: dict) -> None:
    """A top-level refunds endpoint: ``stripe.Refund.create``, which matched nothing before, now resolves."""
    doc["paths"]["/v1/refunds"] = {"post": json.loads(json.dumps(doc["paths"]["/v1/charges/{charge}/refunds"]["post"]))}
    doc["components"]["schemas"]["refund"]["x-stripeOperations"].append(
        {"method_name": "create", "method_type": "create", "path": "/v1/refunds", **_OP}
    )


MUTATIONS = {
    "unrelated": _unrelated, "currency": _currency, "response": _response, "endpoint_removed": _endpoint_removed,
    "required": _required, "restored": _restored, "repick": _repick, "refunds": _refunds,
}


def release(*names: str, version: str = THIRD_VERSION, base: dict | None = None) -> dict:
    """The NEW_VERSION spec (or ``base``) one release later, with the named mutations applied."""
    doc = json.loads(json.dumps(base)) if base is not None else new_doc()
    assert doc["info"]["version"] != version and NEW_VERSION <= doc["info"]["version"]
    doc["info"]["version"] = version
    for name in names:
        MUTATIONS[name](doc)
    return doc


def save_release(doc: dict, month: int = 3):
    return save_snapshot(doc, datetime(2026, month, 1, tzinfo=timezone.utc))


def poll(settings, conn, gh, doc: dict):
    """Let the poller fetch ``doc`` as Stripe's current spec; returns the new snapshot path (or None)."""
    body = json.dumps(doc).encode()
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    return poll_stripe(settings, conn, client, gh)


def checked(conn, gh, settings) -> int:
    """Run every queued job to the end (as the worker does); returns how many ran."""
    conn.execute("UPDATE jobs SET not_before = ? WHERE status = 'queued'", (db.now(),))
    return drain(conn, gh, settings)


def latest(settings):
    return snapshots(settings.data)[-1]


# --- which code the sandboxed child runs ------------------------------------------------------
#
# ``scan_subprocess`` starts ``python -I -m apidrift.cli``: isolated mode ignores PYTHONPATH, so the child
# imports the *installed* apidrift. In a git worktree that is another checkout than the one under test.

_child_is_ours: bool | None = None


def child_is_this_checkout() -> bool:
    global _child_is_ours
    if _child_is_ours is None:
        import subprocess
        import sys
        from pathlib import Path

        import apidrift

        probe = subprocess.run([sys.executable, "-I", "-c", "import apidrift; print(apidrift.__file__)"], capture_output=True, text=True)
        found = probe.stdout.strip()
        _child_is_ours = bool(found) and Path(found).resolve() == Path(apidrift.__file__).resolve()
    return _child_is_ours


def in_process_child(tree, old, new, data_dir, max_mem_mb=0, timeout=0, fix=False) -> dict:
    """The child's pipeline without the process boundary: the same ``run_local``, the same JSON round trip."""
    from apidrift.app.checker import run_local

    return json.loads(json.dumps(run_local(tree, old, new, fix=fix).payload()))


@pytest.fixture
def own_child(monkeypatch):
    """Full checks run this checkout's child code, also where the installed package is another checkout."""
    if not child_is_this_checkout():
        from apidrift.app import checker

        monkeypatch.setattr(checker, "scan_subprocess", in_process_child)


def clean_tree(tmp_path):
    """A copy of the plain fixture repo that no longer sends ``description``: clean against NEW_VERSION."""
    import re
    import shutil

    tree = tmp_path / "clean"
    shutil.copytree(REPO, tree)
    for path in tree.iterdir():
        path.write_text(re.sub(r",\s*description[:=][^,)\n]*", "", path.read_text()))
    return tree
