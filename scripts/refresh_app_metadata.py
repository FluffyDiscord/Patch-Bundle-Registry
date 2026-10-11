"""Cache F-Droid index metadata and bounded Google Play lookups during refreshes."""

import argparse
import json
import re
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote

import httpx

try:
    from scripts.app_catalog import safe_icon
    from scripts.generate_database import ROOT, encoded, read_json
except ModuleNotFoundError:
    from app_catalog import safe_icon
    from generate_database import ROOT, encoded, read_json

INDEXES = (
    "https://f-droid.org/repo/index-v2.json",
    "https://f-droid.org/repo/index-v1.json",
)
MAX_INDEX_BYTES = 256 * 1024 * 1024
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+")


def localized(value):
    if not isinstance(value, dict):
        return value
    for locale in ("en-US", "en-GB", "en"):
        if value.get(locale):
            return value[locale]
    return next((item for item in value.values() if item), None)


def fdroid_apps(payload):
    result = {}
    if not isinstance(payload, dict):
        raise ValueError("Unrecognized F-Droid index")
    if isinstance(payload.get("packages"), dict) and "apps" not in payload:
        entries = (
            (name, item.get("metadata", {}) if isinstance(item, dict) else {})
            for name, item in payload["packages"].items()
        )
        version = 2
    elif isinstance(payload.get("apps"), list):
        entries = (
            (item.get("packageName"), item) for item in payload["apps"] if isinstance(item, dict)
        )
        version = 1
    else:
        raise ValueError("Unrecognized F-Droid index")
    for package, metadata in entries:
        if not isinstance(metadata, dict):
            continue
        if not isinstance(package, str) or not PACKAGE.fullmatch(package):
            continue
        name = localized(metadata.get("name"))
        if not isinstance(name, str) or not name.strip():
            continue
        icon = metadata.get("icon")
        if version == 2:
            icon = (
                icon
                if isinstance(icon, dict) and isinstance(icon.get("name"), str)
                else localized(icon)
            )
            icon = icon.get("name") if isinstance(icon, dict) else icon
        elif icon:
            icon = "icons/" + icon
        if isinstance(icon, str) and ".." not in icon.split("/"):
            icon = "https://f-droid.org/repo/" + quote(icon.lstrip("/"), safe="/-._~")
        else:
            icon = None
        result[package] = {"app_name": name.strip(), "icon_url": icon, "metadata_source": "fdroid"}
    if not result:
        raise ValueError("F-Droid index contains no application metadata")
    return result


class PlayMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = {}
        self.structured = []
        self.in_json = False
        self.json_parts = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta":
            key = values.get("property") or values.get("name")
            if key in {"og:title", "og:image"}:
                self.values[key] = values.get("content")
        if tag == "script" and values.get("type") == "application/ld+json":
            self.in_json = True
            self.json_parts = []

    def handle_data(self, data):
        if self.in_json:
            self.json_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.in_json:
            try:
                data = json.loads("".join(self.json_parts))
                self.structured.extend(data if isinstance(data, list) else [data])
            except ValueError:
                pass
            self.in_json = False

    def metadata(self):
        for value in self.structured:
            if not isinstance(value, dict):
                continue
            kinds = value.get("@type", [])
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if isinstance(kinds, list) and any(
                kind
                in {
                    "SoftwareApplication",
                    "MobileApplication",
                }
                for kind in kinds
                if isinstance(kind, str)
            ):
                name = value.get("name")
                if isinstance(name, str) and name.strip():
                    image = value.get("image")
                    image = image.get("url") if isinstance(image, dict) else image
                    return {
                        "app_name": unescape(name.strip()),
                        "icon_url": safe_icon(image),
                        "metadata_source": "google_play",
                    }
        title = self.values.get("og:title")
        image = safe_icon(self.values.get("og:image"))
        if isinstance(title, str) and title.endswith(" - Apps on Google Play") and image:
            return {
                "app_name": title.removesuffix(" - Apps on Google Play").strip(),
                "icon_url": image,
                "metadata_source": "google_play",
            }
        return None


def expired(stamp, now, ttl):
    try:
        return now - datetime.fromisoformat(stamp) >= ttl
    except (TypeError, ValueError):
        return True


def provider_retry_after(response, received_at):
    retry = response.headers.get("retry-after", "3600").strip()
    try:
        seconds = (
            int(retry)
            if retry.isdigit()
            else (parsedate_to_datetime(retry) - received_at).total_seconds()
        )
        return (received_at + timedelta(seconds=max(60, seconds))).isoformat()
    except (ValueError, TypeError, OverflowError):
        return (received_at + timedelta(hours=1)).isoformat()


def fetch_index(client, url, headers, deadline):
    with client.stream("GET", url, headers=headers) as response:
        if response.status_code == 304:
            return None, dict(response.headers)
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_INDEX_BYTES or time.monotonic() >= deadline:
                raise ValueError("F-Droid index exceeds download budget")
            chunks.append(chunk)
        return fdroid_apps(json.loads(b"".join(chunks))), dict(response.headers)


