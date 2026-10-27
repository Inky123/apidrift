import json
from datetime import datetime, timezone

import pytest

from apidrift import snapshot as snap
from apidrift.providers import PROVIDERS
from specs import base_doc

STRIPE = PROVIDERS["stripe"]


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("APIDRIFT_DATA", str(tmp_path))
    return tmp_path


def raw(version="2026-01-01.test", extra=None):
    doc = base_doc()
    doc["info"]["version"] = version
    if extra:
        doc["paths"]["/v1/extra"] = extra
    return json.dumps(doc).encode()


def test_save_names_snapshot_by_time_version_and_hash():
    path = snap.save(STRIPE, raw(), now=datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc))
    assert path.name == f"2026-10-26T093000Z_2026-01-01-test_{snap.digest(raw())[:12]}.json.gz"
    assert snap.load(path) == json.loads(raw())


def test_identical_content_is_not_stored_twice():
    assert snap.save(STRIPE, raw()) is not None
    assert snap.save(STRIPE, raw()) is None
    assert len(snap.list_snapshots("stripe")) == 1


def test_resolve_latest_previous_prefix_and_file(tmp_path):
    first = snap.save(STRIPE, raw(), now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    second = snap.save(STRIPE, raw(extra={"get": {}}), now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert snap.resolve("stripe", "latest") == second
    assert snap.resolve("stripe", "previous") == first
    assert snap.resolve("stripe", "2026-01-01") == first
    plain = tmp_path / "spec.json"
    plain.write_bytes(raw())
    assert snap.resolve(None, str(plain)) == plain


def test_save_is_atomic_so_a_full_disk_leaves_no_truncated_snapshot(monkeypatch):
    real_open = snap.gzip.open

    class Truncating:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()

        def write(self, data):
            self.fh.write(data[:10])
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(snap.gzip, "open", lambda path, mode: Truncating(real_open(path, mode)))
    with pytest.raises(OSError):
        snap.save(STRIPE, raw())
    folder = snap.snapshot_dir("stripe")
    assert list(folder.iterdir()) == [] and snap.list_snapshots("stripe") == []
    monkeypatch.setattr(snap.gzip, "open", real_open)
    path = snap.save(STRIPE, raw())
    assert path is not None and [p.name for p in folder.iterdir()] == [path.name]


def test_resolve_rejects_unknown_and_ambiguous_refs():
    snap.save(STRIPE, raw(), now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    snap.save(STRIPE, raw(extra={"get": {}}), now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    with pytest.raises(SystemExit):
        snap.resolve("stripe", "nope")
    with pytest.raises(SystemExit):
        snap.resolve("stripe", "2026-01")
