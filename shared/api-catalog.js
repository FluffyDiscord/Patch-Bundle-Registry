import { Catalog, CatalogError } from "./catalog.js";

// Bootstrap only current bundles; source history is requested when the source is selected.
export class ApiCatalog extends Catalog {
  constructor(api) {
    super(api.replace(/\/$/,"") + "/catalog/data/",fetch,false);
    this.api = api.replace(/\/$/,"");
    this.bundleRows = [];
    this.histories = new Map();
  }
  async bundlePages(source = null) {
    const rows = [];
    let offset = 0, limit = 100;
    while (true) {
      const params = new URLSearchParams({limit:String(limit),offset:String(offset),
        generation:this.manifest.generation});
      if (source) params.set("source",source);
      const response = await fetch(this.api + "/catalog/bundles?" + params);
      if (response.status === 422 && limit > 1) {
        limit = Math.ceil(limit / 2);
        continue;
      }
      if (!response.ok) {
        const error = new CatalogError("Catalog changed or unavailable; reload the page",response.status);
        error.generationUnavailable = response.status === 409;
        throw error;
      }
      const page = await response.json();
      rows.push(...page.data);
      if (page.data.length < limit) break;
      offset += page.data.length;
    }
    return rows;
  }
  async file(name) {
    const match = name.match(/^bundles\/([a-f0-9]+)\.json$/);
    if (!match) return super.file(name);
    if (!this.loaded.has(name)) this.loaded.set(name,(async()=>{
      const response=await fetch(this.api+"/bundles/"+match[1]);
      if (!response.ok || response.headers.get("X-Catalog-Generation") !== this.manifest.generation) {
        const error=new CatalogError("Catalog generation unavailable",409);
        error.generationUnavailable=true;
        throw error;
      }
      return response.json();
    })());
    try { return await this.loaded.get(name); }
    catch (error) { this.loaded.delete(name); throw error; }
  }
  async patchRows(args = {}) {
    if (!args.bundle_id || args.q) return super.patchRows(args);
    const rows=[];
    let cursor=args.page_cursor ?? 0;
    do {
      const params=new URLSearchParams({bundle_id:args.bundle_id,generation:this.manifest.generation,
        page_cursor:String(cursor),include_unverified:String(Boolean(args.include_unverified))});
      if (args.package) params.set("package",args.package);
      const response=await fetch(this.api+"/catalog/patches?"+params);
      if (!response.ok) {
        const error = new CatalogError("Patch metadata unavailable; retry",response.status);
        error.generationUnavailable = response.status === 409;
        throw error;
      }
      const page=await response.json();
      rows.push(...page.rows);cursor=page.next_cursor;
      if (!args.all) return {...page,rows};
    } while(cursor !== null);
    return {rows,next_cursor:null,candidate_pages:1};
  }
  async bundles({sources = []} = {}) {
    if (!this.bundleRows.length) this.bundleRows = await this.bundlePages();
    for (const source of new Set(sources)) await this.historyBundles(source);
    return this.bundleRows;
  }
  async historyBundles(source) {
    if (!this.histories.has(source)) this.histories.set(source,(async()=>{
      const rows = await this.bundlePages(source);
      const merged = new Map(this.bundleRows.map(row=>[row.id,row]));
      for (const row of rows) merged.set(row.id,row);
      this.bundleRows = [...merged.values()];
      return rows;
    })().catch(error=>{this.histories.delete(source);throw error;}));
    return this.histories.get(source);
  }
}
