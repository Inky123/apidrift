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
