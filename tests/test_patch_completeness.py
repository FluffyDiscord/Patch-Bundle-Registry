import pytest
from test_generate_database import db, fixture, snapshot, write


@pytest.mark.parametrize("family", ["Morphe:V1", "ReVanced:v21", "ReVanced:v22", "Reseam"])
@pytest.mark.parametrize("completeness", [0, 1, 2])
def test_empty_lists_require_family_specific_proof_before_publication(
    tmp_path, family, completeness
):
    folder = fixture(tmp_path)
    for channel in ("latest", "stable"):
        raw_path = folder / f"demo-{channel}-patches-bundle.json"
        raw = db.read_json(raw_path)
        raw["bundle_type"] = family
        write(raw_path, raw)
        path = folder / f"demo-{channel}-patches-list.json"
        patches = db.read_json(path)
        patches["patches"] = []
        write(path, patches)
        status_path = folder / f"demo-{channel}-extraction.json"
        status = db.read_json(status_path)
        status.update(
            patch_list_hash=db.digest(path.read_bytes()), completeness_version=completeness
        )
        write(status_path, status)
    _, rows = snapshot(tmp_path, db.build_database(tmp_path, restore=False))
    verified = completeness >= (1 if family == "Reseam" else 2)
    assert rows[0]["metadata_status"] == ("verified" if verified else "unverified")
    assert rows[0]["need_patches_update"] is not verified


def test_retained_empty_history_also_loses_outdated_verification(tmp_path):
    folder = fixture(tmp_path)
    path, _ = snapshot(tmp_path, db.build_database(tmp_path))
    history = db.read_json(path / "history.json")
    for item in history.values():
        item["bundle"].update(patch_count=0, patch_completeness_version=1)
        item["patches"] = []
    write(path / "history.json", history)
    for file in folder.iterdir():
        file.unlink()
    _, rows = snapshot(tmp_path, db.build_database(tmp_path, restore=False))
    assert rows[0]["metadata_status"] == "unverified"
    assert rows[0]["need_patches_update"]


@pytest.mark.parametrize("completeness", [1, 2, 3, 4])
def test_morphe_helpers_require_public_loader_selection_proof(tmp_path, completeness):
    folder = fixture(tmp_path)
    for channel in ("latest", "stable"):
        raw_path = folder / f"demo-{channel}-patches-bundle.json"
        raw = db.read_json(raw_path)
        raw["bundle_type"] = "Morphe:V1"
        write(raw_path, raw)
        path = folder / f"demo-{channel}-patches-list.json"
        patches = db.read_json(path)
        patches["patches"][0].update(
            name=None, display_name="Internal helper", loader_excluded=True
        )
        write(path, patches)
        status_path = folder / f"demo-{channel}-extraction.json"
        status = db.read_json(status_path)
        status.update(
            patch_list_hash=db.digest(path.read_bytes()), completeness_version=completeness
        )
        write(status_path, status)
    path, rows = snapshot(tmp_path, db.build_database(tmp_path, restore=False))
    assert rows[0]["metadata_status"] == ("verified" if completeness >= 4 else "unverified")
    assert rows[0]["patch_count"] == 0
    assert db.read_json(path / "packages.json") == []
    history = db.read_json(path / "history.json")
    assert all(item["patches"] == [] for item in history.values())
    helpers = [p for item in history.values() for p in item["internal_patches"]]
    assert helpers and all(p["display_name"] == "Internal helper" for p in helpers)


