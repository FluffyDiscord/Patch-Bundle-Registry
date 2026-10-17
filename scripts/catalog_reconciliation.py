"""Repair retained catalog aliases without discarding distinct historical artifacts."""

import re

from scripts.extraction_policy import patch_completeness_current
from scripts.release_assets import auxiliary_bundle, canonical_bundle_type


def reconcile_retained_records(records):
    bundles = {key: row for (kind, key), row in records.items() if kind == "bundle"}
    patches_by_bundle = {}
    for (kind, _), row in records.items():
        if kind == "patch":
            patches_by_bundle.setdefault(row["bundle_id"], []).append(row)
    metadata_bundles = set(patches_by_bundle)
    canonical = {}
    aliases = {}
    invalid = set()
    outdated = set()
    for key, row in sorted(
        bundles.items(), key=lambda item: (item[1].get("availability") == "unavailable", item[0])
    ):
        if auxiliary_bundle({"url": row.get("download_url") or ""}):
            invalid.add(key)
            continue
        if row.get("metadata_status") == "verified" and not patch_completeness_current(
            canonical_bundle_type(row.get("bundle_type")),
            row.get("patch_count"),
            row.get("patch_completeness_version", 0),
            patches_by_bundle.get(key, []),
        ):
            outdated.add(key)
            bundles[key] = row = {
                **row,
                "metadata_status": "unverified",
                "patch_count": 0,
                "need_patches_update": True,
            }
        file_hash = row.get("file_hash")
        # Without a binary hash, matching URLs cannot prove artifact identity.
        if not isinstance(file_hash, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", file_hash):
            continue
        identity = (
            row["source_id"],
            row["version"],
            bool(row.get("is_prerelease")),
            row.get("download_url"),
            file_hash,
            canonical_bundle_type(row.get("bundle_type")),
        )
        if identity in canonical:
            selected_key = canonical[identity]
            selected = bundles[selected_key]
            selected_metadata = (
                selected.get("patch_list_available") or selected_key in metadata_bundles
            )
            available_metadata = row.get("patch_list_available") or key in metadata_bundles
            # An older alias can contain a fuller verified extraction of the same binary.
            # Keep it until the selected identity covers at least that many patches.
            selected_count = max(selected.get("patch_count") or 0,
                                 len(patches_by_bundle.get(selected_key, [])))
            available_count = max(row.get("patch_count") or 0,
                                  len(patches_by_bundle.get(key, [])))
            selected_covers_verified = selected.get("metadata_status") == "verified" and (
                row.get("metadata_status") != "verified" or selected_count >= available_count
            )
            if selected_covers_verified or (
                row.get("metadata_status") != "verified"
                and (selected_metadata or not available_metadata)
            ):
                aliases[key] = selected_key
        else:
            canonical[identity] = key
    removed = invalid | aliases.keys()
    removed_patches = {
        key
        for (kind, key), row in records.items()
        if kind == "patch" and row["bundle_id"] in removed
    }
    result = {}
    for (kind, key), row in records.items():
        if kind == "bundle" and key in removed:
            continue
        if kind == "patch" and key in removed_patches:
            continue
        if kind == "patch_package" and row["patch_id"] in removed_patches:
            continue
        if kind == "bundle":
            row = bundles[key]
        elif kind == "patch" and row["bundle_id"] in outdated:
            row = {**row, "metadata_status": "unverified"}
        if kind == "history_release":
            row = {
                **row,
                "bundle_ids": sorted(
                    {
                        aliases.get(identifier, identifier)
                        for identifier in row.get("bundle_ids", [])
                        if identifier not in invalid
                        and aliases.get(identifier, identifier) in bundles
                    }
                ),
            }
        result[(kind, key)] = row
    return result
