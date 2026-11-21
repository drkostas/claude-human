import base64
import hashlib
import json
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import HTTP

import pytest

from claude_human import expo_updates as eu

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl command")

BASE = "https://updates.example.org/updates"


def make_key(d, name="key"):
    key = d / f"{name}.pem"
    cert = d / f"{name}.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "30", "-subj", "/CN=test updates"], check=True, capture_output=True)
    return key.read_text(), cert.read_text()


def make_export(d):
    dist = d / "dist"
    (dist / "_expo/static/js/android").mkdir(parents=True)
    (dist / "assets").mkdir()
    (dist / "_expo/static/js/android/index-1.hbc").write_bytes(b"the bundle")
    (dist / "assets/aaa").write_bytes(b"a picture")
    (dist / "assets/bbb").write_bytes(b"a font")
    (dist / "metadata.json").write_text(json.dumps({"version": 0, "bundler": "metro", "fileMetadata": {"android": {
        "bundle": "_expo/static/js/android/index-1.hbc",
        "assets": [{"path": "assets/aaa", "ext": "png"}, {"path": "assets/bbb", "ext": "ttf"}]}}}))
    return dist


def parts(status, headers, body):
    assert status == 200
    raw = b"content-type: " + headers["content-type"].encode() + b"\r\n\r\n" + body
    msg = BytesParser(policy=HTTP).parsebytes(raw)
    return list(msg.iter_parts())


def test_manifest_names_every_file_by_its_hash(tmp_path):
    dist = make_export(tmp_path)
    at = datetime(2030, 1, 2, 3, 4, 5, 678000, tzinfo=timezone.utc)
    m, files = eu.build_manifest(dist, platform="android", runtime_version="abc123", base_url=BASE + "/",
                                 expo_client={"name": "app"}, created_at=at)
    h = eu.asset_hash(b"the bundle")
    assert h == base64.urlsafe_b64encode(hashlib.sha256(b"the bundle").digest()).rstrip(b"=").decode()
    assert m["launchAsset"] == {"hash": h, "key": hashlib.md5(b"the bundle").hexdigest(),
                                "contentType": "application/javascript", "url": f"{BASE}/assets/{h}"}
    assert [a["fileExtension"] for a in m["assets"]] == [".png", ".ttf"]
    assert m["assets"][0]["contentType"] == "image/png"
    assert m["runtimeVersion"] == "abc123" and m["createdAt"] == "2030-01-02T03:04:05.678Z"
    assert m["extra"] == {"expoClient": {"name": "app"}} and m["metadata"] == {}
    assert str(uuid.UUID(m["id"])) == m["id"]
    assert len(files) == 3
    again, _ = eu.build_manifest(dist, platform="android", runtime_version="abc123", base_url=BASE, expo_client={"name": "app"})
    other, _ = eu.build_manifest(dist, platform="android", runtime_version="def456", base_url=BASE, expo_client={"name": "app"})
    assert again["id"] == m["id"] and other["id"] != m["id"]


def test_manifest_refuses_a_path_out_of_the_export_and_a_missing_platform(tmp_path):
    dist = make_export(tmp_path)
    meta = json.loads((dist / "metadata.json").read_text())
    with pytest.raises(ValueError, match="no ios files"):
        eu.build_manifest(dist, platform="ios", runtime_version="r", base_url=BASE)
    (tmp_path / "outside").write_bytes(b"x")
    meta["fileMetadata"]["android"]["assets"].append({"path": "../outside", "ext": "png"})
    (dist / "metadata.json").write_text(json.dumps(meta))
    with pytest.raises(ValueError, match="leaves the export"):
        eu.build_manifest(dist, platform="android", runtime_version="r", base_url=BASE)
    with pytest.raises(ValueError, match="plain name"):
        eu.build_manifest(dist, platform="android", runtime_version="../r", base_url=BASE)


