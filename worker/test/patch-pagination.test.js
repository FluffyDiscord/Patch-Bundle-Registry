import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";
import {ApiCatalog} from "../../shared/api-catalog.js";
import {d1Fixture} from "./d1-fixture.js";

function largeBundle(count, descriptionBytes = 32000, patchBytes = 0) {
  const f = d1Fixture();
  const template = JSON.parse(f.old.bodies["patches/00000.json"])[0];
  f.sqlite.exec("DELETE FROM catalog_records WHERE kind='patch'");
  f.sqlite.prepare("UPDATE catalog_records SET data=json_set(data,'$.description',?,'$.patch_count',?) WHERE kind='bundle'")
    .run("x".repeat(descriptionBytes),count);
  for (let i=0;i<count;i++) {
    const id=f.old.bundle.id+":"+String(i).padStart(4,"0");
    f.add("patch",id,{...template,id:i+1,catalog_id:id,bundle_fk:f.old.bundle.legacy_id,name:"Patch "+i,
      description:"\u00e9".repeat(patchBytes),options:[{key:"setting",default:"value"}]});
  }
  return f;
}
const patchPath = f => "/catalog/patches?bundle_id="+f.old.bundle.id+
  "&generation="+f.old.manifest.generation;

test("Large bundle pages stay within the byte budget without losing patches or relationships",async()=>{
  const f=largeBundle(180);
  try {
    const ids=[],counts=[];
    let cursor=0;
    do {
      const response=await worker.fetch(new Request("https://api.test"+patchPath(f)+"&page_cursor="+cursor),f.env);
      assert.equal(response.status,200,await response.clone().text());
      const body=await response.text();
      assert.ok(Buffer.byteLength(body)<=2*1024*1024);
      const page=JSON.parse(body);
      counts.push(page.rows.length);
      for (const row of page.rows) {
        assert.equal(row.bundle.description.length,32000);
        assert.ok(row.patch_packages.length);
        ids.push(row.id);
      }
      if (page.next_cursor!==null) assert.equal(page.next_cursor,cursor+page.rows.length);
      cursor=page.next_cursor;
    } while(cursor!==null);
    assert.equal(ids.length,180);
    assert.equal(new Set(ids).size,180);
    assert.ok(counts[0]<100);
    const limited=await f.request(patchPath(f)+"&limit=7");
    assert.equal(limited.rows.length,7);
    assert.equal(limited.next_cursor,7);
    for (const limit of ["0","101","1.5","nope"]) {
      const response=await worker.fetch(new Request("https://api.test"+patchPath(f)+"&limit="+limit),f.env);
      assert.equal(response.status,400);
    }
  } finally {f.sqlite.close();}
});

test("Compact website patch pages preserve all patch metadata and compatibility",async()=>{
  const f=largeBundle(180);
  const original=globalThis.fetch,calls=[];
  globalThis.fetch=async (url,options)=>{
    calls.push(new URL(url));
    return worker.fetch(new Request(url,options),f.env);
  };
  try {
    const api=new ApiCatalog("https://api.test");
    api.manifest=f.old.manifest;
    const result=await api.patchRows({bundle_id:f.old.bundle.id,all:true,compact:true,include_unverified:true});
    assert.equal(result.rows.length,180);
    assert.equal(new Set(result.rows.map(row=>row.id)).size,180);
    assert.equal(calls.length,2);
    for (const url of calls) assert.equal(url.searchParams.get("compact"),"true");
    for (const row of result.rows) {
      assert.equal(row.metadata_status,"verified");
      assert.deepEqual(row.packages,JSON.parse(f.old.bodies["patches/00000.json"])[0].packages);
      assert.deepEqual(row.options,[{key:"setting",default:"value"}]);
      assert.equal(Object.hasOwn(row,"bundle"),false);
      assert.equal(Object.hasOwn(row,"patch_packages"),false);
    }
  } finally {globalThis.fetch=original;f.sqlite.close();}
});

test("Compact pages also shorten for large individual patch metadata using UTF-8 byte sizes",async()=>{
  const f=largeBundle(12,32000,100000);
  try {
    const page=await f.request(patchPath(f)+"&compact=true&limit=12");
    assert.ok(page.rows.length<12);
    assert.equal(page.next_cursor,page.rows.length);
    assert.ok(Buffer.byteLength(JSON.stringify(page))<=2*1024*1024);
    const next=await f.request(patchPath(f)+"&compact=true&page_cursor="+page.next_cursor);
    assert.equal(page.rows.length+next.rows.length,12);
    assert.equal(next.next_cursor,null);
  } finally {f.sqlite.close();}
});

test("Frontend retries oversized pages at the same cursor and retains every patch",async()=>{
  const original=globalThis.fetch,calls=[];
  globalThis.fetch=async input=>{
    const url=new URL(input),limit=Number(url.searchParams.get("limit"));
    const cursor=Number(url.searchParams.get("page_cursor"));
    calls.push([cursor,limit]);
    if(limit>25) return Response.json({error:"Response too large; reduce the query limit"},{status:422});
    const length=Math.min(limit,136-cursor);
    return Response.json({rows:Array.from({length},(_,i)=>({id:cursor+i})),
      next_cursor:cursor+length<136?cursor+length:null,candidate_pages:1});
  };
  try {
    const api=new ApiCatalog("https://api.test");api.manifest={generation:"d".repeat(24)};
    const result=await api.patchRows({bundle_id:"a".repeat(24),all:true});
    assert.deepEqual(calls.slice(0,3),[[0,100],[0,50],[0,25]]);
    assert.deepEqual(result.rows.map(row=>row.id),Array.from({length:136},(_,i)=>i));
  } finally {globalThis.fetch=original;}
});

test("An oversized single patch fails clearly after bounded retries",async()=>{
  const f=largeBundle(1,0,1100000);
  const original=globalThis.fetch;let calls=0;
  globalThis.fetch=async (url,options)=>{
    calls++;return worker.fetch(new Request(url,options),f.env);
  };
  try {
    const response=await worker.fetch(new Request("https://api.test"+patchPath(f)+"&compact=true&limit=1"),f.env);
    assert.equal(response.status,422);
    assert.equal((await response.json()).code,"response_too_large");
    const api=new ApiCatalog("https://api.test");api.manifest=f.old.manifest;
    await assert.rejects(api.patchRows({bundle_id:f.old.bundle.id,all:true,compact:true}),
      error=>error.status===422 && /exceeds the API size limit/.test(error.message));
    assert.equal(calls,8);
  } finally {globalThis.fetch=original;f.sqlite.close();}
});

test("Frontend preserves generation and non-size errors, and rejects stalled cursors",async()=>{
  const original=globalThis.fetch;
  try {
    const api=new ApiCatalog("https://api.test");api.manifest={generation:"d".repeat(24)};
    for (const [status,message] of [[409,"Catalog changed"],[422,"Query exceeds catalog budget"],[503,"Backend unavailable"]]) {
      let calls=0;
      globalThis.fetch=async()=>{calls++;return Response.json({error:message},{status});};
      await assert.rejects(api.patchRows({bundle_id:"a".repeat(24),all:true}),
        error=>error.status===status && error.message.includes(message) && error.generationUnavailable===(status===409));
      assert.equal(calls,status===503?3:1);
    }
    globalThis.fetch=async()=>Response.json({rows:[{id:1}],next_cursor:0});
    await assert.rejects(api.patchRows({bundle_id:"a".repeat(24),all:true}),/Invalid patch pagination/);
  } finally {globalThis.fetch=original;}
});
