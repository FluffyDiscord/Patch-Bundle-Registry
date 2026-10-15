import json
import shutil

import pytest
from test_cloudflare_catalog import SOURCE, MemoryCloud, catalog, release
from test_generate_database import db, fixture, write

from scripts import cloudflare_catalog as cloud
from scripts.notify_catalog_update import messages
from scripts.refresh_issues import extraction_issues, read_issues, record_issue
from scripts.release_archive import reconcile_archive
from scripts.source_cleanup import clean_inputs

KEPT = "https://github.com/owner/kept"


def paired_catalog(root):
    original = fixture(root)
    inventory = db.read_json(root / "config/sources.json")
    inventory["kept-stable"] = {"patches": KEPT}
    write(root / "config/sources.json", inventory)
    kept = original.parent / "kept-patch-bundles"
    kept.mkdir()
    for path in original.glob("demo-stable-*.json"):
        shutil.copyfile(path, kept / path.name.replace("demo-stable", "kept-stable"))
    for channel in ("latest", "stable"):
        path = original / f"demo-{channel}-patches-list.json"
        payload = db.read_json(path)
        payload["patches"][0]["compatiblePackages"]["com.only"] = ["1"]
        write(path, payload)
        status_path = original / f"demo-{channel}-extraction.json"
        status = db.read_json(status_path)
        status["patch_list_hash"] = db.digest(path.read_bytes())
        write(status_path, status)
    write(
        root / "database/app-metadata.json",
        {
            "apps": {
                "com.video": {"app_name": "Shared"},
                "com.only": {"app_name": "Exclusive"},
            }
        },
    )
    reconcile_archive(
        root,
        inventory,
        {SOURCE: [release(1)], KEPT: [release(2)], "https://gitlab.com/team/empty": []},
    )
    return db.build_database(root)


def remove_demo(root):
    inventory = db.read_json(root / "config/sources.json")
    inventory = {key: row for key, row in inventory.items() if not key.startswith("demo-")}
    write(root / "config/sources.json", inventory)
    return db.build_database(root)


@pytest.mark.parametrize("removed", [True, False])
def test_site_build_after_source_removal_or_disabling(tmp_path, monkeypatch, removed):
    monkeypatch.delenv("CATALOG_BACKEND", raising=False)
    first = paired_catalog(tmp_path)
    inventory = db.read_json(tmp_path / "config/sources.json")
    for alias in list(inventory):
        if alias.startswith("demo-"):
            if removed:
                del inventory[alias]
            else:
                inventory[alias]["disabled"] = True
    write(tmp_path / "config/sources.json", inventory)
    current = db.build_database(tmp_path)
    (tmp_path / "web").mkdir()
    (tmp_path / "shared").mkdir()
    (tmp_path / "web/index.html").write_text("Catalog")
    db.build_site(tmp_path)
    expected = [current["snapshot"]] if removed else [first["snapshot"], current["snapshot"]]
    assert db.read_json(tmp_path / "database/snapshot-history.json") == expected
    public = tmp_path / "site-dist/database"
    assert {path.name for path in (public / "snapshots").iterdir()} == {
        name.split("/")[-1] for name in expected
    }
    assert (tmp_path / "database" / first["snapshot"]).exists() == (not removed)
    assert db.read_json(public / "manifest.json") == current
    for name in current["files"]:
        assert (public / current["snapshot"] / name).is_file()


@pytest.mark.parametrize("deleted_before_failure", [True, False])
def test_interrupted_snapshot_cleanup_keeps_publication_registry_consistent(
    tmp_path, monkeypatch, deleted_before_failure
):
    from scripts import source_cleanup

    first = paired_catalog(tmp_path)
    remove_tree = source_cleanup.remove_tree

    def interrupt(root, path):
        if path == tmp_path / "database" / first["snapshot"]:
            if deleted_before_failure:
                remove_tree(root, path)
            raise RuntimeError("Snapshot deletion interrupted")
        remove_tree(root, path)

    monkeypatch.setattr(source_cleanup, "remove_tree", interrupt)
    with pytest.raises(RuntimeError, match="Snapshot deletion interrupted"):
        remove_demo(tmp_path)
    current = db.read_json(tmp_path / "database/manifest.json")
    assert db.read_json(tmp_path / "database/snapshot-history.json") == [current["snapshot"]]
    monkeypatch.setattr(source_cleanup, "remove_tree", remove_tree)
    source_cleanup.clean_snapshots(tmp_path)
    assert not (tmp_path / "database" / first["snapshot"]).exists()
    assert (tmp_path / "database" / current["snapshot"]).is_dir()
    assert db.read_json(tmp_path / "database/snapshot-history.json") == [current["snapshot"]]


