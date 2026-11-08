"""A cross-file cap must show in the scan's output: past one, a language's wrappers are not followed, which
otherwise reads exactly like a checkout with no wrappers."""

import json
import re

import test_crossfile_js as crossjs
import test_crossfile_py as crosspy
from test_fixer import docs

from apidrift import crosstable
from apidrift.impact import assess, render_impact_json, render_impact_text
from apidrift.openapi import load_spec
from apidrift.scanner import render_scan_json, render_scan_text, scan_repo
from apidrift.sdkmap import SdkIndex


def scan(root, files):
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return scan_repo(root, SdkIndex.build(*(load_spec(d) for d in docs())))


def test_a_scan_under_the_caps_carries_no_note_and_follows_the_wrapper(tmp_path):
    result = scan(tmp_path, crosspy.FILES)
    assert result.notes == [] and any("[through " in u.call for u in result.usages)
    assert json.loads(render_scan_json(result))["notes"] == []


def test_the_candidate_cap_is_named_in_the_scan_text_and_json(tmp_path, monkeypatch):
    monkeypatch.setattr(crosstable, "MAX_CANDIDATES", 0)
    result = scan(tmp_path, crosspy.FILES)
    assert not any("[through " in u.call for u in result.usages)  # the per-file scan: the caller is not a site
    assert len(result.notes) == 1
    assert re.fullmatch(r"cross-file: \d+ wrappers? not followed \(cap\), e\.g\. \S+\.py:\d+ \w+", result.notes[0]), result.notes
    assert result.notes[0] in render_scan_text(result).splitlines()
    assert json.loads(render_scan_json(result))["notes"] == result.notes


def test_the_parse_cap_reaches_the_impact_payload_the_child_prints(tmp_path, monkeypatch):
    monkeypatch.setattr(crosstable, "MAX_PARSE_FILES", 0)
    result = scan(tmp_path, crossjs.FILES)
    assert result.notes == [
        "cross-file: 1 wrapper not followed (cap: too many files name it), e.g. src/stripe/payment.service.ts:5 createIntent"
    ]
    payload = json.loads(render_impact_json(result, assess(result.usages, []), "old", "new"))
    assert payload["notes"] == result.notes and "notes" not in json.dumps(payload.get("index"))
    assert result.notes[0] in render_impact_text(result, [], "old", "new").splitlines()


def test_notes_about_one_candidate_are_not_surfaced(tmp_path):
    """A name too common or defined twice describes that candidate, not a limit of the scan."""
    service = crosspy.FILES[crosspy.WRAPPER[0]]
    twice = {**crosspy.FILES, "server/other.py": service}  # the wrapper is defined in two files
    result = scan(tmp_path, twice)
    assert any("several wrappers of this name" in n for n in result.table.notes) and result.notes == []
