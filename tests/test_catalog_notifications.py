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
    fields = [
        field for payload in contents for embed in payload["embeds"] for field in embed["fields"]
    ]
    assert [field["name"] for field in fields] == ["Updated Bundles", "New Patches"]
    assert "owner/bundle: v1 → v2 (patches updated)" in fields[0]["value"]
    assert fields[1]["value"].splitlines() == [
        "• Patch three (owner/bundle)",
        "• Patch two (owner/bundle)",
    ]
    assert contents[0]["embeds"][0]["url"] == "https://example.test"


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
    for content in contents:
        fields = content["embeds"][0]["fields"]
        headings = [field["name"] for field in fields if field["name"] != "\u200b"]
        assert len(headings) == len(set(headings))
        assert fields[0]["name"] != "\u200b"
        assert "New Patches" in headings
    assert any(
        field["name"] == "\u200b"
        for content in contents
        for field in content["embeds"][0]["fields"]
    )
    assert all(
        sum(
            notify.size(embed["title"])
            + sum(
                notify.size(field["name"]) + notify.size(field["value"])
                for field in embed["fields"]
            )
            for embed in content["embeds"]
        )
        <= 6000
        for content in contents
    )
    assert all(
        notify.size(field["value"]) <= 1024
        for content in contents
        for embed in content["embeds"]
        for field in embed["fields"]
    )
    for name in names:
        assert sum(json.dumps(content).count(name) for content in contents) == 1
    assert all("(owner/bundle)" in json.dumps(content) for content in contents[1:])


def test_provider_text_is_escaped_and_long_unicode_stays_within_limit():
    item = release(
        version="😀*" * 500,
        name="😀*" * 500,
        names=["@everyone\n**Injected heading**"],
    )
    contents = notify.messages({}, {"source/release": item}, "")
    assert all(
        sum(
            notify.size(embed["title"])
            + sum(
                notify.size(field["name"]) + notify.size(field["value"])
                for field in embed["fields"]
            )
            for embed in content["embeds"]
        )
        <= 6000
        for content in contents
    )
    assert all(
        notify.size(field["value"]) <= 1024
        for content in contents
        for embed in content["embeds"]
        for field in embed["fields"]
    )
    assert "\\*\\*Injected heading\\*\\*" in contents[-1]["embeds"][0]["fields"][-1]["value"]
    assert "@everyone\n" not in contents[-1]["embeds"][0]["fields"][-1]["value"]


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
    notify.send(
        "https://example.test/webhook?thread_id=123",
        [{"embeds": [{"title": "one"}]}, {"embeds": [{"title": "two"}]}],
    )
    assert sleeps == [2.5, 1]
    assert len(requests) == 3
    assert [json.loads(request.content)["embeds"][0]["title"] for request in requests] == [
        "one",
        "one",
        "two",
    ]
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
        notify.send("https://example.test/SECRET", [{"embeds": [{"title": "one"}]}])
    assert "SECRET" not in str(error.value)
    assert error.value.__suppress_context__


def test_workflow_captures_before_any_published_checkpoint():
    workflow = (notify.ROOT / ".github/workflows/catalog-updater.yml").read_text(encoding="utf-8")
    assert workflow.index("notify_catalog_update.py capture") < workflow.index(
        "run_catalog_refresh.py"
    )
    assert workflow.index("notify_catalog_update.py notify") > workflow.index("HEAD:bundles")
    assert workflow.count('--baseline "$RUNNER_TEMP/catalog-before.json"') == 4
    cloud = workflow.split("  cloudflare:", 1)[1]
    assert cloud.index("notify_catalog_update.py capture") < cloud.index(
        "scripts.run_catalog_refresh"
    )
    assert cloud.index("notify_catalog_update.py notify") > cloud.index(
        "scripts.cloudflare_catalog publish"
    )


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


def embed_fields(contents):
    return [
        field for payload in contents for embed in payload["embeds"] for field in embed["fields"]
    ]


