import site from "../../config/site.json" with { type: "json" };

const parameter = (name, description, schema = {type:"string"}, required = false, location = "query") =>
  ({ name, in: location, description, required, schema });
const responses = {
  200: { description: "Catalog result", content: {"application/json":{schema:{type:"object"}}} },
  400: { description: "Invalid query" }, 404: { description: "No matching cached record" },
  422: { description: "Query exceeds catalog budget; narrow the filter" },
  503: { description: "Catalog temporarily unavailable" }
};
const list = [parameter("limit","Rows per page",{type:"integer",minimum:1,maximum:100,default:25}),
  parameter("offset","Rows to skip",{type:"integer",minimum:0,maximum:10000,default:0}),
  parameter("q","Search text"), parameter("sort","Sort field"),
  parameter("order","Sort direction",{type:"string",enum:["asc","desc"]})];
const get = (summary, parameters = [], responseSet = responses) =>
  ({get:{summary, parameters, responses: responseSet}});
export const openapi = {
  openapi: "3.0.3",
  info: { title: "Patch Bundle Registry API", version: "3.0.0",
    description: "Public read-only patch catalog. D1 catalog mode serves indexed entities and R2 historical snapshots; legacy mode serves immutable snapshots. Live refresh status comes from GitHub Actions. " +
      "Queries use limits of 100 rows, 2 MiB responses and bounded shard loading. " +
      "Numeric IDs belong to this catalog; IDs from other installations are not interchangeable." },
  servers: [{url:site.api}],
  paths: {
    "/refresh-jobs": get("Recent and active refresh jobs",list),
    "/api/v1/refresh/status/{jobId}": {get:{summary:"Refresh job status",parameters:[
      parameter("jobId","Phase UUID or GitHub Actions refresh run ID",undefined,true,"path")],
      responses:{...responses,202:{description:"Job pending or running"}}}},
    "/health": get("Catalog generation and counts"),
    "/api/v3/bundle": get("Import a current or historical bundle", [
      parameter("source_url","Repository URL on a supported Git host",undefined,true),
      parameter("version","Exact release tag or latest",undefined,true),
      parameter("channel","Required for latest",{type:"string",enum:["any","stable","prerelease"]})]),
    "/api/reseam/bundle": get("Reseam Manager patch-source index", [
      parameter("source_url","Repository URL on a supported Git host",undefined,true),
      parameter("version","Exact release tag or latest",undefined,true),
      parameter("channel","Required for latest",{type:"string",enum:["any","stable","prerelease"]})]),
    "/api/v1/bundle/{id}": get("Legacy numeric-ID import", [
      parameter("id","Stable catalog bundle ID",{type:"integer"},true,"path")]),
    "/api/v1/bundle/{owner}/{repo}/{version}": get("Legacy repository exact-version or latest import", [
      parameter("owner","Recorded repository owner or namespace",undefined,true,"path"),
      parameter("repo","Repository name",undefined,true,"path"),
      parameter("version","Release tag or latest",undefined,true,"path"),
      parameter("prerelease","Select prerelease for latest",{type:"boolean",default:false})]),
    "/api/v2/bundle/{owner}/{repo}/latest": get("Legacy repository channel import", [
      parameter("owner","Recorded repository owner or namespace",undefined,true,"path"),
      parameter("repo","Repository name",undefined,true,"path"),
      parameter("channel","Release channel",{type:"string",enum:["any","stable","prerelease"]},true)]),
    "/sources": get("Browse source repositories", [...list,parameter("source_url","Exact repository URL")]),
    "/sources/{id}": get("Source details",[parameter("id","Catalog source hash",undefined,true,"path")]),
    "/bundles": get("Browse releases", [...list,parameter("source","Source ID, alias or repository URL"),
      parameter("ecosystem","revanced, morphe or reseam"),
      parameter("bundle_type","ReVanced:v21, ReVanced:v22, Morphe:V1 or Reseam"),parameter("version","Exact release tag"),
      parameter("channel","latest, stable or dev"),parameter("package","Compatible package name"),
      parameter("metadata_status","verified, stale, unverified or missing")]),
    "/releases": get("Browse releases (bundle alias)",list),
    "/bundles/{id}": get("Bundle details and release notes",[
      parameter("id","Catalog bundle hash",undefined,true,"path")]),
    "/bundles/{id}/patches": get("Browse patches in a bundle",[
      parameter("id","Catalog bundle hash",undefined,true,"path"),...list,
      parameter("include_unverified","Include fallback metadata",{type:"boolean"})]),
    "/patches": get("Browse indexed patches", [...list,parameter("source","Source ID or alias"),
      parameter("ecosystem","revanced, morphe or reseam"),
      parameter("bundle_type","ReVanced:v21, ReVanced:v22, Morphe:V1 or Reseam"),parameter("channel","latest, stable or dev"),
      parameter("bundle_id","Catalog bundle hash"),parameter("package","Exact package name"),
      parameter("app_version","Compatible application version"),
      parameter("page_cursor","Continue loading candidate shards",{type:"integer",minimum:0}),
      parameter("include_unverified","Include fallback metadata",{type:"boolean",default:false})]),
    "/catalog/patches": get("Page a complete bundle patch list (D1 deployments)",[
      parameter("bundle_id","Catalog bundle hash",undefined,true),
      parameter("generation","Catalog generation from /health; keep unchanged between pages",undefined,true),
      parameter("limit","Maximum rows; pages may shorten to fit the response budget",
        {type:"integer",minimum:1,maximum:100,default:100}),
      parameter("page_cursor","Returned next_cursor; continue until null",
        {type:"integer",minimum:0,default:0}),
      parameter("package","Exact package name"),
      parameter("include_unverified","Include fallback metadata",{type:"boolean",default:false}),
      parameter("compact","Omit repeated bundle and patch_packages relationships; retain patch packages",
        {type:"boolean",default:false})],
      {...responses,409:{description:"Catalog changed; restart pagination"},
        422:{description:"A single patch exceeds 2 MiB; response code is response_too_large"}}),
    "/packages": get("Browse application packages and versions",list),
    "/availability": get("Missing channels and retired source records"),
    "/graphql": {
      get: { summary: "Query the public GraphQL schema", parameters:[
        parameter("query","GraphQL query",undefined,true),
        parameter("variables","JSON variables"),parameter("operationName","Named operation")],responses },
      post: { summary: "Query the public GraphQL schema", requestBody:{required:true,
        content:{"application/json":{schema:{type:"object",required:["query"],properties:{
          query:{type:"string"},variables:{type:"object"},operationName:{type:"string"}}}}}},responses }
    }
  }
};
for (const alias of ["/v1/graphql","/hasura/v1/graphql"])
  openapi.paths[alias] = openapi.paths["/graphql"];
