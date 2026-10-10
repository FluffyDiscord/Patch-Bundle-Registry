"""Install pinned native catalog inspectors; downloaded executables stay in the local cache."""

import hashlib
import json
import os
import platform
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def install(runtime, root=ROOT):
    machine = platform.machine().lower()
    key = platform.system().lower() + ("-x64" if machine in {"amd64", "x86_64"} else "-" + machine)
    asset = runtime["executables"].get(key)
    if not asset:
        return {"available": False, "executable": ""}
    expected = asset["sha256"]
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("Invalid native runtime SHA-256")
    directory = root / "internal/cache/runtimes" / runtime["id"]
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / (expected + (".exe" if key.startswith("windows-") else ""))
    if not executable.exists() or hashlib.sha256(executable.read_bytes()).hexdigest() != expected:
        temporary = executable.with_suffix(".tmp")
        try:
            with httpx.stream(
                "GET",
                asset["url"],
                follow_redirects=True,
                timeout=60,
                headers={"User-Agent": "Patch-Bundle-Registry"},
            ) as response:
                response.raise_for_status()
                digest = hashlib.sha256()
                length = 0
                with temporary.open("wb") as output:
                    for chunk in response.iter_bytes():
                        length += len(chunk)
                        if length > 128 * 1024 * 1024:
                            raise ValueError("Native runtime exceeds download limit")
                        digest.update(chunk)
                        output.write(chunk)
                if digest.hexdigest() != expected:
                    raise ValueError("Native runtime checksum mismatch")
            temporary.replace(executable)
        finally:
            temporary.unlink(missing_ok=True)
    if os.name != "nt":
        executable.chmod(0o755)
    return {"available": True, "executable": str(executable.resolve())}


def prepare(output, root=ROOT):
    configured = json.loads((root / "config/patcher-runtimes.json").read_text(encoding="utf-8"))
    installed = {}
    for runtime in configured["runtimes"]:
        if runtime["loader"] != "reseam":
            continue
        try:
            installed[runtime["id"]] = install(runtime, root)
        except Exception as error:
            print(
                "Native runtime " + runtime["id"] + " unavailable (" + type(error).__name__ + ").",
                file=sys.stderr,
            )
            installed[runtime["id"]] = {"available": False, "executable": ""}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(installed, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    prepare(Path(sys.argv[1]))