def test_embeds_list_linked_new_bundles_apps_and_patches_once_across_channels():
    item = release(
        url="https://git.example/owner/bundle",
        release_url="https://git.example/owner/bundle/releases/tag/v1",
        apps={"com.example": "Example"},
        app_urls={"com.example": "https://f-droid.org/packages/com.example"},
    )
    contents = notify.messages(
        {},
        {"source/release": item, "source/prerelease": {**item, "category": "prerelease"}},
        "https://catalog.example",
    )
    fields = embed_fields(contents)
    assert [field["name"] for field in fields] == ["New Bundles", "New Apps", "New Patches"]
    assert fields[0]["value"] == "• [owner/bundle](https://git.example/owner/bundle)"
    assert fields[1]["value"] == (
        "• [Example](https://f-droid.org/packages/com.example) (owner/bundle)"
    )
    assert fields[2]["value"] == (
        "• [Patch one](https://git.example/owner/bundle/releases/tag/v1) (owner/bundle)"
    )
    assert contents[0]["allowed_mentions"] == {"parse": []}
    assert "content" not in contents[0]


def test_app_associations_are_new_per_bundle_and_labels_alone_are_not_changes():
    before = {"s/release": release(apps={"com.example": "Old name"})}
    after = {"s/release": release(apps={"com.example": "Correct name"})}
    assert notify.messages(before, after, "") == []
    after["s/release"] = release(
        "v2", names=("Patch one", "Second"), apps={"com.example": "Correct", "com.new": "New App"}
    )
    after["other/release"] = release(apps={"com.example": "Correct"})
    fields = embed_fields(notify.messages(before, after, ""))
    app_text = "\n".join(field["value"] for field in fields if field["name"] == "New Apps")
    assert app_text.count("[Correct]") == 1  # New to the other bundle only.
    assert app_text.count("[New App]") == 1


def test_capture_uses_app_names_provenance_and_keeps_prior_apps_for_delayed_metadata(tmp_path):
    snapshot, history = catalog(tmp_path)
    history["latest"]["patches"][0]["compatiblePackages"] = {"com.old": None}
    history["latest"]["bundle"].update(is_latest=False, created_at="2026-10-01T00:00:00Z")
    del history["old"]
    history["pending"] = {
        "bundle": {
            **history["latest"]["bundle"],
            "version": "v2",
            "is_latest": True,
            "created_at": "2026-10-02T00:00:00Z",
            "metadata_status": "missing",
        },
        "patches": [],
    }
    write(snapshot / "history.json", history)
    write(
        snapshot / "apps.json",
        [
            {"package_name": "com.old", "app_name": "Old App", "metadata_source": "google_play"},
            {"package_name": "com.new", "app_name": "New App", "metadata_source": "izzyondroid"},
        ],
    )
    before = notify.capture(tmp_path)
    assert before["s/release"]["previous_apps"] == {"com.old": "Old App"}
    history["pending"]["bundle"]["metadata_status"] = "verified"
    history["pending"]["patches"] = [
        *history["latest"]["patches"],
        {"name": "New Patch", "compatiblePackages": {"com.new": ["1.0"]}},
    ]
    write(snapshot / "history.json", history)
    after = notify.capture(tmp_path)
    fields = embed_fields(notify.messages(before, after, ""))
    apps = next(field["value"] for field in fields if field["name"] == "New Apps")
    assert "Old App" not in apps
    assert "[New App](https://apt.izzysoft.de/fdroid/index/apk/com.new)" in apps
    assert "New Patch" in next(field["value"] for field in fields if field["name"] == "New Patches")


def test_legacy_baseline_without_app_comparison_does_not_announce_existing_apps():
    before = {"s/release": release()}
    after = {"s/release": release("v2", apps={"com.old": "Existing"})}
    assert all(
        field["name"] != "New Apps" for field in embed_fields(notify.messages(before, after, ""))
    )


def test_long_linked_entries_split_with_complete_links_and_all_names():
    names = tuple(f"Patch {index:04} " + "😀[]" * 200 for index in range(120))
    contents = notify.messages(
        {},
        {
            "s/release": release(
                names=names,
                url="https://repo.example/owner/repo",
                release_url="https://repo.example/owner/repo/releases/tag/v2",
                version="😀" * 500,
                name="😀" * 500,
            )
        },
        "",
    )
    assert len(contents) > 2
    for payload in contents:
        embed = payload["embeds"][0]
        assert len(embed["fields"]) <= 25
        assert (
            notify.size(embed["title"])
            + sum(
                notify.size(field["name"]) + notify.size(field["value"])
                for field in embed["fields"]
            )
            <= 6000
        )
        for field in embed["fields"]:
            assert 0 < notify.size(field["value"]) <= 1024
    text = "\n".join(field["value"] for field in embed_fields(contents))
    for index in range(120):
        assert text.count(f"Patch {index:04}") == 1
    assert "javascript:" not in notify.link("Bad [link]", "javascript:alert(1)")
    assert notify.link("Bad [link]", "https://repo.example/a(b)") == (
        "[Bad \\[link\\]](https://repo.example/a%28b%29)"
    )


