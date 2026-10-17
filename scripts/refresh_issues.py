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


def extraction_cause(attempts):
    """Expose known diagnoses, never arbitrary worker output or URL contents."""
    causes = []
    for attempt in attempts:
        cause = attempt.get("cause")
        if not isinstance(cause, str):
            continue
        if cause == "No JVM patch classes":
            causes.append("No JVM patch classes; archive contains DEX or metadata only")
        elif attempt.get("status") == "runtime_missing_dependency":
            match = re.fullmatch(
                r"(?:[A-Za-z_$][A-Za-z0-9_$]* <- )*[A-Za-z_$][A-Za-z0-9_$]* "
                r"([A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)+)",
                cause,
            )
            if match:
                causes.append("Missing class " + match[1])
        elif cause in {"bundle_too_old", "bundle_too_new", "engine_too_old"}:
            causes.append(cause)
    return "; ".join(dict.fromkeys(causes))[:400]


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
            count = status.get("fallback_patch_count")
            fallback = type(count) is int and 0 <= count <= 50000
            if fallback:
                list_path = path.with_name(
                    path.name.replace("-extraction.json", "-patches-list.json")
                )
                try:
                    metadata = json.loads(list_path.read_bytes())
                    fallback = (
                        metadata.get("version") == status.get("version")
                        and isinstance(metadata.get("patches"), list)
                        and len(metadata["patches"]) == count
                    )
                except (OSError, ValueError, AttributeError):
                    fallback = False
            record_issue(
                root,
                source,
                "Metadata fallback" if fallback else "Patch extraction",
                str(status.get("version") or "Unknown version")
                + ": "
                + (
                    f"{count} publisher patches available (unverified); binary extraction: "
                    if fallback else ""
                )
                + str(status.get("status") or "Extraction failed")
                + (
                    ": " + explanation
                    if (explanation := extraction_cause(status.get("runtime_attempts", [])))
                    else ""
                )
                + (
                    " ("
                    + "; ".join(
                        dict.fromkeys(
                            str(attempt.get("runtime", "unknown"))
                            + ": "
                            + str(attempt.get("status", "failed"))
                            for attempt in status.get("runtime_attempts", [])
                            if attempt.get("status") != "verified"
                        )
                    )[:600]
                    + ")"
                    if status.get("runtime_attempts") and not fallback
                    else ""
                ),
            )