def test_source_removal_purges_d1_r2_checkpoints_and_exclusive_apps(tmp_path, monkeypatch):
    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    first = paired_catalog(tmp_path)
    memory.publish()
    original = memory.document(memory.active())
    old_records = memory.read_records(original)
    removed_bundles = {
        row["key"]
        for row in old_records
        if row["kind"] == "bundle"
        and row["data"]["source_id"]
        == next(
            row["key"]
            for row in old_records
            if row["kind"] == "source" and row["data"]["url"] == SOURCE
        )
    }
    removed_patches = {
        row["key"]
        for row in old_records
        if row["kind"] == "patch" and row["data"]["bundle_id"] in removed_bundles
    }
    memory.checkpoint()
    manifest = remove_demo(tmp_path)
    assert not (tmp_path / "database" / first["snapshot"]).exists()
    memory.publish()
    active = memory.active()
    document = memory.document(active)
    assert document["source_purge_pending"] is False
    records = memory.read_records(document)
    assert not any(row["kind"] == "source" and row["data"]["url"] == SOURCE for row in records)
    assert not any(row["key"] in removed_bundles | removed_patches for row in records)
    apps = {row["key"]: row["data"] for row in records if row["kind"] == "app"}
    assert set(apps) == {"com.video"}
    assert apps["com.video"]["bundle_count"] == 1
    assert apps["com.video"]["app_name"] == "Shared"
    assert not any(
        row["kind"] == "history_release" and row["data"]["source_url"] == SOURCE for row in records
    )
    assert len(memory.sql("SELECT * FROM catalog_generations")) == 1
    assert all(row["to_seq"] is None for row in memory.sql("SELECT * FROM catalog_records"))
    from scripts.cloudflare_checkpoint import payloads, retained_keys

    assert set(memory.objects) == (
        {"objects/" + digest for digest in memory.object_digests(document)}
        | {active["manifest_key"]} | retained_keys(memory, tmp_path)
    )
    checkpoint = json.loads(memory.objects["checkpoints/latest.json"])
    for name, body in payloads(memory, tmp_path, checkpoint).items():
        if name != "database/ids.json":  # Numeric IDs remain reserved, never recycled.
            assert SOURCE.encode() not in body
            assert b"com.only" not in body
    assert (tmp_path / "database" / manifest["snapshot"]).exists()
    memory.restore_checkpoint()
    assert SOURCE not in db.read_json(tmp_path / "internal/cache/history/archive.json")["sources"]


def test_disabled_sources_and_alias_removal_keep_history(tmp_path, monkeypatch):
    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    paired_catalog(tmp_path)
    memory.publish()
    inventory = db.read_json(tmp_path / "config/sources.json")
    inventory.pop("demo-latest")
    inventory["demo-stable"]["disabled"] = True
    write(tmp_path / "config/sources.json", inventory)
    db.build_database(tmp_path)
    memory.publish()
    records = memory.read_records(memory.document(memory.active()))
    source = next(
        row["data"] for row in records if row["kind"] == "source" and row["data"]["url"] == SOURCE
    )
    assert source["enabled"] is False
    assert any(
        row["kind"] == "history_release" and row["data"]["source_url"] == SOURCE for row in records
    )
    assert len(memory.sql("SELECT * FROM catalog_generations")) == 2


