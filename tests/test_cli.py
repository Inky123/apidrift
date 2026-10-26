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
