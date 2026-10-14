"""Read the signed container metadata used by early JVM-based Reseam bundles."""

import base64
import hashlib
import json
import re
import sys
import tomllib
import zipfile

MAX_CONTAINER_BYTES = 128 * 1024 * 1024


def inspect_manifest(path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 5000 or len({item.filename for item in entries}) != len(entries):
            raise ValueError("Invalid Reseam archive entries")
        if sum(item.file_size for item in entries) > MAX_CONTAINER_BYTES:
            raise ValueError("Reseam archive exceeds extraction budget")
        manifest = archive.read("manifest.toml")
        if len(manifest) > 1024 * 1024:
            raise ValueError("Reseam manifest exceeds extraction budget")
        document = tomllib.loads(manifest.decode("utf-8"))
        bundle = document["bundle"]
        if bundle.get("format_version") != 1:
            raise ValueError("Unsupported legacy Reseam container")
        publisher = {key: bundle[key] for key in ("name", "author", "description")}
        if not all(isinstance(value, str) for value in publisher.values()):
            raise ValueError("Invalid Reseam publisher metadata")
        key = archive.read("manifest.pubkey")
        signature = archive.read("manifest.sig")
        if len(key) != 32 or len(signature) != 64:
            raise ValueError("Invalid Reseam signing material")
        files = document["files"]
        if not isinstance(files, dict):
            raise ValueError("Invalid Reseam file manifest")
        for name, expected in files.items():
            if not isinstance(expected, str) or not re.fullmatch("[a-fA-F0-9]{64}", expected):
                raise ValueError("Invalid Reseam file digest")
            with archive.open(name) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected.lower():
                    raise ValueError("Reseam file digest mismatch")
        jars = sorted(name for name in files if name.lower().endswith(".jar"))
        if not jars:
            raise ValueError("Legacy Reseam bundle has no JVM patches")
        publisher["public_key"] = key.hex()
        return {
            "publisher": publisher,
            "jars": jars,
            "manifest": base64.b64encode(manifest).decode(),
            "signature": base64.b64encode(signature).decode(),
        }


if __name__ == "__main__":
    print(json.dumps(inspect_manifest(sys.argv[1])))
