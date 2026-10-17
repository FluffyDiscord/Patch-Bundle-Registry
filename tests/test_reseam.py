import hashlib

import httpx
import pytest
from test_generate_database import db, fixture, snapshot, write

from scripts.git_hosts import endpoint
from scripts.prepare_native_runtimes import install
from scripts.release_assets import BUNDLE_TYPES, asset_extension, choose_patch_bundle


def test_reseam_assets_and_host():
    asset = {"url": "https://git.reseam.app/download/123", "name": "patches.reseam"}
    assert choose_patch_bundle([{"url": "https://example.test/legacy.jar"}, asset]) == asset
    assert BUNDLE_TYPES[asset_extension(asset)] == "Reseam"
    assert endpoint("https://git.reseam.app/reseam/patches").endswith(
        "/api/v1/repos/reseam/patches/releases"
    )


def test_cached_old_family_names_keep_history_and_ids(tmp_path):
    folder = fixture(tmp_path)
    first = db.build_database(tmp_path)
    path, old = snapshot(tmp_path, first)
    history = db.read_json(path / "history.json")
    for item in history.values():
        item["bundle"]["bundle_type"] = "ReVanced:V4"
    write(path / "history.json", history)
    for channel in ("stable", "latest"):
        raw_path = folder / f"demo-{channel}-patches-bundle.json"
        raw = db.read_json(raw_path)
        raw["bundle_type"] = "ReVanced:v22"
        write(raw_path, raw)
    _, new = snapshot(tmp_path, db.build_database(tmp_path))
    assert len(new) == 1
    assert new[0]["id"] == old[0]["id"]
    assert new[0]["legacy_id"] == old[0]["legacy_id"]
    assert new[0]["bundle_type"] == "ReVanced:v22"
    assert new[0]["metadata_status"] == "verified"


def test_reseam_catalog_preserves_publisher_and_native_import(tmp_path):
    folder = fixture(tmp_path)
    publisher = {
        "name": "patches",
        "author": "Example",
        "description": "Apps",
        "public_key": "a" * 64,
    }
    for channel in ("stable", "latest"):
        raw_path = folder / f"demo-{channel}-patches-bundle.json"
        raw = db.read_json(raw_path)
        raw.update(download_url="https://example.test/patches.reseam", bundle_type="Reseam")
        write(raw_path, raw)
        status_path = folder / f"demo-{channel}-extraction.json"
        status = db.read_json(status_path)
        status.update(
            download_url=raw["download_url"], reseam_bundle=publisher, completeness_version=1
        )
        write(status_path, status)
    first = db.build_database(tmp_path)
    _, bundles = snapshot(tmp_path, first)
    assert bundles[0]["ecosystem"] == "reseam"
    assert bundles[0]["reseam_bundle"] == publisher
    assert "/api/reseam/bundle?" in bundles[0]["import_urls"]["latest"]
    for file in folder.iterdir():
        file.unlink()
    db.restore_catalog_cache(tmp_path)
    status = db.read_json(folder / "demo-latest-extraction.json")
    assert status["reseam_bundle"] == publisher


def test_native_runtime_checksum_and_cache(tmp_path, monkeypatch):
    content = b"fixture executable"
    calls = []

    def stream(*args, **kwargs):
        calls.append(args)
        from contextlib import nullcontext

        return nullcontext(
            httpx.Response(200, content=content, request=httpx.Request("GET", args[1]))
        )

    monkeypatch.setattr(httpx, "stream", stream)
    import scripts.prepare_native_runtimes as native

    monkeypatch.setattr(native.platform, "system", lambda: "Linux")
    monkeypatch.setattr(native.platform, "machine", lambda: "x86_64")
    runtime = {
        "id": "reseam-test",
        "executables": {
            "linux-x64": {
                "url": "https://example.test/cli",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        },
    }
    assert install(runtime, tmp_path)["available"]
    assert install(runtime, tmp_path)["available"]
    assert len(calls) == 1
    runtime["executables"]["linux-x64"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="checksum"):
        install(runtime, tmp_path)
    assert not list((tmp_path / "internal/cache/runtimes").rglob("*.tmp"))