def test_interrupted_purge_retries_on_unchanged_publication(tmp_path, monkeypatch):
    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    paired_catalog(tmp_path)
    memory.publish()
    remove_demo(tmp_path)
    delete = memory.s3.delete_objects
    monkeypatch.setattr(
        memory.s3,
        "delete_objects",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Delete interrupted")),
    )
    with pytest.raises(RuntimeError, match="Delete interrupted"):
        memory.publish()
    assert memory.document(memory.active())["source_purge_pending"]
    monkeypatch.setattr(memory.s3, "delete_objects", delete)
    memory.publish()
    assert memory.document(memory.active())["source_purge_pending"] is False
    assert len(memory.sql("SELECT * FROM catalog_generations")) == 1


def test_partial_inventory_publishes_other_sources_without_losing_cached_data(
    tmp_path, monkeypatch
):
    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    paired_catalog(tmp_path)
    memory.publish()
    inventory = db.read_json(tmp_path / "config/sources.json")
    before = db.read_json(tmp_path / "internal/cache/history/archive.json")
    reconcile_archive(
        tmp_path, inventory, {KEPT: [release(3)], "https://gitlab.com/team/empty": []}
    )
    record_issue(tmp_path, SOURCE, "Release check", "HTTP 502")
    db.build_database(tmp_path)
    memory.publish()
    document = memory.document(memory.active())
    records = memory.read_records(document)
    assert document["complete"] is False
    assert any(
        row["kind"] == "history_release" and row["data"]["version"] == "v3" for row in records
    )
    after = db.read_json(tmp_path / "internal/cache/history/archive.json")
    assert {key: row for key, row in after["releases"].items() if row["source_url"] == SOURCE} == {
        key: row for key, row in before["releases"].items() if row["source_url"] == SOURCE
    }


def test_old_checkpoint_cleanup_keeps_disabled_sources(tmp_path):
    paired_catalog(tmp_path)
    inventory = db.read_json(tmp_path / "config/sources.json")
    inventory = {key: row for key, row in inventory.items() if not key.startswith("demo-")}
    inventory["kept-stable"]["disabled"] = True
    write(tmp_path / "config/sources.json", inventory)
    clean_inputs(tmp_path)
    archive = db.read_json(tmp_path / "internal/cache/history/archive.json")
    assert SOURCE not in archive["sources"]
    assert KEPT in archive["sources"]
    assert not (tmp_path / "internal/cache/bundles/demo-patch-bundles").exists()
    assert (tmp_path / "internal/cache/bundles/kept-patch-bundles").exists()


def test_discord_issues_are_last_deduplicated_and_notify_without_catalog_changes(tmp_path):
    record_issue(tmp_path, SOURCE, "Release check", "HTTP 404")
    record_issue(tmp_path, SOURCE, "Release check", "HTTP 404")
    record_issue(tmp_path, KEPT, "Release check", "HTTP 502")
    payloads = messages({}, {}, "https://example.test", read_issues(tmp_path))
    assert len(payloads) == 1
    fields = payloads[0]["embeds"][0]["fields"]
    assert fields[-1]["name"] == "Issues"
    assert "HTTP 404" in fields[-1]["value"] and "HTTP 502" in fields[-1]["value"]
    assert len(fields[-1]["value"].splitlines()) == 2
    assert payloads[0]["allowed_mentions"] == {"parse": []}


def test_issues_split_within_discord_limits_and_escape_provider_text():
    issues = [
        {
            "source": SOURCE,
            "phase": "Release check",
            "error": "**failure** " + "😀" * 400 + str(index),
        }
        for index in range(100)
    ]
    payloads = messages({}, {}, "https://example.test", issues)
    from scripts.notify_catalog_update import size

    for payload in payloads:
        embed = payload["embeds"][0]
        assert len(embed["fields"]) <= 25
        assert (
            size(embed["title"]) + sum(size(f["name"]) + size(f["value"]) for f in embed["fields"])
            <= 6000
        )
        assert all(size(field["value"]) <= 1024 for field in embed["fields"])
    assert "\\*\\*failure\\*\\*" in json.dumps(payloads).replace("\\\\", "\\")


