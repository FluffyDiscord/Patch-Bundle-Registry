"""Remove unconfigured sources without deleting disabled or shared entities."""

import json
import re
import shutil

try:
    from scripts.app_catalog import build_apps
    from scripts.catalog_records import encode
    from scripts.git_hosts import endpoint, normalize_source
    from scripts.source_policy import source_channels
except ModuleNotFoundError:
    from app_catalog import build_apps
    from catalog_records import encode
    from git_hosts import endpoint, normalize_source
    from source_policy import source_channels


def configured_sources(root):
    inventory = json.loads((root / "config/sources.json").read_bytes())
    if not isinstance(inventory, dict):
        raise ValueError("Source inventory must be an object")
    if any(
        not isinstance(row, dict)
        or not isinstance(row.get("patches"), str)
        or not row["patches"].strip()
        for row in inventory.values()
    ):
        raise ValueError("Every configured source requires a repository URL")
    channels = source_channels(inventory, root)
    return {normalize_source(row["patches"], root).lower() for row in channels.values()}, channels


def read(path, default):
    return json.loads(path.read_bytes()) if path.exists() else default


def write(path, data):
    if path.exists() and read(path, None) == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".cleanup-tmp")
    temporary.write_bytes(encode(data))
    temporary.replace(path)


def remove_tree(root, path):
    # Resolve and check every recursive deletion, including restored cache paths.
    resolved = path.resolve()
    if (
        path.is_symlink()
        or not resolved.is_relative_to(root.resolve())
        or resolved == root.resolve()
    ):
        raise ValueError("Unsafe source cleanup path")
    shutil.rmtree(resolved)


def clean_inputs(root):
    configured, channels = configured_sources(root)
    history = root / "internal/cache/history"
    archive_path = history / "archive.json"
    archive = read(archive_path, {"sources": {}, "releases": {}})
    marker = root / "internal/cache/source-removals.json"
    known = set(archive["sources"]) | set(read(marker, []))
    manifest = read(root / "database/manifest.json", {})
    snapshot = manifest.get("snapshot", "")
    if re.fullmatch(r"snapshots/[a-f0-9]{24}", snapshot):
        known.update(
            row["url"].lower() for row in read(root / "database" / snapshot / "sources.json", [])
        )
    removed = sorted(known - configured)
    if removed:
        write(marker, removed)
    else:
        marker.unlink(missing_ok=True)
    archive["sources"] = {
        key: row for key, row in archive["sources"].items() if key.lower() in configured
    }
    archive["releases"] = {
        key: row
        for key, row in archive["releases"].items()
        if row["source_url"].lower() in configured
    }
    if archive_path.exists():
        write(archive_path, archive)
    registry_path = history / "sources.json"
    registry = {
        key: row
        for key, row in read(registry_path, {}).items()
        if normalize_source(row["patches"], root).lower() in configured
    }
    if registry_path.exists():
        write(registry_path, registry)
    issues_path = root / "internal/cache/refresh-issues.json"
    if issues_path.exists():
        write(
            issues_path,
            [
                row
                for row in read(issues_path, [])
                if normalize_source(row["source"], root).lower() in configured
            ],
        )
    allowed = set(channels) | set(registry)
    for name in (
        "internal/cache/history/releases.json",
        "database/repository-metadata.json",
        "database/source-availability.json",
        "internal/cache/release-checks.json",
    ):
        path = root / name
        if path.exists():
            write(
                path, {key: row for key, row in read(path, {}).items() if key.lower() in configured}
            )
    for name in ("internal/cache/bundles/discovery-status.json",):
        path = root / name
        if path.exists():
            write(path, {key: row for key, row in read(path, {}).items() if key in allowed})
    run_path = root / "internal/cache/bundle-run-metadata.json"
    if run_path.exists():
        run = read(run_path, {})
        run["bundles"] = {key: row for key, row in run.get("bundles", {}).items() if key in allowed}
        write(run_path, run)
    allowed_folders = {
        (
            root
            / "internal/cache/bundles"
            / (re.sub(r"-(latest|stable|dev)$", "", alias) + "-patch-bundles")
        ).resolve()
        for alias in channels
    } | {(root / row["cache_path"]).resolve().parent for row in registry.values()}
    for folder in (root / "internal/cache/bundles", history):
        for path in folder.glob("*-patch-bundles"):
            keys = {
                raw.name.removesuffix("-patches-bundle.json")
                for raw in path.glob("*-patches-bundle.json")
            }
            if (keys and not keys & allowed) or (
                not keys and path.resolve() not in allowed_folders
            ):
                remove_tree(root, path)
            elif keys:
                for key in keys - allowed:
                    for suffix in ("patches-bundle", "patches-list", "extraction"):
                        (path / (key + "-" + suffix + ".json")).unlink(missing_ok=True)
    endpoints = {
        endpoint(url, root=root)
        for row in channels.values()
        for url in (row["patches"], row.get("integration"))
        if url
    }
    cache = root / "internal/cache"
    for path in [cache / "release_cache.json", *sorted((cache / "release-pages").glob("*.json"))]:
        if not path.exists():
            continue
        retained = {
            key: row for key, row in read(path, {}).items() if key.split("?", 1)[0] in endpoints
        }
        if retained:
            write(path, retained)
        else:
            path.unlink()
    return configured


