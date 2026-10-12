"""Bulk-provider caching and regional Play behavior with real request budgets."""

from datetime import timedelta

import httpx
import pytest
from test_app_catalog import NOW, index_v2, play, prepare
from test_generate_database import write

from scripts import refresh_app_metadata as metadata


def test_independent_bulk_indexes_priority_icons_and_conditional_revalidation(tmp_path):
    prepare(tmp_path, ["org.example.free", "com.izzy"])
    calls = []

    def handler(request):
        calls.append(request)
        if request.headers.get("if-none-match"):
            assert request.headers["if-none-match"] == request.url.host
            return httpx.Response(304)
        payload = index_v2()
        if request.url.host == "apt.izzysoft.de":
            payload["packages"]["org.example.free"]["metadata"]["name"] = "Lower priority"
            payload["packages"]["com.izzy"] = {
                "metadata": {
                    "name": {"en": "Izzy App"},
                    "icon": {"en": {"name": "/com.izzy/en/icon.png"}},
                }
            }
        return httpx.Response(200, json=payload, headers={"ETag": request.url.host})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = metadata.refresh(tmp_path, client, now=NOW)
        assert len(calls) == 2
        assert state["apps"]["org.example.free"]["app_name"] == "Free App"
        assert state["apps"]["com.izzy"]["metadata_source"] == "izzyondroid"
        assert state["apps"]["com.izzy"]["icon_url"] == (
            "https://apt.izzysoft.de/fdroid/repo/com.izzy/en/icon.png"
        )
        metadata.refresh(tmp_path, client, now=NOW + timedelta(hours=1))
        assert len(calls) == 2
        state = metadata.refresh(tmp_path, client, now=NOW + timedelta(days=1), max_play=0)
    assert len(calls) == 4
    assert state["apps"]["com.izzy"]["app_name"] == "Izzy App"


