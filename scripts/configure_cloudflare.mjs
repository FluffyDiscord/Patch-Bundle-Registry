import { readFileSync, writeFileSync } from "node:fs";
const staging = process.argv.includes("--staging");
const backend = process.env.CATALOG_BACKEND ?? "legacy";
if (!["legacy","d1"].includes(backend)) throw new Error("Invalid CATALOG_BACKEND");
const config = JSON.parse(readFileSync(new URL("../worker/wrangler.jsonc",import.meta.url),"utf8"));
const target = config.env.production;
target.vars = {...target.vars,CATALOG_BACKEND:backend};
if (backend === "d1") {
  const id = process.env.CATALOG_D1_ID;
  if (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(id ?? "")) throw new Error("CATALOG_D1_ID must be a database UUID");
  target.d1_databases = [{binding:"DB",database_name:process.env.CATALOG_D1_NAME ||
    "patch-bundle-registry",database_id:id,migrations_dir:"migrations"}];
  target.r2_buckets = [{binding:"ARCHIVE",bucket_name:process.env.CATALOG_R2_BUCKET ||
    "patch-bundle-registry"}];
}
if (staging) {
  target.name = "patch-bundle-registry-api-staging";
  target.routes = [];
}
writeFileSync(new URL("../worker/wrangler.generated.json",import.meta.url),JSON.stringify(config,null,2)+"\n");
