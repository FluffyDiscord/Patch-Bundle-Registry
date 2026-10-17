"""Persist public release metadata independently of patch extraction and channel pointers."""

import hashlib
import json
from datetime import UTC, datetime

try:
    from scripts.git_hosts import normalize_source
    from scripts.release_assets import release_assets
    from scripts.release_history import release_is_prerelease
    from scripts.source_policy import source_availability
except ModuleNotFoundError:
    from git_hosts import normalize_source
    from release_assets import release_assets
    from release_history import release_is_prerelease
    from source_policy import source_availability


def stable_id(*parts):
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()[:24]


def reconcile_archive(root, inventory, release_sets):
    path = root / "internal/cache/history/archive.json"
    try:
        archive = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        archive = {"releases": {}, "sources": {}}
    if not isinstance(archive, dict) or not all(
        isinstance(archive.get(key), dict) for key in ("releases", "sources")
    ):
        raise ValueError("Invalid historical archive; refusing to overwrite it")
    now = datetime.now(UTC).isoformat()
    availability = source_availability(root)
    configured = {}
    for settings in inventory.values():
        if isinstance(settings, dict) and settings.get("patches"):
            url = normalize_source(settings["patches"], root)
            configured[url.lower()] = url
    for canonical, url in configured.items():
        source_id = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        state = archive["sources"].setdefault(canonical, {})
        state.update(source_id=source_id, source_url=url, checked_at=now, configured=True)
        state["required"] = not any(
            settings.get("disabled", False)
            for settings in inventory.values()
            if isinstance(settings, dict)
            and settings.get("patches")
            and normalize_source(settings["patches"], root).lower() == canonical
        )
        if not state["required"]:
            state.update(status="disabled", error="Explicitly disabled in sources.json")
            continue
        releases = release_sets.get(canonical)
        if releases is None:
            reason = availability.get(canonical)
            error = "Complete upstream inventory unavailable"
            state.update(status="incomplete", error=f"{error}: {reason}" if reason else error)
            continue
        seen = set()
        for release in releases:
            # Authenticated API tokens can see drafts: never publish draft/private records.
            if release.get("draft") or release.get("upcoming_release"):
                continue
            upstream_id = release.get("id")
            tag = release.get("tag_name")
            if upstream_id is None and not tag:
                raise ValueError("Release has neither an upstream ID nor a tag")
            identity = stable_id(canonical, str(upstream_id) if upstream_id is not None else tag)
            seen.add(identity)
            keys = (
                "id",
                "node_id",
                "name",
                "tag_name",
                "body",
                "description",
                "html_url",
                "url",
                "created_at",
                "published_at",
                "updated_at",
                "released_at",
                "target_commitish",
                "commit",
                "assets",
                "_links",
                "prerelease",
            )
            raw = {key: release[key] for key in keys if key in release}
            text = release.get("body")
            if text is None:
                text = release.get("description")
            record = {
                "id": identity,
                "source_id": source_id,
                "source_url": url,
                "upstream_id": str(upstream_id) if upstream_id is not None else None,
                "name": release.get("name"),
                "tag": tag,
                "version": tag,
                "is_prerelease": release_is_prerelease(release),
                "draft": False,
                "created_at": release.get("created_at"),
                "published_at": release.get("published_at") or release.get("released_at"),
                "updated_at": release.get("updated_at"),
                "changelog": text,
                "release_notes": text,
                "release_url": release.get("html_url") or release.get("_links", {}).get("self"),
                "target_commitish": release.get("target_commitish"),
                "assets": raw.get("assets", []),
                "bundle_assets": release_assets(release),
                "raw_metadata": raw,
                "availability": "available",
                "last_verified_at": now,
                "binary_archived": False,
            }
            previous = archive["releases"].get(identity, {})
            record["first_seen_at"] = previous.get("first_seen_at", now)
            if previous:
                ignored = {"first_seen_at", "last_verified_at"}
                if {k: v for k, v in record.items() if k not in ignored} == {
                    k: v for k, v in previous.items() if k not in ignored
                }:
                    record["last_verified_at"] = previous.get("last_verified_at", now)
            archive["releases"][identity] = record
        for identity, record in archive["releases"].items():
            if (
                record["source_id"] == source_id
                and identity not in seen
                and record.get("availability") != "unavailable"
            ):
                record.update(availability="unavailable", last_verified_at=now)
        state.update(status="complete", error=None, verified_at=now, upstream_count=len(seen))
    archive["sources"] = {key: row for key, row in archive["sources"].items() if key in configured}
    archive["releases"] = {
        key: row
        for key, row in archive["releases"].items()
        if row["source_url"].lower() in configured
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(archive, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return archive
