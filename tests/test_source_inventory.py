"""Repository-only inventories retain channel discovery and published identities."""

import asyncio
import shutil
from pathlib import Path

import httpx
from test_discover_releases import release
from test_generate_database import db, fixture, snapshot, write

from scripts import discover_releases as discovery
from scripts.git_hosts import endpoint, normalize_source
from scripts.release_history import save_history
from scripts.source_policy import source_channels


def test_public_inventory_has_one_canonical_entry_per_repository():
    root = Path(__file__).resolve().parents[1]
    inventory = db.read_json(root / "config/sources.json")
    urls = [normalize_source(item["patches"], root).lower() for item in inventory.values()]
    assert len(urls) == len(set(urls))
    for item in inventory.values():
        assert item["patches"] == normalize_source(item["patches"], root)
        assert isinstance(item["disabled"], bool)
        assert isinstance(item["integrations"], bool)
        assert item["integrations"] == bool(item.get("integration"))
        assert not {"latest", "prerelease"} & item.keys()
        if item.get("integration"):
            assert item["integration"] == normalize_source(item["integration"], root)


def test_repository_urls_expand_to_channels_and_provider_endpoints(tmp_path):
    write(
        tmp_path / "config/git-hosts.json",
        {
            "git.example.test": "gitea",
            "lab.example.test": "gitlab",
            "enterprise.example.test": "github",
        },
    )
    urls = {
        "github": (
            "https://github.com/team/patches",
            "https://api.github.com/repos/team/patches/releases",
        ),
        "gitlab": (
            "https://gitlab.com/team/subgroup/patches",
            "https://gitlab.com/api/v4/projects/team%2Fsubgroup%2Fpatches/releases",
        ),
        "gitea": (
            "https://git.example.test/team/patches",
            "https://git.example.test/api/v1/repos/team/patches/releases",
        ),
        "lab": (
            "https://lab.example.test/team/subgroup/patches",
            "https://lab.example.test/api/v4/projects/team%2Fsubgroup%2Fpatches/releases",
        ),
        "enterprise": (
            "https://enterprise.example.test/team/patches",
            "https://enterprise.example.test/api/v3/repos/team/patches/releases",
        ),
    }
    for name, (url, api) in urls.items():
        channels = source_channels({name: {"patches": url, "disabled": False}}, tmp_path)
        assert set(channels) == {f"{name}-{channel}" for channel in ("latest", "stable", "dev")}
        assert channels[f"{name}-latest"]["latest"]
        assert channels[f"{name}-dev"]["prerelease"]
        assert not channels[f"{name}-stable"]["prerelease"]
        assert {endpoint(item["patches"], root=tmp_path) for item in channels.values()} == {api}


def test_consolidation_preserves_bundles_and_cache_restoration(tmp_path):
    fixture(tmp_path)
    original = db.build_database(tmp_path)
    _, before = snapshot(tmp_path, original)
    write(
        tmp_path / "config/sources.json",
        {
            "demo": {"patches": "https://github.com/owner/demo", "disabled": False},
            "empty": {"patches": "https://gitlab.com/team/empty", "disabled": False},
        },
    )
    migrated = db.build_database(tmp_path)
    _, after = snapshot(tmp_path, migrated)
    assert after == before
    shutil.rmtree(tmp_path / "internal/cache")
    db.restore_catalog_cache(tmp_path)
    restored = db.build_database(tmp_path)
    _, rows = snapshot(tmp_path, restored)
    assert rows == before
    inventory = db.read_json(tmp_path / "config/sources.json")
    inventory["demo"]["disabled"] = True
    write(tmp_path / "config/sources.json", inventory)
    path, disabled = snapshot(tmp_path, db.build_database(tmp_path))
    assert disabled
    assert (
        next(row for row in db.read_json(path / "sources.json") if row["repo"] == "demo")["enabled"]
        is False
    )


def test_one_repository_discovers_all_channels_with_one_release_request(tmp_path, monkeypatch):
    sources_path = tmp_path / "config/sources.json"
    write(
        sources_path, {"demo": {"patches": "https://github.com/example/patches", "disabled": False}}
    )
    for name, value in {
        "PROJECT_ROOT": tmp_path,
        "PATCH_BUNDLES_DIR": tmp_path / "internal/cache/bundles",
        "BUNDLE_SOURCES_PATH": sources_path,
        "RELEASE_CACHE_FILE": tmp_path / "release-cache.json",
        "METADATA_PATH": tmp_path / "metadata.json",
        "RELEASE_CACHE": {},
        "RELEASE_REQUESTS": {},
        "RELEASE_COOLDOWNS": {},
        "HTTP_SEMAPHORE": asyncio.Semaphore(6),
    }.items():
        monkeypatch.setattr(discovery, name, value)
    requests = []

    def handler(request):
        requests.append(request)
        stable = {**release("v1"), "body": "Stable notes"}
        development = {**release("v2-dev", True), "body": "Preview notes"}
        return httpx.Response(200, json=[development, stable])

    monkeypatch.setattr(
        discovery,
        "AsyncClient",
        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs),
    )
    assert asyncio.run(discovery.main()) == 0
    assert len(requests) == 1
    assert str(requests[0].url).startswith("https://api.github.com/repos/example/patches/releases")
    rows = db.read_json(tmp_path / "metadata.json")["bundles"]
    assert {name: value["patches"]["version"] for name, value in rows.items()} == {
        "demo-latest": "v2-dev",
        "demo-stable": "v1",
        "demo-dev": "v2-dev",
    }
    assert (
        db.read_json(
            tmp_path / "internal/cache/bundles/demo-patch-bundles/demo-stable-patches-bundle.json"
        )["description"]
        == "Stable notes"
    )


def test_custom_host_release_notes_reach_history_and_catalog(tmp_path):
    fixture(tmp_path)
    write(tmp_path / "config/git-hosts.json", {"git.example.test": "gitea"})
    url = "https://git.example.test/team/patches"
    inventory = {"custom": {"patches": url, "disabled": False}}
    write(tmp_path / "config/sources.json", inventory)
    payload = {
        **release("v1"),
        "body": "## Changes\nSelf-hosted release notes",
        "created_at": "2026-10-07T12:00:00Z",
    }
    save_history(tmp_path, inventory, {url: [payload]})
    _, bundles = snapshot(tmp_path, db.build_database(tmp_path))
    assert len(bundles) == 1
    assert bundles[0]["description"] == payload["body"]


def test_repository_keys_can_end_in_channel_words(tmp_path):
    channels = source_channels(
        {
            "example-dev": {
                "patches": "https://github.com/team/example-dev",
                "integration": "https://github.com/team/integrations",
                "disabled": False,
            }
        },
        tmp_path,
    )
    assert set(channels) == {"example-dev-latest", "example-dev-stable", "example-dev-dev"}
    assert all(
        item["integration"] == "https://github.com/team/integrations" for item in channels.values()
    )


def test_disabled_repository_keys_keep_their_complete_extraction_folder(tmp_path):
    from scripts.source_policy import write_extraction_policy

    for name in ("example", "example-dev", "example-stable", "example-latest"):
        write(
            tmp_path / "config/sources.json",
            {name: {"patches": "https://github.com/team/" + name, "disabled": True}},
        )
        assert write_extraction_policy(tmp_path) == {name + "-patch-bundles"}
    write(
        tmp_path / "config/sources.json",
        {
            "example-dev": {
                "patches": "https://github.com/team/example",
                "prerelease": True,
                "disabled": True,
            }
        },
    )
    assert write_extraction_policy(tmp_path) == {"example-patch-bundles"}
