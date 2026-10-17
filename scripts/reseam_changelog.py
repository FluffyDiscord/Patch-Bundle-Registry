"""Bounded, optional release-note enrichment from Reseam's per-release patches.json."""

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit

from httpx import HTTPError

try:
    from scripts.git_hosts import auth_headers
    from scripts.release_assets import asset_extension, choose_patch_bundle, release_assets
except ModuleNotFoundError:
    from git_hosts import auth_headers
    from release_assets import asset_extension, choose_patch_bundle, release_assets

MAX_RESEAM_JSON_BYTES = 2 * 1024 * 1024
MAX_RESEAM_NOTES_BYTES = 128 * 1024
MAX_RESEAM_DOWNLOADS_PER_RUN = 12
FAILED_RETRY_DELAY = timedelta(minutes=30)


def reseam_metadata_asset(repository: str, release: Mapping) -> str | None:
    """Only accept an unambiguous patches.json uploaded alongside a Reseam artifact."""
    assets = release_assets(release)
    bundle = choose_patch_bundle(assets)
    if not bundle or asset_extension(bundle) != ".reseam":
        return None
    origin = urlsplit(repository)
    bundle_path = urlsplit(bundle["url"]).path.rsplit("/", 1)[0]
    candidates = []
    for asset in assets:
        url = asset["url"]
        parsed = urlsplit(url)
        if (
            asset["name"].lower() == "patches.json"
            and parsed.path.rsplit("/", 1)[-1].lower() == "patches.json"
            and parsed.path.rsplit("/", 1)[0] == bundle_path
            and parsed.scheme == "https"
            and parsed.netloc.lower() == origin.netloc.lower()
            and not parsed.username
            and not parsed.password
            and not parsed.fragment
        ):
            candidates.append(url)
    return candidates[0] if len(candidates) == 1 else None


