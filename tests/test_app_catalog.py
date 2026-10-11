import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_generate_database import db, fixture, snapshot, write

from scripts.app_catalog import build_apps
from scripts.refresh_app_metadata import INDEXES, PlayMetadata, fdroid_apps, refresh
from scripts.source_policy import source_channels

NOW = datetime(2026, 10, 10, tzinfo=UTC)


def index_v2():
    return {
        "packages": {
            "org.example.free": {
                "metadata": {
                    "name": {"de": "Deutsch", "en-US": "Free App"},
                    "icon": {"en-US": {"name": "/org.example.free/en-US/icon.png"}},
                }
            }
        }
    }


def prepare(root, packages):
    write(root / "database/manifest.json", {"snapshot": "snapshots/test"})
    write(
        root / "database/snapshots/test/apps.json",
        [{"package_name": package, "bundle_count": 1} for package in packages],
    )


def play(name="Video App"):
    return (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": "SoftwareApplication",
                "name": name,
                "image": "https://images.test/icon.png",
            }
        )
        + "</script>"
    )


def test_fdroid_versions_locales_icons_and_invalid_payloads():
    assert fdroid_apps(index_v2())["org.example.free"] == {
        "app_name": "Free App",
        "icon_url": "https://f-droid.org/repo/org.example.free/en-US/icon.png",
        "metadata_source": "fdroid",
    }
    v1 = {"apps": [{"packageName": "org.example.free", "name": "Free", "icon": "free.png"}]}
    assert fdroid_apps(v1)["org.example.free"]["icon_url"].endswith("/repo/icons/free.png")
    v1["apps"][0]["icon"] = "../escape.png"
    assert fdroid_apps(v1)["org.example.free"]["icon_url"] is None
    with pytest.raises(ValueError):
        fdroid_apps({"packages": {}})


def test_play_metadata_uses_real_listing_fields_and_rejects_interstitial():
    parser = PlayMetadata()
    parser.feed(play("Video &amp; Music"))
    assert parser.metadata()["app_name"] == "Video & Music"
    parser = PlayMetadata()
    parser.feed(
        '<meta property="og:title" content="Video - Apps on Google Play">'
        '<meta property="og:image" content="https://images.test/v.png">'
    )
    assert parser.metadata()["app_name"] == "Video"
    parser = PlayMetadata()
    parser.feed("<title>Before you continue to Google</title>")
    assert parser.metadata() is None


def test_bulk_index_then_play_and_daily_positive_cache(tmp_path):
    prepare(tmp_path, ["org.example.free", "com.video"])
    requests = []

    def handler(request):
        requests.append(request)
        if str(request.url) == INDEXES[0]:
            return httpx.Response(200, json=index_v2(), headers={"ETag": "index-one"})
        assert request.url.host == "play.google.com"
        return httpx.Response(200, text=play())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = refresh(tmp_path, client, now=NOW, sleep=lambda _: None)
        assert state["apps"]["org.example.free"]["metadata_source"] == "fdroid"
        assert state["apps"]["com.video"]["app_name"] == "Video App"
        assert len(requests) == 2
        refresh(tmp_path, client, now=NOW + timedelta(hours=1), sleep=lambda _: None)
        assert len(requests) == 2

    def revalidate(request):
        assert request.headers["if-none-match"] == "index-one"
        return httpx.Response(304)

    with httpx.Client(transport=httpx.MockTransport(revalidate)) as client:
        state = refresh(tmp_path, client, now=NOW + timedelta(days=1), max_play=0)
        assert state["apps"]["org.example.free"]["app_name"] == "Free App"


def test_fdroid_fallback_keeps_cache_and_does_not_reuse_other_index_etags(tmp_path):
    prepare(tmp_path, ["org.example.free"])
    write(
        tmp_path / "database/app-metadata.json",
        {
            "fdroid": {
                "url": INDEXES[0],
                "etag": "v2",
                "applications": {"org.example.free": {"app_name": "Cached"}},
            }
        },
    )

    def handler(request):
        if str(request.url) == INDEXES[0]:
            return httpx.Response(500)
        assert "if-none-match" not in request.headers
        return httpx.Response(
            200, json={"apps": [{"packageName": "org.example.free", "name": "Fallback"}]}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = refresh(tmp_path, client, now=NOW)
        assert state["apps"]["org.example.free"]["app_name"] == "Fallback"
        assert state["fdroid"]["etag"] is None

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
        state = refresh(tmp_path, client, now=NOW + timedelta(days=2), max_play=0)
        assert state["apps"]["org.example.free"]["app_name"] == "Fallback"


def test_play_batch_negative_cache_and_retry_after(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.refresh_app_metadata.time.monotonic", lambda: 0)
    prepare(tmp_path, ["com.one", "com.two", "com.three"])
    calls, delays = [], []

    def handler(request):
        calls.append(request)
        if request.url.host == "f-droid.org":
            return httpx.Response(200, json=index_v2())
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        refresh(tmp_path, client, now=NOW, max_play=2, sleep=delays.append)
        assert len([r for r in calls if r.url.host == "play.google.com"]) == 2
        assert delays == [1]
        refresh(tmp_path, client, now=NOW + timedelta(hours=1), max_play=2, sleep=delays.append)
        assert len([r for r in calls if r.url.host == "play.google.com"]) == 3

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json=index_v2())
                if request.url.host == "f-droid.org"
                else httpx.Response(429, headers={"Retry-After": "120"})
            )
        )
    ) as client:
        state = refresh(tmp_path, client, now=NOW + timedelta(days=8))
        assert datetime.fromisoformat(state["play_retry_after"]) == NOW + timedelta(
            days=8, seconds=120
        )
        state = refresh(tmp_path, client, now=NOW + timedelta(days=8, seconds=60))
        assert datetime.fromisoformat(state["play_retry_after"]) == NOW + timedelta(
            days=8, seconds=120
        )


