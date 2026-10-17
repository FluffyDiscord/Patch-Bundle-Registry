import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";
import {d1Fixture} from "./d1-fixture.js";

test("Retired bundles and their patches stay in history and leave public queries",async()=>{
  const f=d1Fixture();
  try {
    const row=f.sqlite.prepare("SELECT record_key FROM catalog_records WHERE kind='bundle' LIMIT 1").get();
    f.sqlite.prepare("UPDATE catalog_records SET data=json_set(data,'$.availability','unavailable','$.is_latest',json('false')) WHERE kind='bundle' AND record_key=?").run(row.record_key);
    const publicBundles=await f.request("/bundles?limit=100");
    assert.ok(publicBundles.data.every(b=>b.id!==row.record_key));
    const allBundles=await f.request("/history/bundles?limit=100");
    assert.ok(allBundles.data.some(b=>b.id===row.record_key));
    const publicPatches=await f.request("/bundles/"+row.record_key+"/patches?include_unverified=true");
    assert.equal(publicPatches.data.length,0);
    const history=await f.request("/history/patches?bundle_id="+row.record_key);
    assert.ok(history.data.length>0);
    const query="{bundle(where:{catalog_id:{_eq:\""+row.record_key+"\"}}){id} patch(where:{bundle_id:{_eq:\""+row.record_key+"\"}}){id}}";
    const graph=await f.graph(query);
    assert.deepEqual(graph.data,{bundle:[],patch:[]});
  } finally {f.sqlite.close();}
});

test("Packages referenced only by retired patches remain historical",async()=>{
  const f=d1Fixture();
  try {
    f.sqlite.exec("UPDATE catalog_records SET data=json_set(data,'$.availability','unavailable') WHERE kind='bundle'");
    const graph=await f.graph("{package{id} patch_package{patch_fk package_fk}}");
    assert.deepEqual(graph.data,{package:[],patch_package:[]});
    assert.ok((await f.request("/history/packages")).data.length>0);
  } finally {f.sqlite.close();}
});


test("Retired patches cannot consume public page limits or GraphQL hydration budgets",async()=>{
  const f=d1Fixture();
  try {
    f.sqlite.prepare("UPDATE catalog_records SET data=json_set(data,'$.availability','unavailable') WHERE kind='bundle' AND record_key=?")
      .run(f.old.bundle.id);
    const template=JSON.parse(f.sqlite.prepare("SELECT data FROM catalog_records WHERE kind='patch' ORDER BY record_key LIMIT 1").get().data);
    for (let index=0;index<5001;index++) {
      const key="retired-"+index;
      f.add("patch",key,{...template,id:index+3,catalog_id:key});
    }
    const page=await f.request("/catalog/patches?bundle_id="+f.old.bundle.id+
      "&generation="+f.old.manifest.generation+"&limit=1");
    assert.deepEqual(page.rows,[]);
    assert.equal(page.next_cursor,null);
    // Text filters use GraphQL's fallback planner and its bounded patch hydration.
    const graph=await f.graph('{patch(where:{name:{_ilike:"%ads%"}}){id}}');
    assert.equal(graph.errors,undefined,JSON.stringify(graph));
    assert.deepEqual(graph.data.patch.map(row=>row.id),[2]);
    assert.ok((await f.request("/history/patches?bundle_id="+f.old.bundle.id)).data.length>0);
  } finally {f.sqlite.close();}
});
