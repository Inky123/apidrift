"""The index is the same machinery for every provider: Plaid's calls are recorded, stored and assessed from the
index exactly as Stripe's are, and a repo keeps one index per provider."""

import json
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import SHA

from apidrift import snapshot as snap
from apidrift.app import db, indexcheck, indexview, inventory
from apidrift.app.checker import run_local
from apidrift.profiles import PROFILES
from apidrift.providers import PROVIDERS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from plaidspec import STRING, plaid_doc, schemas  # noqa: E402

NODE = """\
    import { PlaidApi } from 'plaid';
    const client = new PlaidApi(config);

    export async function sync(token, cursor) {
      return client.transactionsSync({ access_token: token, cursor, count: 100 });
    }

    export async function link(user) {
      return client.linkTokenCreate({
        client_name: 'Shop', language: 'en', country_codes: ['US'], user: { client_user_id: user },
      });
    }
"""
PYTHON = """\
    from plaid.api import plaid_api
    from plaid.model.transactions_get_request import TransactionsGetRequest
    from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions

    client = plaid_api.PlaidApi(api_client)

    def fetch(token):
        request = TransactionsGetRequest(token, "2026-01-01", "2026-02-01", options=TransactionsGetRequestOptions(count=10))
        return client.transactions_get(request)

    def accounts(token):
        return client.accounts_get({"access_token": token})
"""


def _cursor_removed(doc):
    schemas(doc)["TransactionsSyncRequest"]["properties"].pop("cursor")


def _unrelated(doc):
    schemas(doc)["PaymentCreateRequest"]["properties"].pop("reference")
    schemas(doc)["PaymentCreateRequest"]["required"].remove("reference")


def _method_renamed(doc):
    doc["paths"]["/transactions/sync"]["post"]["operationId"] = "transactionsSyncV2"


def _languages(doc):
    """``language`` as an inline enum, as Plaid's real spec has it for ``processor``."""
    schemas(doc)["LinkTokenCreateRequest"]["properties"]["language"] = {"type": "string", "enum": ["en", "fr", "es"]}
    return doc


def _sent_value_withdrawn(doc):
    schemas(doc)["LinkTokenCreateRequest"]["properties"]["language"]["enum"].remove("en")  # the TypeScript sends 'en'


def _other_value_withdrawn(doc):
    schemas(doc)["LinkTokenCreateRequest"]["properties"]["language"]["enum"].remove("es")


def _required_added(doc):
    get = schemas(doc)["TransactionsGetRequest"]
    get["properties"]["account_id"] = STRING
    get["required"].append("account_id")


# Releases after the indexed one. The index answers for each: its calls still resolve against the baseline,
# as they do in a full check, so only the impacts can change.
MUTATE = {
    "cursor_removed": _cursor_removed, "unrelated": _unrelated, "method_renamed": _method_renamed, "required_added": _required_added,
    "sent_value_withdrawn": _sent_value_withdrawn, "other_value_withdrawn": _other_value_withdrawn,
}


def save(doc, version, month):
    doc["info"]["version"] = version
    path = snap.save(PROVIDERS["plaid"], json.dumps(doc).encode(), now=datetime(2026, month, 1, tzinfo=timezone.utc))
    assert path is not None
    return path


@pytest.fixture
def plaid(tmp_path, monkeypatch):
    """A repo calling Plaid from TypeScript and Python, and two Plaid snapshots: the second drops ``count`` from
    POST /transactions/sync, which the TypeScript sends."""
    monkeypatch.setenv("APIDRIFT_DATA", str(tmp_path / "data"))
    tree = tmp_path / "repo"
    tree.mkdir()
    (tree / "sync.ts").write_text(textwrap.dedent(NODE))
    (tree / "worker.py").write_text(textwrap.dedent(PYTHON))
    old = save(_languages(plaid_doc()), "2020-09-14_1.0.0", 1)
    second = _languages(plaid_doc())
    schemas(second)["TransactionsSyncRequest"]["properties"].pop("count")
    new = save(second, "2020-09-14_1.1.0", 2)
    return tree, old, new


