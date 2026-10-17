from scripts.catalog_reconciliation import reconcile_retained_records


def bundle(identifier, file_hash="a" * 64, **extra):
    return {
        "id": 1,
        "catalog_id": identifier,
        "source_id": "source",
        "version": "v1",
        "bundle_type": "Morphe:V1",
        "download_url": "https://test/patches.mpp",
        "file_hash": file_hash,
        "metadata_status": "verified",
        **extra,
    }


def test_exact_artifact_aliases_are_removed_and_release_links_are_remapped():
    records = {
        ("bundle", "current"): bundle("current"),
        ("bundle", "retired"): bundle("retired", availability="unavailable"),
        ("patch", "retired:0"): {"bundle_id": "retired"},
        ("patch", "current:0"): {"bundle_id": "current"},
        ("patch_package", "1:1"): {"patch_id": "retired:0"},
        ("patch_package", "2:1"): {"patch_id": "current:0"},
        ("history_release", "release"): {"bundle_ids": ["retired", "current"]},
    }
    result = reconcile_retained_records(records)
    assert ("bundle", "retired") not in result
    assert ("patch", "retired:0") not in result
    assert ("patch_package", "1:1") not in result
    assert ("patch", "current:0") in result
    assert ("patch_package", "2:1") in result
    assert result["history_release", "release"]["bundle_ids"] == ["current"]


def test_distinct_historical_binaries_and_unproven_hashes_remain_recoverable():
    records = {
        ("bundle", "current"): bundle("current"),
        ("bundle", "replacement"): bundle("replacement", "b" * 64, availability="unavailable"),
        ("bundle", "unknown"): bundle("unknown", None, availability="unavailable"),
        ("bundle", "other-category"): bundle("other-category", is_prerelease=True),
    }
    assert reconcile_retained_records(records) == records


def test_invalid_compiler_archives_and_related_patches_are_not_kept_as_history():
    records = {
        ("bundle", "docs"): bundle("docs", download_url="https://test/patches-javadoc.mpp"),
        ("patch", "docs:0"): {"bundle_id": "docs"},
        ("history_release", "release"): {"bundle_ids": ["docs"]},
    }
    result = reconcile_retained_records(records)
    assert result == {("history_release", "release"): {"bundle_ids": []}}


def test_unavailable_upstream_history_reports_the_reason_and_retains_inventory(tmp_path):
    from scripts.release_archive import reconcile_archive
    from scripts.source_policy import save_availability

    url = "https://github.com/owner/repo"
    inventory = {"repo": {"patches": url}}
    first = reconcile_archive(tmp_path, inventory, {url: [{"id": 1, "tag_name": "v1"}]})
    save_availability(tmp_path, {url: "404: Not Found"})
    failed = reconcile_archive(tmp_path, inventory, {})
    assert failed["releases"] == first["releases"]
    assert failed["sources"][url]["upstream_count"] == 1
    assert failed["sources"][url]["status"] == "incomplete"
    assert failed["sources"][url]["error"].endswith("404: Not Found")


def test_alias_cleanup_never_discards_the_only_verified_copy():
    records = {
        ("bundle", "current"): bundle("current", metadata_status="missing"),
        ("bundle", "retired"): bundle("retired", availability="unavailable"),
        ("patch", "retired:0"): {"bundle_id": "retired", "name": "Public patch"},
    }
    assert reconcile_retained_records(records) == records


def test_retired_d1_records_cannot_preserve_obsolete_empty_verification():
    records = {
        ("bundle", "retired"): bundle(
            "retired", availability="unavailable", patch_count=0, patch_completeness_version=1
        )
    }
    result = reconcile_retained_records(records)
    assert result["bundle", "retired"]["metadata_status"] == "unverified"
    assert result["bundle", "retired"]["need_patches_update"]


def test_unverified_reseam_proof_preserves_fallback_patches_without_trusting_them():
    records = {
        ("bundle", "retired"): bundle(
            "retired", bundle_type="Reseam", patch_count=1, availability="unavailable"
        ),
        ("patch", "retired:0"): {
            "bundle_id": "retired",
            "metadata_status": "verified",
            "name": "Fallback",
        },
    }
    result = reconcile_retained_records(records)
    assert result["bundle", "retired"]["metadata_status"] == "unverified"
    assert result["patch", "retired:0"]["metadata_status"] == "unverified"
    assert result["patch", "retired:0"]["name"] == "Fallback"


def test_alias_cleanup_preserves_the_only_available_fallback_list():
    records = {
        ("bundle", "current"): bundle(
            "current", metadata_status="missing", patch_list_available=False
        ),
        ("bundle", "retired"): bundle(
            "retired",
            metadata_status="unverified",
            availability="unavailable",
            patch_list_available=True,
        ),
        ("patch", "retired:0"): {"bundle_id": "retired", "name": "Only fallback patch"},
    }
    assert reconcile_retained_records(records) == records
    # Older retained records may lack the availability flag but still have patch rows.
    records["bundle", "retired"].pop("patch_list_available")
    assert reconcile_retained_records(records) == records


def test_retained_unnamed_morphe_metadata_is_repaired_without_discarding_named_cache():
    records = {
        ("bundle", "named"): bundle("named", patch_count=1, patch_completeness_version=1),
        ("patch", "named:0"): {"bundle_id": "named", "name": "Public patch"},
        ("bundle", "helper"): bundle(
            "helper", "b" * 64, patch_count=1, patch_completeness_version=2
        ),
        ("patch", "helper:0"): {"bundle_id": "helper", "name": None},
    }
    result = reconcile_retained_records(records)
    assert result["bundle", "named"]["metadata_status"] == "verified"
    assert result["bundle", "helper"]["need_patches_update"]
    assert result["bundle", "helper"]["metadata_status"] == "unverified"
    assert ("patch", "helper:0") in result

def test_alias_cleanup_preserves_a_richer_verified_inventory():
    records = {
        ("bundle", "current"): bundle("current", patch_count=1),
        ("patch", "current:0"): {"bundle_id": "current", "name": "Patch A"},
        ("bundle", "retired"): bundle("retired", availability="unavailable", patch_count=2),
        ("patch", "retired:0"): {"bundle_id": "retired", "name": "Patch A"},
        ("patch", "retired:1"): {"bundle_id": "retired", "name": "Patch B"},
        ("history_release", "release"): {"bundle_ids": ["current", "retired"]},
    }
    assert reconcile_retained_records(records) == records
    # Count the stored inventory too, since older records may lack patch_count.
    records["bundle", "current"].pop("patch_count")
    records["bundle", "retired"].pop("patch_count")
    assert reconcile_retained_records(records) == records
