"""Cache repository JSON indexes and bounded regional Google Play metadata."""

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
REPOSITORIES = (
    ("fdroid", "https://f-droid.org/repo/", INDEXES),
    (
        "izzyondroid",
        "https://apt.izzysoft.de/fdroid/repo/",
        (
            "https://apt.izzysoft.de/fdroid/repo/index-v2.json",
            "https://apt.izzysoft.de/fdroid/repo/index-v1.json",
        ),
    ),
)
PLAY_COUNTRIES = ("US", "GB", "IN")
MAX_INDEX_BYTES = 256 * 1024 * 1024
PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+")


def localized(value):
    if not isinstance(value, dict):
        return value
    for locale in ("en-US", "en-GB", "en"):
        if value.get(locale):
            return value[locale]
    return next((item for item in value.values() if item), None)


def fdroid_apps(payload, base_url="https://f-droid.org/repo/", source="fdroid"):
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
            icon = base_url + quote(icon.lstrip("/"), safe="/-._~")
        else:
            icon = None
        result[package] = {"app_name": name.strip(), "icon_url": icon, "metadata_source": source}
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


def fetch_index(client, url, headers, deadline, base_url, source):
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
        return fdroid_apps(json.loads(b"".join(chunks)), base_url, source), dict(response.headers)


