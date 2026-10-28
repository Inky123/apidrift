import io
import tarfile

import pytest

from apidrift.app.checkout import ExtractError, extract_tarball

GOOD = [("top", "dir", None), ("top/a.py", "file", "import stripe\n"), ("top/sub/b.js", "file", "x" * 40)]


def make_tar(path, entries):
    with tarfile.open(path, "w:gz") as tar:
        for name, kind, payload in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                data = payload.encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                continue
            info.type = {"dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE, "lnk": tarfile.LNKTYPE, "fifo": tarfile.FIFOTYPE}[kind]
            if payload:
                info.linkname = payload
            tar.addfile(info)
    return path


def test_good_archive_returns_the_top_directory(tmp_path):
    top = extract_tarball(make_tar(tmp_path / "src.tgz", GOOD), tmp_path / "tree")
    assert top == tmp_path / "tree" / "top"
    assert (top / "a.py").read_text() == "import stripe\n" and (top / "sub" / "b.js").stat().st_size == 40


@pytest.mark.parametrize(
    "entry",
    [
        ("../x", "file", "evil"),
        ("/etc/x", "file", "evil"),
        ("top/../../x", "file", "evil"),
        ("top/fifo", "fifo", None),
    ],
)
def test_escaping_files_and_special_entries_are_rejected(tmp_path, entry):
    src = make_tar(tmp_path / "src.tgz", GOOD + [entry])
    with pytest.raises(ExtractError) as caught:
        extract_tarball(src, tmp_path / "tree")
    assert not (tmp_path / "x").exists() and not (tmp_path / "outside").exists()
    assert "etc" not in str(caught.value) and "outside" not in str(caught.value)  # no entry names in the error


@pytest.mark.parametrize(
    "entry",
    [
        ("top/link", "sym", "../../etc/passwd"),
        ("top/link", "sym", "/etc/passwd"),
        ("top/hard", "lnk", "../../outside"),
        ("top/hard", "lnk", "/etc/passwd"),
    ],
)
def test_a_link_pointing_outside_is_skipped_and_the_rest_extracted(tmp_path, entry):
    """Repos do ship stray symlinks; one must not fail the whole check. The link itself is never created."""
    src = make_tar(tmp_path / "src.tgz", GOOD + [entry])
    top = extract_tarball(src, tmp_path / "tree")
    assert (top / "a.py").read_text() == "import stripe\n" and (top / "sub" / "b.js").exists()
    assert not (top / entry[0].split("/", 1)[1]).exists() and not (top / entry[0].split("/", 1)[1]).is_symlink()
    assert not (tmp_path / "outside").exists()


def test_a_link_pointing_inside_is_kept(tmp_path):
    src = make_tar(tmp_path / "src.tgz", GOOD + [("top/alias.py", "sym", "a.py")])
    top = extract_tarball(src, tmp_path / "tree")
    assert (top / "alias.py").is_symlink() and (top / "alias.py").read_text() == "import stripe\n"


def test_entry_cap(tmp_path):
    with pytest.raises(ExtractError, match="entries"):
        extract_tarball(make_tar(tmp_path / "src.tgz", GOOD), tmp_path / "tree", max_entries=2)


def test_byte_cap(tmp_path):
    with pytest.raises(ExtractError, match="bytes"):
        extract_tarball(make_tar(tmp_path / "src.tgz", GOOD), tmp_path / "tree", max_bytes=50)


def test_archive_must_have_a_single_top_directory(tmp_path):
    two = GOOD + [("other", "dir", None), ("other/c.rb", "file", "y")]
    with pytest.raises(ExtractError, match="single top-level"):
        extract_tarball(make_tar(tmp_path / "src.tgz", two), tmp_path / "tree")


def test_garbage_is_rejected(tmp_path):
    src = tmp_path / "src.tgz"
    src.write_bytes(b"this is not a tarball")
    with pytest.raises(ExtractError, match="could not be read"):
        extract_tarball(src, tmp_path / "tree")
