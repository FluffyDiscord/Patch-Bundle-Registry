import pytest
from test_generate_database import db, fixture, snapshot, write

from scripts.release_assets import auxiliary_bundle, choose_patch_bundle
from scripts.release_history import save_history


@pytest.mark.parametrize(
    "suffix", ["sources", "source", "javadoc", "docs", "tests", "test-fixtures"]
)
@pytest.mark.parametrize("extension", ["rvp", "mpp", "jar"])
def test_compiler_archives_never_replace_the_runtime(suffix, extension):
    auxiliary = {"url": f"https://test/patches-1.0-{suffix}.{extension}"}
    compiled = {"url": f"https://test/patches-1.0.{extension}"}
    assert auxiliary_bundle(auxiliary)
    assert choose_patch_bundle([auxiliary, compiled]) == compiled
    assert choose_patch_bundle([auxiliary]) is None


def test_asset_name_fallback_and_url_suffix_precedence():
    assert auxiliary_bundle({"url": "https://test/asset/123", "name": "patches-SOURCES.mpp"})
    assert auxiliary_bundle({"url": "https://test/patches%2Dsources.rvp?token=x"})
    assert not auxiliary_bundle(
        {"url": "https://test/patches.mpp", "name": "Source documentation.mpp"}
    )
    assert not auxiliary_bundle({"url": "https://test/open-source-patches.mpp"})


def test_cached_source_archives_are_not_published_as_verified_empty_bundles(tmp_path):
    folder = fixture(tmp_path)
    for channel in ("latest", "stable"):
        path = folder / f"demo-{channel}-patches-bundle.json"
        raw = db.read_json(path)
        raw["download_url"] = "https://example.test/patches-sources.rvp"
        write(path, raw)
    _, rows = snapshot(tmp_path, db.build_database(tmp_path, restore=False))
    assert not rows


def test_history_reselects_compiled_assets_from_unchanged_release_metadata(tmp_path):
    inventory = {"demo": {"patches": "https://github.com/owner/demo"}}
    releases = [
        {
            "tag_name": "v1",
            "assets": [
                {"browser_download_url": "https://test/patches-javadoc.mpp"},
                {"browser_download_url": "https://test/patches.mpp"},
            ],
        }
    ]
    rows = save_history(tmp_path, inventory, {"https://github.com/owner/demo": releases})
    row = next(iter(rows.values()))
    assert db.read_json(tmp_path / row["cache_path"])["download_url"] == "https://test/patches.mpp"
