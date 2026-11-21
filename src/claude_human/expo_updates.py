"""expo_updates.py — over-the-air updates for an Expo app, served from a folder under the Expo Updates protocol
(version 1, https://docs.expo.dev/technical-specs/expo-updates-1/), with code signing.

`publish` takes what `npx expo export --platform android` wrote and stores one update. Each asset is kept under
its SHA-256, and the manifest is kept as the exact bytes a client will be sent, with its signature beside it when
a signer is given. Signing happens once, here, so the program that answers the app never needs the private key.
It only reads stored bytes. `manifest_response` and `asset_response` give the status, headers and body to send
for a request, and `openssl_signer` signs with RSA-SHA256 through the openssl command, with the key on its
standard input, so the key is never written to a file.

    sign = openssl_signer(key_pem)
    update = publish("dist", "store", platform="android", runtime_version=rv, base_url="https://updates.example.org/updates",
                     expo_client=config, sign=sign, certificate=certificate_pem)
    status, headers, body = manifest_response("store", platform="android", runtime_version=rv)
    status, headers, body = asset_response("store", asset_hash)
    republish("store", "android", rv, earlier_id, sign=sign, certificate=certificate_pem)   # a rollback

The app's config names the manifest's address in `updates.url`, the certificate in
`updates.codeSigningCertificate`, and `updates.codeSigningMetadata` as {"keyid": "main", "alg":
"rsa-v1_5-sha256"}. A client that checks signatures refuses a manifest without one, or with one its
certificate does not verify, and keeps the bundle it has. Asset addresses are `<base_url>/assets/<hash>`.

A client never loads an update older than the one it runs, so a rollback is a new update made from an earlier
one's files, with a later createdAt (`republish`), not the earlier update named as `latest` again.

The store:

    <store>/assets/<hash>                              the file, named by its base64url SHA-256 (the manifest's hash)
    <store>/assets/<hash>.type                         its content type
    <store>/<platform>/<runtime>/<id>/manifest.json    the manifest's bytes, as sent
    <store>/<platform>/<runtime>/<id>/signature        the expo-signature value, when it was signed
    <store>/<platform>/<runtime>/latest                the id of the update offered for that runtime

Every file is written whole, through a temporary file and a rename, so a request never reads half of one, and an
update's folder is complete before `latest` names it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import secrets
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

PROTOCOL_VERSION = "1"
PLATFORMS = ("android", "ios")
#: a runtime version or an update id, as a folder name
_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
#: a base64url SHA-256 without padding
_HASH = re.compile(r"^[A-Za-z0-9_-]{43}$")

Response = tuple[int, dict[str, str], bytes]


def asset_hash(data: bytes) -> str:
    """The manifest's `hash` for a file: its SHA-256 in base64url, without padding (RFC 4648 section 5)."""
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def _safe(what: str, value: str) -> str:
    if not isinstance(value, str) or not _SAFE.match(value) or value in (".", ".."):
        raise ValueError(f"{what} is not a plain name (letters, digits, '.', '_' or '-'): {value!r}")
    return value


def _platform(platform: str) -> str:
    if platform not in PLATFORMS:
        raise ValueError(f"platform must be one of {PLATFORMS}: {platform!r}")
    return platform


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _created(at: datetime | None) -> str:
    at = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def build_manifest(dist: str | os.PathLike, *, platform: str, runtime_version: str, base_url: str,
                   expo_client: Mapping | None = None, created_at: datetime | None = None
                   ) -> tuple[dict, list[tuple[str, Path, str]]]:
    """The manifest for the export in `dist`, and its files as (hash, path, content type).

    The id is a UUID made from the export's metadata.json, the platform, the runtime version and `expo_client`, so
    the same export published again is the same update. An asset path that leaves `dist` is refused."""
    dist = Path(dist).resolve()
    _platform(platform)
    _safe("runtime version", runtime_version)
    raw = (dist / "metadata.json").read_bytes()
    try:
        files_of = json.loads(raw)["fileMetadata"][platform]
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"{dist / 'metadata.json'} has no {platform} files: {e}") from None
    base = base_url.rstrip("/")
    files: list[tuple[str, Path, str]] = []

    def entry(rel: str, ext: str | None, launch: bool) -> dict:
        path = (dist / rel).resolve()
        if dist not in path.parents:
            raise ValueError(f"an asset path leaves the export: {rel!r}")
        data = path.read_bytes()
        h = asset_hash(data)
        ctype = "application/javascript" if launch else (mimetypes.guess_type(f"x.{ext}")[0] or "application/octet-stream")
        files.append((h, path, ctype))
        out = {"hash": h, "key": hashlib.md5(data).hexdigest(), "contentType": ctype, "url": f"{base}/assets/{h}"}  # noqa: S324 - the protocol's asset key, not a check
        if not launch and ext:
            out["fileExtension"] = f".{ext}"
        return out

    launch = entry(files_of["bundle"], None, True)
    assets = [entry(a["path"], a.get("ext"), False) for a in files_of.get("assets", [])]
    seed = hashlib.sha256(raw + b"\0" + platform.encode() + b"\0" + runtime_version.encode() + b"\0"
                          + json.dumps(expo_client or {}, sort_keys=True).encode()).hexdigest()
    manifest = {
        "id": str(uuid.UUID(seed[:32])),
        "createdAt": _created(created_at),
        "runtimeVersion": runtime_version,
        "launchAsset": launch,
        "assets": assets,
        "metadata": {},
        "extra": {"expoClient": dict(expo_client)} if expo_client else {},
    }
    return manifest, files


