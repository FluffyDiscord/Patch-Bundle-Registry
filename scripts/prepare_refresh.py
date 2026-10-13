"""Restrict expensive extraction without restricting full upstream inventory verification."""

import json
import os

from scripts.git_hosts import normalize_source


def prepare_extraction(root):
    target = os.environ.get("CATALOG_SOURCE", "").strip()
    force = os.environ.get("CATALOG_FORCE") == "true"
    if not target and not force:
        return
    inventory = json.loads((root / "config/sources.json").read_text(encoding="utf-8"))
    history = root / "internal/cache/history/sources.json"
    if history.exists():
        inventory.update(json.loads(history.read_text(encoding="utf-8")))
    import re

    selected = set()
    excluded = set()
    found = not target
    for alias, row in inventory.items():
        if not isinstance(row, dict) or not row.get("patches"):
            continue
        url = normalize_source(row["patches"], root)
        folder = (
            row.get("cache_path", "").split("/")[-2]
            if row.get("cache_path")
            else re.sub(r"-(latest|stable|dev)$", "", alias) + "-patch-bundles"
        )
        if not target or target in (alias, row.get("alias"), url):
            selected.add(folder)
            found = True
        else:
            excluded.add(folder)
    if not found:
        raise ValueError("Requested source is not configured")
    policy = root / "internal/cache/disabled-extraction.json"
    disabled = set(json.loads(policy.read_text(encoding="utf-8")))
    disabled.update(excluded - selected)
    policy.write_text(json.dumps(sorted(disabled)), encoding="utf-8")
    if force:
        cache = (root / "internal/cache").resolve()
        for path in cache.rglob("*-extraction.json"):
            if path.parent.name in selected and path.resolve().is_relative_to(cache):
                path.unlink()
