"""Compare the current public releases before and after a refresh for Discord."""

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import httpx

try:
    from scripts.generate_database import packages_for, timestamp_rank
except ModuleNotFoundError:
    from generate_database import packages_for, timestamp_rank

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def capture(root):
    manifest_path = root / "database/manifest.json"
    if not manifest_path.exists():
        return {}
    snapshot = read(manifest_path)["snapshot"]
    if not re.fullmatch(r"snapshots/[a-f0-9]{24}", snapshot):
        raise ValueError("Invalid catalog snapshot")
    folder = root / "database" / snapshot
    sources = {row["id"]: row for row in read(folder / "sources.json") if row["enabled"]}
    history = read(folder / "history.json")
    app_index = folder / "apps.json"
    app_metadata = (
        {app["package_name"]: app for app in read(app_index)} if app_index.exists() else {}
    )

    def applications(patches):
        return {
            package["name"]: app_metadata.get(package["name"], {}).get("app_name")
            or package["name"]
            for patch in patches
            for package in packages_for(patch)
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package["name"])
        }

    verified_history = {}
    for item in history.values():
        bundle = item["bundle"]
        if bundle["source_id"] not in sources or bundle["metadata_status"] != "verified":
            continue
        category = (bundle["source_id"], bool(bundle["is_prerelease"]))
        previous = verified_history.get(category)
        if previous is None or timestamp_rank(bundle.get("created_at")) > timestamp_rank(
            previous["bundle"].get("created_at")
        ):
            verified_history[category] = item
    releases = {}
    for item in history.values():
        bundle = item["bundle"]
        source = sources.get(bundle["source_id"])
        if source is None or not bundle["is_latest"]:
            continue
        verified = bundle["metadata_status"] == "verified"
        patches = item["patches"] if verified else []
        # Positions and object key order are not patch changes.
        serialized = sorted(
            json.dumps(patch, sort_keys=True, ensure_ascii=False) for patch in patches
        )
        fingerprint = hashlib.sha256(json.dumps(serialized).encode()).hexdigest()
        category = "prerelease" if bundle["is_prerelease"] else "release"
        prior = verified_history.get((source["id"], bool(bundle["is_prerelease"])))
        releases[f"{source['id']}/{category}"] = {
            "name": f"{source['namespace']}/{source['repo']}",
            "category": category,
            "url": source.get("url"),
            "release_url": bundle.get("release_url") or source.get("url"),
            "apps": applications(patches),
            "app_urls": {
                package: (
                    "https://f-droid.org/packages/"
                    if info.get("metadata_source") == "fdroid"
                    else "https://apt.izzysoft.de/fdroid/index/apk/"
                    if info.get("metadata_source") == "izzyondroid"
                    else "https://play.google.com/store/apps/details?id="
                )
                + quote(package, safe="")
                for package in applications(patches)
                for info in [app_metadata.get(package, {})]
            },
            "previous_apps": applications(prior["patches"]) if not verified and prior else None,
            "version": bundle["version"],
            "verified": verified,
            "fingerprint": fingerprint,
            "names": sorted({patch.get("name") or "Unnamed patch" for patch in patches}),
            # Retain a known comparison if extraction finishes in a later refresh.
            "previous_names": sorted(
                {patch.get("name") or "Unnamed patch" for patch in prior["patches"]}
            )
            if not verified and prior is not None
            else None,
        }
    return releases