def refresh(root, client, *, now=None, max_play=50, delay=1, sleep=time.sleep, budget=180):
    now = now or datetime.now(UTC)
    stamp = now.isoformat()
    started = time.monotonic()
    deadline = started + budget

    def current_time():
        return now + timedelta(seconds=time.monotonic() - started)

    path = root / "database/app-metadata.json"
    state = read_json(path, {})
    fdroid = state.setdefault("fdroid", {})
    apps = state.setdefault("apps", {})
    manifest = read_json(root / "database/manifest.json", {})
    snapshot = root / "database" / manifest.get("snapshot", "")
    catalog_apps = read_json(snapshot / "apps.json", [])
    if not catalog_apps:
        catalog_apps = [
            {"package_name": item["name"], "bundle_count": 0}
            for item in read_json(snapshot / "packages.json", [])
        ]
    wanted = {item["package_name"]: item for item in catalog_apps}
    if (
        wanted
        and expired(fdroid.get("checked_at"), now, timedelta(days=1))
        and expired(fdroid.get("attempted_at"), now, timedelta(minutes=10))
        and expired(fdroid.get("retry_after"), now, timedelta(0))
    ):
        fdroid["attempted_at"] = stamp
        for url in INDEXES:
            headers = {}
            if fdroid.get("url") == url and fdroid.get("applications"):
                if fdroid.get("etag"):
                    headers["If-None-Match"] = fdroid["etag"]
                if fdroid.get("last_modified"):
                    headers["If-Modified-Since"] = fdroid["last_modified"]
            try:
                applications, received = fetch_index(client, url, headers, deadline)
                if applications is None and not fdroid.get("applications"):
                    raise ValueError("F-Droid returned 304 without a cached index")
                if fdroid.get("url") != url:
                    fdroid.pop("etag", None)
                    fdroid.pop("last_modified", None)
                fdroid.update(
                    checked_at=stamp,
                    url=url,
                    etag=received.get("etag", fdroid.get("etag")),
                    last_modified=received.get("last-modified", fdroid.get("last_modified")),
                )
                if applications is not None:
                    fdroid["applications"] = applications
                fdroid.pop("retry_after", None)
                break
            except (httpx.HTTPError, ValueError, TypeError) as error:
                print("Application metadata: F-Droid index unavailable; retaining cached names.")
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {
                    429,
                    503,
                }:
                    fdroid["retry_after"] = provider_retry_after(error.response, current_time())
                    break
                if time.monotonic() >= deadline:
                    break
    indexed = fdroid.get("applications", {})
    for package in wanted:
        if package in indexed:
            apps[package] = {**indexed[package], "checked_at": fdroid.get("checked_at")}
    overrides = read_json(root / "config/apps.json", {})
    candidates = sorted(
        wanted, key=lambda package: (-wanted[package].get("bundle_count", 0), package)
    )
    lookups = 0
    for package in candidates:
        if package in indexed or package in overrides or not PACKAGE.fullmatch(package):
            continue
        cached = apps.get(package, {})
        ttl = timedelta(days=30 if cached.get("app_name") else 7)
        if not expired(cached.get("checked_at"), now, ttl):
            continue
        if lookups >= max_play or time.monotonic() >= deadline:
            break
        if not expired(state.get("play_retry_after"), now, timedelta(0)):
            break
        if lookups:
            sleep(delay)
        if time.monotonic() >= deadline:
            break
        lookups += 1
        url = (
            "https://play.google.com/store/apps/details?id="
            + quote(package, safe="")
            + "&hl=en&gl=US"
        )
        try:
            with client.stream("GET", url) as response:
                if response.status_code in {429, 503}:
                    state["play_retry_after"] = provider_retry_after(response, current_time())
                    break
                if response.status_code == 404:
                    apps[package] = {**cached, "checked_at": stamp}
                    continue
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 4 * 1024 * 1024 or time.monotonic() >= deadline:
                        raise ValueError("Google Play response exceeds metadata budget")
                    chunks.append(chunk)
                content = b"".join(chunks).decode("utf-8", errors="replace")
            parser = PlayMetadata()
            parser.feed(content)
            found = parser.metadata()
            if found:
                apps[package] = {**found, "checked_at": stamp}
            else:
                # Do not let one unlisted package block all later lookups.
                if response.url.host != "play.google.com" or re.search(
                    r"<title[^>]*>\s*Before you continue", content, re.IGNORECASE
                ):
                    state["play_retry_after"] = (current_time() + timedelta(hours=1)).isoformat()
                    break
                apps[package] = {**cached, "checked_at": stamp}
        except (httpx.HTTPError, ValueError):
            state["play_retry_after"] = (current_time() + timedelta(hours=1)).isoformat()
            break
    state["apps"] = {key: value for key, value in apps.items() if key in wanted}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(encoded(state))
    temporary.replace(path)
    print(f"Application metadata: {len(state['apps'])} cached apps; {lookups} Google Play lookups.")
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--play-limit", type=int, default=50)
    args = parser.parse_args()
    with httpx.Client(
        timeout=httpx.Timeout(20, connect=10),
        follow_redirects=True,
        headers={"User-Agent": "Patch-Bundle-Registry/1.0"},
    ) as client:
        refresh(ROOT, client, max_play=args.play_limit)