@pytest.fixture
def repo_row(conn):
    db.upsert_installation(conn, 77, "octo")
    db.upsert_repo(conn, 501, 77, "octo/shop", None)
    return 501


def full_check(tree, old, new):
    return json.loads(json.dumps(run_local(tree, old.name, new.name, provider="plaid").payload()))


def plaid_snaps(old):
    return sorted(old.parent.glob(f"*{snap.SUFFIX}"))


def test_a_plaid_scan_asks_only_plaids_questions_and_its_export_parses_back(plaid):
    tree, old, new = plaid
    local = run_local(tree, old.name, new.name, provider="plaid")
    exported = json.loads(json.dumps(local.payload()["index"]))
    assert exported["provider"] == "plaid" and not exported["unusable"]
    asked = {name for name, _ in exported["asked"]}
    assert {"method", "required", "has_children"} <= asked <= set(PROFILES["plaid"].questions)
    assert {(u.method, u.path) for u in local.result.usages} == {
        ("post", "/transactions/sync"), ("post", "/link/token/create"), ("post", "/transactions/get"), ("post", "/accounts/get"),
    }
    index = inventory.parse(exported, "plaid")
    assert [u.to_dict() for u in index.usages] == [u.to_dict() for u in local.result.usages]
    with pytest.raises(inventory.Invalid, match="another provider"):
        inventory.parse(exported)  # a Plaid index is never taken for a Stripe one
    assert any(i["change"]["target"] == "count" for i in local.payload()["impacts"])


@pytest.mark.parametrize("name", sorted(MUTATE))
def test_a_plaid_release_assessed_from_the_index_equals_a_full_check(plaid, conn, repo_row, name):
    tree, old, new = plaid
    payload = full_check(tree, old, new)
    assert indexcheck.record(conn, plaid_snaps(old), 501, 1, SHA, payload, old, new, "plaid") is None
    header = inventory.get(conn, 501, "plaid")
    assert header["provider"] == "plaid" and inventory.get(conn, 501) is None  # no Stripe index was written
    third_doc = json.loads(json.dumps(snap.load(new)))
    MUTATE[name](third_doc)
    third = save(third_doc, "2020-09-14_1.2.0", 3)
    stored = inventory.load(conn, header)
    pr = indexcheck.pair(plaid_snaps(old), old, third, "plaid")
    assert indexcheck.same_answers(pr.index, "plaid", stored.answers)
    mine = indexcheck.payload_of(stored.usages, stored.unread, pr)
    expected = full_check(tree, old, third)
    for key in ("impacts", "call_sites", "documentation_matches", "unread", "old_version", "new_version", "versions"):
        assert mine[key] == expected[key], key
    assert {i["change"]["target"] for i in mine["impacts"]} >= ({"count", "cursor"} if name == "cursor_removed" else {"count"})
    language = [i["reason"] for i in mine["impacts"] if i["change"]["target"] == "language"]
    assert language == (["language is sent here as en"] if name == "sent_value_withdrawn" else [])


def test_the_values_a_call_sends_are_kept_only_as_hashes(plaid, conn, repo_row):
    tree, old, new = plaid
    payload = full_check(tree, old, new)
    sent = [s["sent"] for s in payload["index"]["sites"] if s["path"] == "/link/token/create"][0]
    assert ["language", "#" + inventory.name_hash("en")] in sent and ["client_name", "#" + inventory.name_hash("Shop")] in sent
    assert indexcheck.record(conn, plaid_snaps(old), 501, 1, SHA, payload, old, new, "plaid") is None
    rows = json.dumps([dict(r) for r in conn.execute("SELECT * FROM index_sites")] + [dict(conn.execute("SELECT * FROM repo_index").fetchone())])
    assert "Shop" not in rows and '"en"' not in rows and "US" not in rows
    clear = json.loads(json.dumps(payload))
    clear["index"]["sites"][1]["sent"] = [["client_name", "Shop"]]
    assert indexcheck.record(conn, plaid_snaps(old), 501, 2, SHA, clear, old, new, "plaid") == "a sent value is kept in clear"