def test_extraction_issues_only_report_attempts_from_current_run(tmp_path):
    folder = fixture(tmp_path)
    write(
        folder / "demo-stable-extraction.json",
        {
            "attempted_at": "2030-01-02T00:00:00Z",
            "version": "v1",
            "status": "download_failure",
        },
    )
    write(
        folder / "demo-latest-extraction.json",
        {
            "attempted_at": "2029-01-01T00:00:00Z",
            "version": "v0",
            "status": "runtime_failure",
        },
    )
    extraction_issues(tmp_path, "2030-01-01T00:00:00+00:00")
    rows = read_issues(tmp_path)
    assert len(rows) == 1 and rows[0]["error"] == "v1: download_failure"


@pytest.mark.parametrize("failure", [404, 502, "timeout", "malformed_asset", "missing_identity"])
def test_discovery_reports_provider_errors_and_continues(tmp_path, monkeypatch, failure):
    import asyncio

    import httpx

    from scripts import discover_releases as discovery

    bad = "https://github.com/owner/bad"
    good = "https://github.com/owner/good"
    inventory = {"bad-stable": {"patches": bad}, "good-stable": {"patches": good}}
    write(tmp_path / "config/sources.json", inventory)
    reconcile_archive(tmp_path, inventory, {bad: [release(1)], good: [release(2)]})
    previous = db.read_json(tmp_path / "internal/cache/history/archive.json")

    def handle(request):
        if "/owner/bad/" in request.url.path:
            if failure == "timeout":
                raise httpx.ReadTimeout("upstream timeout", request=request)
            if failure == "malformed_asset":
                return httpx.Response(
                    200,
                    json=[
                        release(3, assets=[{"browser_download_url": "https://[invalid/patch.rvp"}])
                    ],
                )
            if failure == "missing_identity":
                return httpx.Response(200, json=[{"body": "Invalid release"}])
            return httpx.Response(failure)
        return httpx.Response(200, json=[release(3)])

    monkeypatch.setattr(discovery, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(discovery, "PATCH_BUNDLES_DIR", tmp_path / "internal/cache/bundles")
    monkeypatch.setattr(
        discovery, "METADATA_PATH", tmp_path / "internal/cache/bundle-run-metadata.json"
    )
    monkeypatch.setattr(
        discovery, "RELEASE_CACHE_FILE", tmp_path / "internal/cache/release_cache.json"
    )
    monkeypatch.setattr(discovery, "_load_sources_sync", lambda: inventory)
    monkeypatch.setattr(discovery, "MAX_RETRIES", 1)
    monkeypatch.setattr(discovery, "RELEASE_CACHE", {})
    monkeypatch.setattr(discovery, "METADATA_LOCK", asyncio.Lock())
    monkeypatch.setattr(discovery, "RELEASE_CACHE_LOCK", asyncio.Lock())
    monkeypatch.setattr(discovery, "HTTP_SEMAPHORE", asyncio.Semaphore(4))
    monkeypatch.setattr(
        discovery,
        "AsyncClient",
        lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handle), **kwargs),
    )
    assert asyncio.run(discovery.main()) == 0
    saved = db.read_json(tmp_path / "internal/cache/history/archive.json")
    assert saved["sources"][bad]["status"] == "incomplete"
    assert saved["sources"][good]["status"] == "complete"
    assert next(row for row in saved["releases"].values() if row["source_url"] == bad) == next(
        row for row in previous["releases"].values() if row["source_url"] == bad
    )
    rows = read_issues(tmp_path)
    assert len(rows) == 1
    expected = (
        "ReadTimeout"
        if failure == "timeout"
        else "Invalid upstream release data"
        if isinstance(failure, str)
        else f"HTTP {failure}"
    )
    assert rows[0]["error"] == expected


def test_stale_error_cannot_authorize_a_new_incomplete_inventory(tmp_path, monkeypatch):
    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    catalog(tmp_path)
    memory.publish()
    record_issue(tmp_path, SOURCE, "Release check", "HTTP 502")
    inventory = db.read_json(tmp_path / "config/sources.json")
    reconcile_archive(tmp_path, inventory, {"https://gitlab.com/team/empty": []})
    db.build_database(tmp_path)
    with pytest.raises(ValueError, match="incomplete"):
        memory.publish()


