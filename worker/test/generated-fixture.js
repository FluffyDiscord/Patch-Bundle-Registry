import { execFile } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { promisify } from "node:util";

// Exercise the real generator without depending on a checked-in or local catalog.
export async function generatedCatalog(t) {
  const temporary = await mkdtemp(join(tmpdir(), "worker-catalog-"));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  await promisify(execFile)(process.env.PYTHON || "python", ["-c", `
import sys
from pathlib import Path
sys.path.insert(0, "tests")
from test_generate_database import db, fixture, write
root = Path(sys.argv[1])
folder = fixture(root)
config = db.read_json(root / "config/site.json")
write(root / "config/site.json", {**config, "page_size": 20})
inventory = db.read_json(root / "config/sources.json")
inventory["third"] = {"patches": "https://github.com/owner/third", "disabled": False}
write(root / "config/sources.json", inventory)
for channel in ("latest", "stable"):
    path = folder / f"demo-{channel}-patches-list.json"
    payload = db.read_json(path)
    original = payload["patches"][0]
    payload["patches"] = [
        {**original, "name": None if index % 5 == 0 else f"Patch {index:02}",
         "use": None if index % 4 == 0 else True}
        for index in range(40)
    ]
    write(path, payload)
    proof = folder / f"demo-{channel}-extraction.json"
    write(proof, {**db.read_json(proof), "patch_list_hash": db.digest(path.read_bytes())})
db.build_database(root)
`, temporary], {
    cwd: fileURLToPath(new URL("../../", import.meta.url)), timeout: 30000
  });
  return pathToFileURL(join(temporary, "database") + sep);
}