@pytest.mark.parametrize("name", ["method_renamed", "required_added"])
def test_a_plaid_index_declines_when_its_calls_would_resolve_differently(plaid, conn, repo_row, name):
    """Once the baseline has moved past a release that renamed a method or changed a model's positional
    arguments, the recorded questions get other answers: the index declines and the full check runs."""
    tree, old, new = plaid
    assert indexcheck.record(conn, plaid_snaps(old), 501, 1, SHA, full_check(tree, old, new), old, new, "plaid") is None
    third_doc = json.loads(json.dumps(snap.load(new)))
    MUTATE[name](third_doc)
    third = save(third_doc, "2020-09-14_1.2.0", 3)
    stored = inventory.load(conn, inventory.get(conn, 501, "plaid"))
    assert indexcheck.same_answers(indexcheck.pair(plaid_snaps(old), old, third, "plaid").index, "plaid", stored.answers)
    moved = indexcheck.pair(plaid_snaps(old), third, third, "plaid")  # the baseline is now that release
    assert not indexcheck.same_answers(moved.index, "plaid", stored.answers)


def test_a_repo_keeps_one_index_per_provider(plaid, conn, repo_row, data_dir):
    tree, old, new = plaid
    assert indexcheck.record(conn, plaid_snaps(old), 501, 1, SHA, full_check(tree, old, new), old, new, "plaid") is None
    from indexing import TREES

    from apidrift.app.poller import snapshots

    s_old, s_new = snapshots(data_dir)[:2]
    stripe = json.loads(json.dumps(run_local(TREES["plain"], s_old.name, s_new.name).payload()))
    assert indexcheck.record(conn, [s_old, s_new], 501, 2, SHA, stripe, s_old, s_new) is None
    plaid_header, stripe_header = inventory.get(conn, 501, "plaid"), inventory.get(conn, 501, "stripe")
    assert (plaid_header["job_id"], stripe_header["job_id"]) == (1, 2)
    assert len(inventory.sites(conn, 501, "plaid")) == 4 and len(inventory.sites(conn, 501, "stripe")) == 6
    rendered = indexview.render("octo/shop", plaid_header, inventory.sites(conn, 501, "plaid"))
    assert rendered.startswith(f"octo/shop at {SHA[:12]}: 4 Plaid call sites on 4 endpoints")
    assert indexview.summary(conn)["repos"] == 1
    inventory.drop(conn, 501, "plaid")
    assert inventory.get(conn, 501, "plaid") is None and inventory.get(conn, 501, "stripe") is not None
    bad = full_check(tree, old, new)
    bad["index"]["asked"].append(["resolve", [["stripe", "charges", "create"]]])  # a Stripe question in a Plaid index
    assert indexcheck.record(conn, plaid_snaps(old), 501, 3, SHA, bad, old, new, "plaid") == (
        "a question is not one the provider's index answers"
    )
    inventory.drop(conn, 501)
    assert conn.execute("SELECT count(*) FROM repo_index").fetchone()[0] == 0


def test_every_provider_declares_the_questions_its_index_answers():
    """A provider added later is recorded only through the questions its profile lists; each must be a method
    of its index (a dotted one, of an object the index holds)."""
    for profile in PROFILES.values():
        assert profile.questions, profile.name
        for name, arity in profile.questions.items():
            owner = profile.build_index.__self__
            *holders, method = name.split(".")
            assert arity >= 1 and all(part.isidentifier() and not part.startswith("_") for part in name.split("."))
            if not holders:
                assert callable(getattr(owner, method)), name
