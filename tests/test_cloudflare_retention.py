import gzip
import hashlib
import json
from copy import deepcopy

import pytest
from test_cloudflare_catalog import MemoryCloud, archive, catalog, release

from scripts import cloudflare_catalog as cloud
from scripts import cloudflare_checkpoint as checkpoint


def test_timestamp_changes_reuse_stable_compressed_patch_blocks(tmp_path, monkeypatch):
    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    stable = [
        {"kind": "patch", "key": str(index),
         "data": {"id": index, "description": "Patch metadata " * 200}}
        for index in range(600)
    ]
    mutable = [
        {"kind": "history_source", "key": str(index), "data": {"checked_at": "before"}}
        for index in range(256)
    ]
    blocks = memory.upload_records(stable + mutable, set())
    assert all(memory.objects["objects/" + digest].startswith(b"\x1f\x8b") for digest in blocks)
    stable_blocks = {
        digest for digest in blocks
        if json.loads(gzip.decompress(memory.objects["objects/" + digest]))[0]["kind"] == "patch"
    }
    changed = deepcopy(mutable)
    for item in changed:
        item["data"]["checked_at"] = "after"
    second = memory.upload_records(stable + changed, set(blocks))
    assert stable_blocks <= set(second)
    loaded = memory.read_records({"record_blocks": second, "record_block_encoding": "gzip"})
    checked = {row["data"]["checked_at"] for row in loaded if row["kind"] == "history_source"}
    assert checked == {"after"}
    assert sorted(row["data"]["id"] for row in loaded if row["kind"] == "patch") == list(range(600))
    assert sum(len(memory.objects["objects/" + digest]) for digest in second) < 100_000


def test_legacy_uncompressed_record_backups_remain_verifiable(tmp_path, monkeypatch):
    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    catalog(tmp_path)
    memory.publish()
    document = memory.document(memory.active())
    expected = memory.read_records(document)
    legacy = deepcopy(document)
    legacy["record_blocks"] = [memory.blob(cloud.encode(expected), set())]
    legacy.pop("record_block_encoding")
    legacy.pop("record_objects")
    legacy.pop("refresh_inputs")
    digest = hashlib.sha256(cloud.encode(legacy["record_blocks"])).hexdigest()
    generation = hashlib.sha256(cloud.encode(
        [legacy["base_generation"], digest, legacy["identity_digest"]]
    )).hexdigest()[:24]
    legacy["manifest"].update(generation=generation, snapshot="snapshots/" + generation)
    assert memory.read_records(legacy) == expected
    memory.verify_document(legacy)
    memory.verify_database(memory.active()["seq"], legacy)


def test_automatic_retention_is_bounded_and_preserves_history_and_latest_checkpoint(
    tmp_path, monkeypatch
):
    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    monkeypatch.setenv("CATALOG_RETENTION_GENERATIONS", "2")
    catalog(tmp_path)
    checkpoint_file = tmp_path / "internal/cache/progress.json"
    checkpoint_file.write_text('{"progress":1}')
    memory.checkpoint()
    saved = checkpoint.retained_keys(memory, tmp_path)
    for index in range(4):
        archive(tmp_path, [release(1, body=str(index)), release(2)])
        memory.publish()
    generations = memory.sql("SELECT * FROM catalog_generations ORDER BY seq")
    assert len(generations) == 2
    assert saved <= set(memory.objects)
    for row in generations:
        document = memory.verify_document(memory.document(row))
        memory.verify_database(row["seq"], document)
        assert len([item for item in memory.read_records(document)
                    if item["kind"] == "history_release"]) == 2
    assert not any(key.startswith("generations/") and key not in
                   {row["manifest_key"] for row in generations} for key in memory.objects)


def test_fast_retention_never_downloads_record_blocks_and_keeps_large_documents(
    tmp_path, monkeypatch
):
    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    catalog(tmp_path)
    archive(tmp_path, [release(1, body="Large original notes\n" * 100_000)])
    memory.publish()
    current = memory.active()
    document = memory.document(current)
    # Simulate a legacy generation with no explicit external object index.
    document.pop("record_objects")
    memory.put(current["manifest_key"], cloud.encode(document))
    keys = {row["document_key"] for row in
            (json.loads(item["data"]) for item in memory.sql(
                "SELECT data FROM catalog_records WHERE kind='history_release'"))}
    get = memory.get
    reads = []
    memory.get = lambda key: (reads.append(key), get(key))[-1]
    memory.prune("PRUNE", verify=False)
    assert not set(reads) & {"objects/" + digest for digest in document["record_blocks"]}
    assert {"objects/" + digest for digest in keys} <= set(memory.objects)
    memory.get = get
    memory.verify_document(document)


def test_retention_delete_failures_leave_active_catalog_recoverable(tmp_path, monkeypatch):
    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    catalog(tmp_path)
    memory.publish()
    active = memory.active()
    orphan = "objects/" + "f" * 64
    memory.objects[orphan] = b"orphan"
    delete = memory.s3.delete_objects
    memory.s3.delete_objects = lambda **kwargs: {"Errors": [{"Key": orphan}]}
    with pytest.raises(RuntimeError, match="retention"):
        memory.prune("PRUNE", verify=False)
    assert memory.active() == active
    memory.verify_document(memory.document(active))
    memory.s3.delete_objects = delete
    memory.prune("PRUNE", verify=False)
    assert orphan not in memory.objects


def test_unchanged_publication_does_not_issue_retention_write_queries(tmp_path, monkeypatch):
    memory = MemoryCloud(tmp_path)
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    catalog(tmp_path)
    memory.publish()
    statements = []
    sql = memory.sql
    memory.sql = lambda query, params=(): (statements.append(query), sql(query, params))[-1]
    memory.publish()
    assert not any(query.startswith(("INSERT", "UPDATE", "DELETE")) for query in statements)
