// Save independent browsing preferences without requiring browser storage.
const KEY = "patch-bundle-registry:browsing:v1";
function normalize(value, grid, tab) {
    const state = value && typeof value === "object" ? value : {};
    const text = key => typeof state[key] === "string" ? state[key].slice(0, 512) : "";
    return {
        search: text("search"), package: text("package"), source: text("source"),
        version: tab === "bundles" ? text("version") : "", type: text("type").replace(/^ReSeam/, "Reseam"),
        release: ["all", "latest", "release", "prerelease"].includes(state.release) ? state.release : "latest",
        grid: typeof state.grid === "boolean" ? state.grid : grid
    };
}
export function createBrowsePreferences(getStorage, defaultGrid = true) {
    let saved;
    try { saved = JSON.parse(getStorage()?.getItem(KEY) || "null"); } catch { /* Storage is optional. */ }
    const tabs = Object.fromEntries(["bundles", "apps"].map(tab =>
        [tab, normalize(saved?.tabs?.[tab], defaultGrid, tab)]));
    let activeTab = saved?.activeTab === "apps" ? "apps" : "bundles";
    function persist() {
        try { getStorage()?.setItem(KEY, JSON.stringify({ activeTab, tabs })); } catch { /* Keep in-memory state. */ }
    }
    return {
        get activeTab() { return activeTab; },
        read(tab) { return { ...tabs[tab] }; },
        save(tab, state) { tabs[tab] = normalize(state, defaultGrid, tab); persist(); },
        activate(tab) { activeTab = tab === "apps" ? "apps" : "bundles"; persist(); }
    };
}
