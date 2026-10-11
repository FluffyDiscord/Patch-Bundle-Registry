"""Preview uncommitted website code with an isolated, freshly generated catalog."""

import argparse
import mimetypes
import re
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    from scripts.generate_database import ROOT, build_database, encoded, read_json
except ModuleNotFoundError:
    from generate_database import ROOT, build_database, encoded, read_json


def prepare_preview(root=ROOT, *, refresh_apps=False):
    root = Path(root)
    preview = root / "internal/site-preview"
    preview.mkdir(parents=True, exist_ok=True)
    shutil.copytree(root / "config", preview / "config", dirs_exist_ok=True)
    manifest = read_json(root / "database/manifest.json", {})
    snapshot = manifest.get("snapshot", "")
    if not re.fullmatch(r"snapshots/[a-f0-9]{24}", snapshot):
        raise ValueError("A generated local catalog is required for preview")
    destination = preview / "database"
    destination.mkdir(exist_ok=True)
    shutil.copytree(root / "database" / snapshot, destination / snapshot, dirs_exist_ok=True)
    (destination / "manifest.json").write_bytes(encoded(manifest))
    metadata = root / "database/app-metadata.json"
    cached = destination / "app-metadata.json"
    if metadata.exists() and (
        not cached.exists() or metadata.stat().st_mtime > cached.stat().st_mtime
    ):
        shutil.copyfile(metadata, cached)
    # Restore published extraction inputs, without modifying the working catalog.
    build_database(preview)
    if refresh_apps:
        import httpx

        try:
            from scripts.refresh_app_metadata import refresh
        except ModuleNotFoundError:
            from refresh_app_metadata import refresh
        with httpx.Client(
            timeout=httpx.Timeout(20, connect=10),
            follow_redirects=True,
            headers={"User-Agent": "Patch-Bundle-Registry/1.0"},
        ) as client:
            refresh(preview, client)
        build_database(preview, restore=False)
    return preview


def preview_handler(root, preview):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            name = unquote(urlsplit(self.path).path).lstrip("/")
            if name == "config.json":
                config = read_json(root / "config/site.json")
                config["live_data"] = False
                body, kind = encoded(config), "application/json"
            else:
                if name.startswith("database/"):
                    base, relative = preview / "database", name.removeprefix("database/")
                elif name.startswith("shared/"):
                    base, relative = root / "shared", name.removeprefix("shared/")
                elif name == "config/git-hosts.json":
                    base, relative = root / "config", "git-hosts.json"
                else:
                    base, relative = root / "web", name or "index.html"
                path = (base / relative).resolve()
                if not path.is_relative_to(base.resolve()) or not path.is_file():
                    self.send_error(404)
                    return
                body = path.read_bytes()
                kind = {
                    ".js": "text/javascript",
                    ".json": "application/json",
                    ".css": "text/css",
                }.get(path.suffix, mimetypes.guess_type(str(path))[0] or "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh-apps",
        action="store_true",
        help="Fetch cached F-Droid/Google Play names and icons",
    )
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    preview = prepare_preview(refresh_apps=args.refresh_apps)
    if args.prepare_only:
        print("Preview catalog ready in " + str(preview))
        return
    print(f"Preview: http://localhost:{args.port} (Ctrl+C to stop)", flush=True)
    with ThreadingHTTPServer(("127.0.0.1", args.port), preview_handler(ROOT, preview)) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