def signature_value(signature_b64: str, keyid: str = "main") -> str:
    """The expo-signature value (an Expo SFV dictionary) for a base64 signature."""
    if not re.fullmatch(r"[A-Za-z0-9+/=]+", signature_b64):
        raise ValueError("a signature is base64")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", keyid):
        raise ValueError(f"a keyid is a plain name: {keyid!r}")
    return f'sig="{signature_b64}", keyid="{keyid}"'


def openssl_signer(key_pem: str, *, openssl: str = "openssl", env: Mapping[str, str] | None = None
                   ) -> Callable[[bytes], str]:
    """A signer for `publish`: RSA-SHA256 (RSASSA-PKCS1-v1_5) of the given bytes, in base64, made by the openssl
    command with the PEM key on its standard input. Only the bytes to sign go into a temporary file."""
    def sign(data: bytes) -> str:
        with tempfile.NamedTemporaryFile(prefix="expo-update-") as fh:
            fh.write(data)
            fh.flush()
            r = subprocess.run([openssl, "dgst", "-sha256", "-sign", "/dev/stdin", fh.name], input=key_pem.encode(),
                               capture_output=True, timeout=60, check=False, env=dict(env) if env is not None else None)
        if r.returncode != 0 or not r.stdout:
            last = (r.stderr.decode(errors="replace").strip().splitlines() or ["no output"])[-1]
            raise RuntimeError(f"openssl could not sign: {last}")
        return base64.b64encode(r.stdout).decode()
    return sign


def verify(data: bytes, signature_b64: str, certificate_pem: str, *, openssl: str = "openssl",
           env: Mapping[str, str] | None = None) -> bool:
    """Whether `signature_b64` is an RSA-SHA256 signature of `data` by the key of `certificate_pem`, as a client
    with that certificate checks it."""
    run_env = dict(env) if env is not None else None
    with tempfile.TemporaryDirectory(prefix="expo-update-verify-") as d:
        cert, pub, sig, body = (Path(d) / n for n in ("cert.pem", "pub.pem", "sig.bin", "body"))
        cert.write_text(certificate_pem)
        try:
            sig.write_bytes(base64.b64decode(signature_b64, validate=True))
        except ValueError:
            return False
        body.write_bytes(data)
        r = subprocess.run([openssl, "x509", "-in", str(cert), "-noout", "-pubkey"], capture_output=True, timeout=60,
                           check=False, env=run_env)
        if r.returncode != 0:
            return False
        pub.write_bytes(r.stdout)
        r = subprocess.run([openssl, "dgst", "-sha256", "-verify", str(pub), "-signature", str(sig), str(body)],
                           capture_output=True, timeout=60, check=False, env=run_env)
        return r.returncode == 0