def test_a_signed_update_is_answered_from_stored_bytes_with_a_signature_the_certificate_verifies(tmp_path):
    key, cert = make_key(tmp_path)
    dist, store = make_export(tmp_path), tmp_path / "store"
    up = eu.publish(dist, store, platform="android", runtime_version="r1", base_url=BASE, sign=eu.openssl_signer(key),
                    certificate=cert, expo_client={"name": "app"})
    assert up["signed"] and up["assets"] == 2
    assert eu.latest(store, "android", "r1") == up["id"]
    stored = (store / "android" / "r1" / up["id"] / "manifest.json").read_bytes()
    status, headers, body = eu.manifest_response(store, platform="android", runtime_version="r1")
    assert headers["expo-protocol-version"] == "1" and headers["expo-sfv-version"] == "0"
    assert headers["cache-control"] == "private, max-age=0"
    (part,) = parts(status, headers, body)
    assert part["content-disposition"] == 'form-data; name="manifest"'
    assert part.get_content_type() == "application/json"
    assert part.get_payload(decode=True) == stored
    value = part["expo-signature"]
    assert value.startswith('sig="') and value.endswith('", keyid="main"')
    sig = value[len('sig="'):-len('", keyid="main"')]
    assert eu.verify(stored, sig, cert)
    assert not eu.verify(stored + b" ", sig, cert)
    _, other_cert = make_key(tmp_path, "other")
    assert not eu.verify(stored, sig, other_cert)
    # the answer is the same bytes each time: nothing in it is made per request but the boundary
    _, _, body2 = eu.manifest_response(store, platform="android", runtime_version="r1", boundary="b0")
    assert stored in body2


def test_a_signer_the_certificate_would_refuse_stores_nothing(tmp_path):
    key, _ = make_key(tmp_path)
    _, cert = make_key(tmp_path, "other")
    store = tmp_path / "store"
    with pytest.raises(ValueError, match="would refuse it"):
        eu.publish(make_export(tmp_path), store, platform="android", runtime_version="r1", base_url=BASE,
                   sign=eu.openssl_signer(key), certificate=cert)
    assert eu.latest(store, "android", "r1") is None


def test_an_unsigned_update_carries_no_signature_and_a_newer_one_replaces_it(tmp_path):
    key, cert = make_key(tmp_path)
    dist, store = make_export(tmp_path), tmp_path / "store"
    plain = eu.publish(dist, store, platform="android", runtime_version="r1", base_url=BASE)
    (part,) = parts(*eu.manifest_response(store, platform="android", runtime_version="r1"))
    assert not plain["signed"] and part["expo-signature"] is None
    (dist / "_expo/static/js/android/index-1.hbc").write_bytes(b"a newer bundle")
    (dist / "metadata.json").write_text((dist / "metadata.json").read_text() + " ")
    newer = eu.publish(dist, store, platform="android", runtime_version="r1", base_url=BASE,
                       sign=eu.openssl_signer(key), certificate=cert)
    assert newer["id"] != plain["id"] and eu.latest(store, "android", "r1") == newer["id"]
    (part,) = parts(*eu.manifest_response(store, platform="android", runtime_version="r1"))
    assert json.loads(part.get_payload(decode=True))["launchAsset"]["hash"] == eu.asset_hash(b"a newer bundle")
    assert part["expo-signature"]


def test_requests_for_nothing_stored_or_not_plain_are_refused(tmp_path):
    store = tmp_path / "store"
    eu.publish(make_export(tmp_path), store, platform="android", runtime_version="r1", base_url=BASE)
    assert eu.manifest_response(store, platform="android", runtime_version="r2")[0] == 404
    assert eu.manifest_response(store, platform="web", runtime_version="r1")[0] == 400
    assert eu.manifest_response(store, platform="android", runtime_version="..")[0] == 400
    assert eu.manifest_response(store, platform="android", runtime_version="../r1")[0] == 400
    assert eu.latest(store, "android", "../r1") is None


def test_assets_are_answered_by_their_hash_alone(tmp_path):
    store = tmp_path / "store"
    eu.publish(make_export(tmp_path), store, platform="android", runtime_version="r1", base_url=BASE)
    h = eu.asset_hash(b"a picture")
    status, headers, body = eu.asset_response(store, h)
    assert (status, body, headers["content-type"]) == (200, b"a picture", "image/png")
    assert eu.asset_response(store, eu.asset_hash(b"never stored"))[0] == 404
    assert eu.asset_response(store, "../assets/" + h)[0] == 400
    assert eu.asset_response(store, h + "x")[0] == 400
    (store / "assets" / h).write_bytes(b"changed on disk")
    assert eu.asset_response(store, h)[0] == 404


def test_the_signer_keeps_the_key_off_the_disk(tmp_path, monkeypatch):
    key, cert = make_key(tmp_path)
    seen = []
    real = subprocess.run

    def spy(args, **kw):
        # every file openssl is given while signing is read as it is at that moment
        named = [open(a, "rb").read() for a in args if isinstance(a, str) and a.startswith("/") and a != "/dev/stdin"]
        seen.append((args, kw.get("input"), named))
        return real(args, **kw)

    monkeypatch.setattr(eu.subprocess, "run", spy)
    sig = eu.openssl_signer(key, env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin"})(b"data")
    args, given, named = seen[0]
    assert "/dev/stdin" in args and given == key.encode()
    assert named == [b"data"]
    assert eu.verify(b"data", sig, cert)
