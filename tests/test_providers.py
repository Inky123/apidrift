"""More than one vendor: YAML sources, the registry, and snapshots that never mix."""

import gzip
import json
from datetime import datetime, timezone

import pytest

from apidrift import snapshot as snap
from apidrift.cli import main
from apidrift.providers import PROVIDERS, get_provider
from specs import base_doc

STRIPE, PLAID = PROVIDERS["stripe"], PROVIDERS["plaid"]

YAML = b"""\
# a comment that is not part of the document
openapi: 3.0.0
info:
  version: 2020-09-14_1.2.3
paths:
  /item/get:
    post:
      operationId: itemGet
      responses:
        200:
          description: OK
components:
  schemas:
    Flag:
      type: string
      enum: [yes, no, on, off, true, 2020-09-14, 0123, 12:30, 7, 1.5, null]
"""


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("APIDRIFT_DATA", str(tmp_path / "data"))
    return tmp_path / "data"


def as_yaml(doc):
    """JSON is YAML; a leading comment keeps the sniffing honest."""
    return b"# generated\n---\n" + json.dumps(doc, indent=1).encode()


def test_registry_knows_plaid_and_rejects_unknown_names():
    assert get_provider("plaid") is PLAID
    assert PLAID.spec_url.endswith("/plaid/plaid-openapi/master/2020-09-14.yml")
    assert PLAID.changelog_url.endswith("CHANGELOG.md")
    assert all(name == provider.name for name, provider in PROVIDERS.items())
    with pytest.raises(SystemExit):
        get_provider("nope")


def test_yaml_scalars_are_typed_the_way_json_types_them():
    doc = snap.parse(YAML)
    assert doc["info"]["version"] == "2020-09-14_1.2.3"
    assert doc["components"]["schemas"]["Flag"]["enum"] == [
        "yes", "no", "on", "off", True, "2020-09-14", "0123", "12:30", 7, 1.5, None,
    ]


def test_json_is_parsed_as_json_and_anything_else_is_refused():
    assert snap.parse(b'\xef\xbb\xbf  {"info": {"version": "1"}}') == {"info": {"version": "1"}}
    for bad in (b"[1, 2]", b"just words", b"a: [unclosed", b"{not json}", b""):
        with pytest.raises(ValueError):
            snap.parse(bad)


def test_yaml_cannot_build_python_objects():
    with pytest.raises(ValueError):
        snap.parse(b"info: !!python/object/apply:os.system ['true']\n")


def test_a_yaml_source_is_stored_as_compact_json_named_by_that_content():
    path = snap.save(PLAID, YAML, now=datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc))
    stored = gzip.open(path, "rb").read()
    assert stored.startswith(b'{"openapi":"3.0.0","info":{"version":"2020-09-14_1.2.3"}')  # the vendor's key order
    assert json.loads(stored) == json.loads(json.dumps(snap.parse(YAML)))  # 200 became "200", nothing else changed
    assert path.name == f"2026-11-04T120000Z_2020-09-14-1-2-3_{snap.digest(stored)[:12]}.json.gz"
    assert snap.sha_of(path) == snap.digest(stored)[:12]
    assert snap.load(path)["paths"]["/item/get"]["post"]["responses"]["200"] == {"description": "OK"}


def test_reformatting_a_yaml_source_does_not_make_a_new_snapshot_but_a_change_does():
    assert snap.save(PLAID, YAML) is not None
    assert snap.save(PLAID, YAML.replace(b"# a comment", b"# another comment").replace(b"[yes, no,", b"[yes,   no,")) is None
    assert snap.save(PLAID, YAML.replace(b"1.2.3", b"1.2.4")) is not None
    assert len(snap.list_snapshots("plaid")) == 2


def test_a_json_source_is_still_stored_byte_for_byte():
    raw = json.dumps(base_doc(), indent=2).encode()
    path = snap.save(STRIPE, raw)
    assert gzip.open(path, "rb").read() == raw and snap.sha_of(path) == snap.digest(raw)[:12]


