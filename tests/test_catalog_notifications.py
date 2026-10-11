import json

import httpx
import pytest

from scripts import notify_catalog_update as notify


def release(version="v1", names=("Patch one",), **changes):
    return {
        "name": "owner/bundle",
        "category": "release",
        "version": version,
        "verified": True,
        "fingerprint": json.dumps(names),
        "names": list(names),
        **changes,
    }


def test_version_and_patch_changes_are_grouped_by_bundle():
    before = {"source/release": release()}
    after = {"source/release": release("v2", ("Patch one", "Patch two", "Patch three"))}
    contents = notify.messages(before, after, "https://example.test")
    assert contents == [
        "**Updated Bundles**\n\nowner/bundle v1 → owner/bundle v2 (patches updated)"
        "\n\n**New Patches**\n\nPatch three, Patch two - owner/bundle\n\nhttps://example.test",
    ]


def test_patch_options_and_removals_without_version_change_are_updates():
    before = {"source/release": release(fingerprint="old options")}
    after = {"source/release": release(fingerprint="new options")}
    updated, added = notify.changes(before, after)
    assert updated == ["owner/bundle v1 → owner/bundle v1 (patches updated)"]
    assert added == []
    after["source/release"] = release(names=())
    assert notify.changes(before, after)[0]
    assert notify.changes(before, after)[1] == []


def test_unchanged_catalog_and_metadata_regression_do_not_notify():
    before = {"source/release": release()}
    assert notify.messages(before, before, "https://example.test") == []
    assert notify.messages(before, {}, "https://example.test") == []
    assert notify.messages(before, {"source/release": release(verified=False)}, "") == []


def test_new_sources_prereleases_and_extraction_completion_are_distinct():
    after = {
        "source/release": release(),
        "source/prerelease": release("v2-dev", category="prerelease"),
    }
    updated, added = notify.changes({}, after)
    assert "New → owner/bundle v1" in updated
    assert "New → owner/bundle (prerelease) v2-dev" in updated
    assert len(added) == 2
    before = {"source/release": release(verified=False, names=())}
    updated, added = notify.changes(before, {"source/release": release()})
    assert updated == ["owner/bundle v1 → owner/bundle v1 (patch metadata now available)"]
    assert added == []


def test_large_patch_lists_split_without_losing_names():
    names = tuple(f"Patch {index:04}" for index in range(500))
    contents = notify.messages({}, {"source/release": release(names=names)}, "")
    assert len(contents) > 2
    assert all(notify.size(content) <= 2000 for content in contents)
    for name in names:
        assert sum(content.count(name) for content in contents) == 1
    assert all(" - owner/bundle" in content for content in contents[1:])


def test_provider_text_is_escaped_and_long_unicode_stays_within_limit():
    item = release(
        version="😀*" * 500,
        name="😀*" * 500,
        names=["@everyone\n**Injected heading**"],
    )
    contents = notify.messages({}, {"source/release": item}, "")
    assert all(notify.size(content) <= 2000 for content in contents)
    assert "\\*\\*Injected heading\\*\\*" in contents[-1]
    assert "@everyone\n" not in contents[-1]


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def catalog(root, patch_options=None):
    snapshot = root / "database/snapshots" / ("a" * 24)
    write(root / "database/manifest.json", {"snapshot": "snapshots/" + "a" * 24})
    write(
        snapshot / "sources.json",
        [
            {"id": "s", "namespace": "owner", "repo": "bundle", "enabled": True},
            {"id": "disabled", "namespace": "owner", "repo": "disabled", "enabled": False},
        ],
    )
    history = {}
    for key, source, latest in (
        ("latest", "s", True),
        ("old", "s", False),
        ("disabled", "disabled", True),
    ):
        history[key] = {
            "bundle": {
                "source_id": source,
                "version": "v1",
                "is_latest": latest,
                "is_prerelease": False,
                "metadata_status": "verified",
            },
            "patches": [
                {"name": "Patch one", "options": patch_options or []},
                {"name": None, "description": "Unnamed"},
            ],
        }
    write(snapshot / "history.json", history)
    return snapshot, history