def publish(dist: str | os.PathLike, store: str | os.PathLike, *, platform: str, runtime_version: str,
            base_url: str, expo_client: Mapping | None = None, sign: Callable[[bytes], str] | None = None,
            keyid: str = "main", certificate: str | None = None, created_at: datetime | None = None,
            openssl: str = "openssl", env: Mapping[str, str] | None = None) -> dict:
    """Store the export in `dist` as the update offered for `runtime_version` on `platform`.

    With `sign`, the manifest's bytes are signed once and the expo-signature value is kept beside them. With
    `certificate` as well, the signature is first checked against it (`verify`), and a signature the app's
    certificate would refuse is never stored. Returns the update's id, whether it is signed, the size of the
    manifest and the number of assets."""
    manifest, files = build_manifest(dist, platform=platform, runtime_version=runtime_version, base_url=base_url,
                                     expo_client=expo_client, created_at=created_at)
    body = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode()
    value = None
    if sign is not None:
        sig = sign(body)
        if certificate is not None and not verify(body, sig, certificate, openssl=openssl, env=env):
            raise ValueError("the signature does not verify against the certificate, so the app would refuse it")
        value = signature_value(sig, keyid)
    root = Path(store)
    for h, path, ctype in files:
        target = root / "assets" / h
        if not target.exists():
            _write(target, path.read_bytes())
        _write(root / "assets" / f"{h}.type", ctype.encode())
    folder = root / platform / runtime_version / manifest["id"]
    _write(folder / "manifest.json", body)
    sig_file = folder / "signature"
    if value is None:
        sig_file.unlink(missing_ok=True)
    else:
        _write(sig_file, value.encode())
    _write(root / platform / runtime_version / "latest", manifest["id"].encode())
    return {"id": manifest["id"], "signed": value is not None, "manifest_bytes": len(body),
            "assets": len(manifest["assets"]), "launch_hash": manifest["launchAsset"]["hash"]}


def latest(store: str | os.PathLike, platform: str, runtime_version: str) -> str | None:
    """The id of the update offered for `runtime_version` on `platform`, or None."""
    try:
        f = Path(store) / _platform(platform) / _safe("runtime version", runtime_version) / "latest"
        value = f.read_text().strip()
        return _safe("update id", value)
    except (OSError, ValueError):
        return None


def updates(store: str | os.PathLike, platform: str, runtime_version: str) -> list[dict]:
    """Every update stored for `runtime_version` on `platform`, newest first by its createdAt: its id, createdAt,
    whether it is signed and whether it is the one offered now."""
    try:
        root = Path(store) / _platform(platform) / _safe("runtime version", runtime_version)
    except ValueError:
        return []
    offered = latest(store, platform, runtime_version)
    out = []
    for folder in (root.iterdir() if root.is_dir() else []):
        if not folder.is_dir() or not _SAFE.match(folder.name):
            continue
        try:
            m = json.loads((folder / "manifest.json").read_bytes())
        except (OSError, ValueError):
            continue
        out.append({"id": folder.name, "created_at": m.get("createdAt"), "signed": (folder / "signature").exists(),
                    "offered": folder.name == offered})
    return sorted(out, key=lambda u: str(u["created_at"] or ""), reverse=True)


def _parse_created(value: object) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def republish(store: str | os.PathLike, platform: str, runtime_version: str, update_id: str, *,
              sign: Callable[[bytes], str], certificate: str, keyid: str = "main", created_at: datetime | None = None,
              openssl: str = "openssl", env: Mapping[str, str] | None = None) -> dict:
    """Offer an earlier stored update again, as a new update: the way to roll back (or forward again).

    A client never loads an update whose createdAt is older than the one it runs, nor one whose id it already has, so
    naming the earlier update as `latest` again would change nothing on a device. This makes a new update instead,
    with the earlier one's launch asset, assets, metadata and extra, a new id (made from the earlier id and the new
    createdAt) and createdAt now, or `created_at`. It must be later than the createdAt of the update offered now.

    The earlier update is checked first. Its manifest must parse and name this id and runtime version, every file it
    lists must be stored with its hash, and its stored signature must verify against `certificate`, so the key signs
    again only what it signed before. The new manifest is signed once and verified against `certificate` before
    anything is stored, and `latest` names it only then. Any failed check raises ValueError and leaves the store and
    `latest` as they were. Returns the new id, the id it copied, the id it replaced, and its createdAt."""
    root = Path(store) / _platform(platform) / _safe("runtime version", runtime_version)
    folder = root / _safe("update id", update_id)
    try:
        body = (folder / "manifest.json").read_bytes()
        manifest = json.loads(body)
    except (OSError, ValueError):
        raise ValueError(f"no stored update {update_id} for {platform} {runtime_version}") from None
    if not isinstance(manifest, dict) or manifest.get("id") != update_id \
            or manifest.get("runtimeVersion") != runtime_version:
        raise ValueError(f"the stored manifest of {update_id} names another id or runtime version")
    for asset in [manifest.get("launchAsset") or {}] + list(manifest.get("assets") or []):
        h = asset.get("hash") if isinstance(asset, dict) else None
        status = asset_response(store, h)[0] if isinstance(h, str) else 400
        if status != 200:
            raise ValueError(f"{update_id} lists a file that is not stored with its hash ({h!r})")
    value = (folder / "signature").read_text().strip() if (folder / "signature").exists() else ""
    m = re.fullmatch(r'sig="([A-Za-z0-9+/=]+)"(?:, .*)?', value)
    if not m or not verify(body, m.group(1), certificate, openssl=openssl, env=env):
        raise ValueError(f"{update_id} has no signature the certificate verifies, so it is not signed again")
    before = latest(store, platform, runtime_version)
    created = _created(created_at)
    if before is not None:
        try:
            offered_at = _parse_created(json.loads((root / before / "manifest.json").read_bytes()).get("createdAt"))
        except (OSError, ValueError, AttributeError):
            offered_at = None
        if offered_at is None:
            raise ValueError(f"the createdAt of {before}, the update offered now, cannot be read, so a later one "
                             "cannot be chosen")
        if _parse_created(created) <= offered_at:
            raise ValueError(f"createdAt {created} is not later than {_created(offered_at)} of {before}, the update "
                             "offered now, and a client never loads an update older than the one it runs")
    seed = hashlib.sha256(update_id.encode() + b"\0" + created.encode()).hexdigest()
    new = {**manifest, "id": str(uuid.UUID(seed[:32])), "createdAt": created}
    new_body = json.dumps(new, separators=(",", ":"), ensure_ascii=False).encode()
    sig = sign(new_body)
    if not verify(new_body, sig, certificate, openssl=openssl, env=env):
        raise ValueError("the signature does not verify against the certificate, so the app would refuse it")
    target = root / new["id"]
    _write(target / "manifest.json", new_body)
    _write(target / "signature", signature_value(sig, keyid).encode())
    _write(root / "latest", new["id"].encode())
    return {"id": new["id"], "copied": update_id, "replaced": before, "created_at": created}


