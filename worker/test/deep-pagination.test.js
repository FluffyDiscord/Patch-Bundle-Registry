import test from "node:test";
import assert from "node:assert/strict";
import {d1Fixture} from "./d1-fixture.js";

test("D1 deep history pages sort identities before loading generation-bound bodies",async()=>{
  const f=d1Fixture();
  try {
    f.sqlite.exec("DELETE FROM catalog_records WHERE kind IN ('patch','patch_package'); BEGIN");
    for(let id=1;id<=12005;id++) f.add("patch","p"+id,{
      id,catalog_id:"p"+id,bundle_fk:f.old.bundle.legacy_id,
      name:"Patch "+id,description:"metadata ".repeat(64)
    });
    f.sqlite.exec("COMMIT");
    f.add("patch","p11002",{id:90000,catalog_id:"p11002",name:"Future"},2);
    const page=await f.request("/history/patches?offset=11000&limit=3");
    assert.deepEqual(page.data.map(row=>row.legacy_id),[11001,11002,11003]);
    assert.equal(page.data[1].name,"Patch 11002");
    assert.equal(page.total,12005);
    const sql=f.queryLog.find(sql=>sql.includes("OFFSET ?") && !sql.includes("COUNT(*)"));
    const plan=f.sqlite.prepare("EXPLAIN QUERY PLAN "+sql).all("patch",3,11000).map(row=>row.detail);
    assert.ok(plan.some(detail=>/SEARCH r .*record_key=.*from_seq=/.test(detail)),JSON.stringify(plan));
    const graph=await f.graph("{historical_patches(offset:11000,limit:3,order_by:{id:asc}){id name}}");
    assert.equal(graph.errors,undefined,JSON.stringify(graph));
    assert.deepEqual(graph.data.historical_patches.map(row=>row.id),[11001,11002,11003]);
    const descending=await f.graph("{historical_patches(offset:11000,limit:3,order_by:{id:desc}){id}}");
    assert.deepEqual(descending.data.historical_patches.map(row=>row.id),[1005,1004,1003]);
  } finally {f.sqlite.close();}
});