def test_capture_uses_public_latest_releases_and_survives_snapshot_pruning(tmp_path):
    assert notify.capture(tmp_path) == {}
    snapshot, history = catalog(tmp_path)
    before = notify.capture(tmp_path)
    assert list(before) == ["s/release"]
    assert before["s/release"]["names"] == ["Patch one", "Unnamed patch"]
    # Patch and JSON object ordering are immaterial, as are newly backfilled old releases.
    history["latest"]["patches"].reverse()
    history["extra-history"] = history["old"]
    write(snapshot / "history.json", history)
    assert notify.capture(tmp_path) == before
    baseline = tmp_path / "before.json"
    write(baseline, before)
    catalog(tmp_path, [{"key": "changed", "default": True}])
    after = notify.capture(tmp_path)
    (snapshot / "history.json").unlink()
    assert notify.changes(notify.read(baseline), after)[0]
    assert notify.changes(before, after)[1] == []


def test_delivery_splits_safely_and_respects_discord_retry_after(monkeypatch):
    requests, sleeps = [], []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, json={"retry_after": 2.5})
        return httpx.Response(200, json={"id": "message"})

    client_type = httpx.Client
    monkeypatch.setattr(
        notify.httpx,
        "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    monkeypatch.setattr(notify.time, "sleep", sleeps.append)
    notify.send("https://example.test/webhook?thread_id=123", ["one", "two"])
    assert sleeps == [2.5, 1]
    assert len(requests) == 3
    assert [json.loads(request.content)["content"] for request in requests] == ["one", "one", "two"]
    assert all(
        json.loads(request.content)["allowed_mentions"] == {"parse": []} for request in requests
    )
    assert all(request.url.params["wait"] == "true" for request in requests)
    assert all(request.url.params["thread_id"] == "123" for request in requests)


def test_delivery_errors_do_not_leak_webhook_tokens(monkeypatch):
    def respond(request):
        raise httpx.ConnectError("https://example.test/SECRET", request=request)

    client_type = httpx.Client
    monkeypatch.setattr(
        notify.httpx,
        "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    with pytest.raises(RuntimeError, match="Could not deliver") as error:
        notify.send("https://example.test/SECRET", ["one"])
    assert "SECRET" not in str(error.value)
    assert error.value.__suppress_context__


def test_workflow_captures_before_any_published_checkpoint():
    workflow = (notify.ROOT / ".github/workflows/catalog-updater.yml").read_text(encoding="utf-8")
    assert workflow.index("notify_catalog_update.py capture") < workflow.index(
        "run_catalog_refresh.py"
    )
    assert workflow.index("notify_catalog_update.py notify") > workflow.index("HEAD:bundles")
    assert workflow.count('--baseline "$RUNNER_TEMP/catalog-before.json"') == 2


def test_delayed_extraction_reports_additions_against_last_verified_release(tmp_path):
    snapshot, history = catalog(tmp_path)
    history["latest"]["bundle"]["created_at"] = "2026-10-01T00:00:00Z"
    history["latest"]["bundle"]["is_latest"] = False
    history["pending"] = {
        "bundle": {
            "source_id": "s",
            "version": "v2",
            "is_latest": True,
            "is_prerelease": False,
            "metadata_status": "missing",
            "created_at": "2026-10-02T00:00:00Z",
        },
        "patches": [],
    }
    write(snapshot / "history.json", history)
    before = notify.capture(tmp_path)
    assert before["s/release"]["previous_names"] == ["Patch one", "Unnamed patch"]
    history["pending"]["bundle"]["metadata_status"] = "verified"
    history["pending"]["patches"] = [
        *history["latest"]["patches"],
        {"name": "New patch"},
    ]
    write(snapshot / "history.json", history)
    updated, added = notify.changes(before, notify.capture(tmp_path))
    assert updated == ["owner/bundle v2 → owner/bundle v2 (patch metadata now available)"]
    assert added == [("owner/bundle", ["New patch"])]


def test_delayed_extraction_can_compare_against_a_verified_empty_release(tmp_path):
    snapshot, history = catalog(tmp_path)
    history["latest"]["bundle"].update(is_latest=False, created_at="2026-10-01T00:00:00Z")
    history["latest"]["patches"] = []
    del history["old"]
    history["pending"] = {
        "bundle": {
            "source_id": "s",
            "version": "v2",
            "is_latest": True,
            "is_prerelease": False,
            "metadata_status": "missing",
            "created_at": "2026-10-02T00:00:00Z",
        },
        "patches": [],
    }
    write(snapshot / "history.json", history)
    before = notify.capture(tmp_path)
    history["pending"]["bundle"]["metadata_status"] = "verified"
    history["pending"]["patches"] = [{"name": "First patch"}]
    write(snapshot / "history.json", history)
    assert notify.changes(before, notify.capture(tmp_path))[1] == [
        ("owner/bundle", ["First patch"])
    ]
