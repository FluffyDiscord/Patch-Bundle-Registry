"""Cloudflare operations used by reviewed GitHub Actions; never called by ordinary tests."""

import argparse
import hashlib
import json
import os
import re
import time
from collections import Counter
from pathlib import Path

import httpx

from scripts.catalog_records import catalog_records, encode, record_hash
from scripts.cloudflare_storage import (
    REFRESH_FILES,
    REFRESH_ROOT_FILES,
    BlobCache,
    R2Reader,
    pack_refresh,
    unpack_refresh,
)
from scripts.git_hosts import normalize_source
from scripts.refresh_issues import read_issues
from scripts.source_cleanup import clean_inputs, clean_snapshots, configured_sources, filter_records
from scripts.source_policy import disabled_sources, source_channels

ROOT = Path(__file__).resolve().parents[1]


def merge_ids(*registries):
    merged = {}
    for registry in registries:
        if not isinstance(registry, dict):
            raise ValueError("Invalid catalog ID registry")
        for table, mapping in registry.items():
            if not isinstance(mapping, dict):
                raise ValueError("Invalid catalog ID mapping")
            target = merged.setdefault(table, {})
            owners = {value: key for key, value in target.items()}
            for key, value in mapping.items():
                if type(value) is not int or value < 1:
                    raise ValueError("Invalid catalog numeric ID")
                if (key in target and target[key] != value) or (
                    value in owners and owners[value] != key
                ):
                    raise ValueError("Catalog conflicts with published numeric IDs")
                target[key] = value
                owners[value] = key
    return merged