@pytest.mark.parametrize("existing_stable", [False, True])
def test_promotions_do_not_reannounce_existing_source_apps_or_patches(existing_stable):
    dev = release(
        "v2-dev", ("Existing patch",), category="prerelease", apps={"com.existing": "Existing App"}
    )
    before = {"s/prerelease": dev}
    if existing_stable:
        before["s/release"] = release(apps={})
    after = {
        **before,
        "s/release": release(
            "v2",
            ("Existing patch", "New patch"),
            apps={"com.existing": "Existing App", "com.new": "New App"},
        ),
    }
    fields = embed_fields(notify.messages(before, after, ""))
    assert any(field["name"] == "Updated Bundles" for field in fields)
    patches = next(field["value"] for field in fields if field["name"] == "New Patches")
    apps = next(field["value"] for field in fields if field["name"] == "New Apps")
    assert "Existing patch" not in patches
    assert "Existing App" not in apps
    assert "New patch" in patches
    assert "New App" in apps


@pytest.mark.parametrize(
    ("source", "label", "target"),
    [
        ("https://github.com/owner/repo", "owner/repo", "https://github.com/owner/repo"),
        (
            "https://gitlab.com/team/subgroup/patches/",
            "team/subgroup/patches",
            "https://gitlab.com/team/subgroup/patches/",
        ),
        (
            "https://git.reseam.app/reseam/reseam",
            "reseam/reseam",
            "https://git.reseam.app/reseam/reseam",
        ),
        (
            "https://github.com/owner/repo_(old)",
            r"owner/repo\_(old)",
            "https://github.com/owner/repo_%28old%29",
        ),
    ],
)
def test_issue_notifications_use_named_repository_links(source, label, target):
    payloads = notify.messages(
        {}, {}, "", [{"source": source, "phase": "Release check", "error": "HTTP 404"}]
    )
    assert payloads[0]["embeds"][0]["fields"] == [
        {
            "name": "Issues",
            "value": f"• [{label}]({target}) — **Release check**: HTTP 404",
            "inline": False,
        }
    ]


def test_issue_provider_text_cannot_break_repository_links_or_add_headings():
    payloads = notify.messages(
        {},
        {},
        "",
        [
            {
                "source": "https://github.com/owner/repo",
                "phase": "Release **check**\nInjected",
                "error": "HTTP 502\n[click](https://bad.test) **heading** @everyone",
            }
        ],
    )
    value = payloads[0]["embeds"][0]["fields"][0]["value"]
    assert value.startswith("• [owner/repo](https://github.com/owner/repo) — ")
    assert r"**Release \*\*check\*\* Injected**:" in value
    assert r"\[click\](https://bad.test) \*\*heading\*\*" in value
    assert len(value.splitlines()) == 1
    assert payloads[0]["allowed_mentions"] == {"parse": []}


def test_long_issue_notifications_keep_each_repository_link_intact():
    source = "https://github.com/owner/repo"
    issues = [
        {"source": source, "phase": "Patch extraction", "error": f"v{n}: " + "😀" * 400}
        for n in range(100)
    ]
    payloads = notify.messages({}, {}, "", issues)
    lines = []
    for payload in payloads:
        embed = payload["embeds"][0]
        fields = embed["fields"]
        assert [f["name"] for f in fields].count("Issues") == 1
        assert fields[0]["name"] == "Issues"
        assert len(fields) <= 25
        assert (
            notify.size(embed["title"])
            + sum(notify.size(f["name"]) + notify.size(f["value"]) for f in fields)
            <= 6000
        )
        for field in fields:
            assert notify.size(field["value"]) <= 1024
            lines.extend(field["value"].splitlines())
    assert len(lines) == len(issues)
    assert all(
        line.startswith(f"• [owner/repo]({source}) — **Patch extraction**: ") for line in lines
    )
