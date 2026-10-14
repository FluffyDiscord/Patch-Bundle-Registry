import assert from "node:assert/strict";
import {pathToFileURL} from "node:url";

const releaseFields="id source_id source_url upstream_id name tag version is_prerelease draft created_at published_at updated_at first_seen_at last_verified_at changelog release_notes release_url target_commitish availability binary_archived assets bundle_assets raw_metadata bundle_ids runtimes bundle_types metadata_document_url".split(" ");
const entities=[
  ["bundles","historical_bundles","id catalog_id source_fk source_id bundle_type ecosystem version created_at description download_url signature_download_url file_hash is_prerelease is_latest metadata_status patcher_runtime patch_count"],
  ["patches","historical_patches","id catalog_id bundle_fk bundle_id source_id name description use options metadata_status patch_metadata_version"],
  ["packages","historical_packages","id name version app_name icon_url"],
  ["patch-packages","historical_patch_packages","package_fk patch_fk patch_id"],
  ["sources","historical_sources","source_id source_url checked_at verified_at status configured required error upstream_count"],
  ["apps","historical_apps",null]
];
const selected=(row,fields)=>Object.fromEntries(fields.map(key=>[key,row[key]??null]));
export async function verifyHistoryApi(base,source=null,fetcher=fetch,expectedGeneration=null,
  {wait=ms=>new Promise(resolve=>setTimeout(resolve,ms)),log=console.log,mode="full"}={}) {
  if(!["smoke","full"].includes(mode))throw new Error("API verification mode must be smoke or full");
  let requests=0;
  const transient=new Set(["ECONNRESET","ETIMEDOUT","ECONNREFUSED","EAI_AGAIN",
    "UND_ERR_SOCKET","UND_ERR_CONNECT_TIMEOUT","UND_ERR_HEADERS_TIMEOUT","UND_ERR_BODY_TIMEOUT","TimeoutError"]);
  const origin=new URL(base);
  if(origin.protocol!=="https:" || origin.username || origin.password)
    throw new Error("API URL must use HTTPS without credentials");
  const url=base.replace(/\/$/,"");
  const json=async(path,options,context=path)=>{
    for(let attempt=0;;attempt++) {
      if(mode==="smoke" && requests>=200)throw new Error("API smoke request budget exhausted at "+context);
      requests++;
      let response;
      try {
        response=await fetcher(url+path,{...options,signal:AbortSignal.timeout(60000)});
        if(response.ok)return await response.json();
      } catch(error) {
        const code=error.name==="TimeoutError"?error.name:(error.cause?.code??error.code??error.name);
        if(attempt>=3 || !transient.has(code))
          throw new Error("API verification request failed at "+path.split("?")[0]+" ("+context+"): "+code,{cause:error});
        const delay=1000*2**attempt;
        log("Retrying "+context+" after "+code+" in "+delay/1000+"s");
        await wait(delay);continue;
      }
      let detail="";
      try {
        const body=await response.json();
        if(typeof body.error==="string")detail=": "+body.error.slice(0,300);
      } catch { /* Provider error pages may not contain JSON. */ }
      const message="API verification failed with HTTP "+response.status+" at "+
        path.split("?")[0]+" ("+context+")"+detail;
      if(attempt>=3 || ![429,500,502,503,504].includes(response.status))throw new Error(message);
      const header=response.headers.get("Retry-After");
      const retryAfter=header===null ? NaN : /^\d+(?:\.\d+)?$/.test(header.trim()) ?
        Number(header)*1000 : Date.parse(header)-Date.now();
      const delay=Math.max(1000*2**attempt,Number.isFinite(retryAfter)?retryAfter:0);
      log("Retrying "+context+" after HTTP "+response.status+" in "+Math.ceil(delay/1000)+"s");
      await wait(delay);
    }
  };
  const graph=async(query,variables,context)=>{
    const result=await json("/graphql",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({query,variables})},context);
    if(result.errors) throw new Error("GraphQL verification failed ("+context+"): "+result.errors.map(row=>row.message).join("; "));
    return result.data;
  };
  const ids=new Set();
  let after=null,generation,prereleases=0,documentChunks=0,limit=25,releasesComplete=false,reported=0,releasePages=0,documents=0;
  log("API verification mode: "+mode+"; "+(mode==="smoke"?"sampled API coverage, maximum 200 requests":"exhaustive API coverage"));
  log("Verifying historical releases");
  do {
    const params=new URLSearchParams({limit:String(limit),order:"asc"});
    if(source)params.set("source",source);
    if(after)params.set("after",after);
    let rest,gql;
    try {
      rest=await json("/history/releases?"+params);
      generation??=rest.generation;
      if(expectedGeneration)assert.equal(generation,expectedGeneration,"API generation differs from selected backup");
      assert.equal(rest.generation,generation,"Catalog changed during verification; retry");
      gql=await graph('query($source:String,$after:String,$limit:Int!){historical_releases(source:$source,after:$after,limit:$limit,order:"asc"){generation next_cursor data{'+releaseFields.join(" ")+'}}}',{source,after,limit},"releases after="+(after??"start")+" limit="+limit);
    } catch(error) {
      if(limit>1 && /HTTP 422|Response too large/.test(error.message)) {limit=Math.ceil(limit/2);continue;}
      throw error;
    }
    assert.equal(gql.historical_releases.generation,generation);
    assert.equal(gql.historical_releases.next_cursor,rest.next_cursor);
    assert.deepEqual(gql.historical_releases.data,rest.data.map(row=>selected(row,releaseFields)));
    for(const row of rest.data) {
      assert.ok(!ids.has(row.id),"Duplicate historical release across pages");
      ids.add(row.id);if(row.is_prerelease)prereleases++;
      if(row.metadata_document_url && (mode==="full" || documents<2)) {
        documents++;
        let offset=0,chunks=0;
        do {
          const chunk=await json("/history/releases/"+row.id+"/document?offset="+offset);
          const data=await graph('query($id:String!,$offset:Int!){historical_release(id:$id){document(offset:$offset){encoding data total_bytes next_offset}}}',{id:row.id,offset},"release document id="+row.id+" offset="+offset);
          assert.deepEqual(data.historical_release.document,chunk);
          assert.equal(chunk.encoding,"base64");
          const length=Buffer.from(chunk.data,"base64").length;
          assert.ok(chunk.next_offset===null || chunk.next_offset>offset,"Document pagination made no progress");
          assert.ok(length>0 || chunk.total_bytes===0,"Empty document page");
          documentChunks++;chunks++;offset=chunk.next_offset;
        } while(offset!==null && (mode==="full" || chunks<2));
      }
    }
    after=rest.next_cursor;
    releasesComplete=after===null;releasePages++;
    if(releasesComplete || ids.size-reported>=2500) {
      log("Verified "+ids.size+" historical releases");reported=ids.size;
    }
  } while(!releasesComplete && (mode==="full" || releasePages<2));
  const counts={},checkedCounts={};
  // Entity parity is installation-wide; source filters above scope release inventory only.
  for(const [path,root,selection] of entities) {
    let offset=0,total,limit=25,entitiesComplete=false,reported=0,checked=0,sampleOffsets=null,sampleIndex=0;
    log("Verifying historical "+path);
    do {
      const params=new URLSearchParams({limit:String(limit),offset:String(offset),generation});
      let rest,gql;
      try {
        rest=await json("/history/"+path+"?"+params);
        total??=rest.total;assert.equal(rest.total,total);
        assert.equal(rest.generation,generation);
        gql=await graph('query($offset:Int!,$generation:String!,$limit:Int!){catalog_generation '+root+
          '(limit:$limit,offset:$offset,generation:$generation)'+(selection?'{'+selection+'}':'')+'}',{offset,generation,limit},path+" offset="+offset+" limit="+limit);
      } catch(error) {
        if(limit>1 && /HTTP 422|Response too large/.test(error.message)) {
          limit=Math.ceil(limit/2);
          if(mode==="smoke" && sampleOffsets && sampleIndex===sampleOffsets.length-1)
            offset=Math.floor(Math.max(0,total-1)/limit)*limit;
          continue;
        }
        throw error;
      }
      assert.equal(gql.catalog_generation,generation);
      const fields=selection?.split(" ");
      const normalized=rest.data.map(row=>{
        const value=row.legacy_id==null?row:{...row,id:row.legacy_id,catalog_id:row.id};
        return fields?selected(value,fields):value;
      });
      assert.deepEqual(gql[root],normalized,"Historical "+path+" differs between protocols");
      assert.equal(rest.data.length,Math.min(limit,Math.max(0,total-offset)),"Historical entity page missed rows");
      checked+=rest.data.length;
      if(mode==="smoke") {
        sampleOffsets??=[...new Set([0,Math.floor(total/2/limit)*limit,
          Math.floor(Math.max(0,total-1)/limit)*limit])];
        sampleIndex++;entitiesComplete=sampleIndex>=sampleOffsets.length;
        if(!entitiesComplete)offset=sampleIndex===sampleOffsets.length-1?
          Math.floor(Math.max(0,total-1)/limit)*limit:sampleOffsets[sampleIndex];
      } else {
        offset+=rest.data.length;entitiesComplete=offset>=total;
      }
      if(entitiesComplete || checked-reported>=2500) {
        log("Verified "+path+" "+checked+"/"+total+(mode==="smoke"?" (sampled)":""));reported=checked;
      }
      if(!rest.data.length)break;
    } while(!entitiesComplete);
    if(mode==="full")assert.equal(checked,total,"Historical entity pagination missed rows");
    counts[path]=total;checkedCounts[path]=checked;
  }
  const result={generation,mode,coverage:mode==="full"?"exhaustive":"sampled",requests,
    releases:ids.size,releases_complete:releasesComplete,prereleases,document_chunks:documentChunks,
    entities:counts,entity_rows_checked:checkedCounts};
  console.log(JSON.stringify(result));
  return result;
}
if(process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href)
  await verifyHistoryApi(process.env.CATALOG_VERIFY_API_URL??process.argv[2],process.env.CATALOG_VERIFY_SOURCE??null,fetch,process.env.GENERATION||null,{mode:process.env.CATALOG_VERIFY_MODE??"smoke"});

