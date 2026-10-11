"""Compare the current public releases before and after a refresh for Discord."""

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path

import httpx

try:
    from scripts.generate_database import timestamp_rank
except ModuleNotFoundError:
    from generate_database import timestamp_rank

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


def clean(value):
    # Keep provider text on one line without letting it inject Discord formatting.
    value = re.sub(r"([\\*_~`|<>])", r"\\\1", " ".join(str(value).split())[:300])
    truncated = size(value) > 300
    while size(value) > 300:
        value = value[:-1]
    return value.rstrip("\\") + "…" if truncated else value


def size(value):
    return len(value.encode("utf-16-le")) // 2


def section_messages(title, lines):
    heading = f"**{title}**\n\n"
    message = heading
    for line in lines:
        if size(message + line) > 1900:
            yield message.rstrip()
            message = heading
        message += line + "\n"
    if message != heading:
        yield message.rstrip()


def messages(before, after, website):
    updated, added = changes(before, after)
    if not updated:
        return []
    result = list(section_messages("Updated Bundles", updated))
    patch_lines = []
    for bundle, names in added:
        group = []
        for name in names:
            if group and size(", ".join([*group, name]) + " - " + bundle) > 1800:
                patch_lines.append(", ".join(group) + " - " + bundle)
                group = []
            group.append(name)
        patch_lines.append(", ".join(group) + " - " + bundle)
    result.extend(section_messages("New Patches", patch_lines or ["None."]))
    combined = []
    for section in result:
        if combined and size(combined[-1] + "\n\n" + section) <= 1900:
            combined[-1] += "\n\n" + section
        else:
            combined.append(section)
    if website and size(combined[-1] + "\n\n" + website) <= 2000:
        combined[-1] += "\n\n" + website
    return combined


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
                        json={"content": content, "allowed_mentions": {"parse": []}},
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
    contents = messages(
        read(args.baseline), capture(ROOT), read(ROOT / "config/site.json")["website"]
    )
    send(webhook, contents)
    print(f"Sent {len(contents)} catalog notification messages.")


if __name__ == "__main__":
    main()