def test_manual_metadata_skips_play_lookup(tmp_path):
    prepare(tmp_path, ["com.manual"])
    write(tmp_path / "config/apps.json", {"com.manual": {"app_name": "Manual"}})
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=index_v2())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        refresh(tmp_path, client, now=NOW)
    assert len(calls) == 1


def test_app_indexes_merge_compatibility_once_per_patch_and_exclude_disabled():
    packages = [{"name": "com.video", "versions": ["1"]}, {"name": "com.video", "versions": ["2"]}]
    rows = [
        {"bundle_id": "a", "packages": packages},
        {"bundle_id": "a", "packages": [{"name": "com.video", "versions": []}]},
        {"bundle_id": "b", "packages": packages},
        {"bundle_id": "disabled", "packages": [{"name": "com.hidden", "versions": []}]},
    ]
    apps = build_apps(
        rows,
        {"a": {}, "b": {}},
        {
            "apps": {
                "com.video": {
                    "app_name": "Video",
                    "icon_url": "https://images.test/v.png",
                    "metadata_source": "google_play",
                }
            }
        },
    )
    assert len(apps) == 1
    assert apps[0]["patch_count"] == 3
    assert apps[0]["bundle_count"] == 2
    assert apps[0]["versions"] == ["1", "2"]
    assert apps[0]["unrestricted"]
    assert apps[0]["bundles"][0]["patch_count"] == 2


def test_metadata_published_in_app_indexes_and_patch_packages(tmp_path):
    fixture(tmp_path)
    write(
        tmp_path / "database/app-metadata.json",
        {
            "apps": {
                "com.video": {
                    "app_name": "Video",
                    "icon_url": "https://images.test/v.png",
                    "metadata_source": "google_play",
                }
            }
        },
    )
    path, bundles = snapshot(tmp_path, db.build_database(tmp_path))
    app = db.read_json(path / "apps.json")[0]
    selectors = db.read_json(path / "app-bundles.json")
    assert selectors[0]["id"] == bundles[0]["id"]
    assert selectors[0]["packages"] == bundles[0]["packages"]
    assert "description" not in selectors[0]
    assert app["app_name"] == "Video" and app["bundle_count"] == 1
    association = db.read_json(path / f"bundle-apps/{bundles[0]['id']}.json")[0]
    assert association["app_name"] == "Video" and association["patch_count"] == 1
    patch = db.read_json(path / "patches/00000.json")[0]
    assert patch["packages"][0]["app_name"] == "Video"
    assert patch["packages"][0]["icon_url"] == "https://images.test/v.png"
    write(tmp_path / "config/apps.json", {"com.video": {"app_name": "Corrected"}})
    path, _ = snapshot(tmp_path, db.build_database(tmp_path))
    assert db.read_json(path / "apps.json")[0]["app_name"] == "Corrected"
    assert db.read_json(path / "apps.json")[0]["metadata_source"] == "manual"


def test_integrations_switch_is_explicit_and_legacy_inputs_remain_compatible(tmp_path):
    url = "https://github.com/owner/patches"
    integration = "https://github.com/owner/integrations"
    disabled = source_channels(
        {
            "test": {
                "patches": url,
                "disabled": False,
                "integrations": False,
                "integration": integration,
            }
        },
        tmp_path,
    )
    assert all("integration" not in source for source in disabled.values())
    enabled = source_channels(
        {
            "test": {
                "patches": url,
                "disabled": False,
                "integrations": True,
                "integration": integration,
            }
        },
        tmp_path,
    )
    assert all(source["integration"] == integration for source in enabled.values())
    legacy = source_channels(
        {"test-stable": {"patches": url, "integration": integration}}, tmp_path
    )
    assert legacy["test-stable"]["integration"] == integration
    for value in ("false", 0, None):
        with pytest.raises(ValueError, match="boolean"):
            source_channels({"test": {"patches": url, "integrations": value}}, tmp_path)
    with pytest.raises(ValueError, match="required"):
        source_channels({"test": {"patches": url, "integrations": True}}, tmp_path)


