import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";
import {createHash} from "node:crypto";
import {d1Fixture} from "./d1-fixture.js";

function apps(f) {
  const rows=[{package_name:"com.video",app_name:"Video",icon_url:null,metadata_source:null,
    bundles:[f.old.bundle,f.old.dev].map(bundle=>({bundle_id:bundle.id,patch_count:1,versions:["1.0","2.0"],unrestricted:false}))}];
  const body=JSON.stringify(rows),hash=createHash("sha256").update(body).digest("hex");
  f.old.bodies["apps.json"]=body;
  f.old.manifest.files["apps.json"]={bytes:body.length,sha256:hash};
  const doc=JSON.parse(f.objects.get("generations/test.json"));
  doc.manifest.files["apps.json"]={bytes:body.length,sha256:hash};doc.files["apps.json"]=hash;
  f.objects.set("objects/"+hash,body);f.objects.set("generations/test.json",JSON.stringify(doc));
  for(const row of rows) f.add("app",row.package_name,row);
}
test("D1 app collections, details and bundle relationships match legacy filters without full R2 indexes",async()=>{
  const f=d1Fixture();apps(f);
  try {
    const doc=JSON.parse(f.objects.get("generations/test.json"));
    for(const name of ["bundles.json","app-bundles.json","apps.json"]) if(doc.manifest.files[name]) doc.manifest.files[name].bytes=24*1024*1024;
    f.objects.set("generations/test.json",JSON.stringify(doc));
    for(const path of ["/apps?limit=1","/api/v1/apps","/apps/com.video","/apps/com.video/bundles",
      "/apps?release=latest","/apps?release=release","/apps?release=prerelease","/apps?source=demo",
      "/apps?app_version=1.0","/apps?package=com.video@2.0","/apps?q=video","/apps?q=video%20com","/apps?sort=patch_count&order=desc"]) {
      const expected=await worker.fetch(new Request("https://api.test"+path),f.old.env);
      const actual=await worker.fetch(new Request("https://api.test"+path),f.env);
      assert.equal(actual.status,expected.status,path);
      const a=await actual.json(),e=await expected.json();
      if(path.includes("/bundles")) assert.deepEqual(a.data.map(row=>[row.id,row.app_compatibility]),e.data.map(row=>[row.id,row.app_compatibility]),path);
      else assert.deepEqual(a,e,path);
    }
    const forbidden=["bundles.json","app-bundles.json","apps.json"].map(name=>"objects/"+doc.files[name]);
    assert.ok(f.reads.every(key=>!forbidden.includes(key)));
  } finally {f.sqlite.close();}
});

test("D1 app pagination returns complete summaries across many applications",async()=>{
  const f=d1Fixture();
  try {
    for(let i=0;i<237;i++) f.add("app","com.app"+i,{
      package_name:"com.app"+i,app_name:"Video "+i,icon_url:null,metadata_source:null,
      bundles:[{bundle_id:f.old.bundle.id,patch_count:2,versions:["1"],unrestricted:false}]
    });
    const ids=[];
    for(let offset=0;offset<237;offset+=37) {
      const page=await f.request("/apps?limit=37&offset="+offset);
      assert.equal(page.total,237);ids.push(...page.data.map(row=>row.package_name));
      assert.ok(page.data.every(row=>row.patch_count===2 && row.bundle_count===1 && row.source_count===1));
    }
    assert.equal(new Set(ids).size,237);
    assert.equal((await f.request("/apps?q=not-present")).total,0);
  } finally {f.sqlite.close();}
});

test("D1 app search preserves Unicode and compatibility selectors",async()=>{
  const f=d1Fixture();
  try {
    f.add("app","com.unicode",{package_name:"com.unicode",app_name:"\u212aelvin \u00c9clair",icon_url:null,metadata_source:null,
      bundles:[{bundle_id:f.old.bundle.id,patch_count:1,versions:["\u212a"],unrestricted:false}]});
    assert.equal((await f.request("/apps?q=kelvin")).total,1);
    assert.equal((await f.request("/apps?q="+encodeURIComponent("éclair"))).total,1);
    assert.equal((await f.request("/apps?package="+encodeURIComponent("com.unicode@k"))).total,1);
    assert.equal((await f.request("/apps?app_version=wrong")).total,0);
    f.sqlite.exec("UPDATE catalog_records SET data=json_set(data,'$.enabled',json('false')) WHERE kind='source'");
    assert.equal((await f.request("/apps")).total,0);
  } finally {f.sqlite.close();}
});


test("D1 app Latest chooses one overall release before applying freshness filters",async()=>{
  const f=d1Fixture();apps(f);
  try {
    f.sqlite.prepare("UPDATE catalog_records SET data=json_set(data,'$.created_at',?) WHERE kind='bundle' AND record_key=?")
      .run("2027-01-01T00:00:00",f.old.dev.id);
    const both=await f.request("/apps?release=latest&include_unverified=true");
    assert.equal(both.total,1);assert.equal(both.data[0].bundle_count,1);
    assert.equal((await f.request("/apps?release=latest")).total,0);
    const dev=await f.request("/apps/com.video/bundles?release=latest&include_unverified=true");
    assert.deepEqual(dev.data.map(row=>row.id),[f.old.dev.id]);
  } finally {f.sqlite.close();}
});


test("Legacy hidden-flag counts fall back when an older aggregate index lacks the column",async()=>{
  const {fixture}=await import("./fixture.js");
  const original=fixture();
  const patches=JSON.parse(original.bodies["patches/00000.json"]).map((patch,index)=>({...patch,hidden:index===0}));
  const f=fixture({"patches/00000.json":patches,"patch-statistics.json":{
    count:2,columns:["id"],null_masks:{"0":2},distinct:{id:2},nonnull:{id:2},aggregate:{}
  }});
  const query="{patch_aggregate{aggregate{plain:count(columns:[hidden]) distinct:count(columns:[hidden],distinct:true) combined:count(columns:[hidden,id])}}}";
  const response=await worker.fetch(new Request("https://api.test/graphql",{
    method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({query})
  }),f.env);
  assert.equal(response.status,200);
  assert.deepEqual((await response.json()).data.patch_aggregate.aggregate,{plain:2,distinct:2,combined:2});
});


test("D1 app versions stay sorted across release relationships and query paths",async()=>{
  const f=d1Fixture();
  try {
    f.add("app","com.order",{package_name:"com.order",app_name:"Order",icon_url:null,metadata_source:null,
      bundles:[
        {bundle_id:f.old.bundle.id,patch_count:1,versions:["2","10"],unrestricted:false},
        {bundle_id:f.old.dev.id,patch_count:1,versions:["1","2"],unrestricted:false}
      ]});
    for(const suffix of ["","&package=com.order"]) {
      const result=await f.request("/apps?include_unverified=true"+suffix);
      assert.deepEqual(result.data[0].versions,["1","10","2"]);
    }
    assert.deepEqual((await f.request("/apps/com.order?include_unverified=true")).versions,["1","10","2"]);
  } finally {f.sqlite.close();}
});
