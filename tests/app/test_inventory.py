"""The index as data: what the child exports, what the parent refuses, what is stored, and when it goes."""

import copy
import json
import logging
import sqlite3

import pytest
from conftest import SECRET, SHA, install_payload, old_doc
from indexing import TREES, release, save_release

from apidrift.app import db, indexcheck, inventory, releasepass
from apidrift.app.checker import run_local
from apidrift.app.poller import snapshots
from apidrift.app.webhooks import handle_event
from apidrift.cli import main
from apidrift.openapi import load_spec
from apidrift.scanner import ScanResult
from apidrift.sdkmap import SdkIndex


@pytest.fixture
def repo_row(conn):
    db.upsert_installation(conn, 77, "octo")
    db.upsert_repo(conn, 501, 77, "octo/shop", None)
    return 501


def check(data_dir, tree="plain"):
    old, new = snapshots(data_dir)[:2]
    return json.loads(json.dumps(run_local(TREES[tree], old.name, new.name).payload())), old, new


def record(conn, data_dir, payload, job_id=1, repo_id=501):
    old, new = snapshots(data_dir)[:2]
    return indexcheck.record(conn, [old, new], repo_id, job_id, SHA, payload, old, new)


# --- export and parse ------------------------------------------------------------------------------


@pytest.mark.parametrize("tree", sorted(TREES))
def test_what_the_child_exports_parses_back_to_the_same_call_sites(data_dir, tree):
    old, new = snapshots(data_dir)
    local = run_local(TREES[tree], old.name, new.name)
    index = inventory.parse(json.loads(json.dumps(local.payload()["index"])))
    assert [u.to_dict() for u in index.usages] == [u.to_dict() for u in local.result.usages]
    assert [u.hidden for u in index.usages] == [u.hidden for u in local.result.usages]
    for mine, real in zip(index.usages, local.result.usages):
        assert all(name in mine.reads for name in real.reads) and len(mine.reads) == len(real.reads)
        assert mine.hides("anything") == real.hides("anything")


def test_the_recorder_notes_every_question_and_flags_anything_else(data_dir):
    index = SdkIndex.build(load_spec(old_doc()))
    recorder = inventory.Recorder(index)
    assert recorder.resolve(["app", "billing", "stripe", "charges", "create"]).path == "/v1/charges"
    assert recorder.resolve(["stripe", "", "nothing", "here"]) is None and recorder.resolve(["alone"]) is None
    assert recorder.match_path("/v1/charges/ch_1") == "/v1/charges/{charge}" and recorder.methods_for("/v1/charges") == ["get", "post"]
    assert list(recorder.chains) == [("billing", "stripe", "charges", "create"), ("stripe", "nothing", "here")]
    assert (list(recorder.urls), list(recorder.paths), recorder.untracked, recorder.empty) == (["/v1/charges/ch_1"], ["/v1/charges"], set(), False)
    assert not inventory.export(ScanResult("r", [], 0, []), recorder)["unusable"]
    assert ("post", "/v1/charges") in recorder.operations  # reaches past the three recorded questions
    exported = inventory.export(ScanResult("r", [], 0, []), recorder)
    assert recorder.untracked == {"operations"} and exported["unusable"]
    with pytest.raises(inventory.Invalid, match="could not be recorded"):
        inventory.parse(exported)