def manifest_response(store: str | os.PathLike, *, platform: str, runtime_version: str,
                      boundary: str | None = None) -> Response:
    """What to answer a manifest request with: the stored update for the request's expo-platform and
    expo-runtime-version headers, as a multipart response whose manifest part carries the stored signature. 400 for
    a platform or runtime version that is not plain, 404 when no update is stored for them."""
    plain = {"content-type": "text/plain; charset=utf-8", "cache-control": "private, max-age=0"}
    if platform not in PLATFORMS or not isinstance(runtime_version, str) or not _SAFE.match(runtime_version) \
            or runtime_version in (".", ".."):
        return 400, plain, b"expo-platform must be android or ios, and expo-runtime-version a plain name\n"
    update = latest(store, platform, runtime_version)
    if update is None:
        return 404, plain, b"no update for this runtime version\n"
    folder = Path(store) / platform / runtime_version / update
    try:
        body = (folder / "manifest.json").read_bytes()
    except OSError:
        return 404, plain, b"no update for this runtime version\n"
    sig_file = folder / "signature"
    value = sig_file.read_text().strip() if sig_file.exists() else None
    b = boundary or secrets.token_hex(16)
    head = (f'content-disposition: form-data; name="manifest"\r\ncontent-type: application/json; charset=utf-8\r\n'
            + (f"expo-signature: {value}\r\n" if value else "") + "\r\n")
    out = f"--{b}\r\n".encode() + head.encode() + body + f"\r\n--{b}--\r\n".encode()
    headers = {"expo-protocol-version": PROTOCOL_VERSION, "expo-sfv-version": "0",
               "cache-control": "private, max-age=0", "content-type": f"multipart/mixed; boundary={b}"}
    return 200, headers, out


def asset_response(store: str | os.PathLike, hash_: str) -> Response:
    """What to answer an asset request with: the stored file named by its hash. 400 for a name that is not a
    hash, 404 for one not stored."""
    plain = {"content-type": "text/plain; charset=utf-8"}
    if not isinstance(hash_, str) or not _HASH.match(hash_):
        return 400, plain, b"an asset is named by its base64url SHA-256\n"
    f = Path(store) / "assets" / hash_
    try:
        data = f.read_bytes()
    except OSError:
        return 404, plain, b"no such asset\n"
    if asset_hash(data) != hash_:
        return 404, plain, b"no such asset\n"
    try:
        ctype = (Path(store) / "assets" / f"{hash_}.type").read_text().strip() or "application/octet-stream"
    except OSError:
        ctype = "application/octet-stream"
    return 200, {"content-type": ctype, "cache-control": "public, max-age=31536000, immutable"}, data