def reseam_asset_revision(release: Mapping, url: str) -> str:
    """Detect replaced upstream release assets even when their download URL is unchanged."""
    assets = release.get("assets", [])
    if isinstance(assets, Mapping):
        assets = assets.get("links", [])
    matches = [
        asset for asset in assets if isinstance(asset, Mapping)
        and (asset.get("browser_download_url") or asset.get("direct_asset_url")
             or asset.get("url")) == url
    ] if isinstance(assets, list) else []
    fields = ("id", "uuid", "digest", "size", "updated_at", "created_at")
    metadata = [
        [asset.get(key) for key in fields] for asset in matches
    ]
    return hashlib.sha256(
        json.dumps([url, metadata], separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def _validated_release_notes(document: object, tag: str) -> tuple[bool, str | None]:
    """Distinguish a checked release with no notes from an invalid metadata response."""
    if not isinstance(document, dict) or not isinstance(document.get("releases"), list):
        return False, None
    target = tag.removeprefix("v")
    matches = [
        row for row in document["releases"]
        if isinstance(row, dict)
        and isinstance(row.get("version"), str)
        and row["version"].removeprefix("v") == target
    ]
    if len(matches) != 1:
        return False, None
    notes = matches[0].get("description")
    if notes is None or notes == "":
        return True, None
    if not isinstance(notes, str):
        return False, None
    if len(notes.encode("utf-8")) > MAX_RESEAM_NOTES_BYTES:
        return False, None
    return True, notes if notes.strip() else None


def reseam_release_notes(document: object, tag: str) -> str | None:
    """Do not substitute another version from the history embedded in patches.json."""
    return _validated_release_notes(document, tag)[1]


async def _fetch_reseam_metadata(client, repository: str, asset: str, tag: str, root):
    """Stream with a hard byte/time budget; never forward host credentials on redirects."""
    headers = auth_headers(repository, root)
    origin = urlsplit(asset)
    current = asset
    async with asyncio.timeout(45):
        for _ in range(4):
            async with client.stream(
                "GET", current,
                headers=headers if urlsplit(current).netloc == origin.netloc else {},
                follow_redirects=False
            ) as response:
                if response.is_redirect:
                    next_url = urljoin(current, response.headers.get("Location", ""))
                    parsed = urlsplit(next_url)
                    trusted_cdn = (
                        origin.hostname == "github.com" and parsed.hostname in {
                            "release-assets.githubusercontent.com", "objects.githubusercontent.com"
                        }
                    ) or (
                        origin.hostname == "gitlab.com" and parsed.hostname == "storage.googleapis.com"
                    )
                    if (
                        parsed.scheme != "https"
                        or (parsed.netloc != origin.netloc and (not trusted_cdn or parsed.port))
                        or parsed.username
                        or parsed.password
                        or parsed.fragment
                        or next_url == current
                    ):
                        return False, None
                    current = next_url
                    continue
                response.raise_for_status()
                length = response.headers.get("Content-Length", "")
                if length.isdecimal() and int(length) > MAX_RESEAM_JSON_BYTES:
                    return False, None
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > MAX_RESEAM_JSON_BYTES:
                        return False, None
                    data.extend(chunk)
                try:
                    document = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    return False, None
                return _validated_release_notes(document, tag)
    return False, None


async def fetch_reseam_release_notes(client, repository: str, asset: str, tag: str, root):
    """Return release notes, or None when the asset has no usable notes."""
    _, notes = await _fetch_reseam_metadata(client, repository, asset, tag, root)
    return notes


def publisher_release_notes(release: Mapping) -> str | None:
    """Use hosting-platform release notes when no Reseam-native description exists."""
    for key in ("body", "description"):
        notes = release.get(key)
        if isinstance(notes, str) and notes.strip():
            return notes
    return None


async def enrich_reseam_releases(
    client, root, release_sets: dict, *, prioritized: set[tuple[str, str]] | None = None
) -> None:
    """Fetch only missing, uncached Reseam notes, with a run-wide request budget."""
    archive_path = root / "internal/cache/history/archive.json"
    try:
        archive = json.loads(archive_path.read_text(encoding="utf-8"))
        previous = archive["releases"]
    except (OSError, ValueError, KeyError, TypeError):
        previous = {}
    try:
        from scripts.release_archive import stable_id
    except ModuleNotFoundError:
        from release_archive import stable_id

    pending = []
    seen = set()
    for repository, releases in release_sets.items():
        for release in releases:
            # Release API pages can come from a prior run's on-disk cache.
            # Never let stale enrichment reset a retry cooldown or override history.
            release.pop("_reseam_notes", None)
            if release.get("draft") or release.get("upcoming_release"):
                continue
            tag = release.get("tag_name")
            if not isinstance(tag, str):
                continue
            asset = reseam_metadata_asset(repository, release)
            if not asset:
                continue
            revision = reseam_asset_revision(release, asset)
            upstream_id = release.get("id")
            identifier = stable_id(
                repository, str(upstream_id) if upstream_id is not None else tag
            )
            old = previous.get(identifier, {})
            if (old.get("reseam_metadata_url") == asset
                    and old.get("reseam_metadata_revision") == revision
                    and old.get("reseam_metadata_checked")):
                release["_reseam_notes"] = {
                    "url": asset, "revision": revision,
                    "description": old.get("reseam_metadata_description"),
                }
                continue
            if (old.get("reseam_metadata_url") == asset
                    and old.get("reseam_metadata_revision") == revision):
                last_failed = old.get("reseam_metadata_attempted_at")
                if isinstance(last_failed, str):
                    try:
                        retry_at = datetime.fromisoformat(last_failed) + FAILED_RETRY_DELAY
                        if retry_at.tzinfo is not None and retry_at > datetime.now(UTC):
                            continue
                    except ValueError:
                        pass
            key = (asset, tag)
            if key not in seen:
                pending.append((repository, release, asset, tag, revision))
                seen.add(key)
    # Favor recently published releases, including the currently selected channel,
    # without downloading every historical patches.json on each refresh.
    pending.sort(
        key=lambda item: (
            (item[0], item[3]) in (prioritized or ()),
            str(item[1].get("published_at") or item[1].get("released_at")
                or item[1].get("created_at") or ""),
        ),
        reverse=True,
    )
    for repository, release, asset, tag, revision in pending[:MAX_RESEAM_DOWNLOADS_PER_RUN]:
        try:
            verified, notes = await _fetch_reseam_metadata(client, repository, asset, tag, root)
        except (HTTPError, OSError, TimeoutError, ValueError):
            verified, notes = False, None
        release["_reseam_notes"] = {
            "url": asset, "revision": revision, "description": notes,
            "verified": verified,
        }
        if not verified:
            continue  # Retry after a cooldown so broken assets cannot starve old releases.
