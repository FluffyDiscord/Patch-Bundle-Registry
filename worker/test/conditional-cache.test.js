import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";
import {d1Fixture} from "./d1-fixture.js";

test("Conditional GET accepts weak tags, tag lists and wildcard on JSON and catalog files",async()=>{
  const f=d1Fixture();
  try {
    for(const path of ["/health","/catalog/data/manifest.json",
      "/catalog/data/snapshots/"+f.old.manifest.generation+"/sources.json"]) {
      const first=await worker.fetch(new Request("https://api.test"+path),f.env);
      assert.equal(first.status,200);
      const etag=first.headers.get("ETag");assert.ok(etag);
      for(const condition of [etag,"W/"+etag,'"other", W/'+etag,"*"]) {
        const response=await worker.fetch(new Request("https://api.test"+path,{
          headers:{"If-None-Match":condition}}),f.env);
        assert.equal(response.status,304,path+" "+condition);
        assert.equal(await response.text(),"");
        assert.equal(response.headers.get("ETag"),etag);
      }
      const changed=await worker.fetch(new Request("https://api.test"+path,{
        headers:{"If-None-Match":'W/"different"'}}),f.env);
      assert.equal(changed.status,200);
    }
    const post=await worker.fetch(new Request("https://api.test/graphql",{method:"POST",
      headers:{"Content-Type":"application/json","If-None-Match":"*"},
      body:JSON.stringify({query:"{bundle(limit:1){id}}"})}),f.env);
    assert.equal(post.status,200);
  } finally {f.sqlite.close();}
});

test("Cached GET responses accept weak validators after proxy compression",async()=>{
  const f=d1Fixture(),original=globalThis.caches,stored=new Map(),pending=[];
  globalThis.caches={default:{async match(key){return stored.get(key.url)?.clone();},
    async put(key,response){stored.set(key.url,response.clone());}}};
  try {
    const context={waitUntil:promise=>pending.push(promise)};
    const initial=await worker.fetch(new Request("https://api.test/health"),f.env,context);
    await Promise.all(pending);
    assert.equal(stored.size,1);
    const response=await worker.fetch(new Request("https://api.test/health",{
      headers:{"If-None-Match":"W/"+initial.headers.get("ETag")}}),f.env,context);
    assert.equal(response.status,304);
  } finally {globalThis.caches=original;f.sqlite.close();}
});