def filter_records(root, records):
    """Follow source -> bundle -> patch -> package edges, then rebuild shared apps."""
    configured, _ = configured_sources(root)
    sources = {
        key: row
        for (kind, key), row in records.items()
        if kind == "source" and row["url"].lower() in configured
    }
    source_ids = set(sources)
    bundles = {
        key: row
        for (kind, key), row in records.items()
        if kind == "bundle" and row["source_id"] in source_ids
    }
    patches = {
        key: row
        for (kind, key), row in records.items()
        if kind == "patch" and row["bundle_id"] in bundles
    }
    associations = {
        key: row
        for (kind, key), row in records.items()
        if kind == "patch_package" and row["patch_id"] in patches
    }
    packages = {row["package_fk"] for row in associations.values()}
    retained = {}
    for (kind, key), row in records.items():
        keep = (
            (kind in ("source", "source_metadata") and key in sources)
            or (kind == "bundle" and key in bundles)
            or (kind == "patch" and key in patches)
            or (kind == "patch_package" and key in associations)
            or (kind == "package" and row["id"] in packages)
            or (kind == "history_source" and key.lower() in configured)
            or (kind == "history_release" and row["source_url"].lower() in configured)
            or kind == "refresh_jobs"
        )
        if keep:
            retained[(kind, key)] = row
    for key, row in sources.items():
        retained[("source", key)] = {
            **row,
            "bundles": sorted(
                bundle for bundle, value in bundles.items() if value["source_id"] == key
            ),
        }
    metadata = {key: row for (kind, key), row in records.items() if kind == "app"}
    for row in build_apps(list(patches.values()), bundles, {"apps": metadata}):
        retained[("app", row["package_name"])] = row
    return retained


def clean_snapshots(root):
    """Discard old local snapshots containing sources absent from configuration."""
    configured, _ = configured_sources(root)
    manifest = read(root / "database/manifest.json", {})
    current = manifest.get("snapshot")
    if not re.fullmatch(r"snapshots/[a-f0-9]{24}", current or ""):
        raise ValueError("Invalid current snapshot for source cleanup")
    removed = False
    for folder in (root / "database/snapshots").iterdir():
        if not folder.is_dir() or folder.name == current.split("/")[-1]:
            continue
        sources = read(folder / "sources.json", [])
        if any(row["url"].lower() not in configured for row in sources):
            remove_tree(root, folder)
            removed = True
    apps = {row["package_name"] for row in read(root / "database" / current / "apps.json", [])} | {
        row["name"] for row in read(root / "database" / current / "packages.json", [])
    }
    metadata_path = root / "database/app-metadata.json"
    if metadata_path.exists():
        metadata = read(metadata_path, {})
        metadata["apps"] = {
            key: row for key, row in metadata.get("apps", {}).items() if key in apps
        }
        write(metadata_path, metadata)
    return removed