def test_source_removal_requires_fresh_snapshot_before_publication(tmp_path, monkeypatch):
    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    paired_catalog(tmp_path)
    memory.publish()
    original = memory.active()
    write(tmp_path / "config/sources.json", {})
    with pytest.raises(ValueError, match="rebuild before publication"):
        memory.publish()
    assert memory.active() == original


def test_partial_r2_delete_errors_leave_retry_marker(tmp_path, monkeypatch):
    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    paired_catalog(tmp_path)
    memory.publish()
    remove_demo(tmp_path)
    delete = memory.s3.delete_objects
    monkeypatch.setattr(
        memory.s3,
        "delete_objects",
        lambda **kwargs: {
            "Errors": [{"Key": kwargs["Delete"]["Objects"][0]["Key"], "Code": "AccessDenied"}]
        },
    )
    with pytest.raises(RuntimeError, match="could not delete"):
        memory.publish()
    assert memory.document(memory.active())["source_purge_pending"]
    monkeypatch.setattr(memory.s3, "delete_objects", delete)
    memory.publish()
    assert memory.document(memory.active())["source_purge_pending"] is False


def test_repeated_failure_refreshes_timestamp_and_deduplicates_api_alias(tmp_path):
    from datetime import UTC, datetime

    record_issue(tmp_path, SOURCE, "Release check", "HTTP 502")
    checked = datetime.now(UTC).isoformat()
    record_issue(tmp_path, "https://api.github.com/repos/owner/demo", "Release check", "HTTP 502")
    rows = read_issues(tmp_path)
    assert len(rows) == 1
    assert rows[0]["source"] == SOURCE and rows[0]["observed_at"] >= checked


def test_invalid_inventory_cannot_purge_caches(tmp_path):
    paired_catalog(tmp_path)
    path = tmp_path / "internal/cache/history/archive.json"
    previous = path.read_bytes()
    write(tmp_path / "config/sources.json", {"typo": {}})
    with pytest.raises(ValueError, match="requires a repository"):
        clean_inputs(tmp_path)
    assert path.read_bytes() == previous


def test_orphaned_source_cache_is_not_saved_in_future_checkpoint(tmp_path):
    paired_catalog(tmp_path)
    orphan = tmp_path / "internal/cache/bundles/removed-patch-bundles"
    write(orphan / "removed-stable-patches-list.json", {"patches": [{"name": "Old patch"}]})
    clean_inputs(tmp_path)
    assert not orphan.exists()


def test_removed_entities_are_absent_from_public_id_and_availability_indexes(tmp_path):
    paired_catalog(tmp_path)
    manifest = remove_demo(tmp_path)
    folder = tmp_path / "database" / manifest["snapshot"]
    public_ids = db.read_json(folder / "ids.json")
    assert not any("com.only" in key for key in public_ids["package"])
    assert db.read_json(folder / "availability.json")["retired_history"] == []
    private_ids = db.read_json(tmp_path / "database/ids.json")
    assert any("com.only" in key for key in private_ids["package"])


def test_disabling_source_preserves_cached_exclusive_app_metadata(tmp_path):
    paired_catalog(tmp_path)
    inventory = db.read_json(tmp_path / "config/sources.json")
    for key, row in inventory.items():
        if key.startswith("demo-"):
            row["disabled"] = True
    write(tmp_path / "config/sources.json", inventory)
    db.build_database(tmp_path)
    assert (
        db.read_json(tmp_path / "database/app-metadata.json")["apps"]["com.only"]["app_name"]
        == "Exclusive"
    )


