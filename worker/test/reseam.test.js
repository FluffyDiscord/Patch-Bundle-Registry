import assert from "node:assert/strict";
import test from "node:test";
import worker from "../src/index.js";
import { bundleImportPath, bundleTypeLabel, Catalog } from "../../shared/catalog.js";
import { fixture } from "./fixture.js";

async function call(state, path) {
  const response = await worker.fetch(new Request("https://api.test" + path), state.env);
  return { status: response.status, body: await response.json() };
}
test("old catalog family names migrate after integrity verification", async () => {
  const original = fixture();
  const old = { ...original.bundle, bundle_type: "ReVanced:V4" };
  const state = fixture({ "bundles.json": [old], ["bundles/" + old.id + ".json"]: old });
  const catalog = await new Catalog("https://example.test/database/", state.env.FETCH).initialize();
  assert.equal((await catalog.bundles())[0].bundle_type, "ReVanced:v22");
  assert.equal((await catalog.file("bundles/" + old.id + ".json")).bundle_type, "ReVanced:v22");
  assert.equal(bundleTypeLabel("ReVanced:V3"), "ReVanced v21");
  assert.equal(bundleTypeLabel("ReVanced:v22"), "ReVanced v22");
  assert.equal(bundleTypeLabel("Morphe:V1"), "Morphe");
  assert.equal(bundleTypeLabel("Morphe:V2"), "Morphe");
});
test("Reseam imports use the signed publisher's native index, with channel and pinning", async () => {
  const original = fixture();
  const publisher = { name: "patches", author: "Example", description: "Apps", public_key: "a".repeat(64) };
  const bundle = { ...original.bundle, bundle_type: "Reseam", ecosystem: "reseam",
    download_url: "https://example.test/patches.reseam", reseam_bundle: publisher };
  const state = fixture({ "bundles.json": [bundle], ["bundles/" + bundle.id + ".json"]: bundle });
  const card = { bundleType: "Reseam", sourceUrl: original.bundle.source?.url ?? "https://github.com/owner/repo",
    version: bundle.version, isPrerelease: false };
  const latest = await call(state, bundleImportPath(card));
  assert.equal(latest.status, 200);
  assert.equal(latest.body.bundle.public_key, publisher.public_key);
  assert.equal(latest.body.releases[0].download_url, bundle.download_url);
  assert.equal((await call(state, bundleImportPath(card, true))).body.releases[0].version, "v1");
  assert.equal((await call(state, "/bundles?bundle_type=Reseam")).body.data.length, 1);
  assert.equal((await call(state, "/bundles?ecosystem=reseam")).body.data.length, 1);
  assert.equal((await call(state, "/patches?bundle_type=Reseam")).body.data.length, 1);
  const failed = { ...bundle, metadata_status: "unverified" };
  const unavailable = fixture({ "bundles.json": [failed], ["bundles/" + failed.id + ".json"]: failed });
  assert.equal((await call(unavailable, bundleImportPath(card))).status, 503);
  assert.equal((await call(original, bundleImportPath(card))).status, 404);
});

test("manager imports read only their compact source index and selected release", async () => {
  const original = fixture();
  const publisher = { name: "patches", author: "Example", description: "Apps", public_key: "a".repeat(64) };
  const bundle = { ...original.bundle, bundle_type: "Reseam", reseam_bundle: publisher };
  const state = fixture({
    "bundles.json": [{ ...bundle, description: "x".repeat(13 * 1024 * 1024) }],
    "imports/s1.json": [bundle], ["bundles/" + bundle.id + ".json"]: bundle
  });
  const path = "/api/reseam/bundle?source_url=https://github.com/owner/repo&version=latest&channel=stable";
  assert.equal((await call(state, path)).status, 200);
  assert(state.requests.some(url => url.endsWith("/imports/s1.json")));
  assert(!state.requests.some(url => url.endsWith("/bundles.json")));
});


test("legacy Reseam compact indexes support app filters and canonical responses", async () => {
  const old = { ...fixture().bundle, bundle_type: "ReSeam" };
  const state = fixture({ "bundles.json": [old], "app-bundles.json": [old],
    ["bundles/" + old.id + ".json"]: old });
  const catalog = await new Catalog("https://example.test/database/", state.env.FETCH).initialize();
  assert.equal((await catalog.bundles({ compact: true }))[0].bundle_type, "Reseam");
  for (const type of ["Reseam", "ReSeam"]) {
    const apps = await call(state, "/apps?bundle_type=" + type);
    assert.equal(apps.status, 200);
    assert.equal(apps.body.data.length, 1);
    const bundles = await call(state, "/apps/com.video/bundles?bundle_type=" + type);
    assert.equal(bundles.status, 200);
    assert.equal(bundles.body.data.length, 1);
    assert.equal(bundles.body.data[0].bundle_type, "Reseam");
  }
});