class Cloud:
    def __init__(self):
        import boto3
        from botocore.config import Config
        from botocore.exceptions import (
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            IncompleteReadError,
            ReadTimeoutError,
            ResponseStreamingError,
        )

        self.account = os.environ["CLOUDFLARE_ACCOUNT_ID"]
        self.database = os.environ.get("CATALOG_D1_ID")
        self.bucket = os.environ.get("CATALOG_R2_BUCKET") or "patch-bundle-registry"
        self.http = httpx.Client(
            base_url="https://api.cloudflare.com/client/v4/accounts/" + self.account,
            headers={"Authorization": "Bearer " + os.environ["CLOUDFLARE_API_TOKEN"]},
            timeout=60,
        )
        self.s3 = None
        self.s3_reads = None
        if os.environ.get("R2_ACCESS_KEY_ID"):
            options = {
                "endpoint_url": "https://" + self.account + ".r2.cloudflarestorage.com",
                "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
                "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
                "region_name": "auto",
            }
            self.s3 = boto3.client(
                "s3",
                **options,
                config=Config(
                    retries={"max_attempts": 5, "mode": "standard"}, max_pool_connections=16
                ),
            )
            # Shared R2Reader policy must see failures before the SDK retries
            # them independently. Writes retain the normal SDK retry policy.
            self.s3_reads = boto3.client(
                "s3",
                **options,
                config=Config(
                    retries={"total_max_attempts": 1, "mode": "standard"},
                    max_pool_connections=4,
                ),
            )
        self._r2_reader = R2Reader(
            self.read_object,
            workers=int(os.environ.get("CATALOG_R2_WORKERS") or "8"),
            transport_errors=(
                ConnectionClosedError,
                ConnectTimeoutError,
                EndpointConnectionError,
                IncompleteReadError,
                ReadTimeoutError,
                ResponseStreamingError,
            ),
        )

    def transfers(self):
        if not hasattr(self, "_transfers"):
            self._transfers = BlobCache(
                ROOT / "internal/r2-objects",
                self.get,
                self.put,
                workers=int(os.environ.get("CATALOG_R2_WORKERS") or "8"),
            )
        return self._transfers

    def report(self, operation, started):
        cache = self.transfers()
        cache.trim(int(os.environ.get("CATALOG_OBJECT_CACHE_BYTES") or str(1024**3)))
        metrics = {
            "operation": operation,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            **dict(cache.metrics),
            **getattr(self, "publication_metrics", {}),
            "r2_read_retries": getattr(getattr(self, "_r2_reader", None), "retries", 0),
        }
        path = ROOT / "internal/cache/cloudflare-performance.json"
        try:
            history = json.loads(path.read_bytes())
            if not isinstance(history, list):
                history = []
        except (OSError, ValueError):
            history = []
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encode([*history[-23:], metrics]))
        print(json.dumps({"cloudflare_performance": metrics}), flush=True)

    def api(self, method, path, **kwargs):
        for attempt in range(5):
            response = self.http.request(method, path, **kwargs)
            if response.status_code in (429, 500, 502, 503, 504):
                # Cloudflare sends seconds; an HTTP-date or junk value must not stop retries.
                header = response.headers.get("Retry-After", "").strip()
                requested = float(header) if header.isascii() and header.isdigit() else 0.0
                time.sleep(max(requested, 2**attempt))
                continue
            if not response.is_success:
                raise RuntimeError("Cloudflare API failed with HTTP " + str(response.status_code))
            result = response.json()
            if not result.get("success"):
                raise RuntimeError("Cloudflare API returned unsuccessful status")
            return result["result"]
        raise RuntimeError("Cloudflare API retries exhausted")

    def sql(self, sql, params=()):
        self.transfers().add("d1_requests")
        result = self.api(
            "POST",
            "/d1/database/" + self.database + "/query",
            json={"sql": sql, "params": list(params)},
        )
        if not all(row.get("success") for row in result):
            raise RuntimeError("D1 query failed")
        return result[0].get("results", [])

    def put(self, key, body):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body)

    def read_object(self, key):
        body = self.s3_reads.get_object(Bucket=self.bucket, Key=key)["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def get(self, key):
        return self._r2_reader.read(key)

    def blob(self, body, known):
        return self.transfers().upload(body, known)

    def active(self):
        rows = self.sql(
            "SELECT g.* FROM catalog_state s JOIN catalog_generations g ON g.seq=s.active_seq"
            " WHERE s.id=1 AND g.status='ready'"
        )
        return rows[0] if rows else None

    def document(self, generation):
        body = self.get(generation["manifest_key"])
        document = json.loads(body)
        if document["manifest"]["generation"] != generation["generation"]:
            raise ValueError("Generation manifest mismatch")
        return document

    def verified_blob(self, digest, fresh=False):
        return self.transfers().read(digest, fresh=fresh)

    @staticmethod
    def object_digests(document):
        return (
            set(document["files"].values())
            | set(document["record_blocks"])
            | {document["ids"]}
            | ({document["refresh_inputs"]} if document.get("refresh_inputs") else set())
        )

    def checkpoint(self):
        from scripts.cloudflare_checkpoint import save

        save(self, ROOT)

    def restore_checkpoint(self):
        from scripts.cloudflare_checkpoint import restore

        restore(self, ROOT, merge_ids, clean_inputs)

    def download_catalog(self, document, refresh_inputs=False):
        manifest = document["manifest"]
        identifiers = json.loads(self.verified_blob(document["ids"]))
        tips = self.sql(
            "SELECT * FROM catalog_generations WHERE status='ready' ORDER BY seq DESC LIMIT 1"
        )
        if tips:
            # Rollback changes the public pointer, but newer records still own their IDs.
            tip = self.document(tips[0])
            identifiers = merge_ids(identifiers, json.loads(self.verified_blob(tip["ids"])))
        else:
            identifiers = merge_ids(identifiers)
        if refresh_inputs:
            self.download_refresh_inputs(document, identifiers)
            return
        target = ROOT / "database" / manifest["snapshot"]
        target.mkdir(parents=True, exist_ok=True)
        self.transfers().prefetch(document["files"].values())
        for name, digest in document["files"].items():
            path = (target / name).resolve()
            if not path.is_relative_to(target.resolve()):
                raise ValueError("Unsafe snapshot path")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.verified_blob(digest))
        (ROOT / "database/manifest.json").write_bytes(encode(manifest))
        (ROOT / "database/ids.json").write_bytes(encode(identifiers))

    @staticmethod
    def history_archive(records):
        computed = {
            "bundle_ids",
            "runtimes",
            "bundle_types",
            "is_latest",
            "channels",
            "metadata_document_url",
            "document_key",
        }
        return {
            "releases": {
                item["key"]: {
                    key: value for key, value in item["data"].items() if key not in computed
                }
                for item in records
                if item["kind"] == "history_release"
            },
            "sources": {
                item["key"]: item["data"] for item in records if item["kind"] == "history_source"
            },
        }

    def refresh_archive(self, document, base, records):
        prefix = "database/" + document["manifest"]["snapshot"] + "/"
        entries = {
            prefix + name: (base / name).read_bytes()
            for name in REFRESH_FILES
            if name in document["files"]
        }
        entries["database/manifest.json"] = encode(document["manifest"])
        entries["database/ids.json"] = (ROOT / "database/ids.json").read_bytes()
        entries["internal/cache/history/archive.json"] = encode(self.history_archive(records))
        for name in REFRESH_ROOT_FILES:
            if (ROOT / name).is_file():
                entries[name] = (ROOT / name).read_bytes()
        return pack_refresh(entries, document["manifest"]["generation"])

    def refresh_entries(self, document):
        manifest = document["manifest"]
        if (
            not re.fullmatch(r"[a-f0-9]{24}", manifest["generation"])
            or manifest["snapshot"] != "snapshots/" + manifest["generation"]
        ):
            raise ValueError("Unsafe refresh snapshot path")
        prefix = "database/" + manifest["snapshot"] + "/"
        required = {prefix + name for name in REFRESH_FILES if name in document["files"]}
        required |= {
            "database/manifest.json",
            "database/ids.json",
            "internal/cache/history/archive.json",
        }
        entries = unpack_refresh(
            self.verified_blob(document["refresh_inputs"]),
            manifest["generation"],
            required | set(REFRESH_ROOT_FILES),
        )
        supplemental = {
            prefix + name for name in ("bundles.json", "packages.json") if name in document["files"]
        }
        if (
            not (required - supplemental) <= entries.keys()
            or json.loads(entries["database/manifest.json"]) != manifest
        ):
            raise ValueError("Refresh archive manifest mismatch")
        # Initial compact archives omitted indexes needed by metadata refresh.
        # Fetch only those missing indexes and verify them against the generation.
        for name in supplemental - entries.keys():
            entries[name] = self.verified_blob(document["files"][name[len(prefix) :]])
        for name, body in entries.items():
            if (
                name.startswith(prefix)
                and hashlib.sha256(body).hexdigest() != document["files"][name[len(prefix) :]]
            ):
                raise ValueError("Refresh archive snapshot integrity mismatch")
        if hashlib.sha256(entries["database/ids.json"]).hexdigest() != document["ids"]:
            raise ValueError("Refresh archive ID integrity mismatch")
        return entries

    def download_refresh_inputs(self, document, identifiers):
        manifest = document["manifest"]
        if (
            not re.fullmatch(r"[a-f0-9]{24}", manifest["generation"])
            or manifest["snapshot"] != "snapshots/" + manifest["generation"]
        ):
            raise ValueError("Unsafe refresh snapshot path")
        prefix = "database/" + manifest["snapshot"] + "/"
        if document.get("refresh_inputs"):
            entries = self.refresh_entries(document)
            print("Restoring compact generation-bound refresh inputs", flush=True)
        else:
            # Existing generations need no reseeding or republishing to use the fast path.
            names = [name for name in REFRESH_FILES if name in document["files"]]
            self.transfers().prefetch(document["files"][name] for name in names)
            entries = {prefix + name: self.verified_blob(document["files"][name]) for name in names}
            entries["database/manifest.json"] = encode(manifest)
            entries["internal/cache/history/archive.json"] = encode(
                self.history_archive(self.read_records(document))
            )
            print(
                "Restoring legacy generation input subset; skipping generated API shards",
                flush=True,
            )
        # Validate every payload and ID merge before replacing any restored input.
        entries["database/ids.json"] = encode(identifiers)
        for body in entries.values():
            json.loads(body)
        for name, body in entries.items():
            target = (ROOT / name).resolve()
            if not target.is_relative_to(ROOT.resolve()):
                raise ValueError("Unsafe refresh input path")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".restore-tmp")
            temporary.write_bytes(body)
            temporary.replace(target)

    def publish(self, require_complete=True):
        clean_inputs(ROOT)
        manifest, records = catalog_records(ROOT)
        configured, _ = configured_sources(ROOT)
        inventory_complete = True
        snapshot_sources = json.loads(
            (ROOT / "database" / manifest["snapshot"] / "sources.json").read_bytes()
        )
        if any(row["url"].lower() not in configured for row in snapshot_sources):
            raise ValueError("Catalog contains removed sources; rebuild before publication")
        base_generation = manifest["generation"]
        if require_complete:
            inventory = source_channels(
                json.loads((ROOT / "config/sources.json").read_text(encoding="utf-8")), ROOT
            )
            required = {
                normalize_source(row["patches"], ROOT).lower() for row in inventory.values()
            } - disabled_sources(inventory, ROOT).keys()
            states = {
                key.lower(): row for (kind, key), row in records.items() if kind == "history_source"
            }
            issue_sources = {
                normalize_source(row["source"], ROOT).lower()
                for row in read_issues(ROOT)
                if row["phase"] == "Release check"
                and row.get("observed_at", "")
                >= states.get(normalize_source(row["source"], ROOT).lower(), {}).get(
                    "checked_at", "~"
                )
            }
            inventory_complete = all(
                states.get(url, {}).get("status") == "complete" for url in required
            )
            if any(
                not states.get(url, {}).get("configured")
                or not states[url].get("required", True)
                or (
                    states[url].get("status") != "complete"
                    and not (states[url].get("status") == "incomplete" and url in issue_sources)
                )
                for url in required
            ):
                raise ValueError("Historical reconciliation incomplete; publication refused")
        active = self.active()
        if active and not require_complete:
            raise ValueError("Seed is only allowed for an empty catalog; use normal publication")
        active_document = self.document(active) if active else None
        if active_document and active_document.get("source_purge_pending"):
            # A prior purge may have discarded generation rows before removing
            # their record versions. Finish it before choosing or reusing a seq.
            self.finish_source_purge(active_document)
        tips = self.sql(
            "SELECT * FROM catalog_generations WHERE status='ready' ORDER BY seq DESC LIMIT 1"
        )
        previous = (
            active_document
            if tips and active and tips[0]["seq"] == active["seq"]
            else self.document(tips[0]) if tips else None
        )
        known = self.object_digests(previous) if previous else set()
        previous_records = self.read_records(previous) if previous else []
        removed = {
            (row["data"].get("url") or row["data"].get("source_url") or row["key"]).lower()
            for row in previous_records
            if row["kind"] in ("source", "history_source")
        } - configured
        # Retain upstream release deletions for configured (including disabled) sources.
        # Removing the source itself is an explicit purge.
        for item in previous_records:
            key = (item["kind"], item["key"])
            if key in records or key[0] == "refresh_jobs":
                continue
            row = item["data"]
            if key[0] == "source":
                row = {**row, "enabled": False, "configured": False}
            elif key[0] == "history_source":
                row = {**row, "configured": False, "status": "removed"}
            elif key[0] in ("bundle", "history_release"):
                row = {**row, "is_latest": False, "availability": "unavailable", "channels": []}
            records[key] = row
        for item in previous_records:
            key = (item["kind"], item["key"])
            if key[0] == "history_release" and key in records:
                for field in ("bundle_ids", "runtimes", "bundle_types"):
                    records[key][field] = sorted(
                        set(records[key].get(field, [])) | set(item["data"].get(field, []))
                    )
        records = filter_records(ROOT, records)
        exported = [
            {"kind": kind, "key": key, "data": row} for (kind, key), row in sorted(records.items())
        ]
        blocks = {}
        for item in exported:
            bucket = hashlib.sha256((item["kind"] + "/" + item["key"]).encode()).hexdigest()[:2]
            blocks.setdefault(bucket, []).append(item)
        record_blocks = self.transfers().parallel(
            lambda item: self.blob(encode(item[1]), known), sorted(blocks.items())
        )
        record_digest = hashlib.sha256(encode(record_blocks)).hexdigest()
        identity_digest = hashlib.sha256((ROOT / "database/ids.json").read_bytes()).hexdigest()
        generation = hashlib.sha256(
            encode([manifest["generation"], record_digest, identity_digest])
        ).hexdigest()[:24]
        existing = self.sql(
            "SELECT * FROM catalog_generations WHERE generation=? AND status='ready'", [generation]
        )
        if existing and active and active["generation"] == generation:
            self.finish_source_purge(active_document)
            print("Catalog unchanged; no D1 writes or repeated full verification")
            return
        if existing:
            self.verify_document(self.document(existing[0]))
            self.sql("UPDATE catalog_state SET active_seq=? WHERE id=1", [existing[0]["seq"]])
            self.finish_source_purge(self.document(existing[0]))
            print("Reactivated verified existing generation")
            return
        if active and active["generation"] == generation:
            print("Catalog unchanged; no D1 writes")
            return
        seq = int(
            self.sql("SELECT COALESCE(MAX(seq),0)+1 AS next FROM catalog_generations")[0]["next"]
        )
        pending = self.sql("SELECT seq FROM catalog_generations WHERE status='staging'")
        for row in pending:
            abandoned = row["seq"]
            self.sql("DELETE FROM catalog_records WHERE from_seq=?", [abandoned])
            self.sql("UPDATE catalog_records SET to_seq=NULL WHERE to_seq=?", [abandoned])
            self.sql("DELETE FROM catalog_generations WHERE seq=?", [abandoned])
        base = ROOT / "database" / manifest["snapshot"]
        names = set(manifest["files"]) | {"history.json"}
        paths = {name: base / name for name in sorted(names)}
        for name, path in paths.items():
            if (
                name in manifest["files"]
                and hashlib.sha256(path.read_bytes()).hexdigest()
                != manifest["files"][name]["sha256"]
            ):
                raise ValueError("Local catalog integrity mismatch")
        files = self.transfers().upload_files(paths, known)
        manifest = {**manifest, "generation": generation, "snapshot": "snapshots/" + generation}
        manifest.pop("snapshot_ref", None)
        document = {
            "manifest": manifest,
            "base_generation": base_generation,
            "identity_digest": identity_digest,
            "files": files,
            "record_blocks": record_blocks,
            "ids": self.blob((ROOT / "database/ids.json").read_bytes(), known),
            "complete": require_complete and inventory_complete,
            "source_purge_pending": bool(removed)
            or (ROOT / "internal/cache/source-removals.json").exists()
            or bool(previous and previous.get("source_purge_pending")),
            "record_count": len(records),
        }
        document["refresh_inputs"] = self.blob(
            self.refresh_archive(document, base, exported), known
        )
        manifest_key = "generations/" + generation + ".json"
        self.put(manifest_key, encode(document))
        self.sql(
            "INSERT INTO catalog_generations(seq,generation,manifest_key,status)"
            " VALUES(?,?,?,'staging')",
            [seq, generation, manifest_key],
        )
        pending_rows = []
        prior = {
            (item["kind"], item["key"]): record_hash(item["data"]) for item in previous_records
        }
        writes = 0
        changed_kinds, metadata_only = Counter(), Counter()
        previous_data = {(item["kind"], item["key"]): item["data"] for item in previous_records}
        volatile = {"checked_at", "verified_at", "last_verified_at", "patch_refresh_attempted_at"}
        for (kind, key), row in sorted(records.items()):
            digest = record_hash(row)
            if prior.get((kind, key)) == digest:
                continue
            body = self.storage_row(kind, row)
            if len(body.encode()) > 1_900_000:
                raise ValueError("Record exceeds D1 row budget; publication refused")
            pending_rows.append((kind, key, seq, body, digest))
            writes += 1
            changed_kinds[kind] += 1
            old = previous_data.get((kind, key))
            if old is not None and {k: v for k, v in old.items() if k not in volatile} == {
                k: v for k, v in row.items() if k not in volatile
            }:
                metadata_only[kind] += 1
        # JSON input uses one binding; each inserted row still runs the version-closing trigger.
        self.insert_rows(pending_rows)
        self.publication_metrics = {
            "changed_records_by_kind": dict(changed_kinds),
            "timestamp_only_changes_by_kind": dict(metadata_only),
        }
        print(json.dumps(self.publication_metrics), flush=True)
        for kind, key in prior.keys() - records.keys():
            self.sql(
                "UPDATE catalog_records SET to_seq=? WHERE kind=? AND record_key=?"
                " AND to_seq IS NULL",
                [seq, kind, key],
            )
        count = self.sql(
            "SELECT COUNT(*) AS n FROM catalog_records WHERE from_seq<=?"
            " AND (to_seq IS NULL OR to_seq>?)",
            [seq, seq],
        )[0]["n"]
        if count != len(records):
            raise ValueError("D1 staged row count mismatch")
        # Ready generations were verified before activation. Re-read only new blobs here;
        # the explicit verify/recovery operations still check every retained object.
        previous_digests = self.object_digests(previous) if previous else set()
        self.verify_document(document, skip_digests=previous_digests)
        # Atomic activation. The workflow concurrency group is the single-writer lock.
        self.sql("UPDATE catalog_generations SET status='ready' WHERE seq=?", [seq])
        self.sql("UPDATE catalog_state SET active_seq=? WHERE id=1", [seq])
        self.finish_source_purge(document)
        print(json.dumps({"generation": generation, "records_changed": writes, "records": count}))

    def finish_source_purge(self, document):
        if not document.get("source_purge_pending") and not (
            ROOT / "internal/cache/source-removals.json"
        ).exists():
            return
        active = self.active()
        if not active or active["generation"] != document["manifest"]["generation"]:
            raise ValueError("Source purge requires the active verified generation")
        if not document.get("source_purge_pending"):
            document["source_purge_pending"] = True
            self.put(active["manifest_key"], encode(document))
        sequence = active["seq"]
        # Older snapshots contain removed data and cannot remain restorable. Keep
        # the newly verified snapshot and all records visible in that snapshot.
        self.sql("DELETE FROM catalog_generations WHERE seq<>?", [sequence])
        self.sql(
            "DELETE FROM catalog_records WHERE NOT (from_seq<=? AND (to_seq IS NULL OR to_seq>?))",
            [sequence, sequence],
        )
        # Discarded future versions must not expire rows retained by this snapshot
        # when their sequence numbers are reused by a later publication.
        self.sql("UPDATE catalog_records SET to_seq=NULL WHERE to_seq>?", [sequence])
        retained = {"objects/" + digest for digest in self.object_digests(document)}
        retained.add(active["manifest_key"])
        for row in self.read_records(document):
            if row["kind"] == "history_release" and len(encode(row["data"])) > 1_500_000:
                retained.add("objects/" + hashlib.sha256(encode(row["data"])).hexdigest())
        # A checkpoint written before removal must never restore deleted content.
        clean_inputs(ROOT)
        clean_snapshots(ROOT)
        (ROOT / "internal/cache/source-removals.json").unlink(missing_ok=True)
        self.checkpoint()
        from scripts.cloudflare_checkpoint import evict_key, retained_keys

        retained.update(retained_keys(self, ROOT))
        obsolete = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket):
            for item in page.get("Contents", []):
                key = item["Key"]
                if (
                    key.startswith(("objects/", "generations/", "checkpoints/"))
                    and key not in retained
                ):
                    obsolete.append(key)

        def delete(keys):
            result = self.s3.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
            )
            failed = {row["Key"] for row in result.get("Errors", [])}
            for key in set(keys) - failed:
                if key.startswith("objects/"):
                    self.transfers().path(key[8:]).unlink(missing_ok=True)
                evict_key(self, key)
            if failed:
                raise RuntimeError("R2 source purge could not delete all obsolete objects")

        self.transfers().parallel(
            delete, [obsolete[start : start + 1000] for start in range(0, len(obsolete), 1000)]
        )
        # Last write is the durable completion marker; a later publish retries
        # interrupted cleanup even when the catalog contents are unchanged.
        document["source_purge_pending"] = False
        self.put(active["manifest_key"], encode(document))
        print(f"Purged removed sources; deleted {len(obsolete)} obsolete R2 objects")

    def read_records(self, document):
        self.transfers().prefetch(document["record_blocks"])
        return [
            row
            for digest in document["record_blocks"]
            for row in json.loads(self.verified_blob(digest))
        ]

    def verify_document(self, document, skip_digests=()):
        record_digest = hashlib.sha256(encode(document["record_blocks"])).hexdigest()
        identity = [document["base_generation"], record_digest]
        if "identity_digest" in document:
            if document["identity_digest"] != document["ids"]:
                raise ValueError("Snapshot ID registry integrity mismatch")
            identity.append(document["identity_digest"])
        generation = hashlib.sha256(encode(identity)).hexdigest()[:24]
        if generation != document["manifest"]["generation"]:
            raise ValueError("Snapshot manifest integrity mismatch")
        if document["manifest"]["snapshot"] != "snapshots/" + generation:
            raise ValueError("Unsafe snapshot path")
        from scripts.generate_database import encoded

        if (
            hashlib.sha256(encoded(document["files"])).hexdigest()[:24]
            != document["base_generation"]
        ):
            raise ValueError("Snapshot base integrity mismatch")
        if set(document["files"]) != set(document["manifest"]["files"]) | {"history.json"}:
            raise ValueError("Snapshot file set mismatch")
        for name, metadata in document["manifest"]["files"].items():
            if document["files"].get(name) != metadata["sha256"]:
                raise ValueError("Snapshot file index mismatch")
        self.transfers().prefetch(self.object_digests(document) - set(skip_digests), fresh=True)
        if document.get("refresh_inputs"):
            self.refresh_entries(document)
        if not skip_digests:
            records = self.read_records(document)
            if len(records) != document["record_count"] or len(
                {(row["kind"], row["key"]) for row in records}
            ) != len(records):
                raise ValueError("Snapshot record count or identity mismatch")
            for row in records:
                body = encode(row["data"])
                if row["kind"] == "history_release" and len(body) > 1_500_000:
                    self.verified_blob(hashlib.sha256(body).hexdigest(), fresh=True)
        return document

    def verify_database(self, sequence, document):
        expected = {
            (row["kind"], row["key"]): record_hash(row["data"])
            for row in self.read_records(document)
        }
        after = ("", "")
        actual = {}
        while True:
            rows = self.sql(
                "SELECT kind,record_key,content_hash FROM catalog_records "
                "WHERE from_seq<=? AND (to_seq IS NULL OR to_seq>?) "
                "AND (kind>? OR (kind=? AND record_key>?)) ORDER BY kind,record_key LIMIT 1000",
                [sequence, sequence, after[0], after[0], after[1]],
            )
            for row in rows:
                actual[(row["kind"], row["record_key"])] = row["content_hash"]
            if len(rows) < 1000:
                break
            after = (rows[-1]["kind"], rows[-1]["record_key"])
        if actual != expected:
            raise ValueError("D1 generation does not match verified R2 records")

    def storage_row(self, kind, row):
        body = encode(row)
        if len(body) <= 1_500_000:
            return body.decode()
        if kind != "history_release":
            raise ValueError("Record exceeds D1 row budget; publication refused")
        digest = hashlib.sha256(body).hexdigest()
        self.blob(body, set())
        compact = {
            key: value
            for key, value in row.items()
            if key not in ("raw_metadata", "assets", "bundle_assets", "changelog", "release_notes")
        }
        compact["document_key"] = digest
        compact["metadata_document_url"] = "/history/releases/" + row["id"] + "/document"
        return encode(compact).decode()

    def rebuild(self, document, confirmation):
        if confirmation != "REBUILD":
            raise ValueError("Database rebuild requires --confirm REBUILD")
        self.verify_document(document)
        records = self.read_records(document)
        # Explicit disaster recovery only. Verified R2 data is loaded before D1 is cleared.
        self.sql("UPDATE catalog_state SET active_seq=NULL WHERE id=1")
        self.sql("DELETE FROM catalog_records")
        self.sql("DELETE FROM catalog_generations")
        manifest = document["manifest"]
        key = "generations/" + manifest["generation"] + ".json"
        self.sql(
            "INSERT INTO catalog_generations(seq,generation,manifest_key,status)"
            " VALUES(1,?,?,'staging')",
            [manifest["generation"], key],
        )
        batch = []
        for row in records:
            batch.append(
                (
                    row["kind"],
                    row["key"],
                    1,
                    self.storage_row(row["kind"], row["data"]),
                    record_hash(row["data"]),
                )
            )
            if len(batch) == 200:
                self.insert_rows(batch)
                batch = []
        if batch:
            self.insert_rows(batch)
        if self.sql("SELECT COUNT(*) AS n FROM catalog_records")[0]["n"] != len(records):
            raise ValueError("Rebuilt D1 count mismatch")
        self.sql("UPDATE catalog_generations SET status='ready' WHERE seq=1")
        self.sql("UPDATE catalog_state SET active_seq=1 WHERE id=1")
        print("Rebuilt verified generation " + manifest["generation"])

    def insert_rows(self, rows):
        batch, size = [], 2

        def flush():
            if not batch:
                return
            self.sql(
                "INSERT INTO catalog_records(kind,record_key,from_seq,data,content_hash) "
                "SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),"
                "json_extract(value,'$[2]'),json_extract(value,'$[3]'),json_extract(value,'$[4]') "
                "FROM json_each(?) WHERE 1 ON CONFLICT(kind,record_key,from_seq) DO NOTHING",
                [encode(batch).decode()],
            )
            self.transfers().add("d1_insert_batches")

        for row in rows:
            length = len(encode(row)) + 1
            if batch and (len(batch) >= 200 or size + length > 512_000):
                flush()
                batch, size = [], 2
            if length > 512_000:
                self.sql(
                    "INSERT INTO catalog_records(kind,record_key,from_seq,data,content_hash) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(kind,record_key,from_seq) DO NOTHING",
                    row,
                )
                self.transfers().add("d1_insert_batches")
                continue
            batch.append(row)
            size += length
        flush()

    def prune(self, confirmation):
        retain = max(2, int(os.environ.get("CATALOG_RETENTION_GENERATIONS") or "96"))
        generations = self.sql("SELECT * FROM catalog_generations ORDER BY seq DESC")
        active = self.active()
        keep = generations[:retain] + [
            row
            for row in generations
            if row["status"] == "staging" or (active and row["seq"] == active["seq"])
        ]
        keys = {row["manifest_key"] for row in keep}
        objects = set()
        for row in keep:
            document = self.verify_document(self.document(row))
            objects.update(self.object_digests(document))
            for record in self.read_records(document):
                if record["kind"] == "history_release" and len(encode(record["data"])) > 1_500_000:
                    objects.add(hashlib.sha256(encode(record["data"])).hexdigest())
        obsolete = [row for row in generations if row["manifest_key"] not in keys]
        from scripts.cloudflare_checkpoint import evict_key, retained_keys

        checkpoints = retained_keys(self, ROOT)
        orphaned = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket):
            for item in page.get("Contents", []):
                key = item["Key"]
                if (key.startswith("objects/") and key[8:] not in objects) or (
                    key.startswith("generations/") and key not in keys
                ) or (key.startswith("checkpoints/") and key not in checkpoints):
                    orphaned.append(key)
        print(
            json.dumps(
                {
                    "retained_generations": len(keys),
                    "old_generations": len(obsolete),
                    "orphaned_objects": len(orphaned),
                    "dry_run": confirmation != "PRUNE",
                }
            )
        )
        if confirmation != "PRUNE":
            return
        if not keep:
            raise ValueError("No recoverable generations; refusing retention cleanup")
        floor = min(row["seq"] for row in keep)
        self.sql("DELETE FROM catalog_records WHERE to_seq<=?", [floor])
        for row in obsolete:
            self.sql("DELETE FROM catalog_generations WHERE seq=?", [row["seq"]])
        for key in orphaned:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
            evict_key(self, key)

    def provision(self):
        name = os.environ.get("CATALOG_D1_NAME") or "patch-bundle-registry"
        databases = self.api("GET", "/d1/database", params={"name": name, "per_page": 100})
        found = [row for row in databases if row["name"] == name]
        database = found[0] if found else self.api("POST", "/d1/database", json={"name": name})
        buckets = self.api("GET", "/r2/buckets")["buckets"]
        if not any(row["name"] == self.bucket for row in buckets):
            self.api("POST", "/r2/buckets", json={"name": self.bucket})
        print("CATALOG_D1_ID=" + database["uuid"])
        print("CATALOG_R2_BUCKET=" + self.bucket)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        choices=[
            "provision",
            "publish",
            "seed",
            "download",
            "checkpoint",
            "resume",
            "verify",
            "restore",
            "rebuild",
            "prune",
        ],
    )
    parser.add_argument("--generation")
    parser.add_argument("--refresh-inputs", action="store_true")
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--confirm")
    args = parser.parse_args()
    cloud = Cloud()
    started = time.perf_counter()
    try:
        if args.operation == "provision":
            cloud.provision()
        elif args.operation in ("publish", "seed"):
            cloud.publish(require_complete=args.operation == "publish")
        elif args.operation == "checkpoint":
            cloud.checkpoint()
        elif args.operation == "prune":
            cloud.prune(args.confirm)
        elif args.operation == "resume":
            cloud.restore_checkpoint()
        else:
            active = cloud.active()
            if args.operation == "rebuild":
                if not args.generation:
                    raise ValueError("Rebuild requires a verified R2 generation ID")
                if not re.fullmatch(r"[a-f0-9]{24}", args.generation):
                    raise ValueError("Invalid generation ID")
                document = json.loads(cloud.get("generations/" + args.generation + ".json"))
                cloud.rebuild(document, args.confirm)
                return
            if args.generation:
                rows = cloud.sql(
                    "SELECT * FROM catalog_generations WHERE generation=? AND status='ready'",
                    [args.generation],
                )
                if not rows:
                    raise ValueError("Unknown ready generation")
                active = rows[0]
            if not active:
                raise ValueError("No active catalog")
            document = cloud.document(active)
            if args.require_complete and not document["complete"]:
                raise ValueError("Historical backfill is not complete")
            if args.operation == "verify":
                cloud.verify_document(document)
                cloud.verify_database(active["seq"], document)
                print(
                    json.dumps(
                        {"generation": active["generation"], "complete": document["complete"]}
                    )
                )
            elif args.operation == "download":
                cloud.download_catalog(document, refresh_inputs=args.refresh_inputs)
            elif args.operation == "restore":
                if args.confirm != "RESTORE":
                    raise ValueError("Restore requires --confirm RESTORE")
                cloud.verify_document(document)
                cloud.sql("UPDATE catalog_state SET active_seq=? WHERE id=1", [active["seq"]])
                print("Restored " + active["generation"])
    finally:
        try:
            cloud.report(args.operation, started)
        except (OSError, ValueError) as error:
            # Diagnostics and a disposable cache must not mask the operation's outcome.
            print("Could not save Cloudflare performance diagnostics: " + str(error), flush=True)


if __name__ == "__main__":
    main()