def test_public_inventory_and_helpers_survive_history_restore_separately(tmp_path):
    from scripts.catalog_records import catalog_records
    from scripts.validate_database import validate

    folder = fixture(tmp_path)
    helper = {"name": None, "display_name": "Debug helper", "loader_excluded": True,
              "compatiblePackages": {"com.helper.only": None}}
    for channel in ("latest", "stable"):
        raw_path = folder / f"demo-{channel}-patches-bundle.json"
        raw = db.read_json(raw_path)
        raw["bundle_type"] = "Morphe:V1"
        write(raw_path, raw)
        patch_path = folder / f"demo-{channel}-patches-list.json"
        payload = db.read_json(patch_path)
        payload["patches"].insert(0, helper)
        write(patch_path, payload)
        status_path = folder / f"demo-{channel}-extraction.json"
        status = db.read_json(status_path)
        status.update(patch_list_hash=db.digest(patch_path.read_bytes()), completeness_version=4)
        write(status_path, status)
    manifest = db.build_database(tmp_path, restore=False)
    validate(tmp_path)
    path, bundles = snapshot(tmp_path, manifest)
    assert bundles[0]["patch_count"] == 1
    assert bundles[0]["packages"] == ["com.video"]
    assert manifest["counts"]["patches"] == 1
    _, records = catalog_records(tmp_path)
    public = [row for (kind, _), row in records.items() if kind == "patch"]
    assert [patch["name"] for patch in public] == ["Hide ads"]
    assert public[0]["catalog_id"].endswith(":1")
    original_id = public[0]["id"]
    assert "com.helper.only" not in db.read_json(path / "patch-index.json")["packages"]
    history = db.read_json(path / "history.json")
    assert all(item["internal_patches"] == [helper] for item in history.values())
    for file in folder.iterdir():
        file.unlink()
    db.restore_catalog_cache(tmp_path)
    for cache in (tmp_path / "internal/cache").rglob("*-patches-list.json"):
        payload = db.read_json(cache)
        assert len(payload["patches"]) == 2
        assert helper in payload["patches"]
    _, rebuilt = snapshot(tmp_path, db.build_database(tmp_path, restore=False))
    assert rebuilt[0]["metadata_status"] == "verified"
    assert rebuilt[0]["patch_count"] == 1
    _, restored_records = catalog_records(tmp_path)
    assert [
        row["id"] for (kind, _), row in restored_records.items() if kind == "patch"
    ] == [original_id]


def test_unexplained_missing_morphe_public_name_is_never_current_proof():
    from scripts.extraction_policy import patch_completeness_current

    assert not patch_completeness_current("Morphe:V1", 1, 4, [{"name": None}])
    assert patch_completeness_current("Reseam", 1, 1, [{"name": None, "hidden": True}])


def test_reseam_hidden_patches_isolated_from_public_catalog_and_survive_restore(tmp_path):
    from scripts.catalog_records import catalog_records
    from scripts.validate_database import validate

    folder = fixture(tmp_path)
    hidden_patch = {
        "name": None,
        "display_name": "Internal dependency",
        "hidden": True,
        "compatiblePackages": {"com.reseam.hidden.only": None},
    }
    for channel in ("latest", "stable"):
        raw_path = folder / f"demo-{channel}-patches-bundle.json"
        raw = db.read_json(raw_path)
        raw["bundle_type"] = "Reseam"
        write(raw_path, raw)
        patch_path = folder / f"demo-{channel}-patches-list.json"
        payload = db.read_json(patch_path)
        payload["patches"].insert(0, hidden_patch)
        write(patch_path, payload)
        status_path = folder / f"demo-{channel}-extraction.json"
        status = db.read_json(status_path)
        status.update(patch_list_hash=db.digest(patch_path.read_bytes()), completeness_version=1)
        write(status_path, status)
    manifest = db.build_database(tmp_path, restore=False)
    validate(tmp_path)
    path, bundles = snapshot(tmp_path, manifest)
    assert bundles[0]["patch_count"] == 1
    assert bundles[0]["packages"] == ["com.video"]
    assert manifest["counts"]["patches"] == 1
    _, records = catalog_records(tmp_path)
    public = [row for (kind, _), row in records.items() if kind == "patch"]
    assert [patch["name"] for patch in public] == ["Hide ads"]
    assert public[0]["catalog_id"].endswith(":1")
    original_id = public[0]["id"]
    assert "com.reseam.hidden.only" not in db.read_json(path / "patch-index.json")["packages"]
    assert "com.reseam.hidden.only" not in [
        pkg["name"] for pkg in db.read_json(path / "packages.json")
    ]
    history = db.read_json(path / "history.json")
    assert all(item["internal_patches"] == [hidden_patch] for item in history.values())
    for file in folder.iterdir():
        file.unlink()
    db.restore_catalog_cache(tmp_path)
    for cache in (tmp_path / "internal/cache").rglob("*-patches-list.json"):
        payload = db.read_json(cache)
        assert len(payload["patches"]) == 2
        assert hidden_patch in payload["patches"]
    _, rebuilt = snapshot(tmp_path, db.build_database(tmp_path, restore=False))
    assert rebuilt[0]["metadata_status"] == "verified"
    assert rebuilt[0]["patch_count"] == 1
    _, restored_records = catalog_records(tmp_path)
    assert [
        row["id"] for (kind, _), row in restored_records.items() if kind == "patch"
    ] == [original_id]

