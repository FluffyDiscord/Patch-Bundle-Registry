import test from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";
import {d1Fixture} from "./d1-fixture.js";
import {accessApiFetch,checkStagingHealth,stagingApiOrigin} from "../../scripts/api_access.mjs";
import {verifyHistoryApi} from "../../scripts/verify_history_api.mjs";

const env={CATALOG_STAGING_API_URL:"https://stage.test",CF_ACCESS_CLIENT_ID:"test-client.access",
  CF_ACCESS_CLIENT_SECRET:"test-secret"};

test("staging Access headers preserve JSON headers and never follow redirects",async()=>{
  const calls=[];
  const request=accessApiFetch("https://stage.test/",{env,fetcher:async(url,options)=>{
    calls.push({url,options});return new Response(null,{status:302,headers:{Location:"https://other.test"}});
  }});
  const response=await request("https://stage.test/graphql",{method:"POST",redirect:"follow",
    headers:{"Content-Type":"application/json"},body:"{}"});
  assert.equal(response.status,302);assert.equal(calls.length,1);
  assert.equal(calls[0].options.redirect,"manual");
  assert.equal(calls[0].options.headers.get("CF-Access-Client-Id"),env.CF_ACCESS_CLIENT_ID);
  assert.equal(calls[0].options.headers.get("CF-Access-Client-Secret"),env.CF_ACCESS_CLIENT_SECRET);
  assert.equal(calls[0].options.headers.get("Content-Type"),"application/json");
  assert.throws(()=>request("https://other.test/health"),/changed origin/);
  assert.throws(()=>request("https://someone:password@stage.test/health"),/changed origin/);
  assert.equal(calls.length,1);
});

test("production and other verification origins never receive staging credentials",async()=>{
  for(const base of ["https://patch-bundles.api.jmancentral.com","https://stage.test.evil.test"]){
    const request=accessApiFetch(base,{env,fetcher:async(url,options)=>{
      assert.equal(options.headers.has("CF-Access-Client-Id"),false);
      assert.equal(options.headers.has("CF-Access-Client-Secret"),false);
      return Response.json({ok:true});
    }});
    await request(base+"/health",{headers:{"CF-Access-Client-Secret":"not-for-production"}});
  }
  const request=accessApiFetch("https://public.test",{env:{},fetcher:async()=>Response.json({ok:true})});
  assert.equal((await request("https://public.test/health")).status,200);
});

test("staging auth configuration fails before a request without exposing credentials",()=>{
  for(const field of ["CF_ACCESS_CLIENT_ID","CF_ACCESS_CLIENT_SECRET"]){
    assert.throws(()=>accessApiFetch("https://stage.test",{env:{...env,[field]:""}}),error=>{
      assert.match(error.message,/Staging requires/);
      assert.ok(!error.message.includes(env.CF_ACCESS_CLIENT_SECRET));return true;
    });
  }
  assert.throws(()=>stagingApiOrigin({}),/repository variable/);
  for(const url of ["http://stage.test","https://stage.test/path","https://stage.test?query=1",
    "https://someone:password@stage.test","https://patch-bundles.api.jmancentral.com"])
    assert.throws(()=>stagingApiOrigin({...env,CATALOG_STAGING_API_URL:url}));
  assert.throws(()=>accessApiFetch("https://stage.test",{env:{...env,CF_ACCESS_CLIENT_SECRET:"bad\nheader"}}),
    /Invalid staging Access credentials/);
});

test("staging health retries propagation failures with authenticated requests",async()=>{
  const delays=[];let calls=0;
  const result=await checkStagingHealth({env,wait:async ms=>delays.push(ms),fetcher:async(url,options)=>{
    assert.equal(url,"https://stage.test/health");assert.equal(options.redirect,"manual");
    assert.equal(options.headers.get("CF-Access-Client-Secret"),env.CF_ACCESS_CLIENT_SECRET);
    return ++calls===1?new Response(null,{status:503}):Response.json({ok:true,generation:"a".repeat(24)});
  }});
  assert.equal(result.generation,"a".repeat(24));assert.equal(calls,2);assert.deepEqual(delays,[2000]);
});

test("staging health rejects Access login redirects, denial and malformed success",async()=>{
  for(const status of [302,401,403]){
    let calls=0;
    await assert.rejects(checkStagingHealth({env,fetcher:async()=>{calls++;return new Response(null,{status});}}),
      new RegExp("HTTP "+status));
    assert.equal(calls,1);
  }
  await assert.rejects(checkStagingHealth({env,fetcher:async()=>Response.json(null)}),/invalid catalog/);
});

test("REST and GraphQL verification attach Access credentials on every staging request",async()=>{
  const f=d1Fixture(3);const requests=[];
  try {
    const fetcher=accessApiFetch("https://stage.test",{env,fetcher:async(url,options)=>{
      assert.equal(options.redirect,"manual");
      assert.equal(options.headers.get("CF-Access-Client-Id"),env.CF_ACCESS_CLIENT_ID);
      assert.equal(options.headers.get("CF-Access-Client-Secret"),env.CF_ACCESS_CLIENT_SECRET);
      requests.push({url,method:options.method??"GET"});
      return worker.fetch(new Request(url,options),f.env);
    }});
    const result=await verifyHistoryApi("https://stage.test",null,fetcher,null,{mode:"smoke",log:()=>{}});
    assert.equal(result.mode,"smoke");
    assert.ok(requests.some(row=>row.url.endsWith("/graphql") && row.method==="POST"));
    assert.ok(requests.some(row=>row.url.includes("/history/releases?") && row.method==="GET"));
  } finally {f.sqlite.close();}
});


test("staging health honors server retry delays",async()=>{
  const delays=[];let calls=0;
  await checkStagingHealth({env,wait:async ms=>delays.push(ms),fetcher:async()=>
    ++calls===1?new Response(null,{status:429,headers:{"Retry-After":"7"}}):
      Response.json({ok:true,generation:"a".repeat(24)})});
  assert.deepEqual(delays,[7000]);assert.equal(calls,2);
});

test("staging network retries stop at five requests and keep credentials out of errors",async()=>{
  let calls=0;
  await assert.rejects(checkStagingHealth({env,wait:async()=>{},fetcher:async()=>{
    calls++;throw new Error("network error "+env.CF_ACCESS_CLIENT_SECRET);
  }}),error=>{
    assert.match(error.message,/five attempts/);
    assert.ok(!error.message.includes(env.CF_ACCESS_CLIENT_SECRET));return true;
  });
  assert.equal(calls,5);
});


test("staging health retries interrupted response bodies",async()=>{
  for(const status of [200,503]) {
    let calls=0;
    const result=await checkStagingHealth({env,wait:async()=>{},fetcher:async()=>{
      if(++calls>1)return Response.json({ok:true,generation:"a".repeat(24)});
      const body=new ReadableStream({start(controller){
        controller.error(Object.assign(new Error("response socket closed"),{code:"ECONNRESET"}));
      }});
      return new Response(body,{status});
    }});
    assert.equal(result.ok,true);assert.equal(calls,2);
  }
});
