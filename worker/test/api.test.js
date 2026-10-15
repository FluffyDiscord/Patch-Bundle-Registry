import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import worker from "../src/index.js";
import { runGraphQL } from "../src/graphql.js";
import { Catalog, bundleImportPath, compareReleaseDates, matches, normalizeSource, packageFilterMatches } from "../../shared/catalog.js";

import { fixture } from "./fixture.js";
import { generatedCatalog } from "./generated-fixture.js";
async function call(state, path, options = {}) {
  const response = await worker.fetch(new Request("https://api.test" + path, options), state.env);
  return { response, body: response.status === 304 ? null : await response.json() };
}
async function query(state, queryText, variables) {
  return call(state, "/graphql", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query: queryText, variables }) });
}
test("cached v3 lookup normalizes URLs and selects latest/stable/prerelease/exact", async () => {
  const state = fixture();
  const base = "/api/v3/bundle?source_url=" + encodeURIComponent("https://api.github.com/repos/owner/repo");
  const latest = await call(state, base + "&version=latest&channel=any");
  assert.equal(latest.response.status, 200);
  assert.equal(latest.body.version, "v1");
  assert.equal(latest.body.bundle_type, "ReVanced:v22");
  assert.equal(latest.body.file_hash, "c".repeat(64));
  assert.equal((await call(state, base + "&version=latest&channel=prerelease")).body.version, "v2-dev");
  assert.equal((await call(state, base + "&version=v1")).body.version, "v1");
  assert.equal((await call(state, base + "&version=latest")).response.status, 400);
  assert.equal((await call(state, base + "&version=absent")).response.status, 404);
  assert(state.requests.every(url => url.startsWith("https://example.test/database/")));
});
test("reconciled releases do not reappear in prerelease imports or GraphQL", async () => {
  const original = fixture();
  const promoted = { ...original.dev, is_prerelease: false, is_latest: false, channels: [] };
  const state = fixture({
    "bundles.json": [original.bundle, promoted],
    ["bundles/" + promoted.id + ".json"]: promoted
  });
  const base = "/api/v3/bundle?source_url=" + encodeURIComponent("https://github.com/owner/repo");
  assert.equal((await call(state, base + "&version=latest&channel=prerelease")).response.status, 404);
  assert.equal((await call(state, "/api/v2/bundle/owner/repo/latest?channel=dev")).response.status, 404);
  assert.equal((await call(state, "/api/v1/bundle/owner/repo/latest?prerelease=true")).response.status, 404);
  assert.equal((await call(state, base + "&version=latest&channel=stable")).body.version, "v1");
  assert.equal((await call(state, base + "&version=v2-dev")).body.version, "v2-dev");
  const listed = await query(state, "{ bundle(where:{is_prerelease:{_eq:true}}){id} }");
  assert.equal(listed.body.errors, undefined);
  assert.deepEqual(listed.body.data.bundle, []);
});
test("REST pagination, package compatibility, freshness, search, and update status", async () => {
  const state = fixture();
  assert.equal((await call(state, "/bundles?limit=1&offset=1")).body.data.length, 1);
  assert.equal((await call(state, "/patches?q=ads&package=com.video&app_version=1.0")).body.data.length, 1);
  assert.equal((await call(state, "/patches?app_version=3.0")).body.data.length, 0);
  assert.equal((await call(state, "/patches?include_unverified=true")).body.data.length, 2);
  assert.equal((await call(state, "/bundles?metadata_status=stale")).body.data[0].need_patches_update, true);
  assert.equal((await call(state, "/packages?q=video")).body.data[0].versions.length, 2);
  assert.equal((await call(state, "/bundles?limit=101")).response.status, 400);
  assert.equal((await call(state, "/bundles?sort=invalid")).response.status, 400);
});
test("GraphQL variables, aliases, nested source/packages and aggregates", async () => {
  const state = fixture();
  const result = await query(state, `
    query Catalog($where: bundle_bool_exp!) {
      selected: bundle(where: $where, order_by: {created_at: desc}, limit: 5) {
        id version need_patches_update file_hash
        source { url source_metadata: source_metadatum { owner_name repo_stars } }
        patches { name patch_packages { package { name version } } }
        patches_aggregate { aggregate { count } }
      }
      bundle_aggregate { aggregate { count } }
    }`, { where: { source: { url: { _eq: "https://github.com/owner/repo" } },
                          is_prerelease: { _eq: false } } });
  assert.equal(result.body.errors, undefined, JSON.stringify(result.body));
  assert.equal(result.body.data.selected.length, 1);
  assert.equal(result.body.data.selected[0].patches[0].name, "Hide ads");
  assert.equal(result.body.data.selected[0].patches[0].patch_packages[1].package.version, "2.0");
  assert.equal(result.body.data.selected[0].patches_aggregate.aggregate.count, 1);
  assert.equal(result.body.data.bundle_aggregate.aggregate.count, 2);
});
test("GraphQL relationship filters, sorting and unavailable old fingerprint", async () => {
  const state = fixture();
  const result = await query(state, `{
    patch(where: {patch_packages: {package: {name: {_eq: "com.video"}}}}) {
      name bundle { version source { url } }
    }
  }`);
  assert.equal(result.body.errors, undefined, JSON.stringify(result.body));
  assert.equal(result.body.data.patch.length, 2);
  const obsolete = await query(state, "{ bundle { patcher_failure_fingerprint } }");
  assert.equal(obsolete.body.errors, undefined);
  assert.deepEqual(obsolete.body.data.bundle.map(b => b.patcher_failure_fingerprint), [null, null]);
});
test("GraphQL rejects unknown fields, mutations, excessive depth and repeated expensive aliases", async () => {
  const state = fixture();
  assert((await query(state, "{ bundle { missing } }")).body.errors);
  assert.equal((await query(state, "mutation { delete_bundle }")).response.status, 400);
  const costly = await query(state, "{ " + Array.from({ length: 20 }, (_, i) =>
    "b" + i + ":bundle(limit:100){patches(limit:100){name description}}").join(" ") + " }");
  assert.equal(costly.response.status, 400);
});
test("aggregate nodes inherit their parent's list limit for complexity checks", async () => {
  const state=fixture();
  const expensive=await query(state,`{
    source_aggregate(limit:100) {
      nodes { bundles(limit:100) { id catalog_id version } }
    }
  }`);
  assert.equal(expensive.response.status,400);
  assert.match(expensive.body.error,/complexity exceeds 20000/);
});
test("skipped GraphQL selections do not consume the complexity budget", async () => {
  const state=fixture();
  const operation=`query Optional($skip:Boolean!) {
    source_aggregate(limit:100) @skip(if:$skip) {
      nodes { bundles(limit:100) { id catalog_id version } }
    }
    bundle(limit:1){id}
  }`;
  const skipped=await query(state,operation,{skip:true});
  assert.equal(skipped.body.errors,undefined,JSON.stringify(skipped.body));
  assert.equal(skipped.body.data.bundle.length,1);
  assert.equal(skipped.body.data.source_aggregate,undefined);
  const included=await query(state,operation,{skip:false});
  assert.equal(included.response.status,400);
  assert.match(included.body.error,/complexity exceeds 20000/);
});
test("aggregate list limits stay validated after the full-set fast path", async () => {
  const state=fixture();
  const excessive=await query(state,`{
    bundle_aggregate(limit:101){aggregate{count}}
  }`);
  assert.match(JSON.stringify(excessive.body),/limit must be 1/);
  const empty=await query(state,`{
    bundle_aggregate(limit:0){aggregate{count}}
  }`);
  assert.equal(empty.body.errors,undefined,JSON.stringify(empty.body));
  assert.equal(empty.body.data.bundle_aggregate.aggregate.count,0);
});
test("aggregate limits cannot exceed 100 on large collections", async () => {
  const base=fixture();
  const records=Array.from({length:150},(_,i)=>({
    ...base.bundle,id:i.toString(16).padStart(24,"0"),legacy_id:i+1
  }));
  const state=fixture({"bundles.json":records});
  const result=await query(state,`{
    bundle_aggregate(limit:101){aggregate{count}}
  }`);
  assert.match(JSON.stringify(result.body),/limit must be 1/);
});
test("unselected and skipped selections do not trigger lazy catalog loads", async () => {
  const base=fixture();
  const records=[base.bundle,base.dev];
  const columns=["id","legacy_id","source_id","version"];
  const overrides={
    "graphql-bundles.json":{columns,rows:records.map(row=>columns.map(key=>row[key]))},
    "graphql-bundle-descriptions.json":Object.fromEntries(
      records.map(row=>[row.id,row.description]))
  };
  const skipped=fixture(overrides);
  const first=await query(skipped,`query {
    package(limit:1){name}
    bundle(where:{description:{_like:"%"}},limit:1) @skip(if:true){id}
  }`);
  assert.equal(first.body.errors,undefined,JSON.stringify(first.body));
  assert(!skipped.requests.some(url=>url.endsWith("/graphql-bundle-descriptions.json")));
  assert(!skipped.requests.some(url=>url.endsWith("/graphql-bundles.json")));

  const unselected=fixture(overrides);
  const document=`query Scalar { patch(where:{id:{_eq:1}},limit:1){id name} }
    query Other { patch(limit:1){...Links} }
    fragment Links on patch { patch_packages { package_fk } }`;
  const second=await call(unselected,"/graphql",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({query:document,operationName:"Scalar"})});
  assert.equal(second.body.errors,undefined,JSON.stringify(second.body));
  assert.equal(second.body.data.patch.length,1);
  assert(!unselected.requests.some(url=>url.endsWith("/packages.json") ||
    url.endsWith("/package-records.json")));
});
test("skipped variable-based package filters do not load package relationships", async () => {
  const state=fixture();
  const result=await query(state,`query Skipped($skip:Boolean!, $where:patch_bool_exp!) {
    hidden:patch(where:$where) @skip(if:$skip) { id }
    visible:patch(where:{id:{_eq:1}},limit:1) { id }
  }`,{skip:true,where:{patch_packages:{package:{name:{_eq:"com.video"}}}}});
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.visible.map(row=>row.id),[1]);
  assert(!state.requests.some(url=>url.endsWith("/packages.json") ||
    url.endsWith("/package-records.json")));
});
test("nested conjunctive bundle description filters retain indexed patch scoping", async () => {
  const base=fixture();
  const index=JSON.parse(base.bodies["patch-index.json"]);
  index.pages.push(...Array.from({length:25},(_,i)=>"patches/unrelated-"+i+".json"));
  const rows=[base.bundle,base.dev],columns=["id","legacy_id","source_id","version"];
  const state=fixture({
    "patch-index.json":index,
    "graphql-bundles.json":{columns,rows:rows.map(row=>columns.map(key=>row[key]))},
    "graphql-bundle-descriptions.json":Object.fromEntries(
      rows.map(row=>[row.id,row.description]))
  });
  const result=await query(state,`{
    patch(where:{bundle:{_and:[{description:{_eq:"Video patches"}}]}},limit:1){id}
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.patch.map(row=>row.id),[1]);
  assert(!state.requests.some(url=>url.includes("unrelated-")));
});
test("nested conjunctive bundle ID filters retain indexed patch scoping", async () => {
  const base=fixture();
  const index=JSON.parse(base.bodies["patch-index.json"]);
  index.pages.push(...Array.from({length:25},(_,i)=>"patches/unrelated-"+i+".json"));
  const state=fixture({"patch-index.json":index});
  const result=await query(state,`{
    patch(where:{bundle:{_and:[{id:{_eq:1}}]}},limit:1){id}
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.patch.map(row=>row.id),[1]);
  assert(!state.requests.some(url=>url.includes("unrelated-")));
});
test("common exact patch names use an indexed ID window instead of a broad shard scan", async () => {
  const base=fixture();
  const patch=JSON.parse(base.bodies["patches/00000.json"])[0];
  const count=30;
  const index=JSON.parse(base.bodies["patch-index.json"]);
  const pages=Array.from({length:count},(_,i)=>
    "patches/"+String(i).padStart(5,"0")+".json");
  index.pages=pages;
  index.bundles[base.bundle.id]=pages;
  index.patch_ids=Object.fromEntries(pages.map((page,i)=>[String(i+1),[page]]));
  const overrides={
    "patch-index.json":index,
    "patch-name-ids.json":{"Hide ads":Array.from({length:count},(_,i)=>i+1)}
  };
  pages.forEach((page,i)=>{overrides[page]=[{...patch,legacy_id:i+1,
    id:base.bundle.id+":"+i}];});
  for (const direction of ["asc","desc"]) {
    const state=fixture(overrides);
    const result=await query(state,`{
      patch(where:{name:{_eq:"Hide ads"}},order_by:{id:${direction}},
        offset:28,limit:1){id name}
    }`);
    assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
    assert.deepEqual(result.body.data.patch.map(row=>row.id),
      [direction==="asc"?29:2]);
    assert.equal(state.requests.filter(url=>/\/patches\/\d+\.json$/.test(url)).length,1);
  }
});
test("mandatory name and bundle ID IN predicates stay within indexed shard reads", async () => {
  const base=fixture();
  const patch=JSON.parse(base.bodies["patches/00000.json"])[0];
  const pages=Array.from({length:30},(_,i)=>
    "patches/"+String(i).padStart(5,"0")+".json");
  const index=JSON.parse(base.bodies["patch-index.json"]);
  index.pages=pages;
  index.bundles[base.bundle.id]=pages;
  index.patch_ids=Object.fromEntries(pages.map((page,i)=>[i+1,[page]]));
  const overrides={
    "patch-index.json":index,
    "patch-name-ids.json":{"Hide ads":Array.from({length:30},(_,i)=>i+1)}
  };
  pages.forEach((page,i)=>{overrides[page]=[
    {...patch,id:base.bundle.id+":"+i,legacy_id:i+1}
  ];});
  const state=fixture(overrides);
  const result=await query(state,`{
    patch(where:{name:{_in:["Hide ads"]}},offset:28,limit:1){id}
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.patch,[{id:29}]);
  assert.equal(state.requests.filter(url=>/\/patches\/\d+\.json$/.test(url)).length,1);

  const sparseIndex=JSON.parse(base.bodies["patch-index.json"]);
  sparseIndex.pages.push(...Array.from({length:30},(_,i)=>
    "patches/unrelated-"+i+".json"));
  const scoped=fixture({"patch-index.json":sparseIndex,
    "patch-bundle-index.json":sparseIndex.bundles});
  const filtered=await query(scoped,`{
    patch(where:{bundle_fk:{_in:[1]}},limit:1){id}
  }`);
  assert.equal(filtered.body.errors,undefined,JSON.stringify(filtered.body));
  assert.deepEqual(filtered.body.data.patch,[{id:1}]);
  assert(!scoped.requests.some(url=>url.includes("unrelated-")));
});
test("conjunctive IN constraints intersect before indexed shard selection", async () => {
  const base=fixture();
  const original=JSON.parse(base.bodies["patches/00000.json"])[0];
  const bundles=Array.from({length:30},(_,i)=>({
    ...base.bundle,id:i===0?base.bundle.id:String(i).padStart(24,"0"),legacy_id:i+1
  }));
  const pages=bundles.map((_,i)=>"patches/"+String(i).padStart(5,"0")+".json");
  const index={pages,bundles:{},packages:{"com.video":pages},patch_ids:{}};
  const overrides={"bundles.json":bundles};
  const nameIndex={};
  for (let i=0;i<bundles.length;i++) {
    const bundle=bundles[i],page=pages[i],id=i+1,name="Patch "+i;
    index.bundles[bundle.id]=[page];
    index.patch_ids[id]=[page];
    nameIndex[name]=[id];
    overrides[page]=[{...original,id:bundle.id+":0",legacy_id:id,
      bundle_id:bundle.id,name}];
  }
  overrides["patch-index.json"]=index;
  overrides["patch-bundle-index.json"]=index.bundles;
  overrides["patch-name-ids.json"]=nameIndex;
  const all=bundles.map(bundle=>bundle.legacy_id).join(",");
  for (const broadFirst of [true,false]) {
    const filters=broadFirst?
      `[{bundle_fk:{_in:[${all}]}},{bundle_fk:{_in:[1]}}]`:
      `[{bundle_fk:{_in:[1]}},{bundle_fk:{_in:[${all}]}}]`;
    const state=fixture(overrides);
    const result=await query(state,`{
      patch(where:{_and:${filters}},limit:1){id bundle_fk}
    }`);
    assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
    assert.deepEqual(result.body.data.patch,[{id:1,bundle_fk:1}]);
    assert.equal(state.requests.filter(url=>/\/patches\/\d+\.json$/.test(url)).length,1);
  }
  const nested=fixture(overrides);
  const relation=await query(nested,`{
    patch(where:{bundle:{_and:[{id:{_in:[${all}]}},{id:{_in:[1]}}]}},
      limit:1){id}
  }`);
  assert.equal(relation.body.errors,undefined,JSON.stringify(relation.body));
  assert.deepEqual(relation.body.data.patch,[{id:1}]);
  assert.equal(nested.requests.filter(url=>/\/patches\/\d+\.json$/.test(url)).length,1);

  const names=bundles.map((_,i)=>JSON.stringify("Patch "+i)).join(",");
  const named=fixture(overrides);
  const byName=await query(named,`{
    patch(where:{_and:[{name:{_in:[${names}]}},{name:{_in:["Patch 0"]}}]},
      limit:1){id name}
  }`);
  assert.equal(byName.body.errors,undefined,JSON.stringify(byName.body));
  assert.deepEqual(byName.body.data.patch,[{id:1,name:"Patch 0"}]);
  assert.equal(named.requests.filter(url=>/\/patches\/\d+\.json$/.test(url)).length,1);
});
test("bundle foreign-key and relationship IN filters share their indexed scope", async () => {
  const base=fixture();
  const original=JSON.parse(base.bodies["patches/00000.json"])[0];
  const bundles=[base.bundle,base.dev];
  const pages=Array.from({length:30},(_,i)=>
    "patches/"+String(i).padStart(5,"0")+".json");
  const index={pages,bundles:{
    [base.bundle.id]:pages.slice(0,15),[base.dev.id]:pages.slice(15)
  },patch_ids:{},packages:{"com.video":pages}};
  const overrides={"patch-index.json":index,
    "patch-bundle-index.json":index.bundles};
  pages.forEach((page,i)=>{
    const bundle=bundles[i<15?0:1];
    overrides[page]=[{...original,id:bundle.id+":"+i,
      bundle_id:bundle.id,legacy_id:i+1}];
    index.patch_ids[i+1]=[page];
  });
  const state=fixture(overrides);
  const result=await query(state,`{
    patch(where:{bundle_fk:{_in:[1,2]},bundle:{id:{_in:[1]}}},
      limit:1){id bundle_fk}
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.patch,[{id:1,bundle_fk:1}]);
  assert(!state.requests.some(url=>url.endsWith("/patches/00015.json")));
});
test("exact patch name with another predicate stays indexed and filters before offset", async () => {
  const base=fixture();
  const patch=JSON.parse(base.bodies["patches/00000.json"])[0];
  const count=30;
  const index=JSON.parse(base.bodies["patch-index.json"]);
  const pages=Array.from({length:count},(_,i)=>
    "patches/"+String(i).padStart(5,"0")+".json");
  index.pages=pages;
  index.bundles[base.bundle.id]=pages;
  index.patch_ids=Object.fromEntries(pages.map((page,i)=>[String(i+1),[page]]));
  const overrides={
    "patch-index.json":index,
    "patch-name-ids.json":{"Hide ads":Array.from({length:count},(_,i)=>i+1)}
  };
  pages.forEach((page,i)=>{overrides[page]=[{...patch,id:base.bundle.id+":"+i,
    legacy_id:i+1,use:i%2===1}];});
  for (const direction of ["asc","desc"]) {
    const state=fixture(overrides);
    const result=await query(state,`{
      patch(where:{name:{_eq:"Hide ads"},use:{_eq:true}},
        order_by:{id:${direction}},offset:1,limit:1){id use}
    }`);
    assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
    assert.deepEqual(result.body.data.patch,
      [{id:direction==="asc"?4:28,use:true}]);
    assert(!state.requests.some(url=>url.includes("patches/00025.json")) ||
      direction==="desc");
  }
  const nested=fixture(overrides);
  const queryNested=await query(nested,`{
    patch(where:{_and:[{name:{_eq:"Hide ads"}},{use:{_eq:true}}]},limit:1){id}
  }`);
  assert.deepEqual(queryNested.body.data.patch,[{id:2}]);
});
test("exact empty patch names do not consume pagination with NULL names", async () => {
  const base=fixture();
  const patches=JSON.parse(base.bodies["patches/00000.json"]);
  patches[0].name=null;
  patches[1].name="";
  const state=fixture({
    "patches/00000.json":patches,
    "patch-name-ids.json":{"":[2]}
  });
  const result=await query(state,`{
    patch(where:{name:{_eq:""}},order_by:{id:asc},limit:1){id name}
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.patch,[{id:2,name:""}]);
});
test("contradictory required patch name predicates short-circuit indexed paging", async () => {
  const base=fixture();
  const patch=JSON.parse(base.bodies["patches/00000.json"])[0];
  const pages=Array.from({length:50},(_,i)=>
    "patches/"+String(i).padStart(5,"0")+".json");
  const index=JSON.parse(base.bodies["patch-index.json"]);
  index.pages=pages;
  index.bundles[base.bundle.id]=pages;
  index.patch_ids=Object.fromEntries(pages.map((page,i)=>[i+1,[page]]));
  const overrides={
    "patch-index.json":index,
    "patch-name-ids.json":{"Hide ads":Array.from({length:50},(_,i)=>i+1)}
  };
  pages.forEach((page,i)=>{overrides[page]=[
    {...patch,id:base.bundle.id+":"+i,legacy_id:i+1}
  ];});
  const state=fixture(overrides);
  const result=await query(state,`{
    patch(where:{_and:[{name:{_eq:"Hide ads"}},{name:{_in:["Nonexistent"]}}]},
      limit:1){id}
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.patch,[]);
  assert(!state.requests.some(url=>/\/patches\/\d+\.json$/.test(url)));
});
test("exact patch name aggregate counts do not require every patch shard", async () => {
  const base=fixture();
  const patch=JSON.parse(base.bodies["patches/00000.json"])[0];
  const pages=Array.from({length:30},(_,i)=>
    "patches/"+String(i).padStart(5,"0")+".json");
  const index=JSON.parse(base.bodies["patch-index.json"]);
  index.pages=pages;
  index.bundles[base.bundle.id]=pages;
  index.patch_ids=Object.fromEntries(pages.map((page,i)=>[i+1,[page]]));
  const overrides={
    "patch-index.json":index,
    "patch-name-ids.json":{"Hide ads":Array.from({length:30},(_,i)=>i+1)}
  };
  pages.forEach((page,i)=>{overrides[page]=[
    {...patch,id:base.bundle.id+":"+i,legacy_id:i+1}
  ];});
  const state=fixture(overrides);
  const result=await query(state,`{
    patch_aggregate(where:{name:{_eq:"Hide ads"}}) {
      aggregate {
        count
        named:count(columns:[name])
        names:count(columns:[name],distinct:true)
        ids:count(columns:[id],distinct:true)
        pairs:count(columns:[id,name],distinct:true)
        repeatedNames:count(columns:[name,name],distinct:true)
        min {name id}
        max {name id}
        sum {id}
        avg {id}
      }
    }
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.patch_aggregate.aggregate,
    {count:30,named:30,names:1,ids:30,pairs:30,repeatedNames:1,
      min:{name:"Hide ads",id:1},max:{name:"Hide ads",id:30},
      sum:{id:465},avg:{id:15.5}});
  assert(!state.requests.some(url=>url.includes("/patches/")));
  const absent=fixture(overrides);
  const empty=await query(absent,`{
    patch_aggregate(where:{name:{_eq:"Nonexistent"}}) {
      aggregate {count extra:count(columns:[description],distinct:true)
        min{name id} max{name id} sum{id} avg{id}}
    }
  }`);
  assert.equal(empty.body.errors,undefined,JSON.stringify(empty.body));
  assert.deepEqual(empty.body.data.patch_aggregate.aggregate,{
    count:0,extra:0,min:{name:null,id:null},max:{name:null,id:null},
    sum:{id:null},avg:{id:null}
  });
  assert(!absent.requests.some(url=>url.includes("/patches/")));
  const withNodes=fixture(overrides);
  const nodesResult=await query(withNodes,`{
    patch_aggregate(where:{name:{_eq:"Hide ads"}}) {
      aggregate { count }
      nodes { id }
    }
  }`);
  assert.equal(nodesResult.body.errors,undefined,JSON.stringify(nodesResult.body));
  assert.equal(nodesResult.body.data.patch_aggregate.aggregate.count,30);
  assert.equal(nodesResult.body.data.patch_aggregate.nodes.length,25);
  assert.equal(nodesResult.body.data.patch_aggregate.nodes.at(-1).id,25);
  const small=fixture({"patch-name-ids.json":{"Hide ads":[1,2]}});
  const other=await query(small,`{
    patch_aggregate(where:{name:{_eq:"Hide ads"}}) {
      aggregate { count min { name } }
    }
  }`);
  assert.equal(other.body.errors,undefined,JSON.stringify(other.body));
  assert.deepEqual(other.body.data.patch_aggregate.aggregate,
    {count:2,min:{name:"Hide ads"}});
});
test("missing patch names cannot match inherited object properties", async () => {
  const state=fixture({"patch-name-ids.json":{"Hide ads":[1,2]}});
  const result=await query(state,`{
    patch_aggregate(where:{name:{_eq:"constructor"}}){aggregate{count}}
    patch(where:{name:{_in:["toString","__proto__"]}},limit:5){id name}
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.patch_aggregate.aggregate,{count:0});
  assert.deepEqual(result.body.data.patch,[]);
});
test("unknown bundle IDs cannot inherit scoped index entries", async () => {
  const base=fixture();
  const state=fixture({"patch-bundle-index.json":{
    [base.bundle.id]:["patches/00000.json"]
  }});
  const result=await query(state,`{
    constructed:patch(bundle_id:"constructor"){id}
    inherited:patch(bundle_id:"__proto__"){id}
    stringified:patch(bundle_id:"toString"){id}
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data,{constructed:[],inherited:[],stringified:[]});
});
test("GraphQL compact bundle indexes avoid the full catalog and preserve descriptions", async () => {
  const base=fixture();
  const columns=["id","legacy_id","source_id","version","description","patch_count"];
  const records=[base.bundle,base.dev];
  const state=fixture({
    "graphql-bundles.json":{columns:columns.filter(c=>c!=="description"),
      rows:records.map(row=>columns.filter(c=>c!=="description").map(c=>row[c]??null))},
    "graphql-bundle-descriptions.json":Object.fromEntries(records.map(row=>[row.id,row.description])),
    "graphql-descriptions/0.json":Object.fromEntries(records.map(row=>[row.id,row.description]))
  });
  const plain=await query(state,`{ package(limit:1) { name } }`);
  assert.equal(plain.body.errors,undefined,JSON.stringify(plain.body));
  assert(!state.requests.some(url=>url.endsWith("/bundles.json")));
  const result=await query(state,`{
    bundle(where:{description:{_like:"Video%"}},limit:2) { id description }
    bundle_aggregate { aggregate { min { description } max { description } } }
    patch(where:{name:{_eq:"Hide ads"}},limit:2) { id bundle { description } }
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.equal(result.body.data.bundle.length,2);
  assert.equal(result.body.data.bundle[0].description,"Video patches");
  assert.equal(result.body.data.bundle_aggregate.aggregate.min.description,"Video patches");
  assert.equal(result.body.data.patch.length,2);
  assert(!state.requests.some(url=>url.endsWith("/bundles.json")));
  assert(!state.requests.some(url=>url.includes("/bundles/"+base.bundle.id+".json")));
  const projectedState=fixture({
    "graphql-bundles.json":{columns:columns.filter(c=>c!=="description"),
      rows:records.map(row=>columns.filter(c=>c!=="description").map(c=>row[c]??null))},
    "graphql-bundle-descriptions.json":Object.fromEntries(records.map(row=>[row.id,row.description])),
    "graphql-descriptions/0.json":Object.fromEntries(records.map(row=>[row.id,row.description]))
  });
  const projected=await query(projectedState,`{
    bundle(limit:2){description}
    patch(where:{id:{_eq:1}}){bundle{description}}
  }`);
  assert.equal(projected.body.errors,undefined,JSON.stringify(projected.body));
  assert.equal(projected.body.data.patch[0].bundle.description,"Video patches");
  assert(projectedState.requests.some(url=>url.endsWith("/graphql-descriptions/0.json")));
  assert(!projectedState.requests.some(url=>url.endsWith("/graphql-bundle-descriptions.json")));
});
test("compact bundle descriptions remain correct for count, distinct and stream cursors", async () => {
  const base=fixture();
  const records=[{...base.bundle,description:"Alpha"},{...base.dev,description:"Zulu"}];
  const columns=["id","legacy_id","source_id","version"];
  const state=fixture({
    "graphql-bundles.json":{columns,rows:records.map(row=>columns.map(name=>row[name]))},
    "graphql-bundle-descriptions.json":Object.fromEntries(
      records.map(row=>[row.id,row.description]))
  });
  const result=await query(state,`{
    bundle_aggregate { aggregate {
      nonnull:count(columns:[description])
      distinct_count:count(columns:[description],distinct:true)
    } }
    bundle(distinct_on:[description],order_by:{description:asc}){description}
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.bundle_aggregate.aggregate,
    {nonnull:2,distinct_count:2});
  assert.deepEqual(result.body.data.bundle.map(row=>row.description),["Alpha","Zulu"]);

  const catalog=await new Catalog(state.env.DATA_BASE_URL,state.env.FETCH).initialize();
  const streamed=await runGraphQL(catalog,{query:`subscription {
    bundle_stream(batch_size:1,cursor:[{initial_value:{description:"Alpha"}}]) {
      description
    }
  }`},{subscription:true});
  assert.equal(streamed.errors,undefined,JSON.stringify(streamed));
  assert.deepEqual(streamed.data.bundle_stream.map(row=>row.description),["Zulu"]);
});
test("compact descriptions participate in relationship aggregate ordering", async () => {
  const base=fixture();
  const source=JSON.parse(base.bodies["sources.json"])[0];
  const bundleRows=[{...base.bundle,description:"Zulu"},
    {...base.dev,source_id:"s2",description:"Alpha"}];
  const columns=["id","legacy_id","source_id","version"];
  const state=fixture({
    "sources.json":[source,{...source,id:"s2",legacy_id:2,
      url:"https://github.com/second/repo"}],
    "graphql-bundles.json":{columns,rows:bundleRows.map(row=>
      columns.map(name=>row[name]))},
    "graphql-bundle-descriptions.json":Object.fromEntries(bundleRows.map(row=>
      [row.id,row.description]))
  });
  const result=await query(state,`{
    source(order_by:{bundles_aggregate:{min:{description:asc}}}){url}
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.source.map(row=>row.url),[
    "https://github.com/second/repo","https://github.com/owner/repo"
  ]);
});
test("description-filtered bundle relationships use the compact patch-page index", async () => {
  const base=fixture();
  const index=JSON.parse(base.bodies["patch-index.json"]);
  const rows=[base.bundle,base.dev];
  const columns=["id","legacy_id","source_id","version"];
  const state=fixture({
    "graphql-bundles.json":{columns,rows:rows.map(row=>columns.map(name=>row[name]))},
    "graphql-bundle-descriptions.json":Object.fromEntries(
      rows.map(row=>[row.id,row.description])),
    "patch-bundle-index.json":index.bundles
  });
  const result=await query(state,`{
    bundle(where:{description:{_eq:"Video patches"}},limit:1) {
      id patches(limit:1,where:{name:{_eq:"Hide ads"}}){id}
    }
    filtered:patch(where:{bundle:{description:{_eq:"Video patches"}}},limit:1){id}
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.equal(result.body.data.bundle.length,1);
  assert.equal(result.body.data.bundle[0].patches.length,1);
  assert.equal(result.body.data.filtered.length,1);
  assert(!state.requests.some(url=>url.endsWith("/patch-index.json")));
  assert(state.requests.some(url=>url.endsWith("/patch-bundle-index.json")));
});
test("ETag conditional requests and production CORS", async () => {
  const state = fixture();
  const first = await call(state, "/health", { headers: { Origin: "https://website.test" } });
  assert.equal(first.response.headers.get("Access-Control-Allow-Origin"), "https://website.test");
  const second = await call(state, "/health", { headers: { "If-None-Match": first.response.headers.get("ETag") } });
  assert.equal(second.response.status, 304);
  assert.equal((await call(state, "/health", { headers: { Origin: "https://untrusted.test" } }))
    .response.headers.get("Access-Control-Allow-Origin"), null);
});
test("file integrity and missing generation fail closed", async () => {
  const state = fixture();
  state.bodies["bundles.json"] = "[]";
  assert.equal((await call(state, "/bundles")).response.status, 503);
  const missing = fixture();
  delete missing.bodies["sources.json"];
  assert.equal((await call(missing, "/sources")).response.status, 503);
});
test("request, response, method and shard budgets are bounded", async () => {
  const state = fixture();
  assert.equal((await call(state, "/sources", { method: "DELETE" })).response.status, 405);
  assert.equal((await call(state, "/graphql", { method: "POST", body: "x".repeat(40000),
    headers: { "Content-Type": "application/json" } })).response.status, 413);
  const catalog = await new Catalog(state.env.DATA_BASE_URL, state.env.FETCH).initialize();
  catalog.requests = 36;
  await assert.rejects(() => catalog.file("sources.json"), /budget/);
});
test("normalization supports GitLab subgroups and immutable request memoization", async () => {
  assert(matches({name:"%literal"}, {name:{_ilike:"%"}}));
  assert(matches({name:"Hide Ads"}, {name:{_ilike:"%ADS%"}}));
  assert(!matches({name:"abc"}, {name:{_like:"%a%a%a%a%a%"}}));
  assert.equal(normalizeSource("https://gitlab.com/api/v4/projects/group%2Fteam%2Frepo"),
    "https://gitlab.com/group/team/repo");
  assert.equal(normalizeSource("github.com/Owner/Repo.git/"), "https://github.com/Owner/Repo");
  const state = fixture();
  const catalog = await new Catalog(state.env.DATA_BASE_URL, state.env.FETCH).initialize();
  await Promise.all([catalog.sources(), catalog.sources()]);
  assert.equal(state.requests.filter(r => r.endsWith("/sources.json")).length, 1);
});
test("generated catalog serves REST through a mocked Pages origin", async t => {
  const root = await generatedCatalog(t);
  const env = { DATA_BASE_URL: "https://local.test/database/", FETCH: async input => {
    const path = new URL(input).pathname.replace("/database/", "");
    try { return new Response((await readFile(new URL(path, root))).toString("utf8").replace(/\r\n/g, "\n")); }
    catch { return new Response("", { status: 404 }); }
  } };
  const result = await call({ env }, "/sources?limit=3");
  assert.equal(result.response.status, 200);
  assert.equal(result.body.data.length, 3);
  const manifest = JSON.parse(await readFile(new URL("manifest.json", root), "utf8"));
  assert.equal(result.body.total, manifest.counts.sources);
});

test("nested bundle/source relationship filters use indexed packages", async () => {
  const state = fixture();
  const result = await query(state, `{
    bundle(where:{patches:{patch_packages:{package:{name:{_eq:"com.video"}}}}}) { id }
    source(where:{bundles:{version:{_eq:"v1"}}}) { url bundles(where:{version:{_eq:"v1"}}) { version } }
    bundle_aggregate(where:{patches:{name:{_ilike:"%ADS%"}}}) { aggregate { count } }
  }`);
  assert.equal(result.body.errors, undefined, JSON.stringify(result.body));
  assert.equal(result.body.data.bundle.length, 2);
  assert.equal(result.body.data.source[0].bundles[0].version, "v1");
  assert.equal(result.body.data.bundle_aggregate.aggregate.count, 2);
});
test("v1 prerelease and v2 required channel match repository lookup conventions", async () => {
  const state = fixture();
  assert.equal((await call(state, "/api/v1/bundle/owner/repo/latest")).body.version, "v1");
  assert.equal((await call(state, "/api/v1/bundle/owner/repo/latest?prerelease=true")).body.version, "v2-dev");
  assert.equal((await call(state, "/api/v2/bundle/owner/repo/latest")).response.status, 400);
  assert.equal((await call(state, "/api/v2/bundle/owner/repo/latest?channel=prerelease")).body.version, "v2-dev");
});
test("cold-cache calls fit the shared free-plan subrequest budget", async () => {
  const pages = Array.from({length:24}, (_,i) => "patches/" + String(i).padStart(5,"0") + ".json");
  const overrides = Object.fromEntries(pages.map(p => [p, []]));
  overrides["patch-index.json"] = {pages, packages:{}, bundles:{}};
  const state = fixture(overrides);
  let cacheCalls = 0;
  globalThis.caches = {default:{
    match: async () => { cacheCalls++; return undefined; },
    put: async () => { cacheCalls++; }
  }};
  try {
    const pending = [];
    const response = await worker.fetch(new Request("https://api.test/graphql", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({query:"{ patch(limit:100) { name } }"})
    }), state.env, {waitUntil: p => pending.push(p)});
    const result = await response.json();
    await Promise.all(pending);
    assert.equal(result.errors, undefined, JSON.stringify(result));
    assert(state.requests.length <= 29);
    assert.equal(cacheCalls, 10);
    assert(state.requests.length + cacheCalls < 50);
  } finally { delete globalThis.caches; }
});

test("invalid GraphQL payloads and malformed encoded URLs return client errors", async () => {
  const state = fixture();
  for (const body of ["null", "[]", '{"query":"{source{id}}","variables":[]}']) {
    assert.equal((await call(state, "/graphql", { method:"POST",
      headers:{"Content-Type":"application/json"}, body })).response.status, 400);
  }
  const source = encodeURIComponent("https://github.com/owner/%ZZ");
  assert.equal((await call(state, "/api/v3/bundle?source_url=" + source +
    "&version=latest&channel=any")).response.status, 400);
});

test("REST patch text search combines with source, ecosystem and channel filters", async () => {
  const state = fixture();
  for (const filter of ["source=demo", "source_url=https://github.com/owner/repo",
                        "ecosystem=revanced", "channel=stable"]) {
    const result = await call(state, "/patches?q=ads&package=com.video&app_version=1.0&" + filter);
    assert.equal(result.response.status, 200);
    assert.deepEqual(result.body.data.map(p => p.bundle_id), [state.bundle.id], filter);
  }
  const dev = await call(state, "/patches?q=ads&channel=dev&include_unverified=true");
  assert.deepEqual(dev.body.data.map(p => p.bundle_id), [state.dev.id]);
});
test("GraphQL logical status filters include explicitly requested stale patches", async () => {
  const state = fixture();
  const result = await query(state, `{
    patch(where:{_and:[{metadata_status:{_eq:"stale"}}]}) { metadata_status }
    patch_aggregate(where:{_or:[{metadata_status:{_eq:"stale"}}]}) { aggregate { count } }
    bundle(where:{catalog_id:{_eq:"${state.dev.id}"}}) {
      patches(where:{_and:[{metadata_status:{_eq:"stale"}}]}) { metadata_status }
    }
  }`);
  assert.equal(result.body.errors, undefined, JSON.stringify(result.body));
  assert.deepEqual(result.body.data.patch, [{metadata_status:"stale"}]);
  assert.equal(result.body.data.patch_aggregate.aggregate.count, 1);
  assert.deepEqual(result.body.data.bundle[0].patches, [{metadata_status:"stale"}]);
});


test("latest imports follow category flags and exclude retired releases", async () => {
  const base = fixture();
  const current = { ...base.bundle, version: "v0", created_at: "2025-01-01T00:00:00" };
  const history = { ...base.bundle, id: "e".repeat(24), version: "v9",
    created_at: "2026-06-01T00:00:00", channels: [], is_latest: false };
  const dev = { ...base.dev, created_at: "2026-07-01T00:00:00" };
  const state = fixture({ "bundles.json": [current, dev, history],
    ["bundles/" + current.id + ".json"]: current,
    ["bundles/" + dev.id + ".json"]: dev,
    ["bundles/" + history.id + ".json"]: history });
  const path = "/api/v3/bundle?source_url=https://github.com/owner/repo&version=";
  for (const [channel, version] of [["any", dev.version], ["latest", dev.version], ["stable", current.version]]) {
    const result = await call(state, path + "latest&channel=" + channel);
    assert.equal(result.response.status, 200);
    assert.equal(result.body.version, version);
  }
  assert.equal((await call(state, path + "latest&channel=prerelease")).body.version, dev.version);
  assert.equal((await call(state, path + history.version)).body.version, history.version);
  const retired = { ...current, channels: [], is_latest: false };
  const missing = fixture({ "bundles.json": [retired, history] });
  assert.equal((await call(missing, path + "latest&channel=any")).response.status, 404);
});

test("legacy numeric and exact-version imports return the upstream five-field DTO", async () => {
  const state = fixture();
  for (const path of ["/api/v1/bundle/1","/api/v1/bundle/owner/repo/v1"]) {
    const result = await call(state, path);
    assert.equal(result.response.status, 200);
    assert.equal(result.body.version, "v1");
    assert.deepEqual(Object.keys(result.body).sort(), [
      "created_at","description","download_url","icon_url","owner_avatar_url","signature_download_url","version"]);
  }
  assert.equal((await call(state,"/api/v1/bundle/99")).response.status,404);
  assert.equal((await call(state,"/api/v1/bundle/owner/repo/missing")).response.status,404);
});
test("all public GraphQL tables, primary keys, aggregate nodes and statistics work", async () => {
  const state = fixture({"refresh-jobs.json":[{id:1,job_id:"run",status:"COMPLETED",
    job_type:"BUNDLES",started_at:"2026-01-01T00:00:00Z",completed_at:"2026-01-01T00:01:00Z"}]});
  const result = await query(state, `{
    bundle_by_pk(id:1) { id source_fk catalog_id patches { id bundle_fk } }
    source_by_pk(id:1) { id source_metadatum { id source_fk source { id } } }
    source_metadata_by_pk(id:1) { repo_stars }
    patch_by_pk(id:1) { id bundle { id } }
    package_by_pk(id:1) { id name patch_packages { patch_fk patch { name } } }
    patch_package_by_pk(package_fk:1,patch_fk:1) { package { name } patch { id } }
    patch_package_aggregate { aggregate { count } nodes { package_fk patch_fk } }
    package_aggregate { aggregate { count(columns:[name],distinct:true) } nodes { version } }
    bundle_aggregate { aggregate { count min { id } max { id } sum { id } avg { id }
      variance { id } stddev_pop { id } } nodes { id } }
    refresh_jobs_by_pk(id:1) { status }
    refresh_jobs { id job_type }
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  const data = result.body.data;
  assert.equal(data.bundle_by_pk.source_fk,1);
  assert.equal(data.bundle_by_pk.patches[0].bundle_fk,1);
  assert.equal(data.source_metadata_by_pk.repo_stars,12);
  assert.equal(data.patch_package_by_pk.patch.id,1);
  assert.equal(data.package_aggregate.aggregate.count,1);
  assert.equal(data.patch_package_aggregate.aggregate.count,4);
  assert.equal(data.bundle_aggregate.aggregate.sum.id,3);
  assert.equal(data.bundle_aggregate.aggregate.avg.id,1.5);
  assert.equal(data.bundle_aggregate.aggregate.variance.id,0.5);
  assert.equal(data.bundle_aggregate.nodes.length,2);
  assert.equal(data.refresh_jobs_by_pk.status,"COMPLETED");
});
test("version fragments, SQL escapes, regex, null ordering and variable defaults", async () => {
  assert(packageFilterMatches([{name:"com.video",versions:["19.16.39"]}],"19.16"));
  assert(packageFilterMatches([{name:"com.video",versions:["19.16.39"]}],"com.video@19.16.39"));
  assert(!packageFilterMatches([{name:"com.video",versions:["19.16.39"]}],"com.video@19.16"));
  assert(matches({name:"literal_percent%"},{name:{_like:"literal\\_percent\\%"}}));
  const result = await query(fixture(), `
    query Default($limit:Int=1) {
      bundle(order_by:{created_at:asc_nulls_first},limit:$limit) { id }
      patch(where:{name:{_iregex:"^hide [a-z]+$"}}) { name }
      patch_aggregate(where:{name:{_nilike:"%nothing%"}}) { aggregate { count } }
    }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.equal(result.body.data.bundle.length,1);
  assert.equal(result.body.data.patch.length,2);
  assert.equal(result.body.data.patch_aggregate.aggregate.count,2);
});
test("OpenAPI and standard GraphiQL introspection remain usable", async () => {
  const state=fixture();
  const spec=await call(state,"/api.json");
  assert.equal(spec.body.openapi,"3.0.3");
  assert(spec.body.paths["/api/v1/bundle/{id}"]);
  assert.equal(spec.body.paths["/health"].get.responses["409"],undefined);
  assert(spec.body.paths["/history/releases"].get.responses["409"]);
  const {getIntrospectionQuery}=await import("graphql");
  const introspection=await query(state,getIntrospectionQuery());
  assert.equal(introspection.response.status,200,JSON.stringify(introspection.body));
  assert.equal(introspection.body.errors,undefined,JSON.stringify(introspection.body));
  assert(introspection.body.data.__schema.types.some(type=>type.name==="patch_package"));
  const swagger=await worker.fetch(new Request("https://api.test/swagger"),state.env);
  assert.equal(swagger.status,302);
});

test("relationship aggregates filter, order and paginate without truncating totals", async () => {
  const state=fixture();
  const result=await query(state,`{
    source(where:{bundles_aggregate:{count:{predicate:{_gt:1}}}}) { id }
    bundle(where:{patches_aggregate:{count:{predicate:{_eq:1}}}},
      order_by:{patches_aggregate:{count:desc}}) { id }
    package(where:{patch_packages_aggregate:{count:{predicate:{_gte:2}}}}) { name }
    bundle_aggregate(limit:1) { aggregate { count } nodes { id } }
    remaining:bundle_aggregate(offset:1) { aggregate { count sum { id } } }
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.equal(result.body.data.source.length,1);
  assert.equal(result.body.data.bundle.length,2);
  assert.equal(result.body.data.package.length,2);
  assert.equal(result.body.data.bundle_aggregate.aggregate.count,1);
});

test("generated catalog serves global patch counts and pages through indexes", async t => {
  const root=await generatedCatalog(t);
  const requests=[];
  const state={env:{DATA_BASE_URL:"https://local.test/database/",FETCH:async input=>{
    const target=new URL(input).pathname.replace("/database/","");
    requests.push(target);
    try{return new Response((await readFile(new URL(target,root))).toString("utf8").replace(/\r\n/g,"\n"));}
    catch{return new Response("",{status:404});}
  }}};
  const result=await query(state,`{
    patch(limit:10,offset:10) { id name bundle_fk }
    patch_aggregate { aggregate { count count_with_use:count(columns:[use])
      count_complete:count(columns:[id,name]) sum { id } } nodes { id } }
  }`);
  const manifest = JSON.parse(await readFile(new URL("manifest.json", root), "utf8"));
  const statistics = JSON.parse(await readFile(
    new URL(manifest.snapshot + "/patch-statistics.json", root), "utf8"));
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.equal(result.body.data.patch.length,10);
  assert.deepEqual(result.body.data.patch.map(patch=>patch.id),
    Array.from({length:10},(_,index)=>index+11));
  assert.equal(statistics.count,40);
  assert.equal(result.body.data.patch_aggregate.aggregate.count_with_use,30);
  assert.equal(result.body.data.patch_aggregate.aggregate.count_complete,32);
  assert.equal(result.body.data.patch_aggregate.aggregate.sum.id,820);
  // The manifest includes retired/disabled source history; API aggregates
  // intentionally count only patches from enabled sources.
  assert(statistics.count <= manifest.counts.patches);
  assert.equal(result.body.data.patch_aggregate.aggregate.count,statistics.count);
  const required = ["id","name"].reduce((mask,field) =>
    mask | (1 << statistics.columns.indexOf(field)),0);
  const complete = Object.entries(statistics.null_masks).reduce((total,[mask,count]) =>
    total + ((Number(mask) & required) === 0 ? count : 0),0);
  assert.equal(result.body.data.patch_aggregate.aggregate.count_complete,complete);
  assert.equal(result.body.data.patch_aggregate.nodes.length,25);
  assert(requests.filter(path=>path.includes("/patches/")).length<=2,JSON.stringify(requests));
  assert(requests.length<=8,JSON.stringify(requests));
});

test("timestamp comparisons match equivalent UTC offsets",()=> {
  assert(matches({created_at:"2026-01-01T02:00:00+02:00"},
    {created_at:{_eq:"2026-01-01T00:00:00Z"}}));
  assert(matches({created_at:"2026-01-01T02:00:00+02:00"},
    {created_at:{_lt:"2026-01-01T00:01:00Z"}}));
});

test("GraphQL relation predicates do not narrow returned patch rows", async () => {
  const base = fixture();
  const original = JSON.parse(base.bodies["patches/00000.json"]);
  const other = {...original[0], id:base.bundle.id+":1", legacy_id:3,
    name:"Other app", packages:[{name:"com.other",versions:[]}]};
  const state = fixture({
    "patches/00000.json":[...original,other],
    "packages.json":[{name:"com.video",versions:["1.0","2.0"]},{name:"com.other",versions:[]}],
    "patch-index.json":{pages:["patches/00000.json"],
      bundles:{[base.bundle.id]:["patches/00000.json"],[base.dev.id]:["patches/00000.json"]},
      packages:{"com.video":["patches/00000.json"],"com.other":["patches/00000.json"]}}
  });
  const result = await query(state, `{
    patch(where:{bundle:{patches:{patch_packages:{package:{name:{_eq:"com.video"}}}}}}) {
      name
    }
  }`);
  assert.equal(result.body.errors, undefined, JSON.stringify(result.body));
  assert.deepEqual(result.body.data.patch.map(p=>p.name),["Hide ads","Hide ads","Other app"]);
});
test("GraphQL distinct patch names are selected before pagination", async () => {
  const base = fixture();
  const original = JSON.parse(base.bodies["patches/00000.json"]);
  const other = {...original[0], id:base.bundle.id+":1", legacy_id:3, name:"Other"};
  const state = fixture({
    "patches/00000.json":[...original,other],
    "patch-index.json":{pages:["patches/00000.json"],
      bundles:{[base.bundle.id]:["patches/00000.json"],[base.dev.id]:["patches/00000.json"]},
      packages:{"com.video":["patches/00000.json"]},
      patch_ids:{"1":["patches/00000.json"],"2":["patches/00000.json"],"3":["patches/00000.json"]}}
  });
  const result = await query(state, "{patch(distinct_on:[name],limit:2){name}}");
  assert.equal(result.body.errors, undefined, JSON.stringify(result.body));
  assert.deepEqual(result.body.data.patch.map(p=>p.name),["Hide ads","Other"]);
});

test("browser manifest revalidates and expiry remains distinguishable from corruption", async () => {
  const state = fixture();
  let manifestOptions;
  const fetcher = async (input, options) => {
    if (new URL(input).pathname.endsWith("/manifest.json")) manifestOptions = options;
    return state.env.FETCH(input);
  };
  const catalog = await new Catalog(state.env.DATA_BASE_URL, fetcher, false).initialize();
  assert.equal(manifestOptions.cache, "no-cache");
  delete state.bodies["sources.json"];
  await assert.rejects(catalog.sources(), error =>
    error.status === 503 && error.generationUnavailable === true);
  const corrupt = fixture();
  corrupt.bodies["sources.json"] = "[]";
  const invalid = await new Catalog(corrupt.env.DATA_BASE_URL, corrupt.env.FETCH, false).initialize();
  await assert.rejects(invalid.sources(), error =>
    error.status === 503 && !error.generationUnavailable);
});

test("source relationship filters stay within the selected source shard budget", async () => {
  const base = fixture();
  const index = JSON.parse(base.bodies["patch-index.json"]);
  index.pages.push(...Array.from({length:25}, (_, i) => "patches/unrelated-" + i + ".json"));
  const state = fixture({"patch-index.json":index});
  const result = await query(state, `{
    source(where:{url:{_eq:"https://github.com/owner/repo"},
      bundles:{patches:{name:{_eq:"Hide ads"}}}}) { id }
    conjunct:source(where:{_and:[
      {url:{_eq:"https://github.com/owner/repo"}},
      {bundles:{patches:{_or:[{name:{_eq:"Hide ads"}},{name:{_eq:"Other"}}]}}}
    ]}) { id }
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.source,[{id:1}]);
  assert.deepEqual(result.body.data.conjunct,[{id:1}]);
  assert(!state.requests.some(url=>url.includes("unrelated-")));
});

test("exact patch name filters use the search index within unchanged shard limits", async () => {
  const base=fixture();
  const index=JSON.parse(base.bodies["patch-index.json"]);
  index.pages.push(...Array.from({length:25},(_,i)=>"patches/unrelated-"+i+".json"));
  const state=fixture({"patch-index.json":index});
  const result=await query(state,`{
    patch(where:{name:{_eq:"Hide ads"}}) { id name }
    patch_aggregate(where:{_and:[{name:{_eq:"Hide ads"}}]}) { aggregate { count } }
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.patch.map(row=>row.name),["Hide ads","Hide ads"]);
  assert.equal(result.body.data.patch_aggregate.aggregate.count,2);
  assert(!state.requests.some(url=>url.includes("unrelated-")));
});
test("paginated patch package searches stop before unrelated shard reads", async () => {
  const base=fixture();
  const index=JSON.parse(base.bodies["patch-index.json"]);
  const unrelated=Array.from({length:25},(_,i)=>"patches/unrelated-"+i+".json");
  index.pages.push(...unrelated);
  index.packages["com.video"]=index.pages;
  const state=fixture({"patch-index.json":index});
  const result=await query(state,`{
    patch(package_name:"com.video",limit:1) { id name }
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.equal(result.body.data.patch.length,1);
  assert.equal(result.body.data.patch[0].name,"Hide ads");
  assert(!state.requests.some(url=>url.includes("unrelated-")));
});
test("descending patch searches read newest candidate shards first", async () => {
  const base=fixture();
  const index=JSON.parse(base.bodies["patch-index.json"]);
  const additional=Array.from({length:25},(_,i)=>"patches/unrelated-"+i+".json");
  index.pages.push(...additional);
  index.packages["com.video"]=index.pages;
  const newest={...JSON.parse(base.bodies["patches/00000.json"])[0],
    id:base.bundle.id+":99",legacy_id:100};
  const state=fixture({
    "patch-index.json":index,
    [additional.at(-1)]:[newest]
  });
  const result=await query(state,`{
    patch(package_name:"com.video",order_by:{id:desc},limit:1){id}
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.patch.map(row=>row.id),[100]);
  assert(!state.requests.some(url=>url.includes("unrelated-0.json")));
});
test("package index skips matching rows for large ascending and descending offsets", async () => {
  const base=fixture();
  const original=JSON.parse(base.bodies["patches/00000.json"])[0];
  const index=JSON.parse(base.bodies["patch-index.json"]);
  const pages=Array.from({length:45},(_,i)=>
    "patches/"+String(i).padStart(5,"0")+".json");
  index.pages=pages;
  index.packages["com.video"]=pages;
  index.bundles[base.bundle.id]=pages;
  const perPage=Object.fromEntries(pages.map(page=>[page,1]));
  const overrides={
    "patch-index.json":index,
    "patch-package-counts.json":{"com.video":perPage}
  };
  for (let i=0;i<pages.length;i++) overrides[pages[i]]=[
    {...original,id:base.bundle.id+":"+i,legacy_id:i+1}
  ];
  for (const order of ["asc","desc"]) {
    const state=fixture(overrides);
    const result=await query(state,`{
      patch(package_name:"com.video",offset:40,limit:1,order_by:{id:${order}}){id}
    }`);
    assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
    assert.deepEqual(result.body.data.patch.map(row=>row.id),[order==="asc"?41:5]);
    assert.equal(state.requests.filter(url=>/\/patches\/\d+\.json$/.test(url)).length,1);
  }
});
test("package pagination skips disabled-only pages even with zero offset", async () => {
  const base=fixture();
  const source=JSON.parse(base.bodies["sources.json"])[0];
  const disabledSource={...source,id:"s2",legacy_id:2,enabled:false};
  const disabledBundle={...base.bundle,id:"f".repeat(24),legacy_id:2,source_id:"s2"};
  const original=JSON.parse(base.bodies["patches/00000.json"])[0];
  const pages=Array.from({length:41},(_,i)=>
    "patches/"+String(i).padStart(5,"0")+".json");
  const index={pages,packages:{"com.video":pages},patch_ids:{},bundles:{
    [disabledBundle.id]:pages.slice(0,-1),[base.bundle.id]:pages.slice(-1)
  }};
  const overrides={
    "sources.json":[source,disabledSource],
    "bundles.json":[base.bundle,disabledBundle],
    "patch-index.json":index,
    "patch-package-counts.json":{"com.video":Object.fromEntries(
      pages.map((page,i)=>[page,Number(i===40)]))}
  };
  for (let i=0;i<pages.length;i++) {
    const enabled=i===40,bundle=enabled?base.bundle:disabledBundle;
    overrides[pages[i]]=[{...original,bundle_id:bundle.id,source_id:bundle.source_id,
      id:bundle.id+":"+i,legacy_id:i+1}];
  }
  const state=fixture(overrides);
  const result=await query(state,`{
    patch(package_name:"com.video",limit:1){id bundle_fk}
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.patch,[{id:41,bundle_fk:1}]);
  assert.equal(state.requests.filter(url=>/\/patches\/\d+\.json$/.test(url)).length,1);
  assert(state.requests.some(url=>url.endsWith("/patch-package-counts.json")));
});
test("absent modern short-search buckets mean no public matches without shard reads", async () => {
  const base=fixture();
  const original=JSON.parse(base.bodies["patches/00000.json"])[0];
  const pages=Array.from({length:40},(_,i)=>
    "patches/"+String(i).padStart(5,"0")+".json");
  const index={pages,bundles:{[base.bundle.id]:pages},patch_ids:{},
    packages:{"com.video":pages}};
  const overrides={"patch-index.json":index,"search-v2/0-0.json":{}};
  pages.forEach((page,i)=>{
    overrides[page]=[{...original,id:base.bundle.id+":"+i,legacy_id:i+1}];
  });
  for (const q of ["z","zx"]) {
    const modern=fixture(overrides);
    const result=await query(modern,`{patch(q:"${q}",limit:1){id}}`);
    assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
    assert.deepEqual(result.body.data.patch,[]);
    assert(!modern.requests.some(url=>/\/patches\/\d+\.json$/.test(url)));
  }
  const old=fixture({"patches/00000.json":[{...original,name:"Zebra"}]});
  const legacy=await query(old,'{patch(q:"z",limit:1){name}}');
  assert.equal(legacy.body.errors,undefined,JSON.stringify(legacy.body));
  assert.deepEqual(legacy.body.data.patch,[{name:"Zebra"}]);
});
test("non-ASCII patch searches avoid cross-runtime Unicode case-fold index errors", async () => {
  const base=fixture();
  const rows=JSON.parse(base.bodies["patches/00000.json"]);
  rows[0].name="AᲉB"; // Python and JavaScript can lowercase this differently.
  const state=fixture({"patches/00000.json":rows});
  const result=await query(state,`{patch(q:"AᲉB",limit:1){name}}`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.equal(result.body.data.patch[0].name,"AᲉB");
  assert(!state.requests.some(url=>url.includes("/search/")));
});
test("Unicode patch searches prune unrelated Unicode pages and preserve ASCII folds", async () => {
  const base=fixture();
  const original=JSON.parse(base.bodies["patches/00000.json"])[0];
  const pages=Array.from({length:40},(_,i)=>
    "patches/"+String(i).padStart(5,"0")+".json");
  const index={pages,bundles:{[base.bundle.id]:pages},patch_ids:{},
    packages:{"com.video":pages}};
  const overrides={"patch-index.json":index,"search-v2/0-0.json":{},
    "search-unicode-v2/0-0.json":{},
    "search-unicode-v2/3-1.json":{"caf":[pages[39]]},
    "search-unicode-v2/1-6.json":{"afé":[pages[39]]},
    "search-unicode-short/1-9.json":{"é":pages.slice(1)},
    "search-short/1-11.json":{"k":[pages[0]]}};
  pages.forEach((page,i)=>{
    overrides[page]=[{...original,id:base.bundle.id+":"+i,legacy_id:i+1,
      name:i===39?"café":i===0?"k":"é-noise"}];
  });
  const unicode=fixture(overrides);
  const result=await query(unicode,`{patch(q:"café",limit:1){id name}}`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.patch,[{id:40,name:"café"}]);
  assert.equal(unicode.requests.filter(url=>/\/patches\/\d+\.json$/.test(url)).length,1);
  const short=fixture(overrides);
  const shortResult=await query(short,`{patch(q:"é",limit:1){id name}}`);
  assert.equal(shortResult.body.errors,undefined,JSON.stringify(shortResult.body));
  assert.deepEqual(shortResult.body.data.patch,[{id:2,name:"é-noise"}]);
  assert.equal(short.requests.filter(url=>/\/patches\/\d+\.json$/.test(url)).length,1);
  const asciiFold=fixture(overrides);
  const folded=await query(asciiFold,`{patch(q:"K",limit:1){id name}}`);
  assert.equal(folded.body.errors,undefined,JSON.stringify(folded.body));
  assert.deepEqual(folded.body.data.patch,[{id:1,name:"k"}]);
  assert.equal(asciiFold.requests.filter(url=>/\/patches\/\d+\.json$/.test(url)).length,1);
});
test("reversing multiple patch pages preserves descending record order", async () => {
  const base=fixture();
  const first=JSON.parse(base.bodies["patches/00000.json"])[0];
  const second={...first,id:base.bundle.id+":1",legacy_id:2};
  const pages=["patches/00000.json","patches/00001.json"];
  const index=JSON.parse(base.bodies["patch-index.json"]);
  index.pages=pages;
  index.packages["com.video"]=pages;
  const state=fixture({
    "patch-index.json":index,
    [pages[0]]:[first],
    [pages[1]]:[second]
  });
  const catalog=await new Catalog(state.env.DATA_BASE_URL,state.env.FETCH).initialize();
  const result=await catalog.patchRows({package:"com.video",page_count:2,
    reverse_pages:true,include_unverified:true});
  assert.deepEqual(result.rows.map(row=>row.legacy_id),[2,1]);
});
test("long patch searches use bounded index reads without changing matches", async () => {
  const state=fixture();
  const result=await query(state,`{
    patch(q:"Remove video advertisements",limit:1){name}
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.equal(result.body.data.patch[0].name,"Hide ads");
  assert(state.requests.filter(url=>url.includes("/search/")).length<=3);
});

test("scoped relationship predicates hydrate siblings without returning other bundles", async () => {
  const base=fixture();
  const original=JSON.parse(base.bodies["patches/00000.json"]);
  const video=original[0];
  const other={...original[1],name:"Other app",packages:[{name:"com.other",versions:[]}]};
  const index={pages:["patches/00000.json","patches/00001.json",
      ...Array.from({length:25},(_,i)=>"patches/unrelated-"+i+".json")],
    bundles:{[base.bundle.id]:["patches/00000.json"],[base.dev.id]:["patches/00001.json"]},
    packages:{"com.video":["patches/00000.json"],"com.other":["patches/00001.json"]},
    patch_ids:{"1":["patches/00000.json"],"2":["patches/00001.json"]}};
  const state=fixture({
    "patches/00000.json":[video],"patches/00001.json":[other],"patch-index.json":index,
    "packages.json":[{name:"com.video",versions:["1.0","2.0"]},{name:"com.other",versions:[]}]
  });
  const result=await query(state,`{
    patch(where:{bundle_fk:{_eq:1},bundle:{source:{bundles:{
      patches:{patch_packages:{package:{name:{_eq:"com.other"}}}}
    }}}}) { name }
    bundle(where:{id:{_eq:1}}) {
      patches(where:{bundle:{source:{bundles:{patches:{name:{_eq:"Other app"}}}}}}) { name }
    }
    source_metadata(where:{id:{_eq:1},source:{bundles:{patches:{name:{_eq:"Other app"}}}}}) { id }
    patch_package(where:{patch_fk:{_eq:1},patch:{bundle:{source:{bundles:{
      patches:{name:{_eq:"Other app"}}
    }}}}}) { patch_fk }
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.patch,[{name:"Hide ads"}]);
  assert.deepEqual(result.body.data.bundle,[{patches:[{name:"Hide ads"}]}]);
  assert.deepEqual(result.body.data.source_metadata,[{id:1}]);
  assert.deepEqual(result.body.data.patch_package,[{patch_fk:1},{patch_fk:1}]);
  const links=await query(state,`{
    patch_package(where:{patch_fk:{_eq:1},patch:{bundle:{source:{bundles:{
      patches:{name:{_eq:"Other app"}}
    }}}}}) { patch_fk }
    byPackage:patch_package(where:{package_fk:{_eq:1}}) { patch_fk }
    unknown:source_metadata(where:{id:{_eq:999},source:{bundles:{patches:{name:{_eq:"Other app"}}}}}) { id }
  }`);
  assert.equal(links.body.errors,undefined,JSON.stringify(links.body));
  assert.deepEqual(links.body.data.patch_package,[{patch_fk:1},{patch_fk:1}]);
  assert.deepEqual(links.body.data.byPackage,[{patch_fk:1}]);
  assert.deepEqual(links.body.data.unknown,[]);
  assert(!state.requests.some(url=>url.includes("unrelated-")));
});

test("latest import fallback compares release instants across UTC offsets", async () => {
  const base=fixture();
  const stable={...base.bundle,channels:["stable"],created_at:"2026-10-08T02:00:00+02:00"};
  const dev={...base.dev,created_at:"2026-10-08T00:30:00Z"};
  const state=fixture({"bundles.json":[stable,dev]});
  const result=await call(state,"/api/v3/bundle?source_url=https://github.com/owner/repo&version=latest&channel=any");
  assert.equal(result.response.status,200);
  assert.equal(result.body.version,"v2-dev");
});

test("release ordering treats legacy dates as UTC and missing dates as oldest", () => {
  const dates = [
    { created_at: null },
    { created_at: "2026-10-08T02:00:00+02:00" },
    { created_at: "2026-10-08T00:30:00" },
    { created_at: "invalid" },
  ];
  assert.deepEqual(dates.toSorted(compareReleaseDates), [dates[2], dates[1], dates[0], dates[3]]);
  assert.equal(compareReleaseDates(dates[2], { created_at: "2026-10-08T00:30:00Z" }), 0);
});

test("GraphQL nullable filters match omitted patch and repository metadata fields", async () => {
  const base = fixture();
  const [patch, stale] = JSON.parse(base.bodies["patches/00000.json"]);
  delete patch.use;
  const state = fixture({ "patches/00000.json": [patch, stale] });
  const result = await query(state, `{
    patch(where: {bundle_id: {_eq: "${base.bundle.id}"}, use: {_is_null: true}}) { name use }
    source_metadata(where: {repo_pushed_at: {_is_null: true}}) { id repo_pushed_at }
  }`);
  assert.equal(result.response.status, 200);
  assert.equal(result.body.errors, undefined);
  assert.deepEqual(result.body.data.patch, [{ name: "Hide ads", use: null }]);
  assert.deepEqual(result.body.data.source_metadata, [{ id: 1, repo_pushed_at: null }]);
});

test("website imports follow channels by default and pin explicitly selected versions", () => {
  const bundle={sourceUrl:"https://gitlab.com/group/sub/repo",version:"v1+release",isPrerelease:false};
  let url=new URL(bundleImportPath(bundle),"https://api.test");
  assert.equal(url.searchParams.get("version"),"latest");
  assert.equal(url.searchParams.get("source_url"),bundle.sourceUrl);
  assert.equal(url.searchParams.get("channel"),"stable");
  url=new URL(bundleImportPath({...bundle,isPrerelease:true},true),"https://api.test");
  assert.equal(url.searchParams.get("version"),"v1+release");
  assert.equal(url.searchParams.get("channel"),"prerelease");
});

test("legacy routes resolve current repository metadata across hosts and renames", async () => {
  const base = fixture();
  const original = JSON.parse(base.bodies["sources.json"])[0];
  for (const url of ["https://github.com/old-owner/old-repo",
                     "https://gitlab.com/team/subgroup/project",
                     "https://codeberg.org/team/project"]) {
    const source = {...original, url, source_metadatum: {
      ...original.source_metadatum, owner_name:"current-owner", repo_name:"current-repo"}};
    const state = fixture({"sources.json":[source]});
    for (const path of ["/api/v1/bundle/current-owner/current-repo/v1",
                       "/api/v1/bundle/current-owner/current-repo/latest",
                       "/api/v2/bundle/current-owner/current-repo/latest?channel=STABLE"]) {
      const result = await call(state,path);
      assert.equal(result.response.status,200,url+" "+path);
      assert.equal(result.body.version,"v1");
    }
    assert.equal((await call(state,"/api/v1/bundle/current-owner/current-repo/latest?prerelease=true")).body.version,"v2-dev");
    assert.equal((await call(state,"/api/v1/bundle/old-owner/old-repo/latest")).response.status,404);
    source.enabled=false;
    const disabled=fixture({"sources.json":[source]});
    assert.equal((await call(disabled,"/api/v1/bundle/current-owner/current-repo/v1")).response.status,404);
    assert.equal((await call(disabled,"/api/v3/bundle?source_url="+encodeURIComponent(url)+"&version=v1")).response.status,404);
  }
});

test("legacy exact-version imports ignore latest channel flags", async () => {
  const base=fixture();
  const history={...base.bundle,id:"e".repeat(24),version:"old/tag",channels:[],is_latest:false};
  const state=fixture({"bundles.json":[base.bundle,base.dev,history],
    ["bundles/"+history.id+".json"]:history});
  assert.equal((await call(state,"/api/v1/bundle/owner/repo/old%2Ftag")).body.version,"old/tag");
  assert.equal((await call(state,"/api/v2/bundle/owner/repo/latest")).response.status,400);
  assert.equal((await call(state,"/api/v2/bundle/owner/repo/v1?channel=stable")).response.status,404);
});

test("scoped package relationships hydrate peer patches for filters and ordering", async () => {
  const base=fixture();
  const original=JSON.parse(base.bodies["patches/00000.json"]);
  const first={...original[0],packages:[
    {name:"com.other",versions:[]},{name:"com.video",versions:["1.0"]}
  ]};
  const second={...original[1],name:"Peer patch",packages:[{name:"com.video",versions:["1.0"]}]};
  const index={pages:["patches/00000.json","patches/00001.json",
      ...Array.from({length:25},(_,i)=>"patches/unrelated-"+i+".json")],
    bundles:{[base.bundle.id]:["patches/00000.json"],[base.dev.id]:["patches/00001.json"]},
    packages:{"com.video":["patches/00000.json","patches/00001.json"],"com.other":["patches/00000.json"]},
    patch_ids:{"1":["patches/00000.json"],"2":["patches/00001.json"]}};
  const overrides={"patches/00000.json":[first],"patches/00001.json":[second],
    "patch-index.json":index,"packages.json":[
      {name:"com.video",versions:["1.0"]},{name:"com.other",versions:[]}
    ]};
  const cases=[
    ['patch_aggregate(where:{id:{_eq:1},patch_packages:{package:{patch_packages:{patch:{id:{_eq:2}}}}}}){aggregate{count} nodes{id}}',
      {patch_aggregate:{aggregate:{count:1},nodes:[{id:1}]}}],
    ['patch(where:{id:{_eq:1},_not:{patch_packages:{package:{patch_packages:{patch:{id:{_eq:2}}}}}}}){id}',
      {patch:[]}],
    ['patch(where:{id:{_eq:1},patch_packages_aggregate:{count:{filter:{package:{patch_packages:{patch:{id:{_eq:2}}}}},predicate:{_eq:1}}}}){id}',
      {patch:[{id:1}]}],
    ['patch(where:{id:{_eq:1},patch_packages:{package:{patch_packages:{patch:{id:{_eq:2}}}}}}){id}',
      {patch:[{id:1}]}],
    ['patch(where:{id:{_eq:1},patch_packages:{package:{patch_packages_aggregate:{count:{predicate:{_gte:2}}}}}}){id}',
      {patch:[{id:1}]}],
    ['patch_package(where:{patch_fk:{_eq:1},package:{patch_packages:{patch:{id:{_eq:2}}}}}){package{name}}',
      {patch_package:[{package:{name:"com.video"}}]}],
    ['patch_package(where:{patch_fk:{_eq:1}},order_by:{package:{patch_packages_aggregate:{count:desc}}},limit:1){package{name}}',
      {patch_package:[{package:{name:"com.video"}}]}],
    [`bundle(where:{id:{_eq:1}}) {
      patches(where:{
        patch_packages:{package:{patch_packages:{patch:{
          bundle:{patches:{name:{_eq:"Peer patch"}}}
        }}}}
      }) {id}
    }`,
      {bundle:[{patches:[{id:1}]}]}],
  ];
  for(const [selection,expected] of cases) {
    const state=fixture(overrides);
    const result=await query(state,"{"+selection+"}");
    assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
    assert.deepEqual(result.body.data,expected,selection);
    assert(!state.requests.some(url=>url.includes("unrelated-")));
  }
});

test("relationship ordering honors mandatory source scope before reading shards", async () => {
  const base=fixture();
  const source=JSON.parse(base.bodies["sources.json"])[0];
  const unrelated=Array.from({length:25},(_,i)=>"patches/unrelated-"+i+".json");
  const other={...base.dev,id:"e".repeat(24),legacy_id:3,source_id:"s2"};
  const index=JSON.parse(base.bodies["patch-index.json"]);
  index.pages.push(...unrelated);
  index.bundles[other.id]=unrelated;
  const state=fixture({
    "sources.json":[source,{...source,id:"s2",legacy_id:2,url:"https://github.com/other/repo"}],
    "bundles.json":[base.bundle,base.dev,other],"patch-index.json":index
  });
  const result=await query(state,`{
    filtered:source(where:{id:{_eq:1},bundles:{patches:{name:{_eq:"Hide ads"}}}}) {id}
    bundle(where:{id:{_eq:1}},order_by:{patches_aggregate:{count:desc}}) {id}
    source(where:{_and:[{url:{_eq:"https://github.com/owner/repo"}}]},
      order_by:{bundles_aggregate:{sum:{patch_count:desc}}}) {id}
    ordered:source(where:{id:{_eq:1}},
      order_by:{source_metadatum:{source:{bundles_aggregate:{count:desc}}}}) {id}
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data,{filtered:[{id:1}],bundle:[{id:1}],source:[{id:1}],ordered:[{id:1}]});
  assert(!state.requests.some(url=>url.includes("unrelated-")));
});

test("release descriptions remain searchable and orderable past 1000 characters",async()=>{
  const initial=fixture(),prefix="a".repeat(1000);
  const rows=[{...initial.bundle,description:prefix+"z tail-marker"},
    {...initial.dev,description:prefix+"b tail-marker"}];
  const state=fixture({"bundles.json":rows,...Object.fromEntries(
    rows.map(row=>["bundles/"+row.id+".json",row]))});
  const rest=await call(state,"/bundles?q=tail-marker");
  assert.equal(rest.response.status,200);
  assert.equal(rest.body.data.length,2);
  const result=await query(state,`{
    bundle(where:{description:{_like:"%tail-marker%"}},order_by:{description:asc}){
      version description
    }
    bundle_aggregate{aggregate{min{description}max{description}}}
  }`);
  assert.equal(result.body.errors,undefined,JSON.stringify(result.body));
  assert.deepEqual(result.body.data.bundle,[
    {version:rows[1].version,description:rows[1].description},
    {version:rows[0].version,description:rows[0].description}
  ]);
  assert.deepEqual(result.body.data.bundle_aggregate.aggregate,{
    min:{description:rows[1].description},max:{description:rows[0].description}
  });
});


test("unnamed patches preserve REST search, SQL null filters and app compatibility", async () => {
  const initial = fixture();
  const original = JSON.parse(initial.bodies["patches/00000.json"])[0];
  const state = fixture({"patches/00000.json": [{...original, name: null}]});
  const found = await call(state, "/patches?q=advertisements&package=com.video&app_version=1.0");
  assert.equal(found.body.data.length, 1);
  assert.equal(found.body.data[0].name, null);
  assert.equal(found.body.data[0].description, original.description);
  assert.equal((await call(state, "/patches?q=null")).body.data.length, 0);
  const graph = await query(state,
    '{patch(where:{name:{_is_null:true}}){name description patch_packages{package{name version}}}' +
    'patch_aggregate{aggregate{count countName:count(columns:[name]) min{name} max{name}}}}');
  assert.equal(graph.body.errors, undefined, JSON.stringify(graph.body));
  assert.equal(graph.body.data.patch.length, 1);
  assert.equal(graph.body.data.patch[0].name, null);
  assert.equal(graph.body.data.patch[0].patch_packages.length, 2);
  assert.deepEqual(graph.body.data.patch_aggregate.aggregate,
    {count:1, countName:0, min:{name:null}, max:{name:null}});
});

test("verified empty bundles and paused rejections remain distinct in REST and GraphQL", async () => {
  const initial = fixture();
  const empty = {...initial.bundle, patch_count:0, patch_list_available:true};
  const paused = {...initial.dev, need_patches_update:false, extraction_terminal:true,
    extraction_status:"runtime_parsing_failure", metadata_status:"missing"};
  const state = fixture({"bundles.json":[empty,paused], "patches/00000.json":[],
    ["bundles/" + empty.id + ".json"]:empty, ["bundles/" + paused.id + ".json"]:paused});
  const graph = await query(state,
    '{bundle(order_by:{id:asc}){metadata_status need_patches_update patches{name}' +
    'patches_aggregate{aggregate{count}}}}');
  assert.equal(graph.body.errors, undefined, JSON.stringify(graph.body));
  assert.deepEqual(graph.body.data.bundle, [
    {metadata_status:"verified",need_patches_update:false,patches:[],patches_aggregate:{aggregate:{count:0}}},
    {metadata_status:"missing",need_patches_update:false,patches:[],patches_aggregate:{aggregate:{count:0}}}
  ]);
  const detail = await call(state, "/bundles/" + empty.id);
  assert.equal(detail.body.metadata_status, "verified");
  assert.equal(detail.body.patch_count, 0);
  assert.equal((await call(state, "/bundles/" + paused.id)).body.need_patches_update, false);
});

test("Boolean aggregate filters support predicates, filters, distinct and SQL NULL semantics", async () => {
  const initial = fixture();
  const source = JSON.parse(initial.bodies["sources.json"])[0];
  const empty = {...source, id:"s2", legacy_id:2, url:"https://github.com/owner/empty", bundles:[],
    source_metadatum:{...source.source_metadatum,id:2}};
  const stable = {...initial.bundle, need_patches_update:false};
  const dev = {...initial.dev, need_patches_update:true};
  const unknown = {...stable,id:"c".repeat(24),legacy_id:3,need_patches_update:null};
  const state = fixture({"sources.json":[source,empty], "bundles.json":[stable,dev,unknown]});
  const sources = async where => {
    const result = await query(state, "{source(where:" + where + ",order_by:{id:asc}){id}}");
    assert.equal(result.body.errors, undefined, JSON.stringify(result.body));
    return result.body.data.source.map(row => row.id);
  };
  const condition = (op, predicate, filter = "", distinct = "") =>
    "{bundles_aggregate:{" + op + ":{arguments:need_patches_update,predicate:" +
      predicate + filter + distinct + "}}}";
  assert.deepEqual(await sources(condition("bool_and","{_eq:false}")), [1]);
  assert.deepEqual(await sources(condition("bool_or","{_eq:true}")), [1]);
  assert.deepEqual(await sources(condition("bool_or","{_eq:false}",",filter:{is_prerelease:{_eq:false}}")), [1]);
  assert.deepEqual(await sources(condition("bool_and","{_eq:false}","",",distinct:true")), [1]);
  const allNull = condition("bool_and","{_is_null:true}",",filter:{id:{_gt:2}}");
  assert.deepEqual(await sources(allNull), [1,2]);
  const unknownComparison = condition("bool_and","{_eq:true}",",filter:{id:{_gt:2}}");
  assert.deepEqual(await sources("{_not:" + unknownComparison + "}"), []);
  assert.deepEqual(await sources("{_or:[{id:{_eq:1}}," + unknownComparison + "]}"), [1]);
  // The filter can traverse nested patch aggregates, loading only the required relationships.
  assert.deepEqual(await sources(condition("bool_and","{_eq:false}",
    ",filter:{patches_aggregate:{bool_or:{arguments:use,predicate:{_eq:true}}}}")), [1]);
  const invalid = await query(state,
    "{source(where:{bundles_aggregate:{bool_and:{arguments:version,predicate:{_eq:true}}}}){id}}");
  assert(invalid.body.errors?.length);
});

test("Boolean aggregate argument enums match the reference schema", async () => {
  const result = await query(fixture(),
    '{__type(name:"bundle_aggregate_bool_exp_bool_and"){inputFields{name type{name kind ofType{name kind}}}}}');
  assert.equal(result.body.errors, undefined);
  const fields = result.body.data.__type.inputFields;
  assert.equal(fields.find(field => field.name === "arguments").type.ofType.name,
    "bundle_select_column_bundle_aggregate_bool_exp_bool_and_arguments_columns");
  assert.equal(fields.find(field => field.name === "predicate").type.ofType.name, "Boolean_comparison_exp");
  assert.deepEqual(fields.map(field => field.name).sort(), ["arguments","distinct","filter","predicate"]);
});

test("patches with no supported versions have no compatibility links or app-version matches", async () => {
  const initial = fixture();
  const original = JSON.parse(initial.bodies["patches/00000.json"])[0];
  const rows = [
    {...original, name:"Any version", packages:[{name:"com.video",versions:[]}]},
    {...original, id:original.bundle_id+":1",legacy_id:3,name:"No versions",
      compatiblePackages:{"com.video":[]},packages:[]},
    {...original, id:original.bundle_id+":2",legacy_id:4,name:"Restricted",
      packages:[{name:"com.video",versions:["1.0"]}]}
  ];
  const state = fixture({"patches/00000.json":rows, "package-records.json":[
    {id:1,name:"com.video",version:"1.0"}, {id:2,name:"com.video",version:null}
  ]});
  const rest = await call(state, "/patches?package=com.video&app_version=999.0");
  assert.deepEqual(rest.body.data.map(row => row.name), ["Any version"]);
  const graph = await query(state,
    "{patch(order_by:{id:asc}){name patch_packages{package{version}}}}");
  assert.equal(graph.body.errors, undefined, JSON.stringify(graph.body));
  assert.deepEqual(graph.body.data.patch, [
    {name:"Any version",patch_packages:[{package:{version:null}}]},
    {name:"No versions",patch_packages:[]},
    {name:"Restricted",patch_packages:[{package:{version:"1.0"}}]}
  ]);
});


test("compact GraphQL bundles preserve REST custom icons and owner fallbacks", async () => {
  for (const bundleType of ["Morphe:V1", "ReVanced:v22"]) {
    const base = fixture();
    const source = JSON.parse(base.bodies["sources.json"])[0];
    source.source_metadatum.owner_avatar_url = "https://images.test/owner.png";
    source.source_metadatum.bundle_icon_url = "https://images.test/patches-bundle.png";
    const bundle = {...base.bundle, bundle_type:bundleType};
    const columns = ["id", "legacy_id", "source_id", "bundle_type", "icon_url", "owner_avatar_url"];
    const state = fixture({
      "sources.json":[source], "bundles.json":[bundle],
      "graphql-bundles.json":{columns,rows:[columns.map(key => bundle[key] ?? null)]}
    });
    const rest = await call(state, "/bundles");
    const graph = await query(state, "{bundle{icon_url owner_avatar_url}}");
    assert.equal(graph.body.errors, undefined, JSON.stringify(graph.body));
    assert.deepEqual(graph.body.data.bundle, rest.body.data.map(row => ({
      icon_url:row.icon_url, owner_avatar_url:row.owner_avatar_url
    })));
    assert.equal(graph.body.data.bundle[0].icon_url, bundleType.startsWith("Morphe:") ?
      source.source_metadatum.bundle_icon_url : source.source_metadatum.owner_avatar_url);
  }
});
