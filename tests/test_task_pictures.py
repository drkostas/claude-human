"""Pictures for a task: stored by content, shrunk on macOS, handed back only by id."""
import base64
import shutil
import struct
import zlib

import pytest

from claude_human.tasks import pictures


def png(width: int, height: int) -> bytes:
    """A grey PNG of this size, written by hand so the tests need no imaging library."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    rows = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@pytest.fixture()
def store_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(pictures.ENV_FOLDER, str(tmp_path / "pics"))
    return tmp_path / "pics"


def test_a_picture_is_stored_by_its_content_and_read_back_by_id(tmp_path, store_dir):
    src = tmp_path / "front.png"
    src.write_bytes(png(40, 20))
    p = pictures.store(src)
    assert p["name"] == "front.png" and len(p["id"]) == 64
    assert pictures.store(src)["id"] == p["id"] and len(list(store_dir.iterdir())) == 1
    uri = pictures.data_uri(p["id"])
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == src.read_bytes()
    assert oct((store_dir / f"{p['id']}.png").stat().st_mode & 0o777) == "0o600"


def test_the_shrink_keeps_the_longest_side(tmp_path, store_dir):
    if shutil.which("sips") is None:
        pytest.skip("sips is macOS only")
    src = tmp_path / "wide.png"
    src.write_bytes(png(3200, 400))
    p = pictures.store(src, max_side=800)
    assert (p["width"], p["height"]) == (800, 100)
    assert pictures.data_uri(p["id"]).startswith("data:image/png;base64,")


@pytest.mark.parametrize("data, why", [
    (b"not a picture at all", "is not a PNG"),
    (b"%PDF-1.7\n", "is not a PNG"),
])
def test_what_is_not_a_picture_is_refused(tmp_path, store_dir, data, why):
    src = tmp_path / "x.png"            # the name says png, the bytes decide
    src.write_bytes(data)
    with pytest.raises(pictures.NotAPicture, match=why):
        pictures.store(src)
    with pytest.raises(pictures.NotAPicture, match="is not a file"):
        pictures.store(tmp_path / "missing.png")


def test_only_an_id_of_its_own_form_reads_a_file(tmp_path, store_dir):
    (tmp_path / "secret.png").write_bytes(png(2, 2))
    for bad in ("../secret", "../../etc/passwd", "a" * 63, "A" * 64, "", "0" * 64):
        assert pictures.data_uri(bad) is None


def test_paths_named_in_a_text_are_found_once_each(tmp_path):
    a, b = tmp_path / "card-v7.png", tmp_path / "plate.JPG"
    a.write_bytes(png(2, 2))
    b.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 20)
    (tmp_path / "fake.png").write_bytes(b"text")
    text = (f"The card is ready (picture at {a} and on the bench). Compare {b}, then {a} again. "
            f"Not these: {tmp_path / 'missing.png'}, {tmp_path / 'fake.png'}, http://example.org/x.png.")
    assert pictures.paths_in(text) == [str(a), str(b)]
    assert pictures.paths_in("") == [] and pictures.paths_in(None) == []
