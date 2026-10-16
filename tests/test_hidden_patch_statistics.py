from scripts.catalog_statistics import patch_statistics


def test_hidden_flags_participate_in_counts_and_null_masks():
    rows = [
        {"id": "b:0", "legacy_id": 1, "bundle_id": "b", "hidden": False},
        {"id": "b:1", "legacy_id": 2, "bundle_id": "b", "hidden": True},
    ]
    stats = patch_statistics(rows, {"b": {"legacy_id": 1}})
    assert stats["nonnull"]["hidden"] == 2
    assert stats["distinct"]["hidden"] == 2
    required = sum(1 << stats["columns"].index(column) for column in ("id", "hidden"))
    assert sum(
        count for mask, count in stats["null_masks"].items() if not int(mask) & required
    ) == 2
