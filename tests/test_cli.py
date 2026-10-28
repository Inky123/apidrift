import json

from apidrift.cli import main
from specs import base_doc


def write(tmp_path, name, doc):
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return str(path)


def test_diff_two_files_exits_1_on_breaking_and_prints_json(tmp_path, capsys):
    old = base_doc()
    new = base_doc()
    new["paths"].pop("/v1/charges/{charge}")
    code = main(["diff", "--old", write(tmp_path, "a.json", old), "--new", write(tmp_path, "b.json", new), "--json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"] == {"breaking": 1, "warning": 0, "info": 0}
    assert payload["changes"][0]["where"] == "GET /v1/charges/{charge}"


def test_diff_identical_files_exits_0_with_text_summary(tmp_path, capsys):
    path = write(tmp_path, "a.json", base_doc())
    assert main(["diff", "--old", path, "--new", path]) == 0
    assert "0 breaking, 0 warnings, 0 informational" in capsys.readouterr().out


def test_path_filter_keeps_schema_changes_that_affect_the_path(tmp_path, capsys):
    old = base_doc()
    new = base_doc()
    new["components"]["schemas"]["charge"]["properties"].pop("receipt_url")
    new["paths"]["/v1/refunds"] = {"get": {"responses": {"200": {}}}}
    main(["diff", "--old", write(tmp_path, "a.json", old), "--new", write(tmp_path, "b.json", new), "--path", "/v1/charges", "--json"])
    kinds = [c["kind"] for c in json.loads(capsys.readouterr().out)["changes"]]
    assert kinds == ["response_field_removed"]


def test_impact_flags_call_sites_that_send_a_removed_field(tmp_path, capsys):
    from specs import sdk_doc

    old, new = sdk_doc(), sdk_doc()
    body = new["paths"]["/v1/charges"]["post"]["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]
    body["properties"].pop("description")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pay.py").write_text(
        "import stripe\n"
        "stripe.Charge.create(amount=1, currency='usd', description='x')\n"
        "stripe.Charge.retrieve('ch_1')\n"
    )
    code = main(["impact", str(repo), "--old", write(tmp_path, "a.json", old), "--new", write(tmp_path, "b.json", new), "--json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["call_sites"] == 2
    assert [(i["usage"]["line"], i["change"]["kind"], i["certainty"]) for i in payload["impacts"]] == [
        (2, "body_removed", "affected")
    ]


def test_scan_lists_endpoints_and_fields(tmp_path, capsys):
    from specs import sdk_doc

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pay.py").write_text("import stripe\nstripe.Charge.create(amount=1, description='x')\n")
    assert main(["scan", str(repo), "--spec", write(tmp_path, "spec.json", sdk_doc())]) == 0
    out = capsys.readouterr().out
    assert "pay.py:2  POST /v1/charges  via stripe.Charge.create" in out
    assert "fields: amount, description" in out


def test_json_outputs_carry_versions_and_introduced_for_plain_files(tmp_path, capsys):
    old, new = base_doc(), base_doc()
    new["paths"].pop("/v1/charges/{charge}")
    new["info"]["version"] = "2026-02-01.test"
    main(["diff", "--old", write(tmp_path, "a.json", old), "--new", write(tmp_path, "b.json", new), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["versions"] == ["2026-01-01.test", "2026-02-01.test"]
    assert payload["changes"][0]["introduced"] == "2026-02-01.test"
    assert set(payload) == {"old", "new", "summary", "changes", "versions"}


def test_snapshot_pairs_are_attributed_through_the_snapshots_between_them(tmp_path, monkeypatch, capsys):
    from datetime import datetime, timezone

    from specs import sdk_doc

    from apidrift import snapshot as snap
    from apidrift.providers import PROVIDERS

    monkeypatch.setenv("APIDRIFT_DATA", str(tmp_path / "data"))
    body = lambda d: d["paths"]["/v1/charges"]["post"]["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]  # noqa: E731
    docs = [sdk_doc(), sdk_doc(), sdk_doc()]
    docs[0]["info"]["version"] = "2026-01-01.a"
    docs[1]["info"]["version"] = "2026-02-01.b"
    body(docs[1])["properties"].pop("description")
    docs[2]["info"]["version"] = "2026-03-01.c"
    body(docs[2])["properties"].pop("description")
    docs[2]["paths"].pop("/v1/charges/{charge}")
    for month, doc in enumerate(docs, start=1):
        snap.save(PROVIDERS["stripe"], json.dumps(doc).encode(), now=datetime(2026, month, 1, tzinfo=timezone.utc))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pay.py").write_text("import stripe\nstripe.Charge.create(amount=1, description='x')\nstripe.Charge.retrieve('ch_1')\n")

    assert main(["diff", "stripe", "--old", "2026-01", "--new", "latest", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["versions"] == ["2026-01-01.a", "2026-02-01.b", "2026-03-01.c"]
    assert {c["kind"]: c["introduced"] for c in payload["changes"]} == {
        "body_removed": "2026-02-01.b", "endpoint_removed": "2026-03-01.c",
    }
    assert main(["impact", str(repo), "--old", "2026-01", "--new", "latest", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["versions"] == ["2026-01-01.a", "2026-02-01.b", "2026-03-01.c"]
    assert [(i["usage"]["line"], i["change"]["kind"], i["change"]["introduced"]) for i in payload["impacts"]] == [
        (2, "body_removed", "2026-02-01.b"), (3, "endpoint_removed", "2026-03-01.c"),
    ]
    assert main(["diff", "stripe", "--old", "2026-02", "--new", "latest", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["versions"] == ["2026-02-01.b", "2026-03-01.c"]
    assert [c["introduced"] for c in payload["changes"]] == ["2026-03-01.c"]