def test_metadata_parsers_handle_bad_indexes_and_array_jsonld_types():
    for payload in (
        None,
        [],
        {"packages": {"com.video": None}},
        {"packages": {"com.video": {"metadata": None}}},
        {"apps": [None]},
    ):
        with pytest.raises(ValueError):
            fdroid_apps(payload)
    parser = PlayMetadata()
    parser.feed(
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@type": ["SoftwareApplication"],
                "name": "Video",
                "image": "https://images.test/v.png",
            }
        )
        + "</script>"
    )
    assert parser.metadata()["app_name"] == "Video"


def test_unlisted_success_page_does_not_starve_later_play_lookups(tmp_path):
    prepare(tmp_path, ["com.one", "com.two"])

    def handler(request):
        if request.url.host == "f-droid.org":
            return httpx.Response(200, json=index_v2())
        return httpx.Response(
            200,
            text=(
                play("Two")
                if request.url.params["id"] == "com.two"
                else "<title>Item unavailable</title>"
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = refresh(tmp_path, client, now=NOW, sleep=lambda _: None)
    assert state["apps"]["com.one"]["checked_at"] == NOW.isoformat()
    assert state["apps"]["com.two"]["app_name"] == "Two"
    assert "play_retry_after" not in state


def test_consent_pages_and_http_date_retry_after_pause_play_requests(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.refresh_app_metadata.time.monotonic", lambda: 0)
    prepare(tmp_path, ["com.one", "com.two"])
    calls = []

    def consent(request):
        calls.append(request)
        if request.url.host == "f-droid.org":
            return httpx.Response(200, json=index_v2())
        return httpx.Response(200, text="<title>Before you continue to Google</title>")

    with httpx.Client(transport=httpx.MockTransport(consent)) as client:
        state = refresh(tmp_path, client, now=NOW, sleep=lambda _: None)
    assert len(calls) == 2
    assert state["apps"] == {}
    assert datetime.fromisoformat(state["play_retry_after"]) == NOW + timedelta(hours=1)

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, headers={"Retry-After": "Sat, 10 Oct 2026 04:00:00 GMT"})
        )
    ) as client:
        state = refresh(tmp_path, client, now=NOW + timedelta(hours=2))
    assert datetime.fromisoformat(state["play_retry_after"]) == NOW + timedelta(hours=4)


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("retry", ["120", "Sat, 10 Oct 2026 00:03:30 GMT"])
def test_play_retry_after_starts_at_response_and_does_not_consume_body(
    tmp_path, monkeypatch, status, retry
):
    prepare(tmp_path, ["com.one"])
    clock, calls = [0], []
    monkeypatch.setattr("scripts.refresh_app_metadata.time.monotonic", lambda: clock[0])

    class UnreadBody(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("Rate-limit responses should not be downloaded")

    def handler(request):
        if request.url.host == "f-droid.org":
            return httpx.Response(200, json=index_v2())
        calls.append(request)
        clock[0] += 90
        return httpx.Response(status, headers={"Retry-After": retry}, stream=UnreadBody())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = refresh(tmp_path, client, now=NOW, sleep=lambda _: None)
        assert datetime.fromisoformat(state["play_retry_after"]) == NOW + timedelta(seconds=210)
        refresh(tmp_path, client, now=NOW + timedelta(seconds=150), sleep=lambda _: None)
    assert len(calls) == 1


def test_app_index_ignores_human_labels_used_as_package_names():
    rows = [
        {
            "bundle_id": "a",
            "packages": [
                {"name": name, "versions": []}
                for name in ("#Notepad", "Adobe Lightroom", "com.adobe.lrmobile")
            ],
        }
    ]
    apps = build_apps(rows, {"a": {}}, {})
    assert [app["package_name"] for app in apps] == ["com.adobe.lrmobile"]


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("retry", ["1200", "Sat, 10 Oct 2026 00:21:30 GMT"])
def test_fdroid_retry_after_retains_cache_and_pauses_both_indexes(
    tmp_path, monkeypatch, status, retry
):
    prepare(tmp_path, ["org.example.free"])
    write(
        tmp_path / "database/app-metadata.json",
        {"fdroid": {"applications": {"org.example.free": {"app_name": "Cached"}}}},
    )
    clock, calls = [0], []
    monkeypatch.setattr("scripts.refresh_app_metadata.time.monotonic", lambda: clock[0])

    class UnreadBody(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("Rate-limit responses should not be downloaded")

    def handler(request):
        calls.append(request)
        clock[0] += 90
        if len(calls) == 1:
            return httpx.Response(status, headers={"Retry-After": retry}, stream=UnreadBody())
        return httpx.Response(200, json=index_v2())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = refresh(tmp_path, client, now=NOW, max_play=0)
        assert state["apps"]["org.example.free"]["app_name"] == "Cached"
        assert datetime.fromisoformat(state["fdroid"]["retry_after"]) == NOW + timedelta(
            seconds=1290
        )
        assert len(calls) == 1
        refresh(tmp_path, client, now=NOW + timedelta(minutes=10), max_play=0)
        assert len(calls) == 1
        state = refresh(tmp_path, client, now=NOW + timedelta(seconds=1290), max_play=0)
    assert len(calls) == 2
    assert state["apps"]["org.example.free"]["app_name"] == "Free App"
    assert "retry_after" not in state["fdroid"]


@pytest.mark.parametrize("retry", ["invalid", "9" * 40])
def test_provider_retry_after_handles_invalid_and_overflowing_values(retry):
    from scripts.refresh_app_metadata import provider_retry_after

    response = httpx.Response(429, headers={"Retry-After": retry})
    assert datetime.fromisoformat(provider_retry_after(response, NOW)) == NOW + timedelta(hours=1)


def test_initial_metadata_backfill_covers_more_than_fifty_apps(tmp_path):
    packages = [f"com.app{i:03}" for i in range(60)]
    prepare(tmp_path, packages)
    calls = []

    def handler(request):
        if request.url.host == "f-droid.org":
            return httpx.Response(200, json=index_v2())
        calls.append(request.url.params["id"])
        return httpx.Response(200, text=play())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = refresh(tmp_path, client, now=NOW, sleep=lambda _: None)
        assert len(calls) == 60
        assert all(state["apps"][package]["app_name"] for package in packages)
        refresh(tmp_path, client, now=NOW + timedelta(hours=1), sleep=lambda _: None)
        assert len(calls) == 60  # Completed metadata is cached.


def test_backfill_checkpoint_survives_interruption_and_resumes(tmp_path):
    packages = [f"com.app{i:03}" for i in range(60)]
    prepare(tmp_path, packages)
    calls = []
    interrupted = False

    def handler(request):
        nonlocal interrupted
        if request.url.host == "f-droid.org":
            return httpx.Response(200, json=index_v2())
        package = request.url.params["id"]
        if len(calls) == 25 and not interrupted:
            interrupted = True
            raise KeyboardInterrupt()
        calls.append(package)
        return httpx.Response(200, text=play())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(KeyboardInterrupt):
            refresh(tmp_path, client, now=NOW, sleep=lambda _: None)
        assert len(db.read_json(tmp_path / "database/app-metadata.json")["apps"]) == 25
        state = refresh(tmp_path, client, now=NOW, sleep=lambda _: None)
    assert len(calls) == len(set(calls)) == 60
    assert len(state["apps"]) == 60


def test_backfill_deadline_and_normal_refresh_caps_remain_bounded(tmp_path, monkeypatch):
    packages = [f"com.app{i:03}" for i in range(60)]
    prepare(tmp_path, packages)
    clock, calls = [0], []
    monkeypatch.setattr("scripts.refresh_app_metadata.time.monotonic", lambda: clock[0])

    def handler(request):
        if request.url.host == "f-droid.org":
            return httpx.Response(200, json=index_v2())
        calls.append(request.url.params["id"])
        clock[0] += 20
        return httpx.Response(200, text=play())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        refresh(tmp_path, client, now=NOW, budget=60, sleep=lambda _: None)
    assert len(calls) == 3  # A response arriving at the deadline is discarded.
    assert len(db.read_json(tmp_path / "database/app-metadata.json")["apps"]) == 2
    write(
        tmp_path / "database/app-metadata.json",
        {
            "apps": {
                package: {"checked_at": (NOW - timedelta(days=40)).isoformat()}
                for package in packages
            }
        },
    )
    clock[0], calls[:] = 0, []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        refresh(tmp_path, client, now=NOW, budget=10000, sleep=lambda _: None)
    assert len(calls) == 50  # Already attempted packages use the regular cap.


def test_initial_backfill_has_a_hard_lookup_cap(tmp_path, monkeypatch):
    packages = [f"com.app{i:04}" for i in range(1501)]
    prepare(tmp_path, packages)
    monkeypatch.setattr("scripts.refresh_app_metadata.time.monotonic", lambda: 0)
    calls = []

    def handler(request):
        if request.url.host == "f-droid.org":
            return httpx.Response(200, json=index_v2())
        calls.append(request.url.params["id"])
        return httpx.Response(200, text=play())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = refresh(tmp_path, client, now=NOW, sleep=lambda _: None)
    assert len(calls) == len(state["apps"]) == 1500
    assert packages[-1] not in state["apps"]
