"""Exercise process isolation with local JVM fixtures and optionally real bundle artifacts."""

import argparse
import json
import os
import subprocess
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command, timeout=180):
    return subprocess.run(  # noqa: S603 - explicit Java/Javac argument arrays, never a shell
        command, check=True, capture_output=True, text=True, timeout=timeout
    )


@contextmanager
def fixture_directory():
    directory = tempfile.TemporaryDirectory(prefix="catalog-smoke-")
    try:
        yield directory.name
    finally:
        # Windows can briefly retain a JAR lock after its worker exits.
        # Retry only permission failures and still fail if the lock persists.
        for attempt in range(10):
            try:
                directory.cleanup()
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1 * (attempt + 1))


def smoke(real=False):
    java_home = Path(os.environ.get("JAVA_HOME", r"C:\Program Files\Java\jdk-17"))
    suffix = ".exe" if os.name == "nt" else ""
    java = str(java_home / "bin" / ("java" + suffix))
    javac = str(java_home / "bin" / ("javac" + suffix))
    libraries = ROOT / "bundle-parser/build/install/bundle-parser/lib"
    classpath = os.pathsep.join(str(file) for file in sorted(libraries.glob("*.jar")))
    if not classpath:
        raise RuntimeError("Run bundle-parser assembleRelease first")
    config = json.loads((ROOT / "config/patcher-runtimes.json").read_text())
    installed = json.loads((ROOT / "bundle-parser/build/runtime-classpaths.json").read_text())
    results = []
    with fixture_directory() as temporary:
        folder = Path(temporary)
        sources = {
            "fixture/BundleMarker.java": "package fixture; public class BundleMarker {}",
            "app/revanced/patcher/patch/Patch.java": """
package app.revanced.patcher.patch;
public class Patch {
 public String getName() { return "Fixture patch"; }
 public String getDescription() { return "Runtime fixture"; }
 public boolean getUse() { return true; }
}
""",
            "app/revanced/patcher/patch/PatchKt.java": """
package app.revanced.patcher.patch;
import java.io.File; import java.util.List;
public class PatchKt {
 public static List<Patch> loadPatches(File file, Object callback) {
   return List.of(new Patch());
 }
}
""",
            "app/morphe/patcher/patch/PatchKt.java": """
package app.morphe.patcher.patch;
import java.util.Set; import java.util.List;
import app.revanced.patcher.patch.Patch;
public class PatchKt {
 public static List<Patch> loadPatchesFromJar(Set files) {
   return List.of(new Patch());
 }
}
""",
        }
        for relative, source in sources.items():
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source)
        classes = folder / "classes"
        run([javac, "-d", str(classes), *[str(folder / name) for name in sources]])
        runtime_jar = folder / "fixture-runtime.jar"
        with zipfile.ZipFile(runtime_jar, "w") as jar:
            for path in classes.rglob("*.class"):
                jar.write(path, path.relative_to(classes).as_posix())
        # Include compiled code in the bundle; the fixture runtime supplies a reflective patch.
        artifact = folder / "fixture.rvp"
        with zipfile.ZipFile(artifact, "w") as jar:
            jar.write(classes / "fixture/BundleMarker.class", "fixture/BundleMarker.class")
            jar.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n\n")
        fixture_config = {
            # Windows JVM startup can take over two seconds for a crash/restart pair.
            "worker": {"timeout_seconds": 6, "max_heap": "128m"},
            "runtimes": [
                {
                    "id": "unavailable",
                    "family": "ReVanced:v22",
                    "loader": "revanced-modern",
                    "coordinate": "test:unavailable:1",
                    "dependencies": [],
                    "range": "*",
                    "fallback": True,
                },
                {
                    "id": "working",
                    "family": "ReVanced:v22",
                    "loader": "revanced-modern",
                    "coordinate": "test:working:1",
                    "dependencies": [],
                    "range": "*",
                    "fallback": True,
                },
                {
                    "id": "morphe",
                    "family": "Morphe:V1",
                    "loader": "morphe",
                    "coordinate": "test:morphe:1",
                    "dependencies": [],
                    "range": ">=1.0.0 <=1.14.1",
                    "fallback": True,
                },
            ],
        }
        runtime_manifest = {
            "unavailable": {"available": False, "classpath": ""},
            "working": {"available": True, "classpath": str(runtime_jar)},
            "morphe": {"available": True, "classpath": str(runtime_jar)},
        }

        def extract(path, label, definition=fixture_config, runtimes=runtime_manifest):
            cfg, manifest, output = (
                folder / "config.json",
                folder / "runtimes.json",
                folder / "result.json",
            )
            cfg.write_text(json.dumps(definition))
            manifest.write_text(json.dumps(runtimes))
            run(
                [
                    java,
                    "-Dcatalog.runtime.config=" + str(cfg),
                    "-Dcatalog.runtime.manifest=" + str(manifest),
                    "-cp",
                    classpath,
                    "me.jman.parser.MainKt",
                    "--extract-one",
                    str(path),
                    "legacy" if path.suffix == ".jar" else "modern",
                    str(output),
                ]
            )
            result = json.loads(output.read_text())
            results.append({"case": label, **result})
            print(label + ": " + json.dumps(result))
            return result

        assert extract(artifact, "unavailable runtime falls back")["status"] == "verified"
        # Exercise the repaired, installed legacy runtime without another fallback
        # hiding a missing dependency or a class-loader failure.
        legacy_id = "revanced-v3-11"
        if installed.get(legacy_id, {}).get("available"):
            legacy_source = folder / "LegacyPatch.java"
            legacy_source.write_text(
                "@app.revanced.patcher.patch.annotations.Patch\n"
                '@app.revanced.patcher.annotation.Name(name="Legacy runtime fixture")\n'
                "public class LegacyPatch {}\n"
            )
            legacy_classes = folder / "legacy-classes"
            run(
                [
                    javac,
                    "-cp",
                    installed[legacy_id]["classpath"],
                    "-d",
                    str(legacy_classes),
                    str(legacy_source),
                ]
            )
            legacy_artifact = folder / "legacy-fixture.jar"
            with zipfile.ZipFile(legacy_artifact, "w") as jar:
                jar.writestr(
                    "META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nPatcher-Version: 11.0.4\n\n"
                )
                jar.write(legacy_classes / "LegacyPatch.class", "LegacyPatch.class")
            legacy_config = {
                **config,
                "runtimes": [r for r in config["runtimes"] if r["id"] == legacy_id],
            }
            inspected = extract(
                legacy_artifact,
                "installed patcher 11 legacy bundle",
                legacy_config,
                {legacy_id: installed[legacy_id]},
            )
            assert inspected["status"] == "verified" and inspected["patch_count"] == 1
            assert inspected["runtime"] == "app.revanced:revanced-patcher:11.0.4"
        morphe = folder / "fixture.mpp"
        with zipfile.ZipFile(morphe, "w") as jar:
            jar.write(classes / "fixture/BundleMarker.class", "fixture/BundleMarker.class")
            jar.writestr(
                "META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nPatcher-Version: 1.9.0\n\n"
            )
        assert extract(morphe, "declared Morphe runtime")["status"] == "verified"
        with zipfile.ZipFile(morphe, "w") as jar:
            jar.write(classes / "fixture/BundleMarker.class", "fixture/BundleMarker.class")
            jar.writestr(
                "META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nPatcher-Version: 2.0.0\n\n"
            )
        assert extract(morphe, "unmatched Morphe version tries fallback")["status"] == "verified"
        with zipfile.ZipFile(morphe, "w") as jar:
            jar.write(classes / "fixture/BundleMarker.class", "fixture/BundleMarker.class")
            jar.writestr(
                "META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nPatcher-Version: legacy\n\n"
            )
        assert extract(morphe, "non-semver declaration tries fallback")["status"] == "verified"
        # Try the declared version, then newest to oldest. Exercise
        # rejection and unavailable dependencies with isolated workers and no network.
        morphe_facade = folder / "app/morphe/patcher/patch/PatchKt.java"
        morphe_facade.write_text(
            sources["app/morphe/patcher/patch/PatchKt.java"].replace(
                "return List.of(new Patch());",
                'throw new IllegalArgumentException("Preferred runtime rejected bundle");',
            )
        )
        rejected_classes = folder / "rejected-classes"
        run([javac, "-cp", str(classes), "-d", str(rejected_classes), str(morphe_facade)])
        rejected_runtime = folder / "rejected-runtime.jar"
        with zipfile.ZipFile(rejected_runtime, "w") as jar:
            for path in rejected_classes.rglob("*.class"):
                jar.write(path, path.relative_to(rejected_classes).as_posix())
        morphe_facade.write_text(sources["app/morphe/patcher/patch/PatchKt.java"])
        morphe_definition = fixture_config["runtimes"][2]
        fallback_config = {
            **fixture_config,
            "runtimes": [
                {
                    **morphe_definition,
                    "id": "alternate",
                    "coordinate": "test:alternate:1.0.0",
                    "range": "<=1.0.0",
                    "fallback": True,
                },
                {
                    **morphe_definition,
                    "id": "preferred",
                    "coordinate": "test:preferred:2.0.0",
                    "range": ">1.0.0",
                    "fallback": False,
                },
            ],
        }
        fallback_manifest = {
            "preferred": {"available": True, "classpath": str(rejected_runtime)},
            "alternate": {"available": True, "classpath": str(runtime_jar)},
        }
        with zipfile.ZipFile(morphe, "w") as jar:
            jar.write(classes / "fixture/BundleMarker.class", "fixture/BundleMarker.class")
            jar.writestr(
                "META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nPatcher-Version: 1.0.0\n\n"
            )
        both_working = {
            **fallback_manifest,
            "preferred": {"available": True, "classpath": str(runtime_jar)},
        }
        declared = extract(
            morphe, "declared older runtime stays first", fallback_config, both_working
        )
        assert declared["status"] == "verified" and declared["runtime"] == "test:alternate:1.0.0"
        older_rejects = {
            **both_working,
            "alternate": {"available": True, "classpath": str(rejected_runtime)},
        }
        recovered = extract(
            morphe, "rejected declared runtime retries latest", fallback_config, older_rejects
        )
        assert recovered["status"] == "verified" and recovered["runtime"] == "test:preferred:2.0.0"
        with zipfile.ZipFile(morphe, "w") as jar:
            jar.write(classes / "fixture/BundleMarker.class", "fixture/BundleMarker.class")
            jar.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n\n")
        newest = extract(
            morphe, "missing declaration starts with latest", fallback_config, both_working
        )
        assert newest["status"] == "verified" and newest["runtime"] == "test:preferred:2.0.0"
        with zipfile.ZipFile(morphe, "w") as jar:
            jar.write(classes / "fixture/BundleMarker.class", "fixture/BundleMarker.class")
            jar.writestr(
                "META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nPatcher-Version: 2.0.0\n\n"
            )
        recovered = extract(
            morphe, "rejected preferred runtime falls back", fallback_config, fallback_manifest
        )
        assert recovered["status"] == "verified" and recovered["runtime"] == "test:alternate:1.0.0"
        unavailable = {**fallback_manifest, "preferred": {"available": False, "classpath": ""}}
        recovered = extract(
            morphe, "unavailable preferred runtime falls back", fallback_config, unavailable
        )
        assert recovered["status"] == "verified" and recovered["runtime"] == "test:alternate:1.0.0"
        all_rejected = {
            **fallback_manifest,
            "alternate": {"available": True, "classpath": str(rejected_runtime)},
        }
        rejected = extract(morphe, "all Morphe runtimes reject", fallback_config, all_rejected)
        assert rejected["terminal"] and rejected["status"] == "runtime_parsing_failure"
        assert extract(artifact, "unrelated bundle after failure")["status"] == "verified"
        # Morphe requires named public patches; legacy nullable metadata remains readable.
        patch_source = folder / "app/revanced/patcher/patch/Patch.java"
        patch_source.write_text(
            sources["app/revanced/patcher/patch/Patch.java"].replace(
                'return "Fixture patch";', "return null;"
            )
        )
        run([javac, "-d", str(classes), str(patch_source)])
        with zipfile.ZipFile(runtime_jar, "w") as jar:
            for path in classes.rglob("*.class"):
                jar.write(path, path.relative_to(classes).as_posix())
        for path in (artifact, morphe):
            if path == morphe:
                with zipfile.ZipFile(path, "w") as jar:
                    jar.write(classes / "fixture/BundleMarker.class", "fixture/BundleMarker.class")
                    jar.writestr(
                        "META-INF/MANIFEST.MF", "Manifest-Version: 1.0\nPatcher-Version: 1.9.0\n\n"
                    )
            result = extract(path, "unnamed " + path.suffix)
            if path == morphe:
                assert result["status"] == "incomplete_patch_inventory" and result["terminal"]
            else:
                assert result["status"] == "verified" and result["patch_count"] == 1
        for name in (
            "app/revanced/patcher/patch/PatchKt.java",
            "app/morphe/patcher/patch/PatchKt.java",
        ):
            (folder / name).write_text(
                sources[name].replace("return List.of(new Patch());", "return List.of();")
            )
        run(
            [
                javac,
                "-cp",
                str(classes),
                "-d",
                str(classes),
                str(folder / "app/revanced/patcher/patch/PatchKt.java"),
                str(folder / "app/morphe/patcher/patch/PatchKt.java"),
            ]
        )
        with zipfile.ZipFile(runtime_jar, "w") as jar:
            for path in classes.rglob("*.class"):
                jar.write(path, path.relative_to(classes).as_posix())
        for path in (artifact, morphe):
            result = extract(path, "empty " + path.suffix)
            assert result["status"] == "verified" and result["patch_count"] == 0
        broken = folder / "broken.rvp"
        broken.write_bytes(b"not an archive")
        assert extract(broken, "malformed artifact")["status"] == "runtime_parsing_failure"
        # Count calls across worker JVMs to distinguish crash retries from bundle rejection.
        facade = folder / "app/revanced/patcher/patch/PatchKt.java"
        marker = folder / "attempts.txt"
        counter = """
   java.nio.file.Path marker = java.nio.file.Path.of(MARKER);
   int attempt = java.nio.file.Files.exists(marker)
       ? Integer.parseInt(java.nio.file.Files.readString(marker)) + 1 : 1;
   java.nio.file.Files.writeString(marker, Integer.toString(attempt));
""".replace("MARKER", json.dumps(str(marker)))
        cases = [
            (
                "worker crash recovers immediately",
                "if (attempt == 1) System.exit(7); return List.of(new Patch());",
                "verified",
                2,
                None,
            ),
            (
                "worker crash retries once",
                "System.exit(7); return List.of(new Patch());",
                "runtime_initialization_failure",
                2,
                None,
            ),
            (
                "worker restart can be disabled",
                "System.exit(7); return List.of(new Patch());",
                "runtime_initialization_failure",
                1,
                0,
            ),
            (
                "bundle rejection is not retried",
                'throw new IllegalArgumentException("Rejected bundle");',
                "runtime_parsing_failure",
                1,
                None,
            ),
            (
                "hung worker deadline",
                "Thread.sleep(10000); return List.of(new Patch());",
                "runtime_timeout",
                1,
                None,
            ),
            (
                "retry shares original deadline",
                "if (attempt == 1) { Thread.sleep(3000); System.exit(7); } "
                "Thread.sleep(10000); return List.of(new Patch());",
                "runtime_timeout",
                2,
                None,
            ),
        ]
        for label, body, status, attempts, restarts in cases:
            marker.unlink(missing_ok=True)
            facade.write_text(
                sources["app/revanced/patcher/patch/PatchKt.java"]
                .replace(
                    "loadPatches(File file, Object callback) {",
                    "loadPatches(File file, Object callback) throws Exception {",
                )
                .replace("return List.of(new Patch());", counter + body)
            )
            run([javac, "-cp", str(classes), "-d", str(classes), str(facade)])
            with zipfile.ZipFile(runtime_jar, "w") as jar:
                for path in classes.rglob("*.class"):
                    jar.write(path, path.relative_to(classes).as_posix())
            settings = dict(fixture_config["worker"])
            if restarts is not None:
                settings["restart_attempts"] = restarts
            if label == "retry shares original deadline":
                # Allow both JVMs to start on a busy runner. A reset deadline
                # would still exceed the elapsed-time assertion below.
                settings["timeout_seconds"] = 6
            started = time.monotonic()
            result = extract(artifact, label, {**fixture_config, "worker": settings})
            elapsed = time.monotonic() - started
            assert result["status"] == status, result
            # The unavailable compatible runtime prevents a permanent rejection.
            assert not result["terminal"], result
            assert int(marker.read_text()) == attempts, label
            if label == "bundle rejection is not retried":
                marker.unlink()
                only_working = {**fixture_config, "runtimes": [fixture_config["runtimes"][1]]}
                terminal = extract(artifact, "all compatible runtimes reject", only_working)
                assert terminal["terminal"] and terminal["status"] == "runtime_parsing_failure"
                assert int(marker.read_text()) == 1
            if status == "runtime_timeout":
                # Includes the parent CLI startup, but not another full worker timeout.
                assert elapsed < settings["timeout_seconds"] + 1.5, (label, elapsed)
        # Use a JVM executable as a portable native-process fixture. Its default
        # classpath supplies the CLI's "bundle" command without changing launch arguments.
        native_artifact = folder / "fixture.reseam"
        native_artifact.write_bytes(b"native fixture")
        native_source = folder / "bundle.java"
        publisher = {
            "bundles": [
                {
                    "name": "fixture",
                    "author": "test",
                    "description": "",
                    "public_key": "a" * 64,
                    "problem": None,
                }
            ],
            "patches": [],
        }
        native_config = {
            "worker": fixture_config["worker"],
            "runtimes": [
                {
                    "id": "native",
                    "family": "Reseam",
                    "loader": "reseam",
                    "coordinate": "test:native:1",
                    "dependencies": [],
                    "fallback": True,
                }
            ],
        }
        native_manifest = {"native": {"available": True, "executable": java}}
        original_classpath = os.environ.get("CLASSPATH")
        os.environ["CLASSPATH"] = str(classes)
        try:
            for label, body, status in (
                (
                    "native crash recovers immediately",
                    "if (attempt == 1) System.exit(7); System.out.println(RESPONSE);",
                    "verified",
                ),
                (
                    "native crash remains retryable",
                    "System.exit(7);",
                    "runtime_initialization_failure",
                ),
            ):
                marker.unlink(missing_ok=True)
                native_source.write_text(
                    "public class bundle { "
                    "public static void main(String[] args) throws Exception {"
                    + counter
                    + body.replace("RESPONSE", json.dumps(json.dumps(publisher)))
                    + "} }"
                )
                run([javac, "-d", str(classes), str(native_source)])
                result = extract(native_artifact, label, native_config, native_manifest)
                assert result["status"] == status and not result["terminal"], result
                assert int(marker.read_text()) == 2, label
        finally:
            if original_classpath is None:
                os.environ.pop("CLASSPATH", None)
            else:
                os.environ["CLASSPATH"] = original_classpath
        # An installed native inspector must classify a rejected archive explicitly.
        reseam_runtime = next(
            runtime for runtime in config["runtimes"] if runtime["loader"] == "reseam"
        )
        if installed.get(reseam_runtime["id"], {}).get("available"):
            invalid_reseam = folder / "invalid.reseam"
            invalid_reseam.write_bytes(b"invalid archive")
            rejected = extract(invalid_reseam, "Reseam rejects invalid archive", config, installed)
            native_candidates = [
                runtime for runtime in config["runtimes"] if runtime["family"] == "Reseam"
            ]
            all_available = all(
                installed.get(runtime["id"], {}).get("available") for runtime in native_candidates
            )
            assert rejected["terminal"] == all_available
            assert rejected["status"] in {
                "runtime_parsing_failure",
                "runtime_missing_dependency",
                "incomplete_patch_inventory",
            }
            # Local live-validation artifacts are optional and never committed.
            for artifact in (ROOT / "internal/reseam-fixtures").glob("*.reseam"):
                inspected = extract(artifact, "real Reseam " + artifact.name, config, installed)
                assert inspected["status"] == "verified" and inspected["patch_count"] > 0
        audit = os.environ.get("CATALOG_COVERAGE_AUDIT")
        if audit:
            directory = Path(audit)
            inventory = json.loads((directory / "inventory.json").read_text(encoding="utf8"))
            unexpected = []
            for row in inventory["available"]:
                artifact = directory / row["path"]
                inspected = extract(artifact, "coverage repair " + artifact.name, config, installed)
                expected = inventory.get("expected_failures", {}).get(row.get("file_hash"))
                if expected:
                    assert inspected["status"] == expected, inspected
                elif inspected["status"] != "verified" or inspected["patch_count"] <= 0:
                    unexpected.append({
                        "source": row["source"]["url"],
                        "version": row["version"], **inspected,
                    })
            assert not unexpected, unexpected
        if real:
            import httpx

            representatives = [
                ("revanced", "revanced-latest", ".rvp"),
                ("lain", "lain-latest", ".mpp"),
                ("piko", "piko-stable", ".jar"),
            ]
            for source, key, extension in representatives:
                path = folder / ("real-" + source + extension)
                try:
                    metadata = json.loads(
                        (
                            ROOT
                            / "internal/cache/bundles"
                            / (source + "-patch-bundles")
                            / (key + "-patches-bundle.json")
                        ).read_text()
                    )
                    url = metadata.get("download_url") or metadata.get("patches", {}).get("url")
                    if not isinstance(url, str) or not url.startswith("https://github.com/"):
                        raise ValueError("Expected a GitHub release artifact")
                    with httpx.stream("GET", url, timeout=30, follow_redirects=True) as response:
                        response.raise_for_status()
                        with path.open("wb") as output:
                            for chunk in response.iter_bytes():
                                output.write(chunk)
                    result = extract(path, "real " + source, config, installed)
                    if result["status"] != "verified":
                        print("Compatibility not verified for " + source)
                except Exception as error:
                    status = getattr(getattr(error, "response", None), "status_code", None)
                    results.append(
                        {
                            "case": "real " + source,
                            "status": "artifact_unavailable",
                            "error_type": type(error).__name__,
                            "http_status": status,
                        }
                    )
                    print("Real bundle unavailable: " + source + " (" + type(error).__name__ + ")")
    audited = [item for item in results if item["case"].startswith("coverage repair ")]
    if audited:
        from collections import Counter
        counts = dict(Counter(item["status"] for item in audited))
        print("Coverage audit summary: " + json.dumps(counts))
    report = ROOT / "bundle-parser/build/runtime-validation.json"
    report.write_text(json.dumps(results, indent=2) + "\n")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args()
    smoke(args.real)
