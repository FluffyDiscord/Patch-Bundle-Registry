"""Convert the existing immutable catalog to D1 rows without changing public IDs."""

import hashlib
import json


def encode(value):
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def catalog_records(root):
    manifest = json.loads((root / "database/manifest.json").read_text(encoding="utf-8"))
    base = root / "database" / manifest["snapshot"]

    def read(name, default=None):
        path = base / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    records = {}

    def add(kind, key, row):
        records[(kind, str(key))] = row

    sources = {row["id"]: row for row in read("sources.json")}
    bundles = {row["id"]: row for row in read("bundles.json")}
    for key, row in sources.items():
        add("source", key, {**row, "id": row["legacy_id"], "catalog_id": key})
        metadata = row["source_metadatum"]
        add("source_metadata", key, {**metadata, "source_fk": row["legacy_id"]})
    for key, row in bundles.items():
        detail = read("bundles/" + key + ".json", {})
        add(
            "bundle",
            key,
            {
                **row,
                **detail,
                "id": row["legacy_id"],
                "catalog_id": key,
                "source_fk": sources[row["source_id"]]["legacy_id"],
            },
        )
    packages = read("package-records.json", [])
    if not packages:
        packages = [
            {**row, "version": version, "id": index + 1}
            for index, (row, version) in enumerate(
                (row, version)
                for row in read("packages.json", [])
                for version in row.get("versions") or [None]
            )
        ]
    package_ids = {}
    for row in packages:
        key = json.dumps([row["name"], row.get("version")], separators=(",", ":"))
        package_ids[key] = row["id"]
        add("package", str(row["id"]), row)
    for name in manifest["files"]:
        if not name.startswith("patches/"):
            continue
        for row in read(name):
            key = row["id"]
            add(
                "patch",
                key,
                {
                    **row,
                    "id": row["legacy_id"],
                    "catalog_id": key,
                    "bundle_fk": bundles[row["bundle_id"]]["legacy_id"],
                    "compatiblePackages": row["packages"],
                },
            )
            for package in row["packages"]:
                for version in package.get("versions") or [None]:
                    package_key = json.dumps([package["name"], version], separators=(",", ":"))
                    package_id = package_ids[package_key]
                    add(
                        "patch_package",
                        str(row["legacy_id"]) + ":" + str(package_id),
                        {
                            "package_fk": package_id,
                            "patch_fk": row["legacy_id"],
                            "patch_id": key,
                        },
                    )
    for row in read("apps.json", []):
        add("app", row["package_name"], row)
    for row in read("refresh-jobs.json", []):
        add("refresh_jobs", row["job_id"], row)
    archive_path = root / "internal/cache/history/archive.json"
    archive = json.loads(archive_path.read_text(encoding="utf-8")) if archive_path.exists() else {}
    bundles_by_release = {}
    for value in bundles.values():
        bundles_by_release.setdefault((value["source_id"], value["version"]), []).append(value)
    for key, row in archive.get("releases", {}).items():
        matching = bundles_by_release.get((row["source_id"], row["version"]), [])
        add(
            "history_release",
            key,
            {
                **row,
                "bundle_ids": [value["id"] for value in matching],
                "runtimes": sorted(
                    {value["patcher_runtime"] for value in matching if value.get("patcher_runtime")}
                ),
                "bundle_types": sorted({value["bundle_type"] for value in matching}),
            },
        )
    for key, row in archive.get("sources", {}).items():
        add("history_source", key, row)
    return manifest, records


def record_hash(row):
    return hashlib.sha256(encode(row)).hexdigest()
