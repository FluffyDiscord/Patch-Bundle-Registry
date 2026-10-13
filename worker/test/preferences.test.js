import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { runInNewContext } from "node:vm";
import { createBrowsePreferences } from "../../shared/preferences.js";
import { countLabel, matchesReleaseFilter } from "../../shared/site.js";
import { compareReleaseDates } from "../../shared/catalog.js";

function storage() {
    let value;
    return { getItem: () => value, setItem: (key, next) => { value = next; } };
}
test("Browsing preferences survive reload with independent tab searches and filters", () => {
    const saved = storage(), prefs = createBrowsePreferences(() => saved);
    prefs.save("bundles", {search:"Hide Ads", package:"com.video@19.16", source:"https://github.com/a/b",
        version:"v1", type:"Morphe:V1", release:"all", grid:false});
    prefs.activate("apps");
    prefs.save("apps", {search:"Video App", package:"20.0", type:"Reseam", release:"latest", grid:true, version:"ignored"});
    const reload = createBrowsePreferences(() => saved);
    assert.equal(reload.activeTab, "apps");
    assert.equal(reload.read("bundles").search, "Hide Ads");
    assert.equal(reload.read("bundles").version, "v1");
    assert.equal(reload.read("apps").search, "Video App");
    assert.equal(reload.read("apps").version, "");
    assert.equal(reload.read("apps").release, "latest");
    reload.read("bundles").search = "mutated";
    assert.equal(reload.read("bundles").search, "Hide Ads");
});
test("Malformed or blocked storage preserves usable defaults and in-memory tab state", () => {
    for (const getStorage of [() => ({getItem: () => "{"}),
        () => { throw new Error("Blocked"); },
        () => ({getItem: () => null, setItem: () => { throw new Error("Full"); }})]) {
        const prefs = createBrowsePreferences(getStorage, false);
        assert.equal(prefs.read("apps").grid, false);
        prefs.save("bundles", {search:"ads", release:"invalid"});
        prefs.activate("apps");
        assert.equal(prefs.read("bundles").search, "ads");
        assert.equal(prefs.read("bundles").release, "latest");
    }
});
test("Count labels use singular for one and plural for zero or many", () => {
    for (const [noun, plural] of [["bundle","bundles"], ["app group","app groups"],
        ["source","sources"], ["patch","patches"]]) {
        assert.equal(countLabel(0, noun, plural), "0 " + plural);
        assert.equal(countLabel(1, noun, plural), "1 " + noun);
        assert.equal(countLabel(2, noun, plural), "2 " + plural);
    }
});
function node(id = "") {
    const classes = new Set();
    return {id, value:"", options:[], dataset:{}, handlers:{},
        classList:{contains: x => classes.has(x), toggle(x, enabled) {
            if (enabled) classes.add(x); else classes.delete(x);
        }},
        setAttribute(){}, replaceChildren(...options) { this.options = options; },
        addEventListener(event, fn) { this.handlers[event] = fn; },
        click() { this.handlers.click(); }, focus() {}
    };
}
async function frontend(prefs, suppliedSummaries) {
    const source = await readFile(new URL("../../web/app.js", import.meta.url), "utf8");
    const tabs = [node("bundles-tab"),node("apps-tab")], fields = {};
    const url = "https://github.com/a/b";
    const summaries = suppliedSummaries || [
        {id:"new", version:"v2", source:{url}, bundle_type:"Morphe:V1", is_latest:true},
        {id:"old", version:"v1", source:{url}, bundle_type:"Morphe:V1", is_latest:false}
    ];
    const transformBundle = b => ({...b, sourceUrl:b.source.url,
        bundleType:b.bundle_type, isLatest:b.is_latest, isPrerelease:Boolean(b.is_prerelease)});
    const context = {document:{querySelectorAll:()=>tabs,querySelector:()=>node(),
        getElementById:id=>fields[id]??=node(id)},results:node(),container:node(),viewToggle:node(),
        input:node(),packageFilter:node(),sourceFilter:node(),bundleVersionFilter:node(),bundleTypeFilter:node(),
        filterButtons:["all","latest","release","prerelease"].map(filter=>Object.assign(node(),{dataset:{filter}})),
        bundleSelection:0,sourceSelection:0,sourcePicker:{sync(){}}, browsePreferences:prefs,currentTab:prefs.activeTab,
        summaries,latestBundles:[transformBundle(summaries[0])],sourceVersions:[],allBundles:[],catalog:{},
        transformBundle,compareReleaseDates,matchesReleaseFilter,bundleTypeLabel:x=>x,
        Option: function(label,value) { this.label=label; this.value=value; },
        clearVirtualizer(){},renderFilteredBundles(){context.saveBrowsePreferences();}};
    runInNewContext(source.slice(source.indexOf("async function selectSource("),
        source.indexOf("\nfunction transformPatch(")), context);
    // Exercise the production selection/reconciliation functions, not a parallel implementation.
    runInNewContext(source.slice(source.indexOf("function reconcileSourceSelection("),
        source.indexOf("\nfunction installAvatarFallback(")), context);
    runInNewContext(source.slice(source.indexOf("function saveBrowsePreferences("),
        source.indexOf("\nvoid loadBundles();")), context);
    return {context,tabs};
}
test("Switching tabs restores controls and a selected historical release", async () => {
    const prefs = createBrowsePreferences(() => storage());
    prefs.save("bundles", {search:"Bundle Search",package:"com.video",source:"https://github.com/a/b",
        version:"v1",type:"Morphe:V1",release:"all",grid:false});
    const {context:c,tabs} = await frontend(prefs);
    assert.equal(c.input.value,"Bundle Search");
    assert.equal(c.currentBundleVersion,"v1");
    assert.equal(c.allBundles[0].id,"old");
    tabs[1].click();
    assert.equal(c.input.value,"");
    assert.equal(c.currentSourceUrl,"");
    assert.equal(c.currentBundleVersion,"");
    c.input.value="Video App";
    c.packageFilter.value="19.16";
    c.currentFilter="latest";
    tabs[0].click();
    assert.equal(c.input.value,"Bundle Search");
    assert.equal(c.packageFilter.value,"com.video");
    assert.equal(c.currentBundleVersion,"v1");
    assert.equal(c.allBundles[0].id,"old");
    tabs[1].click();
    assert.equal(c.input.value,"Video App");
    assert.equal(c.packageFilter.value,"19.16");
    assert.equal(c.currentFilter,"latest");
});
test("Removed sources, versions and bundle types do not restore hidden active filters", async () => {
    const prefs = createBrowsePreferences(() => storage());
    prefs.save("bundles", {source:"https://github.com/deleted/repo",version:"v0",type:"Removed",release:"all"});
    const {context:c} = await frontend(prefs);
    assert.equal(c.currentSourceUrl,"");
    assert.equal(c.currentBundleVersion,"");
    assert.equal(c.currentBundleType,"");
    assert.equal(c.bundleVersionFilter.disabled,true);
    assert.equal(c.allBundles[0].id,"new");
});