def test_the_recorder_notes_the_go_questions_and_flags_any_other_way_into_the_go_names(data_dir):
    index = SdkIndex.build(load_spec(old_doc()))
    recorder = inventory.Recorder(index)
    go, op = recorder.go, ("post", "/v1/charges")
    assert go.resolve("package", "charge", "New").path == "/v1/charges" and go.resolve("api", "Nope", "New") is None
    assert go.owns("api", "Charges") and go.known("api", "Charges", "New") and not go.owns("service", "Nope")
    assert go.field(op, "", "Amount") == "amount" and go.field(op, "", "Nope") is None
    assert go.children(op, "shipping.") and not go.children(op, "amount.") and not go.path_param(op, "Charge")
    assert list(recorder.go_asked) == [
        ("resolve", ("package", "charge", "New")), ("resolve", ("api", "Nope", "New")), ("owns", ("api", "Charges")),
        ("known", ("api", "Charges", "New")), ("owns", ("service", "Nope")), ("field", (op, "", "Amount")),
        ("field", (op, "", "Nope")), ("children", (op, "shipping.")), ("children", (op, "amount.")), ("path_param", (op, "Charge")),
    ]
    exported = json.loads(json.dumps(inventory.export(ScanResult("r", [], 0, []), recorder)))
    assert not exported["unusable"] and inventory.parse(exported).go == tuple(recorder.go_asked)
    stored = json.loads(json.dumps(indexcheck.answers(index, (), (), (), recorder.go_asked)))
    assert [answer for _, answer in stored["go"]] == ["post /v1/charges", None, True, True, False, "amount", None, True, False, False]
    assert indexcheck.same_answers(index, stored)
    other = old_doc()
    other["paths"]["/v1/charges"]["post"]["requestBody"]["content"]["application/x-www-form-urlencoded"]["schema"]["properties"].pop("amount")
    assert not indexcheck.same_answers(SdkIndex.build(load_spec(other)), stored)  # Amount no longer names a request field
    for reach in (lambda: go._names(op, ""), lambda: go.field(op, prefix="", go_name="Amount"), lambda: go.owns("api")):
        fresh = inventory.Recorder(index)
        go = fresh.go
        try:
            reach()
        except TypeError:
            pass
        assert len(fresh.untracked) == 1 and inventory.export(ScanResult("r", [], 0, []), fresh)["unusable"]


def _site(export, **changes):
    export["sites"][0].update(changes)