def test_yaml_that_json_cannot_hold_or_that_explodes_is_refused_and_nothing_is_stored():
    bomb = b"a: &a [x, x, x, x, x, x, x, x, x, x]\n"
    for prev, name in zip("abcdefg", "bcdefgh"):
        bomb += f"{name}: &{name} [*{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}]\n".encode()
    for bad in (bomb, b"info: {version: !!binary aGk=}\n", b"? [a, b]\n: 1\n", b"a: &a [*a]\n"):
        with pytest.raises(ValueError):
            snap.save(PLAID, bad)
    assert snap.list_snapshots("plaid") == []


def test_snapshots_of_two_providers_never_mix(data_dir):
    same = json.dumps(base_doc()).encode()
    first = snap.save(STRIPE, same, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    second = snap.save(PLAID, same, now=datetime(2026, 1, 2, tzinfo=timezone.utc))  # same bytes: not a duplicate
    third = snap.save(PLAID, YAML, now=datetime(2026, 1, 3, tzinfo=timezone.utc))
    assert first.parent == data_dir / "snapshots" / "stripe" and second.parent == data_dir / "snapshots" / "plaid"
    assert snap.list_snapshots("stripe") == [first] and snap.list_snapshots("plaid") == [second, third]
    assert snap.resolve("plaid", "latest") == third and snap.resolve("stripe", "latest") == first
    with pytest.raises(SystemExit):
        snap.resolve("stripe", "previous")  # stripe has one snapshot; plaid's are not counted
    with pytest.raises(SystemExit):
        snap.resolve("stripe", third.name)
    assert snap.between("stripe", second, third) == [] and snap.between("plaid", first, third) == []


def test_cli_snapshot_list_and_diff_work_for_plaid(monkeypatch, capsys):
    old, new = base_doc(), base_doc()
    old["info"]["version"], new["info"]["version"] = "2020-09-14_1.0.0", "2020-09-14_1.1.0"
    new["paths"].pop("/v1/charges/{charge}")
    served = iter([as_yaml(old), as_yaml(old), as_yaml(new)])
    urls = []
    monkeypatch.setattr(snap, "fetch", lambda url: urls.append(url) or next(served))

    assert main(["list", "plaid"]) == 1
    assert main(["snapshot", "plaid"]) == 0 and "saved" in capsys.readouterr().out
    assert main(["snapshot", "plaid"]) == 0 and "unchanged" in capsys.readouterr().out
    assert main(["snapshot", "plaid"]) == 0
    assert urls == [PLAID.spec_url] * 3
    capsys.readouterr()
    assert main(["list", "plaid"]) == 0
    names = capsys.readouterr().out.split()
    assert len(names) == 2 and all(name.endswith(".json.gz") for name in names)
    assert "2020-09-14-1-0-0" in names[0] and "2020-09-14-1-1-0" in names[1]
    assert main(["list", "stripe"]) == 1  # nothing leaked into the other provider
    capsys.readouterr()

    assert main(["diff", "plaid", "--json"]) == 1  # previous -> latest
    payload = json.loads(capsys.readouterr().out)
    assert payload["versions"] == ["2020-09-14_1.0.0", "2020-09-14_1.1.0"]
    assert [(c["kind"], c["where"]) for c in payload["changes"]] == [("endpoint_removed", "GET /v1/charges/{charge}")]
    with pytest.raises(SystemExit):
        main(["diff", "stripe", "--old", names[0], "--new", names[1]])


def test_cli_diff_reads_plain_yaml_files(tmp_path, capsys):
    old, new = base_doc(), base_doc()
    new["paths"].pop("/v1/charges/{charge}")
    (tmp_path / "old.yml").write_bytes(as_yaml(old))
    (tmp_path / "new.yaml").write_bytes(as_yaml(new))
    assert main(["diff", "--old", str(tmp_path / "old.yml"), "--new", str(tmp_path / "new.yaml")]) == 1
    assert "GET /v1/charges/{charge} was removed" in capsys.readouterr().out
