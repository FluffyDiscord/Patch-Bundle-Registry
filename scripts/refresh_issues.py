"""Persist safe, per-source refresh diagnostics for the Discord summary."""

import json
import re
from datetime import UTC, datetime

try:
    from scripts.catalog_records import encode
    from scripts.git_hosts import normalize_source
except ModuleNotFoundError:
    from catalog_records import encode
    from git_hosts import normalize_source


def read_issues(root):
    path = root / "internal/cache/refresh-issues.json"
    return json.loads(path.read_bytes()) if path.exists() else []


def record_issue(root, source, phase, error):
    rows = read_issues(root)
    row = {"source": normalize_source(source, root), "phase": phase, "error": error}
    existing = next(
        (
            existing
            for existing in rows
            if all(existing.get(key) == value for key, value in row.items())
        ),
        None,
    )
    if existing is None:
        existing = row
        rows.append(existing)
    existing["observed_at"] = datetime.now(UTC).isoformat()
    path = root / "internal/cache/refresh-issues.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(encode(rows))
    temporary.replace(path)


def extraction_issues(root, started_at):
    try:
        from scripts.source_cleanup import configured_sources
    except ModuleNotFoundError:
        from source_cleanup import configured_sources
    configured, channels = configured_sources(root)
    started = datetime.fromisoformat(started_at)
    registry_path = root / "internal/cache/history/sources.json"
    registry = json.loads(registry_path.read_bytes()) if registry_path.exists() else {}
    sources = {}
    for alias, row in channels.items():
        base = re.sub(r"-(latest|stable|dev)$", "", alias)
        path = root / "internal/cache/bundles" / (base + "-patch-bundles")
        sources[path / (alias + "-extraction.json")] = row["patches"]
    for alias, row in registry.items():
        if row["patches"].lower() in configured:
            path = (root / row["cache_path"]).with_name(alias + "-extraction.json")
            if not path.resolve().is_relative_to((root / "internal/cache").resolve()):
                raise ValueError("Unsafe extraction issue path")
            sources[path] = row["patches"]
    for path, source in sources.items():
        if not path.exists():
            continue
        status = json.loads(path.read_bytes())
        try:
            attempted = datetime.fromisoformat(
                str(status.get("attempted_at")).replace("Z", "+00:00")
            )
            current = attempted >= started
        except (ValueError, TypeError):
            current = False
        if current and status.get("status") != "verified":
            record_issue(
                root,
                source,
                "Patch extraction",
                str(status.get("version") or "Unknown version")
                + ": "
                + str(status.get("status") or "Extraction failed"),
            )
