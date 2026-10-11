"""Application metadata and compatibility indexes for the public catalog."""

import re
from urllib.parse import urlparse

PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+")


def safe_icon(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
        return value if parsed.scheme == "https" and parsed.netloc else None
    except ValueError:
        return None


def build_apps(rows, bundles, metadata):
    records = metadata.get("apps", {})
    overrides = metadata.get("overrides", {})
    apps = {}
    for row in rows:
        if row["bundle_id"] not in bundles:
            continue
        seen = set()
        for package in row["packages"]:
            name = package["name"]
            if not isinstance(name, str) or not PACKAGE.fullmatch(name):
                continue
            info = {**records.get(name, {}), **overrides.get(name, {})}
            if name in overrides:
                info["metadata_source"] = "manual"
            app = apps.setdefault(
                name,
                {
                    "package_name": name,
                    "app_name": info.get("app_name") or name,
                    "icon_url": safe_icon(info.get("icon_url")),
                    "metadata_source": info.get("metadata_source"),
                    "bundles": {},
                },
            )
            association = app["bundles"].setdefault(
                row["bundle_id"],
                {
                    "bundle_id": row["bundle_id"],
                    "patch_count": 0,
                    "versions": set(),
                    "unrestricted": False,
                },
            )
            if name not in seen:
                association["patch_count"] += 1
                seen.add(name)
            association["versions"].update(package.get("versions") or [])
            association["unrestricted"] |= bool(
                package.get("unrestricted", not package["versions"])
            )
    result = []
    for app in apps.values():
        references = sorted(app["bundles"].values(), key=lambda item: item["bundle_id"])
        for reference in references:
            reference["versions"] = sorted(reference["versions"])
        app.update(
            bundles=references,
            bundle_count=len(references),
            patch_count=sum(reference["patch_count"] for reference in references),
            versions=sorted(
                {version for reference in references for version in reference["versions"]}
            ),
            unrestricted=any(reference["unrestricted"] for reference in references),
        )
        result.append(app)
    return sorted(result, key=lambda app: app["package_name"])