def test_failed_parser_still_collects_bundle_issues(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from scripts import run_catalog_refresh as refresh

    folder = fixture(tmp_path)
    wrapper = tmp_path / "bundle-parser/gradlew"
    wrapper.parent.mkdir()
    wrapper.write_text("")
    monkeypatch.setattr(refresh, "restore_catalog_cache", lambda root: None)
    monkeypatch.setattr(refresh.subprocess, "run", lambda *args, **kwargs: None)
    state = refresh.Refresh(tmp_path, cloudflare=True)

    def phase(kind, command, **kwargs):
        if kind == "PATCHES":
            write(
                folder / "demo-stable-extraction.json",
                {
                    "attempted_at": datetime.now(UTC).isoformat(),
                    "version": "v1",
                    "status": "download_failure",
                },
            )
            raise RuntimeError("Parser stopped")

    monkeypatch.setattr(state, "phase", phase)
    with pytest.raises(RuntimeError, match="Parser stopped"):
        state._run()
    assert any(row["phase"] == "Patch extraction" for row in read_issues(tmp_path))


def test_removed_unpublished_source_also_cleans_orphaned_r2_data(tmp_path, monkeypatch):
    import hashlib

    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    catalog(tmp_path)
    memory.publish()
    temporary = "https://github.com/owner/unpublished"
    archive_path = tmp_path / "internal/cache/history/archive.json"
    saved = db.read_json(archive_path)
    saved["sources"][temporary] = {"source_url": temporary, "status": "incomplete"}
    write(archive_path, saved)
    orphan_key = "objects/" + hashlib.sha256(b"unpublished source data").hexdigest()
    memory.put(orphan_key, b"unpublished source data")
    db.build_database(tmp_path)
    assert (tmp_path / "internal/cache/source-removals.json").exists()
    memory.publish()
    assert orphan_key not in memory.objects
    assert not (tmp_path / "internal/cache/source-removals.json").exists()


def test_legacy_generation_remains_verifiable(tmp_path, monkeypatch):
    import hashlib

    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    catalog(tmp_path)
    memory.publish()
    document = memory.document(memory.active())
    document.pop("identity_digest")
    document.pop("refresh_inputs")
    records_hash = hashlib.sha256(cloud.encode(document["record_blocks"])).hexdigest()
    generation = hashlib.sha256(
        cloud.encode([document["base_generation"], records_hash])
    ).hexdigest()[:24]
    document["manifest"].update(generation=generation, snapshot="snapshots/" + generation)
    memory.verify_document(document)


@pytest.mark.parametrize("interrupt", [False, True])
def test_noop_source_purge_clears_abandoned_version_boundaries(tmp_path, monkeypatch, interrupt):
    from test_cloudflare_catalog import archive

    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    catalog(tmp_path)
    memory.publish()
    original = memory.active()
    row = next(
        row
        for row in memory.read_records(memory.document(original))
        if row["kind"] == "source"
    )
    abandoned = original["seq"] + 1
    memory.sql(
        "INSERT INTO catalog_generations(seq,generation,manifest_key,status)"
        " VALUES(?,?,?,'staging')",
        [abandoned, "f" * 24, "generations/" + "f" * 24 + ".json"],
    )
    changed = {**row["data"], "name": "Unpublished change"}
    memory.insert_rows(
        [
            (
                row["kind"],
                row["key"],
                abandoned,
                cloud.encode(changed).decode(),
                cloud.record_hash(changed),
            )
        ]
    )
    # Removing an unpublished source requests cleanup without changing the catalog.
    write(
        tmp_path / "internal/cache/source-removals.json",
        ["https://github.com/owner/unpublished"],
    )
    if interrupt:
        original_sql = memory.sql

        def interrupted_sql(statement, params=()):
            if statement.startswith("DELETE FROM catalog_records WHERE NOT"):
                raise RuntimeError("Interrupted source purge")
            return original_sql(statement, params)

        monkeypatch.setattr(memory, "sql", interrupted_sql)
        with pytest.raises(RuntimeError, match="Interrupted source purge"):
            memory.publish()
        monkeypatch.setattr(memory, "sql", original_sql)
    else:
        memory.publish()
        assert memory.sql("SELECT seq FROM catalog_generations") == [{"seq": original["seq"]}]
        assert all(row["to_seq"] is None for row in memory.sql("SELECT * FROM catalog_records"))
    assert memory.active() == original
    # A subsequent update must not lose unchanged rows when the discarded seq is reused.
    archive(tmp_path, [release(1, body="Next refresh notes")])
    memory.publish()
    memory.verify_database(memory.active()["seq"], memory.document(memory.active()))