BAD = {
    "another format": lambda e: e.update(format=3),
    "the format before this one": lambda e: e.update(format=1),
    "format true": lambda e: e.update(format=True),
    "unusable": lambda e: e.update(unusable=True),
    "sites not a list": lambda e: e.update(sites={}),
    "site not an object": lambda e: e["sites"].append("x"),
    "absolute file": lambda e: _site(e, file="/etc/passwd"),
    "file with dot-dot": lambda e: _site(e, file="../other/x.py"),
    "file with a newline": lambda e: _site(e, file="a\nb.py"),
    "file too long": lambda e: _site(e, file="a" * 1025),
    "line zero": lambda e: _site(e, line=0),
    "line true": lambda e: _site(e, line=True),
    "line text": lambda e: _site(e, line="6"),
    "method upper": lambda e: _site(e, method="POST"),
    "path with a query": lambda e: _site(e, path="/v1/charges?x=1"),
    "path not stripe": lambda e: _site(e, path="/etc/passwd"),
    "call with a newline": lambda e: _site(e, call="stripe.charges\n.create"),
    "call with an escape": lambda e: _site(e, call="\x1b[31mstripe.charges.create"),
    "call with a bidi override": lambda e: _site(e, call="stripe‮charges.create"),
    "call empty": lambda e: _site(e, call=""),
    "call too long": lambda e: _site(e, call="s" * 501),
    "language": lambda e: _site(e, language="Java Script"),
    "complete not boolean": lambda e: _site(e, complete=1),
    "fields not a list": lambda e: _site(e, fields="amount"),
    "field with a tab": lambda e: _site(e, fields=["a\tb"]),
    "field repeated": lambda e: _site(e, fields=["a", "a"]),
    "field not text": lambda e: _site(e, fields=[1]),
    "field too long": lambda e: _site(e, fields=["f" * 301]),
    "too many fields": lambda e: _site(e, fields=[f"f{i}" for i in range(401)]),
    "hidden not a list": lambda e: _site(e, hidden="x"),
    "reads not a list": lambda e: e.update(reads={}),
    "reads in clear": lambda e: e["reads"].__setitem__(0, ["receipt_url"]),
    "reads repeated": lambda e: e["reads"].__setitem__(0, ["0" * 12, "0" * 12]),
    "reads no call site uses": lambda e: e["reads"].append(["0" * 12]),
    "site reads out of range": lambda e: _site(e, reads=99),
    "site reads true": lambda e: _site(e, reads=True),
    "asked missing": lambda e: e.pop("asked"),
    "go questions missing": lambda e: e["asked"].pop("go"),
    "go question unknown": lambda e: e["asked"]["go"].append(["_aliases", []]),
    "go question with too few arguments": lambda e: e["asked"]["go"].append(["owns", ["api"]]),
    "go question without an operation": lambda e: e["asked"]["go"].append(["field", ["post", "", "Amount"]]),
    "go question with a path that is not stripe": lambda e: e["asked"]["go"].append(["children", [["post", "/etc"], ""]]),
    "go question with a wildcard method": lambda e: e["asked"]["go"].append(["children", [["?", "/v1/charges"], ""]]),
    "go question with a newline": lambda e: e["asked"]["go"].append(["owns", ["api", "a\nb"]]),
    "go question with a number": lambda e: e["asked"]["go"].append(["owns", ["api", 7]]),
    "chain of one": lambda e: e["asked"]["chains"].append(["stripe"]),
    "chain of five": lambda e: e["asked"]["chains"].append(["a", "b", "c", "d", "e"]),
    "chain with a newline": lambda e: e["asked"]["chains"].append(["stripe", "x\ny"]),
    "url with an escape": lambda e: e["asked"]["urls"].append("/v1/\x1b[0m"),
    "asked path not stripe": lambda e: e["asked"]["paths"].append("/v9/charges"),
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_an_export_that_breaks_a_rule_is_refused_whole(data_dir, conn, repo_row, case):
    payload, old, new = check(data_dir)
    assert record(conn, data_dir, payload) is None  # an index from an earlier check is in place
    BAD[case](payload["index"])
    with pytest.raises(inventory.Invalid) as caught:
        inventory.parse(copy.deepcopy(payload["index"]))
    assert all(ch.isprintable() for ch in str(caught.value)) and "‮" not in str(caught.value)
    assert record(conn, data_dir, payload, job_id=2) == str(caught.value)  # nothing is repaired, cut to fit, or kept
    assert inventory.get(conn, 501) is None and conn.execute("SELECT count(*) FROM index_sites").fetchone()[0] == 0


@pytest.mark.parametrize("cap,value", [("MAX_SITES", 5), ("MAX_CHARS", 60), ("MAX_ASKED", 3), ("MAX_READS", 0)])
def test_an_export_over_a_cap_is_refused_not_truncated(data_dir, conn, repo_row, monkeypatch, cap, value):
    payload, old, new = check(data_dir)
    inventory.parse(payload["index"])
    monkeypatch.setattr(inventory, cap, value)
    with pytest.raises(inventory.Invalid):
        inventory.parse(payload["index"])
    assert record(conn, data_dir, payload) is not None and inventory.get(conn, 501) is None


# --- the gate ----------------------------------------------------------------------------------------


def test_an_index_naming_an_operation_the_specs_do_not_have_is_refused(data_dir, conn, repo_row):
    payload, old, new = check(data_dir)
    payload["index"]["sites"][1]["path"] = "/v1/secret_things"
    assert record(conn, data_dir, payload) == "a call site names an operation the specs do not have"
    assert inventory.get(conn, 501) is None


def _drop_a_field(payload):
    payload["index"]["sites"][0]["fields"].remove("description")


def _hide_fields(payload):
    payload["index"]["sites"][0].update(complete=False, hidden=[""])


def _drop_an_unaffected_site(payload):
    payload["index"]["sites"].pop(1)


def _other_impacts(payload):
    payload["impacts"].pop()


@pytest.mark.parametrize("tamper", [_drop_a_field, _hide_fields, _drop_an_unaffected_site, _other_impacts])
def test_an_index_that_does_not_reproduce_its_own_check_is_refused_and_the_old_one_dropped(data_dir, conn, repo_row, caplog, tamper):
    payload, old, new = check(data_dir)
    assert record(conn, data_dir, payload) is None and inventory.get(conn, 501) is not None
    tamper(payload)
    with caplog.at_level(logging.WARNING, logger="apidrift.index"):
        assert record(conn, data_dir, payload, job_id=2) == "the index does not reproduce the impacts of its own check"
    assert inventory.get(conn, 501) is None
    assert [r.message for r in caplog.records] == ["repo 501: no index kept (the index does not reproduce the impacts of its own check)"]


@pytest.mark.parametrize("index", [None, "x", [], {"format": 1}, {"format": 1, "sites": [], "reads": {}, "asked": None}])
def test_a_payload_without_a_usable_index_never_fails_the_check(data_dir, conn, repo_row, index):
    payload, old, new = check(data_dir)
    payload["index"] = index
    assert isinstance(record(conn, data_dir, payload), str) and inventory.get(conn, 501) is None
    payload.pop("index")
    payload["unread"] = {"Go\n": "many"}
    assert isinstance(record(conn, data_dir, payload), str)


def test_an_unread_note_must_be_small_counts(data_dir, conn, repo_row):
    payload, old, new = check(data_dir, "docs")
    assert payload["unread"] == {"Java": 1} and record(conn, data_dir, payload) is None
    assert inventory.load(conn, inventory.get(conn, 501)).unread == {"Java": 1}
    for bad in ({"Java": True}, {"Java": -1}, {"J\x1b": 1}, {f"L{i}": 1 for i in range(51)}):
        payload["unread"] = bad
        assert record(conn, data_dir, payload) is not None


# --- storage -------------------------------------------------------------------------------------------


def test_storage_is_one_bounded_set_of_rows_per_repo_replaced_by_each_check(data_dir, conn, repo_row):
    payload, old, new = check(data_dir, "docs")
    assert record(conn, data_dir, payload) is None
    assert conn.execute("SELECT count(*) FROM index_sites").fetchone()[0] == 11
    payload, old, new = check(data_dir)
    assert record(conn, data_dir, payload, job_id=2) is None
    assert conn.execute("SELECT count(*) FROM index_sites").fetchone()[0] == 6
    header = inventory.get(conn, 501)
    assert (header["sha"], header["job_id"], header["format"], header["checked"], header["checked_by"]) == (SHA, 2, 2, new.name, "full")
    assert header["scanner"] == inventory.scanner_digest() and len(header["scanner"]) == 16
    assert inventory.summary(conn) == {"repos": 1, "by_index": 0, "call_sites": 6, "doc_sites": 0, "endpoints": 2}
    assert "receipt_url" not in header["extras"] and all(set(text) <= set("0123456789abcdef") for text in json.loads(header["extras"])["reads"])


def test_the_index_goes_with_the_repo_and_with_the_installation(data_dir, conn, settings, fake):
    handle_event(conn, "installation", "d-1", install_payload(fake), settings.data)
    added = {"action": "added", "installation": {"id": 77}, "repositories_added": [{"id": 502, "full_name": "octo/other"}]}
    handle_event(conn, "installation_repositories", "d-2", added, settings.data)
    payload, old, new = check(data_dir)
    assert record(conn, data_dir, payload) is None and record(conn, data_dir, payload, repo_id=502) is None
    assert conn.execute("SELECT count(*) FROM index_sites").fetchone()[0] == 12
    removed = {"action": "removed", "installation": {"id": 77}, "repositories_removed": [{"id": 502, "full_name": "octo/other"}]}
    handle_event(conn, "installation_repositories", "d-3", removed, settings.data)
    assert inventory.get(conn, 502) is None and conn.execute("SELECT count(*) FROM index_sites").fetchone()[0] == 6
    handle_event(conn, "installation", "d-4", {"action": "deleted", "installation": {"id": 77}}, settings.data)
    assert conn.execute("SELECT count(*) FROM repo_index").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM index_sites").fetchone()[0] == 0
    assert record(conn, data_dir, payload) == "IntegrityError"  # a check that finishes after the uninstall stores nothing


def test_a_later_finished_check_that_left_the_index_alone_supersedes_it(data_dir, conn, repo_row):
    payload, old, new = check(data_dir)
    first = db.enqueue(conn, "check", 501, "install")
    db.claim_job(conn)
    assert record(conn, data_dir, payload, job_id=first) is None
    db.finish_job(conn, first, "done")
    assert not inventory.later_job(conn, 501, first) and inventory.drop_superseded(conn) == 0
    second = db.enqueue(conn, "check", 501, "push")
    assert not inventory.later_job(conn, 501, first)  # queued: it will run, and the release pass waits for it
    db.claim_job(conn)
    assert inventory.later_job(conn, 501, first) and inventory.drop_superseded(conn) == 0  # running
    db.finish_job(conn, second, "done")  # e.g. "skipped: installation suspended": done without scanning
    assert inventory.drop_superseded(conn) == 1 and inventory.get(conn, 501) is None


# --- migration ---------------------------------------------------------------------------------------------


def test_a_database_with_rows_migrates_in_place_and_has_no_index_until_a_check_writes_one(settings):
    """The live database: version 5, real rows. The migration only adds two empty tables."""
    old = db.connect(settings.db_path)
    with db.write(old):
        for script in db.MIGRATIONS[:5]:
            db._run_script(old, script)
        old.execute("PRAGMA user_version = 5")
    db.upsert_installation(old, 77, "octo")
    db.upsert_repo(old, 501, 77, "octo/shop", snapshots(settings.data)[0].name)
    db.update_repo(old, 501, last_sha=SHA, last_result="findings", issue_number=1, issue_state="open", issue_fingerprint="f" * 64,
                   last_checked_at="2026-02-15T00:00:00Z", pr_number=2, fix_state="open")
    done = db.enqueue(old, "check", 501, "install")
    db.finish_job(old, db.claim_job(old)["id"], "done")
    before = (dict(db.get_repo(old, 501)), dict(db.get_job(old, done)), dict(db.get_installation(old, 77)))
    old.close()
    conn = db.connect(settings.db_path)
    assert db.migrate(conn) == len(db.MIGRATIONS) == 6 and db.migrate(conn) == 6
    assert (dict(db.get_repo(conn, 501)), dict(db.get_job(conn, done)), dict(db.get_installation(conn, 77))) == before
    assert inventory.get(conn, 501) is None and inventory.summary(conn)["repos"] == 0
    assert db.queued_jobs(conn) == []  # no backfill at migration time
    save_release(release("unrelated"))
    counts = releasepass.run(settings, conn)  # the first release after the deploy: today's path, which writes the index
    assert counts == {"same": 0, "differ": 0, "none": 1, "queued": 0} and [j["reason"] for j in db.queued_jobs(conn)] == ["release"]
    conn.close()


def test_index_tables_enforce_their_foreign_keys(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO repo_index (repo_id, sha, format, scanner, extras, written_at) VALUES (9, 'a', 1, 's', '{}', 'now')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO index_sites VALUES (9, 0, 'f', 1, 'get', '/v1/x', 'c', 'python', 1, '[]', NULL)")


# --- the scanner digest and the helper process ---------------------------------------------------------------


def test_the_scanner_digest_follows_the_modules_that_shape_a_scan(tmp_path, monkeypatch):
    package = tmp_path / "apidrift"
    (package / "app").mkdir(parents=True)
    for name in ("scanner.py", "jsparse.py", "impact.py", "fixer.py", "cli.py", "app/checkout.py", "app/inventory.py"):
        (package / name).write_text("x = 1\n")
    monkeypatch.setattr(inventory, "__file__", str(package / "app" / "inventory.py"))

    def digest():
        monkeypatch.setattr(inventory, "_digest", None)
        return inventory.scanner_digest()

    seen = [digest()]
    assert len(seen[0]) == 16 and digest() == seen[0]
    for name in ("impact.py", "fixer.py", "cli.py"):  # these assess, fix or print; they do not find call sites
        (package / name).write_text("x = 2\n")
    assert digest() == seen[0]
    for name in ("jsparse.py", "app/checkout.py", "app/inventory.py", "phpparse.py"):  # the last: a parser added later
        (package / name).write_text("x = 3\n")
        seen.append(digest())
    assert len(set(seen)) == 5
    (package / "scanner.py").unlink()  # sources that cannot be read give a digest that matches nothing
    assert digest() == ""
    (package / "app" / "checkout.py").unlink()
    assert digest() == ""


def test_a_long_chain_of_specs_is_built_in_a_helper_process_with_the_same_result(data_dir, monkeypatch):
    third = save_release(release("currency"))
    snaps = snapshots(data_dir)
    near = indexcheck.build([str(p) for p in snaps])
    monkeypatch.setattr(indexcheck, "ISOLATE_ABOVE", -1)
    monkeypatch.setattr(indexcheck, "_pairs", {})
    far = indexcheck.pair(snaps, snaps[0], third)
    assert far is not near and (far.old_version, far.new_version, far.versions) == (near.old_version, near.new_version, near.versions)
    assert [c.to_dict() for c in far.changes] == [c.to_dict() for c in near.changes] and far.index.operations == near.index.operations
    asked = ([["stripe", "Charge", "create"], ["x", "y"]], ["/v1/charges/ch_1"], ["/v1/charges"])
    assert indexcheck.answers(far.index, *asked) == indexcheck.answers(near.index, *asked)
    assert indexcheck.pair(snaps, snaps[0], third) is far  # cached: one helper per chain


def test_a_helper_that_fails_is_an_error_and_is_not_retried_at_once(data_dir, monkeypatch):
    snaps = snapshots(data_dir)
    monkeypatch.setattr(indexcheck, "ISOLATE_ABOVE", -1)
    monkeypatch.setattr(indexcheck, "_pairs", {})
    monkeypatch.setattr(indexcheck, "_HELPER", "raise SystemExit(9)")
    with pytest.raises(RuntimeError, match="exit 9"):
        indexcheck.pair(snaps, snaps[0], snaps[1])
    monkeypatch.setattr(indexcheck.subprocess, "run", lambda *a, **k: pytest.fail("the helper was started again"))
    with pytest.raises(RuntimeError, match="a moment ago"):
        indexcheck.pair(snaps, snaps[0], snaps[1])


# --- the operator's view ----------------------------------------------------------------------------------------


@pytest.fixture
def configured(monkeypatch, settings):
    monkeypatch.setenv("APIDRIFT_GITHUB_APP_ID", settings.github_app_id)
    monkeypatch.setenv("APIDRIFT_GITHUB_APP_SLUG", settings.github_app_slug)
    monkeypatch.setenv("APIDRIFT_GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("APIDRIFT_GITHUB_PRIVATE_KEY_PATH", str(settings.github_private_key_path))
    monkeypatch.setenv("APIDRIFT_HOST", settings.host)
    monkeypatch.chdir(settings.data.parent)
    return settings


def test_the_index_command_prints_one_repo_grouped_by_endpoint_and_status_counts_it(configured, capsys):
    settings = configured
    conn = db.connect(settings.db_path)
    db.migrate(conn)
    db.upsert_installation(conn, 77, "octo")
    db.upsert_repo(conn, 501, 77, "octo/shop", None)
    db.upsert_repo(conn, 502, 77, "octo/unchecked", None)
    payload, old, new = check(settings.data, "docs")
    assert record(conn, settings.data, payload, job_id=7) is None
    conn.close()
    assert main(["index", "Octo/Shop"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("octo/shop at 1a57c3b2f5aa: 4 Stripe call sites on 2 endpoints\n")
    assert f"by job 7; last checked against {new.name} (by a full check, " in out
    assert "\nPOST /v1/charges  (3)\n  app.py:8  stripe.Charge.create  [python]  fields: amount, currency, description\n" in out
    assert "  scripts/charge.sh:3  api.stripe.com/v1/charges  [http]  fields: (+ fields not visible)\n" in out
    assert "\nPOST /v1/charges/{charge}/capture  (1)\n  app.py:13  api.stripe.com/v1/charges/{charge_id}/capture  [http]" in out
    assert "\ndocumentation, fixtures and tests (not counted): 7\n  README.md:5  ? /v1/charges  api.stripe.com/v1/charges\n" in out
    assert main(["index", "octo/unchecked"]) == 1 and "has no index yet" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="not an installed repository"):
        main(["index", "octo/nope"])
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "index: 1 repos indexed, 4 call sites on 2 endpoints (7 more in documentation and tests); 0 last checked from the index\n" in out
    assert out.rstrip().endswith("last release pass: never")
    conn = db.connect(settings.db_path)
    save_release(release("unrelated"))
    db.update_repo(conn, 501, last_sha=SHA, last_result="clean", baseline=old.name)
    db.update_repo(conn, 502, gone=1)
    assert releasepass.run(settings, conn)["differ"] == 1  # the index has findings the "clean" row does not
    conn.close()
    assert main(["status"]) == 0
    assert "last release pass: 2026-03-01.test at 2" in (out := capsys.readouterr().out)
    assert "1 repos checked from the index in " in out and " ms, 1 needed a full check, 0 had no usable index, 0 already had a check queued" in out
