import {pathToFileURL} from "node:url";
import site from "../config/site.json" with {type:"json"};

function apiOrigin(value,label) {
  let url;
  try {url=new URL(value);} catch {throw new Error(label+" must be an HTTPS origin");}
  if(url.protocol!=="https:" || url.username || url.password || url.pathname!=="/" || url.search || url.hash)
    throw new Error(label+" must be an HTTPS origin without a path, query or credentials");
  return url.origin;
}
export function stagingApiOrigin(env=process.env) {
  if(!env.CATALOG_STAGING_API_URL)throw new Error("Set the CATALOG_STAGING_API_URL repository variable");
  const origin=apiOrigin(env.CATALOG_STAGING_API_URL,"CATALOG_STAGING_API_URL");
  if(origin===new URL(site.api).origin)throw new Error("Staging API URL must differ from production");
  return origin;
}
export function accessApiFetch(base,{env=process.env,fetcher=fetch}={}) {
  const origin=apiOrigin(base,"API URL");
  const staging=env.CATALOG_STAGING_API_URL?stagingApiOrigin(env):null;
  const protectedApi=origin===staging;
  const id=env.CF_ACCESS_CLIENT_ID,secret=env.CF_ACCESS_CLIENT_SECRET;
  if(protectedApi && (!id || !secret))
    throw new Error("Staging requires STAGING_CF_ACCESS_CLIENT_ID and STAGING_CF_ACCESS_CLIENT_SECRET GitHub secrets");
  if(protectedApi && /[\r\n]/.test(id+secret))throw new Error("Invalid staging Access credentials");
  return (input,options={})=>{
    const requested=new URL(input);
    if(requested.origin!==origin || requested.username || requested.password)
      throw new Error("API request changed origin");
    const headers=new Headers(options.headers);
    headers.delete("CF-Access-Client-Id");headers.delete("CF-Access-Client-Secret");
    if(protectedApi) {
      headers.set("CF-Access-Client-Id",id);headers.set("CF-Access-Client-Secret",secret);
    }
    // Fetch otherwise forwards custom Access headers when following redirects.
    return fetcher(input,{...options,headers,redirect:"manual"});
  };
}
export async function checkStagingHealth({env=process.env,fetcher=fetch,
  wait=ms=>new Promise(resolve=>setTimeout(resolve,ms))}={}) {
  const base=stagingApiOrigin(env),request=accessApiFetch(base,{env,fetcher});
  let retryAfter=0;
  for(let attempt=0;attempt<5;attempt++) {
    if(attempt)await wait(Math.max(2000*attempt,retryAfter));
    retryAfter=0;
    let response,result;
    try {
      response=await request(base+"/health",{signal:AbortSignal.timeout(15000)});
      if(response.ok)result=await response.json();
    } catch(error) {
      if(error instanceof SyntaxError)throw new Error("Staging health returned invalid JSON");
      if(attempt===4)throw new Error("Staging health request failed after five attempts");
      continue;
    }
    if(response.ok) {
      if(result?.ok!==true || !/^[a-f0-9]{24}$/.test(result.generation??""))
        throw new Error("Staging health returned an invalid catalog response");
      return result;
    }
    const header=response.headers.get("Retry-After");
    const requested=header===null?NaN:/^\d+(?:\.\d+)?$/.test(header.trim())?
      Number(header)*1000:Date.parse(header)-Date.now();
    retryAfter=Number.isFinite(requested)?Math.max(0,requested):0;
    try {await response.body?.cancel();} catch { /* Keep the HTTP status if cleanup fails. */ }
    if(![404,429,500,502,503,504].includes(response.status) || attempt===4)
      throw new Error("Staging health failed with HTTP "+response.status+"; check Access policy and service token");
  }
}
if(process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) {
  if(process.argv.includes("--check-config")) {
    accessApiFetch(stagingApiOrigin());
    console.log("Staging Access configuration is present");
  } else {
    const health=await checkStagingHealth();
    console.log("Staging API health passed: "+health.generation);
  }
}
