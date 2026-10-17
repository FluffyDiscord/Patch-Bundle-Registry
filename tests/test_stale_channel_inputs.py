import json

from scripts.release_history import save_history

SOURCE = "https://github.com/owner/demo"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def release(version="v2", url="https://test/patches-v2.mpp", prerelease=False):
    return {
        "tag_name": version,
        "prerelease": prerelease,
        "assets": [{"browser_download_url": url}],
    }


def channel(root, category, version, url):
    path = root / "internal/cache/bundles/demo-patch-bundles"
    return write(path / f"demo-{category}-patches-bundle.json", {
        "version": version, "download_url": url, "bundle_type": "Morphe:V1",
    })


def inventory(disabled=False):
    return {"demo": {"patches": SOURCE, "integrations": False, "disabled": disabled}}


def test_complete_inventory_retires_deleted_channels_without_deleting_patch_metadata(tmp_path):
    current = channel(tmp_path, "stable", "v2", "https://test/patches-v2.mpp")
    deleted = channel(tmp_path, "dev", "dev-build", "https://test/deleted.mpp")
    metadata = write(deleted.with_name("demo-dev-patches-list.json"), {
        "version": "dev-build", "patches": [{"name": "Retained patch"}],
    })
    record = write(deleted.with_name("demo-dev-extraction.json"), {"status": "verified"})
    registry = save_history(tmp_path, inventory(), {SOURCE: [release()]})
    assert current.exists()
    assert not deleted.exists()
    assert metadata.exists() and record.exists()
    assert len(registry) == 1
    assert (tmp_path / next(iter(registry.values()))["cache_path"]).exists()


def test_failed_inventory_and_disabled_sources_keep_their_last_channel_inputs(tmp_path):
    pointer = channel(tmp_path, "dev", "dev-build", "https://test/deleted.mpp")
    save_history(tmp_path, inventory(), {})
    assert pointer.exists()
    save_history(tmp_path, inventory(True), {SOURCE: []})
    assert pointer.exists()


def test_replaced_artifact_does_not_keep_a_stale_download_queued(tmp_path):
    pointer = channel(tmp_path, "stable", "v2", "https://test/replaced.mpp")
    registry = save_history(tmp_path, inventory(), {SOURCE: [release()]})
    assert not pointer.exists()
    assert len(registry) == 1


def test_promoted_release_stops_using_its_old_prerelease_channel(tmp_path):
    stable = channel(tmp_path, "stable", "v2", "https://test/patches-v2.mpp")
    dev = channel(tmp_path, "dev", "v2", "https://test/patches-v2.mpp")
    save_history(tmp_path, inventory(), {SOURCE: [release()]})
    assert stable.exists() and not dev.exists()


def test_empty_successful_inventory_retires_all_current_channels(tmp_path):
    pointer = channel(tmp_path, "latest", "v2", "https://test/patches-v2.mpp")
    save_history(tmp_path, inventory(), {SOURCE: []})
    assert not pointer.exists()


def test_malformed_pointer_does_not_abort_other_release_reconciliation(tmp_path):
    pointer = channel(tmp_path, "dev", {}, "https://test/invalid.mpp")
    registry = save_history(tmp_path, inventory(), {SOURCE: [release()]})
    assert pointer.exists()
    assert len(registry) == 1