def changes(before, after):
    updated, added = [], []
    for key, new in sorted(
        after.items(), key=lambda entry: (entry[1]["name"].casefold(), entry[0])
    ):
        old = before.get(key)
        version_changed = old is None or old["version"] != new["version"]
        patches_changed = bool(
            old and old["verified"] and new["verified"] and old["fingerprint"] != new["fingerprint"]
        )
        populated = bool(old and not old["verified"] and new["verified"])
        if not (version_changed or patches_changed or populated):
            continue
        label = clean(new["name"])
        if new["category"] == "prerelease":
            label += " (prerelease)"
        previous = f"{label} {clean(old['version'])}" if old else "New"
        line = f"{previous} → {label} {clean(new['version'])}"
        if patches_changed:
            line += " (patches updated)"
        elif populated:
            line += " (patch metadata now available)"
        updated.append(line)
        # A newly extracted list is not proof that every patch was newly added.
        previous_names = (
            [] if old is None else (old["names"] if old["verified"] else old.get("previous_names"))
        )
        if new["verified"] and previous_names is not None:
            names = sorted(set(new["names"]) - set(previous_names))
            if names:
                added.append((label, [clean(name) for name in names]))
    return updated, added


def clean(value, limit=300):
    # Keep provider text on one line without letting it inject Discord formatting.
    value = re.sub(r"([\\*_~`|<>\[\]])", r"\\\1", " ".join(str(value).split()))
    truncated = size(value) > limit
    while size(value) > limit - int(truncated):
        value = value[:-1]
    return value.rstrip("\\") + "…" if truncated else value


def size(value):
    return len(value.encode("utf-16-le")) // 2


def link(label, url):
    label = clean(label, 120)
    if not isinstance(url, str):
        return label
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return label
        url = quote(url, safe=":/?&=%#@+;,.-_~")
        if len(url) > 350:
            return label
        return f"[{label}]({url})"
    except ValueError:
        return label


def issue_line(row):
    source = row["source"]
    try:
        parsed = urlsplit(source)
        label = unquote(parsed.path).strip("/") or parsed.hostname or source
    except ValueError:
        label = source
    # Match the named repository links used by the other notification sections.
    return f"• {link(label, source)} — **{clean(row['phase'], 80)}**: {clean(row['error'], 300)}"


