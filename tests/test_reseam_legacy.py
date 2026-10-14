import hashlib
import zipfile

import pytest

from scripts.reseam_legacy import inspect_manifest


def container(tmp_path, *, payload=b"jar", format_version=1, expected=None):
    expected = expected or hashlib.sha256(payload).hexdigest()
    manifest = (
        '[bundle]\nname = "historical"\nauthor = "Example"\n'
        f'description = "Old patches"\nformat_version = {format_version}\n'
        f'[files]\n"patches.jar" = "{expected}"\n'
    ).encode()
    path = tmp_path / "bundle.reseam"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.toml", manifest)
        archive.writestr("manifest.pubkey", b"k" * 32)
        archive.writestr("manifest.sig", b"s" * 64)
        archive.writestr("patches.jar", payload)
    return path


def test_container_reader_preserves_signed_manifest_and_publisher(tmp_path):
    path = container(tmp_path)
    result = inspect_manifest(path)
    assert result["publisher"] == {
        "name": "historical", "author": "Example", "description": "Old patches",
        "public_key": (b"k" * 32).hex(),
    }
    assert result["jars"] == ["patches.jar"]
    assert result["manifest"] and result["signature"]


@pytest.mark.parametrize("changes", [
    {"expected": "a" * 64}, {"format_version": 2}, {"expected": "invalid"},
])
def test_container_reader_rejects_tampered_or_unsupported_inputs(tmp_path, changes):
    with pytest.raises(ValueError):
        inspect_manifest(container(tmp_path, **changes))


def test_duplicate_archive_members_cannot_replace_signed_payload(tmp_path):
    path = container(tmp_path)
    with zipfile.ZipFile(path, "a") as archive:
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("patches.jar", b"replacement")
    with pytest.raises(ValueError, match="archive entries"):
        inspect_manifest(path)


def test_container_budget_is_checked_before_loading_files(tmp_path, monkeypatch):
    import scripts.reseam_legacy as legacy

    path = container(tmp_path)
    monkeypatch.setattr(legacy, "MAX_CONTAINER_BYTES", 1)
    with pytest.raises(ValueError, match="extraction budget"):
        inspect_manifest(path)
