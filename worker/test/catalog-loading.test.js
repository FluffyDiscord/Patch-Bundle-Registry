import test from "node:test";
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {runInNewContext} from "node:vm";
import {ApiCatalog} from "../../shared/api-catalog.js";
import {patchMatchesSearch,patchDisplayName} from "../../shared/site.js";
import {d1Fixture} from "./d1-fixture.js";

test("Bundle summaries omit repeated inventories and load full changelogs on demand",async()=>{
  const f=d1Fixture();
  try {
    f.sqlite.exec("UPDATE catalog_records SET data=json_set(data,'$.bundles',json('["+Array.from({length:800},(_,i)=>JSON.stringify("bundle"+i)).join(",")+"]')) WHERE kind='source'");
    const page=await f.request("/catalog/bundles?limit=100&generation="+f.old.manifest.generation);
    assert.equal(page.total,page.data.length);
    assert.ok(page.data.every(b=>!("bundles" in b.source)));
    assert.ok(page.data.every(b=>!b.description || b.description==="Available"));
    const detail=await f.request("/bundles/"+f.old.bundle.id);
    assert.equal(detail.description,f.old.bundle.description);
    assert.ok(detail.source.bundles.length>=800);
  } finally {f.sqlite.close();}
});

test("Bundle pages load concurrently in order, share bootstrap and obey a three-request cap",async()=>{
  const original=globalThis.fetch;
  let active=0, peak=0;
  const calls=[];
  globalThis.fetch=async input=>{
    const u=new URL(input), offset=Number(u.searchParams.get("offset")),limit=Number(u.searchParams.get("limit"));
    calls.push(offset);peak=Math.max(peak,++active);
    await new Promise(resolve=>setTimeout(resolve,offset===100?20:5));
    active--;
    return Response.json({total:650,data:Array.from({length:Math.min(limit,650-offset)},(_,i)=>({id:offset+i}))});
  };
  try {
    const api=new ApiCatalog("https://api.test");api.manifest={generation:"a".repeat(24)};
    const [a,b]=await Promise.all([api.bundles(),api.bundles()]);
    assert.deepEqual(a.map(row=>row.id),Array.from({length:650},(_,i)=>i));
    assert.deepEqual(a,b);assert.equal(peak,3);assert.equal(calls.length,7);
    await api.bundles();assert.equal(calls.length,7);
  } finally {globalThis.fetch=original;}
});

test("Parallel bundle pagination splits oversized pages without dropping or duplicating rows",async()=>{
  const original=globalThis.fetch;
  globalThis.fetch=async input=>{
    const u=new URL(input),offset=Number(u.searchParams.get("offset")),limit=Number(u.searchParams.get("limit"));
    if (limit>17) return Response.json({error:"Response too large"},{status:422});
    return Response.json({total:237,data:Array.from({length:Math.max(0,Math.min(limit,237-offset))},(_,i)=>({id:offset+i}))});
  };
  try {
    const api=new ApiCatalog("https://api.test");api.manifest={generation:"a".repeat(24)};
    assert.deepEqual((await api.bundles()).map(row=>row.id),Array.from({length:237},(_,i)=>i));
  } finally {globalThis.fetch=original;}
});

test("Failed parallel bootstrap is discarded and can be retried",async()=>{
  const original=globalThis.fetch;let fail=true;
  globalThis.fetch=async input=>{
    const offset=Number(new URL(input).searchParams.get("offset"));
    if (fail && offset===100) return Response.json({error:"Catalog changed"},{status:409});
    return Response.json({total:200,data:Array.from({length:100},(_,i)=>({id:offset+i}))});
  };
  try {
    const api=new ApiCatalog("https://api.test");api.manifest={generation:"a".repeat(24)};
    await assert.rejects(api.bundles(),e=>e.generationUnavailable);
    assert.deepEqual(api.bundleRows,[]);
    fail=false;
    assert.equal((await api.bundles()).length,200);
  } finally {globalThis.fetch=original;}
});

test("Internal display labels are searchable without replacing publisher names",()=>{
  assert.ok(patchMatchesSearch({name:null,display_name:"Enable Android Debugging Patch"},"Android debugging"));
  assert.ok(patchMatchesSearch({name:"Hide ads",display_name:"Helper"},"Hide ads"));
});

test("Patch headings fall back from whitespace names while preserving publisher titles",()=>{
  for (const name of [null,undefined,"","  ","\t\n"]) {
    assert.equal(patchDisplayName({name,display_name:"Enable Android Debugging Patch"}),
      "Enable Android Debugging Patch");
  }
  assert.equal(patchDisplayName({name:"  Hide ads  ",display_name:"Helper"}),"  Hide ads  ");
  assert.equal(patchDisplayName({name:" ",display_name:"\t"}),"Name unavailable");
  assert.equal(patchDisplayName({name:null,hidden:true}),"Internal patch");
});

test("A missing public name does not turn a patch into an internal patch",async()=>{
  const source = await readFile(new URL("../../web/app.js",import.meta.url),"utf8");
  const code = source.slice(source.indexOf("function transformPatch("),source.indexOf("\nasync function bundlePatches("));
  const context = {};
  runInNewContext(code,context);
  assert.equal(context.transformPatch({name:null,packages:[]}).hidden,false);
  assert.equal(context.transformPatch({name:null,hidden:true,packages:[]}).hidden,true);
});