def messages(before, after, website, issues=()):
    """Return complete webhook payloads, one rich embed per message."""
    sections = {name: [] for name in ("New Bundles", "Updated Bundles", "New Apps", "New Patches")}
    known_sources = {key.rsplit("/", 1)[0] for key in before}
    known_items = {}
    for key, row in before.items():
        source = key.rsplit("/", 1)[0]
        for field in ("names", "apps"):
            value = row.get(field) if row["verified"] else row.get("previous_" + field)
            if value is not None:
                known_items.setdefault((source, field), set()).update(value)

    def previous_items(source, old, field):
        known = known_items.get((source, field))
        if old is None:
            return known if source in known_sources else set()
        value = old.get(field) if old["verified"] else old.get("previous_" + field)
        # Unknown metadata cannot prove an addition; known channels suppress promotions.
        return None if value is None else set(value) | (known or set())

    announced_sources, announced_apps, announced_patches = set(), set(), set()
    for key, new in sorted(after.items(), key=lambda item: (item[1]["name"].casefold(), item[0])):
        old = before.get(key)
        if not changes({key: old} if old else {}, {key: new})[0]:
            continue
        source = key.rsplit("/", 1)[0]
        label = clean(new["name"], 120)
        category = " (prerelease)" if new["category"] == "prerelease" else ""
        bundle_link = link(new["name"], new.get("url"))
        if source not in known_sources:
            if source not in announced_sources:
                sections["New Bundles"].append(f"• {bundle_link}")
                announced_sources.add(source)
        else:
            previous = clean(old["version"], 90) if old else "New"
            note = ""
            if (
                old
                and old["verified"]
                and new["verified"]
                and old["fingerprint"] != new["fingerprint"]
            ):
                note = " (patches updated)"
            elif old and not old["verified"] and new["verified"]:
                note = " (patch metadata now available)"
            sections["Updated Bundles"].append(
                f"• {bundle_link}{category}: {previous} → {clean(new['version'], 90)}{note}"
            )
        if not new["verified"]:
            continue
        previous_names = previous_items(source, old, "names")
        if previous_names is not None:
            for name in sorted(set(new["names"]) - set(previous_names)):
                identity = (source, name)
                if identity not in announced_patches:
                    sections["New Patches"].append(
                        f"• {link(name, new.get('release_url') or new.get('url'))} ({label})"
                    )
                    announced_patches.add(identity)
        previous_apps = previous_items(source, old, "apps")
        # A baseline from an older script cannot prove that compatibility is new.
        if previous_apps is not None:
            for package, name in sorted(new.get("apps", {}).items()):
                identity = (source, package)
                if package not in previous_apps and identity not in announced_apps:
                    app_url = "https://play.google.com/store/apps/details?id=" + quote(
                        package, safe=""
                    )
                    app_url = new.get("app_urls", {}).get(package, app_url)
                    sections["New Apps"].append(f"• {link(name, app_url)} ({label})")
                    announced_apps.add(identity)
    sections["Issues"] = [
        issue_line(row)
        for row in sorted(issues, key=lambda row: (row["source"], row["phase"], row["error"]))
    ]
    fields = []
    for title, lines in sections.items():
        value = ""
        for line in lines:
            if size(value + line + "\n") > 1024:
                fields.append({"name": title, "value": value.rstrip(), "inline": False})
                value = ""
            value += line + "\n"
        if value:
            fields.append({"name": title, "value": value.rstrip(), "inline": False})
    result = []
    embed = None
    length = 0
    headings = set()
    for field in fields:
        # Discord requires a field name; continuation chunks use an invisible one.
        name = "\u200b" if field["name"] in headings else field["name"]
        field_size = size(name) + size(field["value"])
        if embed is None or len(embed["fields"]) >= 25 or length + field_size > 6000:
            embed = {"title": "Patch Bundle Registry update", "color": 0x79D38A, "fields": []}
            if isinstance(website, str) and link("website", website) != "website":
                embed["url"] = website
            result.append({"embeds": [embed], "allowed_mentions": {"parse": []}})
            length = size(embed["title"])
            headings.clear()
            name = field["name"]
        embed["fields"].append({**field, "name": name})
        headings.add(field["name"])
        length += size(name) + size(field["value"])
    return result


def send(webhook, contents):
    url = httpx.URL(webhook).copy_merge_params({"wait": "true"})
    with httpx.Client(timeout=20) as client:
        for position, content in enumerate(contents):
            if position:
                time.sleep(1)
            for attempt in range(4):
                try:
                    response = client.post(
                        url,
                        json={**content, "allowed_mentions": {"parse": []}},
                    )
                except httpx.HTTPError:
                    raise RuntimeError("Could not deliver the catalog notification") from None
                if response.status_code == 429 and attempt < 3:
                    try:
                        delay = float(response.json()["retry_after"])
                    except (ValueError, KeyError, TypeError):
                        delay = 1
                    if not 0 <= delay <= 60:
                        raise RuntimeError("Discord notification rate limit exceeds retry budget")
                    time.sleep(max(1, delay))
                    continue
                if response.is_error:
                    raise RuntimeError(f"Discord notification failed (HTTP {response.status_code})")
                break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("capture", "notify"))
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "capture":
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps(capture(ROOT)), encoding="utf-8")
        return
    webhook = os.environ.get("WEBHOOK_URL")
    if not webhook:
        return
    try:
        from scripts.refresh_issues import read_issues
    except ModuleNotFoundError:
        from refresh_issues import read_issues
    after = capture(ROOT)
    before = read(args.baseline) if args.baseline.exists() else after
    if os.environ.get("CATALOG_REFRESH_FAILED") == "true":
        after = before  # Failed publication must not announce unpublished changes.
    contents = messages(
        before, after, read(ROOT / "config/site.json")["website"], read_issues(ROOT)
    )
    send(webhook, contents)
    print(f"Sent {len(contents)} catalog notification messages.")


if __name__ == "__main__":
    main()
