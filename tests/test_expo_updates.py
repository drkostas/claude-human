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


def two_updates(tmp_path):
    key, cert = make_key(tmp_path)
    dist, store = make_export(tmp_path), tmp_path / "store"
    sign = eu.openssl_signer(key)
    first = eu.publish(dist, store, platform="android", runtime_version="r1", base_url=BASE, sign=sign, certificate=cert,
                       created_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    (dist / "_expo/static/js/android/index-1.hbc").write_bytes(b"a newer bundle")
    (dist / "metadata.json").write_text((dist / "metadata.json").read_text() + " ")
    second = eu.publish(dist, store, platform="android", runtime_version="r1", base_url=BASE, sign=sign, certificate=cert,
                        created_at=datetime(2025, 1, 2, tzinfo=timezone.utc))
    return store, sign, cert, first, second


def offered(store):
    """The manifest offered now, its bytes, and its signature."""
    (part,) = parts(*eu.manifest_response(store, platform="android", runtime_version="r1"))
    body = part.get_payload(decode=True)
    return json.loads(body), body, part["expo-signature"][len('sig="'):-len('", keyid="main"')]


def store_files(store):
    return sorted((str(p.relative_to(store)), p.read_bytes()) for p in store.rglob("*") if p.is_file())


def test_a_rollback_is_a_new_update_with_the_earlier_files_and_a_later_created_at(tmp_path):
    store, sign, cert, first, second = two_updates(tmp_path)
    listed = eu.updates(store, "android", "r1")
    assert [u["id"] for u in listed] == [second["id"], first["id"]]
    assert [u["offered"] for u in listed] == [True, False] and all(u["signed"] for u in listed)
    earlier = json.loads((store / "android" / "r1" / first["id"] / "manifest.json").read_bytes())
    replaced = json.loads((store / "android" / "r1" / second["id"] / "manifest.json").read_bytes())
    at = datetime(2025, 1, 3, 4, 5, 6, 789000, tzinfo=timezone.utc)
    back = eu.republish(store, "android", "r1", first["id"], sign=sign, certificate=cert, created_at=at)
    assert (back["copied"], back["replaced"], back["created_at"]) == (first["id"], second["id"], "2025-01-03T04:05:06.789Z")
    m, body, sig = offered(store)
    assert m["id"] == back["id"] and back["id"] not in (first["id"], second["id"])
    assert str(uuid.UUID(m["id"])) == m["id"]
    assert m["createdAt"] == back["created_at"] and m["createdAt"] > replaced["createdAt"]
    assert m["launchAsset"] == earlier["launchAsset"] and m["assets"] == earlier["assets"]
    assert m["launchAsset"]["hash"] == eu.asset_hash(b"the bundle")
    assert {k: v for k, v in m.items() if k not in ("id", "createdAt")} == \
        {k: v for k, v in earlier.items() if k not in ("id", "createdAt")}
    assert eu.verify(body, sig, cert)
    assert eu.latest(store, "android", "r1") == back["id"]
    # forward again is the same step, from the update the rollback replaced
    on = eu.republish(store, "android", "r1", second["id"], sign=sign, certificate=cert)
    m2, body2, sig2 = offered(store)
    assert on["replaced"] == back["id"] and m2["createdAt"] > m["createdAt"]
    assert m2["launchAsset"]["hash"] == eu.asset_hash(b"a newer bundle") and eu.verify(body2, sig2, cert)
    assert [u["id"] for u in eu.updates(store, "android", "r1")] == [on["id"], back["id"], second["id"], first["id"]]


def test_a_signer_the_certificate_refuses_stores_nothing_and_leaves_latest(tmp_path):
    store, _, cert, first, second = two_updates(tmp_path)
    other_key, _ = make_key(tmp_path, "other")
    before = store_files(store)
    with pytest.raises(ValueError, match="would refuse it"):
        eu.republish(store, "android", "r1", first["id"], sign=eu.openssl_signer(other_key), certificate=cert)
    assert store_files(store) == before
    assert eu.latest(store, "android", "r1") == second["id"]


def test_a_rollback_is_refused_when_its_update_would_not_load(tmp_path):
    store, sign, cert, first, second = two_updates(tmp_path)
    _, other_cert = make_key(tmp_path, "other")
    before = store_files(store)
    # a createdAt that is not later than the update offered now
    for at in (datetime(2025, 1, 2, tzinfo=timezone.utc), datetime(2024, 12, 31, tzinfo=timezone.utc)):
        with pytest.raises(ValueError, match="never loads an update older"):
            eu.republish(store, "android", "r1", first["id"], sign=sign, certificate=cert, created_at=at)
    # the earlier update's own checks: its signature, its id, a plain name, its files
    with pytest.raises(ValueError, match="certificate verifies"):
        eu.republish(store, "android", "r1", first["id"], sign=sign, certificate=other_cert)
    with pytest.raises(ValueError, match="no stored update"):
        eu.republish(store, "android", "r1", str(uuid.uuid4()), sign=sign, certificate=cert)
    with pytest.raises(ValueError, match="plain name"):
        eu.republish(store, "android", "r1", "../r2", sign=sign, certificate=cert)
    assert store_files(store) == before
    (store / "android" / "r1" / first["id"] / "signature").unlink()
    with pytest.raises(ValueError, match="certificate verifies"):
        eu.republish(store, "android", "r1", first["id"], sign=sign, certificate=cert)
    (store / "assets" / eu.asset_hash(b"a newer bundle")).write_bytes(b"changed on disk")
    with pytest.raises(ValueError, match="not stored with its hash"):
        eu.republish(store, "android", "r1", second["id"], sign=sign, certificate=cert)
    assert eu.latest(store, "android", "r1") == second["id"]
    assert sorted(p.name for p in (store / "android" / "r1").iterdir()) == sorted(["latest", first["id"], second["id"]])
    assert eu.updates(store, "android", "r2") == [] and eu.updates(store, "web", "r1") == []
