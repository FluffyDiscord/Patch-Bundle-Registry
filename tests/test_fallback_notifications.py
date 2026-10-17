import pytest
from test_source_cleanup import fixture, write

from scripts.notify_catalog_update import messages
from scripts.refresh_issues import extraction_issues, read_issues


@pytest.mark.parametrize("metadata_valid", [True, False])
def test_only_available_fallback_lists_are_classified_as_recovered(tmp_path, metadata_valid):
    folder = fixture(tmp_path)
    write(folder / "demo-stable-extraction.json", {
        "attempted_at": "2030-01-02T00:00:00Z",
        "version": "v1", "status": "incomplete_patch_inventory",
        "fallback_patch_count": 1,
        "runtime_attempts": [{
            "runtime": "artifact", "status": "incomplete_patch_inventory",
            "cause": "No JVM patch classes",
        }],
    })
    write(folder / "demo-stable-patches-list.json", {
        "version": "v1" if metadata_valid else "v0", "patches": [{"name": "Fallback"}],
    })
    extraction_issues(tmp_path, "2030-01-01T00:00:00+00:00")
    issue = read_issues(tmp_path)[0]
    assert issue["phase"] == ("Metadata fallback" if metadata_valid else "Patch extraction")
    assert ("1 publisher patches available (unverified)" in issue["error"]) == metadata_valid
    assert "No JVM patch classes" in issue["error"]


def test_recovered_metadata_and_unrecovered_errors_have_distinct_linked_sections():
    source = "https://github.com/owner/repo"
    payload = messages({}, {}, "", [
        {"source": source, "phase": "Metadata fallback", "error": "5 patches, unverified"},
        {"source": source, "phase": "Patch extraction", "error": "Missing artifact"},
    ])[0]
    fields = payload["embeds"][0]["fields"]
    assert [field["name"] for field in fields] == ["Metadata fallbacks", "Issues"]
    assert all("[owner/repo](https://github.com/owner/repo)" in field["value"] for field in fields)
    assert "unverified" in fields[0]["value"]
    assert "Missing artifact" in fields[1]["value"]
