"""``apidrift scan`` and ``impact`` with ``--provider plaid``, end to end on spec files and a small checkout."""

import json

import pytest

from apidrift.cli import main
from plaidspec import plaid_doc, schemas

NODE = """\
import { Configuration, PlaidApi } from 'plaid';
const client = new PlaidApi(new Configuration({}));
export async function link(userId) {
  return client.linkTokenCreate({ client_name: 'App', language: 'en', country_codes: ['US'], user: { client_user_id: userId } });
}
export async function sync(request) {
  return client.transactionsSync(request);
}
"""
PYTHON = """\
from plaid.api import plaid_api
from plaid.model.transactions_sync_request import TransactionsSyncRequest
client = plaid_api.PlaidApi(api_client)

def sync(token, cursor):
    return client.transactions_sync(TransactionsSyncRequest(access_token=token, cursor=cursor, count=100))
"""


def write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return str(path)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "plaid.ts").write_text(NODE)
    (root / "bank.py").write_text(PYTHON)
    (root / "legacy.js").write_text("const plaid = require('plaid');\nconst old = new plaid.Client(cfg);\nold.getCategories(cb);\nold.getAccounts(token);\n")
    (root / "Bank.java").write_text("// PlaidApi plaidClient\n")
    (root / "billing.py").write_text("import stripe\nstripe.Charge.create(amount=1)\n")
    return root


def test_scan_lists_plaid_calls_in_node_and_python_and_says_what_it_did_not_read(tmp_path, repo, capsys):
    spec = write(tmp_path, "plaid.json", plaid_doc())
    assert main(["scan", str(repo), "--provider", "plaid", "--spec", spec, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [(u["file"], u["line"], u["method"], u["path"], u["complete"]) for u in payload["usages"]] == [
        ("bank.py", 6, "post", "/transactions/sync", True),
        ("legacy.js", 4, "post", "/accounts/get", False),  # the pre-2021 client: endpoint known, fields not
        ("src/plaid.ts", 4, "post", "/link/token/create", True),
        ("src/plaid.ts", 7, "post", "/transactions/sync", False),
    ]
    assert payload["unread"] == {"Java": 1}
    assert payload["unresolved"] == [{"file": "legacy.js", "line": 3, "call": "old.getCategories"}]  # not in this spec
    assert payload["files_scanned"] == 4  # the Stripe-only file is not read

    assert main(["scan", str(repo), "--provider", "plaid", "--spec", spec]) == 0
    text = capsys.readouterr().out
    assert "4 Plaid call sites in 3 files (typescript 2, javascript 1, python 1); 4 files mention plaid; 1 other plaid calls (not matched to an endpoint)" in text
    assert "not read: apidrift does not read Java call sites yet: 1 Java file mention Plaid and were not checked." in text
    assert "other plaid calls (SDK helpers, methods whose endpoint the stored specs lack), not matched to an endpoint:" in text


def test_the_default_provider_still_reads_stripe_and_ignores_plaid_calls(tmp_path, repo, capsys):
    from specs import sdk_doc

    assert main(["scan", str(repo), "--spec", write(tmp_path, "stripe.json", sdk_doc()), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [(u["file"], u["path"]) for u in payload["usages"]] == [("billing.py", "/v1/charges")]


def test_fix_says_plainly_that_plaid_has_no_documented_migrations(tmp_path, repo):
    spec = write(tmp_path, "plaid.json", plaid_doc())
    with pytest.raises(SystemExit, match="no documented migrations for plaid"):
        main(["fix", str(repo), "--provider", "plaid", "--old", spec, "--new", spec])


def test_a_spec_without_operation_ids_cannot_resolve_plaid_calls(tmp_path, repo):
    doc = plaid_doc()
    for item in doc["paths"].values():
        item["post"].pop("operationId")
    with pytest.raises(SystemExit, match="no SDK method metadata"):
        main(["scan", str(repo), "--provider", "plaid", "--spec", write(tmp_path, "bare.json", doc)])


def test_impact_reports_a_removed_request_field_where_it_is_sent_and_possibly_where_it_may_be(tmp_path, repo, capsys):
    old, new = plaid_doc(), plaid_doc()
    schemas(new)["TransactionsSyncRequest"]["properties"].pop("count")
    args = ["impact", str(repo), "--provider", "plaid", "--old", write(tmp_path, "a.json", old), "--new", write(tmp_path, "b.json", new)]
    assert main(args + ["--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["call_sites"] == 4
    assert [(i["usage"]["file"], i["usage"]["line"], i["change"]["kind"], i["certainty"]) for i in payload["impacts"]] == [
        ("bank.py", 6, "body_removed", "affected"), ("src/plaid.ts", 7, "body_removed", "possible"),
    ]
    assert main(args) == 1
    text = capsys.readouterr().out
    assert "4 Plaid call sites in 3 files (typescript 2, javascript 1, python 1); 2 hit by this upgrade: 2 breaking" in text
    assert "other plaid calls (SDK helpers, methods whose endpoint the stored specs lack): 1 (apidrift scan lists them)" in text
