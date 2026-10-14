"""Bounded transfers and verified, disposable caches for immutable R2 objects."""

import gzip
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

from scripts.catalog_records import encode

REFRESH_FILES = (
    "history.json",
    "sources.json",
    "releases.json",
    "availability.json",
    "apps.json",
    "bundles.json",
    "packages.json",
)
REFRESH_ROOT_FILES = (
    "database/repository-metadata.json",
    "database/app-metadata.json",
    "database/source-availability.json",
)


class BlobCache:
    def __init__(self, directory, get, put, workers=8):
        self.directory = Path(directory)
        self.get = get
        self.put = put
        self.workers = min(16, max(1, workers))
        self.locks = [Lock() for _ in range(64)]
        self.guard = Lock()
        self.metrics = Counter()

    def add(self, name, value=1):
        with self.guard:
            self.metrics[name] += value

    def path(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid object digest")
        return self.directory / digest[:2] / (digest + ".blob")

    def remember(self, digest, body):
        path = self.path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(body)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def read(self, digest, fresh=False):
        path = self.path(digest)
        with self.locks[int(digest[:2], 16) % len(self.locks)]:
            if not fresh and path.is_file():
                body = path.read_bytes()
                if hashlib.sha256(body).hexdigest() == digest:
                    self.add("cache_hits")
                    os.utime(path, None)
                    return body
                self.add("cache_corruptions")
                path.unlink(missing_ok=True)
            body = self.get("objects/" + digest)
            self.add("object_downloads")
            self.add("download_bytes", len(body))
            if hashlib.sha256(body).hexdigest() != digest:
                raise ValueError("R2 object integrity mismatch")
            self.remember(digest, body)
            return body

    def upload(self, body, known):
        digest = hashlib.sha256(body).hexdigest()
        with self.locks[int(digest[:2], 16) % len(self.locks)]:
            if digest not in known:
                self.put("objects/" + digest, body)
                self.add("object_uploads")
                self.add("upload_bytes", len(body))
                with self.guard:
                    known.add(digest)
            else:
                self.add("uploads_avoided")
            self.remember(digest, body)
        return digest

    def parallel(self, action, items):
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            return list(pool.map(action, items))

    def prefetch(self, digests, fresh=False):
        def fetch(digest):
            self.read(digest, fresh=fresh)

        self.parallel(fetch, sorted(set(digests)))

    def upload_files(self, paths, known):
        digests, unique = {}, {}
        for name, path in paths.items():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            digests[name] = digest
            unique.setdefault(digest, path)
        self.add("duplicate_files_avoided", len(paths) - len(unique))

        def upload(item):
            digest, path = item
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != digest:
                raise ValueError("Local catalog changed during upload")
            self.upload(body, known)

        self.parallel(upload, unique.items())
        return digests

    def trim(self, maximum):
        files = [
            (path.stat().st_mtime, path.stat().st_size, path)
            for path in self.directory.glob("*/*.blob")
        ]
        size = sum(item[1] for item in files)
        for _, length, path in sorted(files):
            if size <= maximum:
                break
            path.unlink(missing_ok=True)
            size -= length
            self.add("cache_evictions")
        self.add("cache_bytes", size)


def pack_refresh(entries, generation):
    metadata = {
        "version": 1,
        "generation": generation,
        "files": {name: hashlib.sha256(body).hexdigest() for name, body in entries.items()},
    }
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name, body in sorted({**entries, "refresh-meta.json": encode(metadata)}.items()):
                json.loads(body)
                info = tarfile.TarInfo(name)
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
    return output.getvalue()


def unpack_refresh(body, generation, allowed):
    entries = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        for member in archive:
            total += member.size
            if (
                not member.isfile()
                or member.name not in allowed | {"refresh-meta.json"}
                or member.name in entries
                or len(entries) >= 16
                or total > 2_000_000_000
            ):
                raise ValueError("Unsafe refresh archive entry")
            content = archive.extractfile(member).read()
            json.loads(content)
            entries[member.name] = content
    metadata = json.loads(entries.pop("refresh-meta.json", b"{}"))
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in entries.items()}
    if (
        metadata.get("version") != 1
        or metadata.get("generation") != generation
        or metadata.get("files") != hashes
    ):
        raise ValueError("Refresh archive integrity mismatch")
    return entries
