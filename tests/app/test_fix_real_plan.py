"""The engine's real plan (``plan_fixes().to_dict()``) for every non-rename action, through the parent's checks
and the pull request body; and wording changes alone never rebuild an open pull request."""

from __future__ import annotations

import shutil

from conftest import SHA
from test_fix import charges_migration, claim, scan  # noqa: F401  (fixtures)
from test_fixer_v2 import V2, plan_v2

from apidrift.app import db, fixcheck, fixpr, prbody
from apidrift.app.checker import run_job

ENDIVE = "2026-09-30.endive"
FILES = ("delete.js", "rename_first.py", "wrap_type.js", "wrap_type.rb")


class Meta:
    """GitHub's content lookup, answering with the blob each fix was planned from."""

    def __init__(self, fixes: list[dict]) -> None:
        self.shas = {f["file"]: f["blob_sha"] for f in fixes}

    def get_content_meta(self, token, full_name, file, sha):
        return {"type": "file", "sha": self.shas[file]}


def real_plan(tmp_path, files=FILES) -> dict:
    root = tmp_path / "repo"
    root.mkdir()
    for name in files:
        shutil.copy(V2 / name, root / name)
    return plan_v2(root).to_dict()


def test_every_real_site_passes_the_parent_unchanged(tmp_path):
    plan = real_plan(tmp_path)
    accepted = fixcheck.validate(Meta(plan["fixes"]), "t", "o/r", SHA, plan["fixes"])
    assert [f["file"] for f in accepted] == [f["file"] for f in plan["fixes"]] and len(accepted) == len(FILES)
    assert [f["sites"] for f in accepted] == [f["sites"] for f in plan["fixes"]]  # the engine's sites match our table
    assert {s["action"] for f in accepted for s in f["sites"]} == {"delete", "rename_first", "wrap_type"}


def test_a_real_mixed_plan_renders_every_action(tmp_path):
    plan = real_plan(tmp_path)
    accepted = fixcheck.validate(Meta(plan["fixes"]), "t", "o/r", SHA, plan["fixes"])
    plan["unfixable"] += [{"file": "x.py", "line": 2, "why": "unsupported_value"}, {"file": "y.py", "line": 9, "why": "@octocat"}]
    plan["pins"] = [{"file": "pin.py", "line": 1, "version": "2023-10-16"}]
    title, body = prbody.render_pr(plan, accepted, full_name="o/r", sha=SHA, version=ENDIVE, issue_number=1, fingerprint="f" * 64)
    sites = sum(len(f["sites"]) for f in accepted)
    assert title == f"Stripe {ENDIVE}: 3 migrations ({sites} call sites)"
    assert fixpr.commit_message(ENDIVE, accepted) == f"Stripe {ENDIVE}: 3 migrations"
    for heading in (
        "**remove `payto` from POST `/v1/payment_methods/{payment_method}`**",
        "**`filters.countries` → `filters.country` on POST `/v1/financial_connections/sessions`**",
        "**`billing_cycle_anchor` becomes an object with `type` on POST `/v1/subscriptions/{subscription}/resume`**",
        "**`subscription_details.billing_cycle_anchor` becomes an object with `type` on POST `/v1/invoices/create_preview`**",
    ):
        assert heading in body, heading
    assert "rejects the new shape." in body and "**Merge together with your upgrade**" in body
    assert "`{type: 'timestamp', timestamp: …}`" in body
    possibly = [p for p in plan["possibly_remaining"] if any(f["file"] == p["file"] for f in accepted)]
    assert possibly  # delete.js sends payto at a call whose arguments are not fully visible
    verified = sites - len(possibly)
    assert f"{verified} call sites now send what Stripe {ENDIVE} expects ({len(possibly)} may still send it" in body
    assert "- `payto` possibly still sent through a spread or a variable apidrift cannot see: " in body
    assert prbody.UNFIXABLE_WHY["unsupported_value"] in body and "@octocat" not in body


def test_a_real_deletes_only_plan_can_merge_any_time(tmp_path):
    plan = real_plan(tmp_path, ("delete.js",))
    accepted = fixcheck.validate(Meta(plan["fixes"]), "t", "o/r", SHA, plan["fixes"])
    title, body = prbody.render_pr(plan, accepted, full_name="o/r", sha=SHA, version=ENDIVE, issue_number=1, fingerprint="f" * 64)
    assert title.startswith(f"Stripe {ENDIVE}: remove payto (")
    assert "Removing these fields is safe on your current API version too, so this can merge any time." in body
    assert "**API-version pins found**" in body and "Merge together" not in body


def test_commit_message_and_body_changes_alone_never_rebuild(scan, installed, conn, gh, settings, monkeypatch):
    installed.grant_writes()
    run_job(claim(conn), conn, gh, settings)
    writes = len(installed.writes)
    monkeypatch.setattr(fixpr, "commit_message", lambda version, fixes: "totally different wording")
    real = prbody.render_pr
    monkeypatch.setattr(prbody, "render_pr", lambda *a, **k: ("other title", real(*a, **k)[1] + "x"))
    db.enqueue(conn, "check", 501, "push")
    assert run_job(claim(conn), conn, gh, settings).endswith("pr: #2 unchanged")
    assert len(installed.writes) == writes