def test_izzy_v1_fallback_and_provider_failure_do_not_replace_other_cache(tmp_path):
    prepare(tmp_path, ["com.izzy", "org.example.free"])
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.host == "f-droid.org":
            return httpx.Response(200, json=index_v2())
        if request.url.path.endswith("index-v2.json"):
            return httpx.Response(404)
        return httpx.Response(
            200, json={"apps": [{"packageName": "com.izzy", "name": "Izzy", "icon": "icon.png"}]}
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = metadata.refresh(tmp_path, client, now=NOW, max_play=0)
    assert state["apps"]["com.izzy"]["icon_url"].endswith("/repo/icons/icon.png")
    assert state["izzyondroid"]["url"].endswith("index-v1.json")

    def unavailable(request):
        if request.url.host == "apt.izzysoft.de":
            assert "if-none-match" not in request.headers
            return httpx.Response(429, headers={"Retry-After": "7200"})
        return httpx.Response(200, json=index_v2())

    with httpx.Client(transport=httpx.MockTransport(unavailable)) as client:
        state = metadata.refresh(tmp_path, client, now=NOW + timedelta(days=2), max_play=0)
    assert state["apps"]["com.izzy"]["app_name"] == "Izzy"
    assert state["apps"]["org.example.free"]["app_name"] == "Free App"
    assert state["izzyondroid"]["retry_after"]


def test_regional_success_uses_shared_limits_pacing_cache_and_preferred_country(tmp_path):
    prepare(tmp_path, ["com.regional"])
    calls, delays = [], []

    def handler(request):
        if request.url.host != "play.google.com":
            return httpx.Response(200, json=index_v2())
        calls.append(request.url.params["gl"])
        return (
            httpx.Response(200, text=play("Regional")) if calls[-1] == "GB" else httpx.Response(404)
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = metadata.refresh(tmp_path, client, now=NOW, max_play=3, sleep=delays.append)
        assert calls == ["US", "GB"]
        assert delays == [1]
        assert state["apps"]["com.regional"]["play_country"] == "GB"
        metadata.refresh(tmp_path, client, now=NOW + timedelta(days=1), sleep=delays.append)
        assert calls == ["US", "GB"]
        metadata.refresh(tmp_path, client, now=NOW + timedelta(days=31), sleep=delays.append)
    assert calls == ["US", "GB", "GB"]


def test_partial_regional_search_resumes_without_negative_caching_or_repeating_us(tmp_path):
    prepare(tmp_path, ["com.regional", "com.zlater"])
    calls = []

    def handler(request):
        if request.url.host != "play.google.com":
            return httpx.Response(200, json=index_v2())
        calls.append((request.url.params["id"], request.url.params["gl"]))
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        for hour in range(3):
            state = metadata.refresh(
                tmp_path, client, now=NOW + timedelta(hours=hour), max_play=1, sleep=lambda _: None
            )
            if hour < 2:
                assert not state["apps"]["com.regional"].get("checked_at")
        metadata.refresh(
            tmp_path, client, now=NOW + timedelta(hours=3), max_play=1, sleep=lambda _: None
        )
    assert calls == [("com.regional", country) for country in ("US", "GB", "IN")] + [
        ("com.zlater", "US")
    ]
    assert state["apps"]["com.regional"]["checked_at"]


def test_existing_us_negative_cache_tries_new_regions_without_waiting_seven_days(tmp_path):
    prepare(tmp_path, ["com.regional"])
    write(
        tmp_path / "database/app-metadata.json",
        {"apps": {"com.regional": {"checked_at": NOW.isoformat()}}},
    )
    calls = []

    def handler(request):
        if request.url.host != "play.google.com":
            return httpx.Response(200, json=index_v2())
        calls.append(request.url.params["gl"])
        return httpx.Response(200, text=play())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = metadata.refresh(tmp_path, client, now=NOW + timedelta(hours=1))
    assert calls == ["GB"]
    assert state["apps"]["com.regional"]["app_name"] == "Video App"


def test_negative_cache_only_after_all_markets_and_retries_after_ttl(tmp_path):
    prepare(tmp_path, ["com.missing"])
    calls = []

    def handler(request):
        if request.url.host != "play.google.com":
            return httpx.Response(200, json=index_v2())
        calls.append(request.url.params["gl"])
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = metadata.refresh(tmp_path, client, now=NOW, sleep=lambda _: None)
        assert state["apps"]["com.missing"]["play_checked_countries"] == ["US", "GB", "IN"]
        metadata.refresh(tmp_path, client, now=NOW + timedelta(days=1), sleep=lambda _: None)
        assert calls == ["US", "GB", "IN"]
        metadata.refresh(tmp_path, client, now=NOW + timedelta(days=8), sleep=lambda _: None)
    assert calls == ["US", "GB", "IN"] * 2


@pytest.mark.parametrize(
    "status, body",
    [
        (429, ""),
        (503, ""),
        (500, ""),
        (200, "<title>Before you continue to Google</title>"),
    ],
)
def test_provider_errors_never_trigger_regional_fanout(tmp_path, status, body):
    prepare(tmp_path, ["com.cached"])
    write(
        tmp_path / "database/app-metadata.json",
        {
            "apps": {
                "com.cached": {
                    "app_name": "Keep me",
                    "icon_url": "https://img.test/icon.png",
                    "checked_at": (NOW - timedelta(days=40)).isoformat(),
                }
            }
        },
    )
    calls = []

    def handler(request):
        if request.url.host != "play.google.com":
            return httpx.Response(200, json=index_v2())
        calls.append(request)
        return httpx.Response(status, text=body, headers={"Retry-After": "120"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = metadata.refresh(tmp_path, client, now=NOW, sleep=lambda _: None)
    assert len(calls) == 1
    assert state["apps"]["com.cached"]["app_name"] == "Keep me"
    assert state["apps"]["com.cached"]["icon_url"] == "https://img.test/icon.png"
    assert state["play_retry_after"]


def test_deadline_prevents_another_provider_or_regional_request(tmp_path, monkeypatch):
    prepare(tmp_path, ["com.missing"])
    clock, calls = [0], []
    monkeypatch.setattr(metadata.time, "monotonic", lambda: clock[0])

    def handler(request):
        calls.append(request)
        clock[0] = 10
        return httpx.Response(200, json=index_v2())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        metadata.refresh(tmp_path, client, now=NOW, budget=10)
    assert len(calls) == 1


def test_indexes_fill_missing_icons_without_losing_preferred_names(tmp_path):
    prepare(tmp_path, ["org.example.free"])

    def handler(request):
        payload = index_v2()
        entry = payload["packages"]["org.example.free"]["metadata"]
        if request.url.host == "f-droid.org":
            del entry["icon"]
        else:
            entry["name"] = "Lower priority name"
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = metadata.refresh(tmp_path, client, now=NOW)
    assert state["apps"]["org.example.free"]["app_name"] == "Free App"
    assert state["apps"]["org.example.free"]["icon_url"].startswith("https://apt.izzysoft.de/")


def test_izzy_metadata_is_published_to_app_and_patch_indexes(tmp_path):
    from test_generate_database import db, fixture, snapshot

    fixture(tmp_path)
    write(
        tmp_path / "database/app-metadata.json",
        {
            "apps": {
                "com.video": {
                    "app_name": "Video",
                    "metadata_source": "izzyondroid",
                    "icon_url": "https://apt.izzysoft.de/fdroid/repo/icon.png",
                }
            }
        },
    )
    path, bundles = snapshot(tmp_path, db.build_database(tmp_path))
    app = db.read_json(path / "apps.json")[0]
    assert app["app_name"] == "Video"
    assert app["metadata_source"] == "izzyondroid"
    assert (
        db.read_json(path / f"bundle-apps/{bundles[0]['id']}.json")[0]["icon_url"]
        == app["icon_url"]
    )
    assert db.read_json(path / "patches/00000.json")[0]["packages"][0]["app_name"] == "Video"
