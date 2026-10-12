import assert from "node:assert/strict";
import test from "node:test";
import worker from "../src/index.js";
import { bundleImages, bundleTypeLabel, canonicalBundleType, Catalog } from "../../shared/catalog.js";
import { fixture } from "./fixture.js";

const source = { id: "s1", legacy_id: 1, url: "https://github.com/owner/repo",
  namespace: "owner", repo: "repo", aliases: ["demo"], enabled: true,
  source_metadatum: { owner_name: "owner", repo_name: "repo",
    bundle_icon_url: "https://raw.test/patches-bundle.png",
    owner_avatar_url: "https://avatars.test/owner.png" } };
async function call(state, path, query) {
  const request = query ? new Request("https://api.test" + path, { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ query }) }) : new Request("https://api.test" + path);
  const response = await worker.fetch(request, state.env);
  return { status: response.status, body: await response.json() };
}
test("family labels hide Morphe and Reseam runtime versions", () => {
  for (const type of ["Morphe:V1", "Morphe:V2"]) assert.equal(bundleTypeLabel(type), "Morphe");
  for (const type of ["Reseam", "Reseam:v0.20.1"]) assert.equal(bundleTypeLabel(type), "Reseam");
});
test("Morphe prefers its root icon; other formats use the owner avatar", () => {
  const morphe = { bundle_type: "Morphe:V1", source };
  assert.equal(bundleImages(morphe).icon_url, source.source_metadatum.bundle_icon_url);
  for (const type of ["Reseam", "ReVanced:v21", "ReVanced:v22"])
    assert.equal(bundleImages({ ...morphe, bundle_type: type }).icon_url,
      source.source_metadatum.owner_avatar_url);
});

test("missing or invalid images fall back to the owner without metadata", () => {
  const row = { bundle_type: "Morphe:V1", source: { ...source, source_metadatum: {} } };
  assert.equal(bundleImages(row).icon_url, "https://github.com/owner.png?size=96");
  row.source.source_metadatum = { bundle_icon_url: "javascript:bad()", owner_avatar_url: "data:bad" };
  assert.equal(bundleImages(row).icon_url, "https://github.com/owner.png?size=96");
  row.source.url = "https://codeberg.org/owner/repo";
  assert.deepEqual(bundleImages(row), { icon_url: null, owner_avatar_url: null });
});
test("bundle collections, details, imports and GraphQL expose consistent images", async () => {
  const initial = fixture();
  const bundle = { ...initial.bundle, bundle_type: "Morphe:V1", ecosystem: "morphe" };
  const state = fixture({ "sources.json": [source], "bundles.json": [bundle],
    ["bundles/" + bundle.id + ".json"]: bundle });
  const paths = ["/api/v3/bundle?source_url=https://github.com/owner/repo&version=v1",
    "/api/v1/bundle/1", "/api/v1/bundle/owner/repo/v1",
    "/api/v2/bundle/owner/repo/latest?channel=stable", "/bundles/" + bundle.id];
  for (const path of paths) {
    const result = await call(state, path);
    assert.equal(result.status, 200, path);
    assert.equal(result.body.icon_url, source.source_metadatum.bundle_icon_url, path);
    assert.equal(result.body.owner_avatar_url, source.source_metadatum.owner_avatar_url, path);
  }
  const list = await call(state, "/bundles");
  assert.equal(list.body.data[0].icon_url, source.source_metadatum.bundle_icon_url);
  const graph = await call(state, "/graphql",
    "{ bundle { icon_url owner_avatar_url source { source_metadatum { bundle_icon_url } } } }");
  assert(!graph.body.errors, JSON.stringify(graph.body));
  assert.equal(graph.body.data.bundle[0].icon_url, source.source_metadatum.bundle_icon_url);
});

test("Reseam native imports include the owner image alongside signed metadata", async () => {
  const initial = fixture();
  const publisher = { name: "patches", author: "owner", description: "", public_key: "a".repeat(64) };
  const bundle = { ...initial.bundle, bundle_type: "Reseam", reseam_bundle: publisher };
  const state = fixture({ "sources.json": [source], "bundles.json": [bundle],
    ["bundles/" + bundle.id + ".json"]: bundle });
  const result = await call(state, "/api/reseam/bundle?source_url=https://github.com/owner/repo&version=v1");
  assert.equal(result.status, 200);
  assert.equal(result.body.icon_url, source.source_metadatum.owner_avatar_url);
  assert.equal(result.body.bundle.public_key, publisher.public_key);
});
test("website catalog reads all records beyond the API input budget", async () => {
  const initial = fixture();
  const bundles = Array.from({ length: 105 }, (_, i) => ({
    ...initial.bundle, id: i.toString(16).padStart(24, "0"), description: "x".repeat(125000) }));
  const state = fixture({ "bundles.json": bundles });
  const site = await new Catalog("https://example.test/database/", state.env.FETCH, false).initialize();
  assert.equal((await site.bundles()).length, 105);
  const api = await new Catalog("https://example.test/database/", state.env.FETCH).initialize();
  await assert.rejects(api.bundles(), /budget/);
});

test("Older Reseam snapshots keep imports and labels after the spelling change", async () => {
    assert.equal(canonicalBundleType("ReSeam"),"Reseam");
    assert.equal(bundleTypeLabel("ReSeam:v1"),"Reseam");
    const state = fixture();
    const row = {...state.bundle,bundle_type:"ReSeam"};
    const snapshot = fixture({"bundles.json":[row],["bundles/"+row.id+".json"]:row});
    const catalog = await new Catalog("https://example.test/database/", snapshot.env.FETCH).initialize();
    assert.equal((await catalog.bundles())[0].bundle_type,"Reseam");
    assert.equal((await catalog.file("bundles/"+row.id+".json")).bundle_type,"Reseam");
});
