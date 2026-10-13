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
export async function verifyHistoryApi(base,source=null,fetcher=fetch,expectedGeneration=null) {
  const origin=new URL(base);
  if(origin.protocol!=="https:" || origin.username || origin.password)
    throw new Error("API URL must use HTTPS without credentials");
  const url=base.replace(/\/$/,"");
  const json=async(path,options)=>{
    const response=await fetcher(url+path,options);
    if(!response.ok) throw new Error("API verification failed with HTTP "+response.status+" at "+path.split("?")[0]);
    return response.json();
  };
  const graph=async(query,variables)=>{
    const result=await json("/graphql",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({query,variables})});
    if(result.errors) throw new Error("GraphQL verification failed: "+result.errors.map(row=>row.message).join("; "));
    return result.data;
  };
  const ids=new Set();
  let after=null,generation,prereleases=0,documentChunks=0,limit=25,releasesComplete=false;
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
      gql=await graph('query($source:String,$after:String,$limit:Int!){historical_releases(source:$source,after:$after,limit:$limit,order:"asc"){generation next_cursor data{'+releaseFields.join(" ")+'}}}',{source,after,limit});
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
      if(row.metadata_document_url) {
        let offset=0;
        do {
          const chunk=await json("/history/releases/"+row.id+"/document?offset="+offset);
          const data=await graph('query($id:String!,$offset:Int!){historical_release(id:$id){document(offset:$offset){encoding data total_bytes next_offset}}}',{id:row.id,offset});
          assert.deepEqual(data.historical_release.document,chunk);
          assert.equal(chunk.encoding,"base64");
          const length=Buffer.from(chunk.data,"base64").length;
          assert.ok(chunk.next_offset===null || chunk.next_offset>offset,"Document pagination made no progress");
          assert.ok(length>0 || chunk.total_bytes===0,"Empty document page");
          documentChunks++;offset=chunk.next_offset;
        } while(offset!==null);
      }
    }
    after=rest.next_cursor;
    releasesComplete=after===null;
  } while(!releasesComplete);
  const counts={};
  // Entity parity is installation-wide; source filters above scope release inventory only.
  for(const [path,root,selection] of entities) {
    let offset=0,total,limit=25,entitiesComplete=false;
    do {
      const params=new URLSearchParams({limit:String(limit),offset:String(offset),generation});
      let rest,gql;
      try {
        rest=await json("/history/"+path+"?"+params);
        total??=rest.total;assert.equal(rest.total,total);
        assert.equal(rest.generation,generation);
        gql=await graph('query($offset:Int!,$generation:String!,$limit:Int!){catalog_generation '+root+
          '(limit:$limit,offset:$offset,generation:$generation)'+(selection?'{'+selection+'}':'')+'}',{offset,generation,limit});
      } catch(error) {
        if(limit>1 && /HTTP 422|Response too large/.test(error.message)) {limit=Math.ceil(limit/2);continue;}
        throw error;
      }
      assert.equal(gql.catalog_generation,generation);
      const fields=selection?.split(" ");
      const normalized=rest.data.map(row=>{
        const value=row.legacy_id==null?row:{...row,id:row.legacy_id,catalog_id:row.id};
        return fields?selected(value,fields):value;
      });
      assert.deepEqual(gql[root],normalized,"Historical "+path+" differs between protocols");
      offset+=rest.data.length;
      entitiesComplete=offset>=total;
      if(!rest.data.length)break;
    } while(!entitiesComplete);
    assert.equal(offset,total,"Historical entity pagination missed rows");
    counts[path]=total;
  }
  const result={generation,releases:ids.size,prereleases,document_chunks:documentChunks,entities:counts};
  console.log(JSON.stringify(result));
  return result;
}
if(process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href)
  await verifyHistoryApi(process.env.CATALOG_VERIFY_API_URL??process.argv[2],process.env.CATALOG_VERIFY_SOURCE??null,fetch,process.env.GENERATION||null);