test("Switching sources rebuilds versions after clearing a retired bundle type", async () => {
    const url = "https://github.com/a/b", other = "https://github.com/c/d";
    const prefs = createBrowsePreferences(() => storage());
    prefs.save("bundles", {source:url,type:"ReVanced:v21",release:"all",version:"v1"});
    const {context:c} = await frontend(prefs, [
        {id:"new",version:"v2",source:{url},bundle_type:"Morphe:V1",is_latest:true},
        {id:"old",version:"v1",source:{url},bundle_type:"ReVanced:v21",is_latest:false},
        {id:"other",version:"v3",source:{url:other},bundle_type:"Morphe:V1",is_latest:true}
    ]);
    assert.equal(c.currentBundleType,"ReVanced:v21");
    await c.selectSource(other);
    assert.equal(c.currentBundleType,"");
    assert.equal(c.currentBundleVersion,"");
    assert(c.bundleVersionFilter.options.some(option => option.value === "v3"));
    assert.equal(prefs.read("bundles").source,other);
});

test("Slow version details cannot overwrite a newer selection or a changed source",async()=>{
    for(const action of ["version","source","tab"]) {
        const prefs=createBrowsePreferences(()=>storage());
        prefs.save("bundles",{source:"https://github.com/a/b",release:"all"});
        const {context:c,tabs}=await frontend(prefs);
        let resolve;
        c.catalog={historyBundles:async()=>[],file:()=>new Promise(done=>{resolve=done;})};
        const pending=c.selectBundleVersion("v1");
        if(action==="version") await c.selectBundleVersion("");
        if(action==="source") await c.selectSource("");
        if(action==="tab") tabs[1].click();
        resolve({description:"Old release details"});
        await pending;
        assert.equal(c.currentBundleVersion,"",action);
        assert.equal(prefs.read(action==="tab"?"apps":"bundles").version,"",action);
    }
});

test("A failed stale version request cannot replace the current status",async()=>{
    const prefs=createBrowsePreferences(()=>storage());
    prefs.save("bundles",{source:"https://github.com/a/b",release:"all"});
    const {context:c}=await frontend(prefs);
    let reject;
    c.status={textContent:"Current view"};
    c.recoverGeneration=async()=>false;
    c.catalog={historyBundles:async()=>[],file:()=>new Promise((_,fail)=>{reject=fail;})};
    const pending=c.selectBundleVersion("v1");
    await c.selectBundleVersion("");
    reject(new Error("Old request failed"));
    await pending;
    assert.equal(c.status.textContent,"Current view");
});


test("A pending source history cannot clear a version restored by tab navigation",async()=>{
    const prefs=createBrowsePreferences(()=>storage());
    const url="https://github.com/a/b";
    prefs.save("bundles",{source:url,release:"all"});
    const {context:c,tabs}=await frontend(prefs);
    let resolve;
    c.catalog={historyBundles:()=>new Promise(done=>{resolve=done;})};
    const pending=c.selectSource(url);
    tabs[1].click();
    prefs.save("bundles",{source:url,version:"v1",release:"all"});
    tabs[0].click();
    assert.equal(c.currentBundleVersion,"v1");
    resolve([]);
    await pending;
    assert.equal(c.currentBundleVersion,"v1");
    assert.equal(prefs.read("bundles").version,"v1");
});

test("A source error awaiting generation recovery cannot overwrite a newer view",async()=>{
    const prefs=createBrowsePreferences(()=>storage());
    const url="https://github.com/a/b";
    prefs.save("bundles",{source:url,release:"all"});
    const {context:c}=await frontend(prefs);
    let finishRecovery,started;
    const recovering=new Promise(done=>{started=done;});
    c.status={textContent:"Current view"};
    c.recoverGeneration=()=>{started();return new Promise(done=>{finishRecovery=done;});};
    c.catalog={historyBundles:async()=>{throw new Error("Old request failed");}};
    const pending=c.selectSource(url);
    await recovering;
    await c.selectSource("");
    finishRecovery(false);
    await pending;
    assert.equal(c.status.textContent,"Current view");
});