for (const name of ["sources","bundles","releases","patches","packages","availability"])
  openapi.paths["/api/v1/" + name] = openapi.paths["/" + name];

for (const path of ["/api/v3/bundle", "/api/reseam/bundle", "/api/v1/bundle/{id}",
  "/api/v1/bundle/{owner}/{repo}/{version}", "/api/v2/bundle/{owner}/{repo}/latest"]) {
  openapi.paths[path].get.responses = { ...responses, 200: {
    description: "Import information with preferred bundle icon and fallback owner avatar URLs",
    content: { "application/json": { schema: { type: "object", properties: {
      icon_url: { type: "string", format: "uri", nullable: true,
        description: "Morphe root patches-bundle.png when available, otherwise the owner avatar" },
      owner_avatar_url: { type: "string", format: "uri", nullable: true }
    } } } }
  } };
}

const appFilters = [
  parameter("source", "Source ID, alias or repository URL"),
  parameter("source_url", "Repository URL"),
  parameter("ecosystem", "revanced, morphe or reseam"),
  parameter("bundle_type", "ReVanced:v21, ReVanced:v22, Morphe:V1 or Reseam"),
  parameter("version", "Exact bundle release tag"),
  parameter("channel", "latest, stable or dev"),
  parameter("release", "Release category", {type:"string",enum:["all","latest","release","prerelease"],default:"all"}),
  parameter("package", "Package fragment or package@version"),
  parameter("app_version", "Exact compatible application version"),
  parameter("include_unverified", "Include fallback compatibility metadata", {type:"boolean",default:false}),
  parameter("metadata_status", "verified, stale, unverified or missing")
];
const appPackage = parameter("packageName", "Android package name", undefined, true, "path");
openapi.paths["/apps"] = get("Browse applications with cached names, icons and matching bundle/patch counts", [...list, ...appFilters]);
openapi.paths["/apps/{packageName}"] = get("Application details and compatibility summary", [appPackage, ...appFilters]);
openapi.paths["/apps/{packageName}/bundles"] = get("Browse bundles supporting an application", [appPackage, ...list, ...appFilters.filter(p => p.name !== "package")]);
openapi.paths["/apps/{packageName}/patches"] = get("Browse patches supporting an application", [appPackage, ...openapi.paths["/patches"].get.parameters.filter(p => p.name !== "package")]);
openapi.paths["/bundles/{id}/apps"] = get("Browse applications supported by a bundle", [
  parameter("id", "Catalog bundle hash", undefined, true, "path"), ...list,
  parameter("app_version", "Exact compatible application version")
]);
for (const path of ["/apps", "/apps/{packageName}", "/apps/{packageName}/bundles", "/apps/{packageName}/patches", "/bundles/{id}/apps"])
  openapi.paths["/api/v1" + path] = openapi.paths[path];