def refresh(root, client, *, now=None, max_play=None, delay=1, sleep=time.sleep, budget=None):
    now = now or datetime.now(UTC)
    stamp = now.isoformat()
    started = time.monotonic()
    deadline = float("inf")  # Set after inspecting the backlog, before any requests.

    def current_time():
        return now + timedelta(seconds=time.monotonic() - started)

    path = root / "database/app-metadata.json"
    state = read_json(path, {})
    repositories = [
        (source, base, urls, state.setdefault(source, {})) for source, base, urls in REPOSITORIES
    ]
    cached_index = {
        package for _, _, _, cache in repositories for package in cache.get("applications", {})
    }
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
    overrides = read_json(root / "config/apps.json", {})

    def unchecked(package):
        return not apps.get(package, {}).get("checked_at")

    pending = sum(
        unchecked(package)
        and package not in cached_index
        and package not in overrides
        and PACKAGE.fullmatch(package) is not None
        for package in wanted
    )
    backfill = pending > 50 and (max_play is None or max_play > 50)
    max_play = (1500 if backfill else 50) if max_play is None else max_play
    budget = (1800 if backfill else 180) if budget is None else budget
    deadline = started + budget

    def save_state():
        state["apps"] = {key: value for key, value in apps.items() if key in wanted}
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(encoded(state))
        temporary.replace(path)

    for source, base_url, urls, cache in repositories:
        if (
            wanted
            and time.monotonic() < deadline
            and expired(cache.get("checked_at"), now, timedelta(days=1))
            and expired(cache.get("attempted_at"), now, timedelta(minutes=10))
            and expired(cache.get("retry_after"), now, timedelta(0))
        ):
            cache["attempted_at"] = stamp
            for url in urls:
                headers = {}
                if cache.get("url") == url and cache.get("applications"):
                    if cache.get("etag"):
                        headers["If-None-Match"] = cache["etag"]
                    if cache.get("last_modified"):
                        headers["If-Modified-Since"] = cache["last_modified"]
                try:
                    applications, received = fetch_index(
                        client, url, headers, deadline, base_url, source
                    )
                    if applications is None and not cache.get("applications"):
                        raise ValueError("F-Droid returned 304 without a cached index")
                    if cache.get("url") != url:
                        cache.pop("etag", None)
                        cache.pop("last_modified", None)
                    cache.update(
                        checked_at=stamp,
                        url=url,
                        etag=received.get("etag", cache.get("etag")),
                        last_modified=received.get("last-modified", cache.get("last_modified")),
                    )
                    if applications is not None:
                        cache["applications"] = applications
                    cache.pop("retry_after", None)
                    break
                except (httpx.HTTPError, ValueError, TypeError) as error:
                    print(
                        f"Application metadata: {source} index unavailable; retaining cached names."
                    )
                    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {
                        429,
                        503,
                    }:
                        cache["retry_after"] = provider_retry_after(error.response, current_time())
                        break
                    if time.monotonic() >= deadline:
                        break

    indexed = {}
    for _, _, _, cache in repositories:
        for package, info in cache.get("applications", {}).items():
            indexed.setdefault(package, {**info, "checked_at": cache.get("checked_at")})
            if not indexed[package].get("icon_url") and info.get("icon_url"):
                indexed[package]["icon_url"] = info["icon_url"]
    for package in wanted:
        if package in indexed:
            apps[package] = {
                **indexed[package],
                "icon_url": indexed[package].get("icon_url")
                or apps.get(package, {}).get("icon_url"),
            }
    save_state()  # Preserve the bulk index even if a long backfill is interrupted.
    candidates = sorted(
        wanted,
        key=lambda package: (
            not unchecked(package),
            -wanted[package].get("bundle_count", 0),
            package,
        ),
    )
    lookups = 0

    halted = False
    for package in candidates:
        if package in indexed or package in overrides or not PACKAGE.fullmatch(package):
            continue
        cached = apps.get(package, {})
        ttl = timedelta(days=30 if cached.get("app_name") else 7)
        countries = list(PLAY_COUNTRIES)
        if cached.get("play_country") in countries:
            countries.remove(cached["play_country"])
            countries.insert(0, cached["play_country"])
        attempted = cached.get("play_checked_countries", [])
        # Old negative entries covered the US only. Try the new regions now.
        if not attempted and cached.get("checked_at") and not cached.get("app_name"):
            attempted = ["US"]
        if not expired(cached.get("checked_at"), now, ttl):
            if cached.get("app_name") or set(countries).issubset(attempted):
                continue
        if expired(cached.get("play_attempted_at", cached.get("checked_at")), now, ttl):
            attempted = []
        for country in countries:
            if country in attempted:
                continue
            if (
                lookups >= max_play
                or time.monotonic() >= deadline
                or not expired(state.get("play_retry_after"), current_time(), timedelta(0))
            ):
                halted = True
                break
            if lookups:
                sleep(delay)
            if time.monotonic() >= deadline:
                halted = True
                break
            if lookups and lookups % 25 == 0:
                save_state()
                print(
                    f"Application metadata: checkpoint after {lookups} Google Play lookups.",
                    flush=True,
                )
            lookups += 1
            url = (
                "https://play.google.com/store/apps/details?id="
                + quote(package, safe="")
                + "&hl=en&gl="
                + country
            )
            try:
                found = None
                with client.stream("GET", url) as response:
                    if response.status_code in {429, 503}:
                        state["play_retry_after"] = provider_retry_after(response, current_time())
                        halted = True
                        break
                    if time.monotonic() >= deadline:
                        halted = True
                        break
                    if response.status_code != 404:
                        response.raise_for_status()
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > 4 * 1024 * 1024 or time.monotonic() >= deadline:
                                raise ValueError("Google Play response exceeds metadata budget")
                            chunks.append(chunk)
                        content = b"".join(chunks).decode("utf-8", errors="replace")
                        if response.url.host != "play.google.com" or re.search(
                            r"<title[^>]*>\s*Before you continue", content, re.IGNORECASE
                        ):
                            state["play_retry_after"] = (
                                current_time() + timedelta(hours=1)
                            ).isoformat()
                            halted = True
                            break
                        parser = PlayMetadata()
                        parser.feed(content)
                        found = parser.metadata()
                if found:
                    apps[package] = {
                        **found,
                        "icon_url": found.get("icon_url") or cached.get("icon_url"),
                        "checked_at": stamp,
                        "play_country": country,
                    }
                    break
                attempted = [*attempted, country]
                cached = {
                    **cached,
                    "play_checked_countries": attempted,
                    "play_attempted_at": current_time().isoformat(),
                }
                if set(countries).issubset(attempted):
                    cached["checked_at"] = stamp
                apps[package] = cached
            except (httpx.HTTPError, ValueError):
                state["play_retry_after"] = (current_time() + timedelta(hours=1)).isoformat()
                halted = True
                break
        if halted:
            break
    save_state()
    named = sum(
        bool(apps.get(package, {}).get("app_name"))
        or bool(overrides.get(package, {}).get("app_name"))
        for package in wanted
    )
    remaining = sum(unchecked(package) and package not in overrides for package in wanted)
    print(
        f"Application metadata: {named}/{len(wanted)} apps named; "
        f"{lookups} Google Play lookups; {remaining} packages awaiting a completed lookup.",
        flush=True,
    )
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--play-limit", type=int, help="Override the adaptive Google Play lookup cap"
    )
    parser.add_argument("--budget", type=float, help="Override the metadata time budget in seconds")
    args = parser.parse_args()
    with httpx.Client(
        timeout=httpx.Timeout(20, connect=10),
        follow_redirects=True,
        headers={"User-Agent": "Patch-Bundle-Registry/1.0"},
    ) as client:
        refresh(ROOT, client, max_play=args.play_limit, budget=args.budget)