Object.assign(openapi.paths["/api/v3/bundle"].get.responses[200].content["application/json"].schema.properties, {
  apps: {type:"array",description:"First 100 supported applications, with package_name, app_name, icon_url, versions, unrestricted and patch_count",items:{type:"object"}},
  app_count: {type:"integer",description:"Total supported applications"},
  apps_url: {type:"string",description:"Paginated application endpoint path"}
});

// Historical routes are available after the D1 backend is imported and activated.
const historyPage = [parameter("limit","Rows per page (no total-history cap)",{type:"integer",minimum:1,maximum:100,default:25}),
  parameter("after","Opaque next_cursor from the previous page; keep filters unchanged"),
  parameter("order","Publication date and ID order",{type:"string",enum:["asc","desc"]}),
  ...["source_id","source","bundle_id","runtime","version","availability","from","to"].map(name=>parameter(name,"Exact filter; from/to are inclusive publication-date bounds")),
  parameter("prerelease","Filter release category",{type:"boolean"})];
const historyResponses = {...responses, 409:{description:"Generation changed; restart pagination"}};
openapi.paths["/history/releases"] = get("Original release metadata, prereleases, changelogs and assets (D1)",historyPage,historyResponses);
openapi.paths["/history/releases/{id}"] = get("Historical release metadata (D1)",[
  parameter("id","Historical release ID",undefined,true,"path")],historyResponses);
openapi.paths["/history/releases/{id}/document"] = get("Complete release JSON as base64 byte chunks (D1)",[
  parameter("id","Historical release ID",undefined,true,"path"),
  parameter("offset","Byte offset",{type:"integer",minimum:0,default:0}),
  parameter("limit","Maximum bytes",{type:"integer",minimum:1,maximum:65536,default:65536})],historyResponses);
for (const kind of ["sources","bundles","patches","packages","patch-packages","apps"])
  openapi.paths["/history/"+kind] = get("All retained "+kind+" metadata, including retired records (D1)",[
    parameter("limit","Rows per page",{type:"integer",minimum:1,maximum:100,default:25}),
    parameter("offset","Rows to skip; no fixed total-history cap",{type:"integer",minimum:0}),
    parameter("generation","Pin subsequent entity-history pages to the first response generation"),
    ...["source_id","bundle_id","catalog_id","name","version","patcher_runtime","metadata_status","source_fk","bundle_fk","patch_fk","package_fk"].map(name=>parameter(name,"Exact filter; entity-specific"))],historyResponses);
