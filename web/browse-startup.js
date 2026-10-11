/* SPDX-License-Identifier: GPL-3.0-only */
// Restore controls before CDN/module loading so the HTML defaults never flash.
(() => {
    const root = document.documentElement;
    root.setAttribute("data-browse-pending", "");
    let saved;
    try { saved = JSON.parse(localStorage.getItem("patch-bundle-registry:browsing:v1") || "null"); }
    catch { /* Storage is optional. */ }
    const tab = saved?.activeTab === "apps" ? "apps" : "bundles";
    const state = saved?.tabs?.[tab] || {};
    const text = key => typeof state[key] === "string" ? state[key].slice(0, 512) : "";
    const release = ["all", "latest", "release", "prerelease"].includes(state.release) ? state.release : "release";
    const grid = typeof state.grid === "boolean" ? state.grid : !matchMedia("(max-width: 700px)").matches;
    function restore() {
        const byId = id => document.getElementById(id);
        byId("search").value = text("search");
        byId("search").placeholder = tab === "apps" ? "Search apps by name or package..." : "Search bundles by source URL or name...";
        byId("search").setAttribute("aria-label", tab === "apps" ? "Search apps" : "Search bundles");
        byId("package-filter").value = text("package");
        for (const [id, value] of [["source-filter", text("source")], ["bundle-type-filter", text("type")],
            ["bundle-version-filter", tab === "bundles" ? text("version") : ""]]) {
            const control = byId(id);
            const label = id === "bundle-type-filter"
                ? value.startsWith("Morphe:") ? "Morphe" : value.startsWith("ReSeam") ? "ReSeam"
                    : value.replace(/^ReVanced:v/, "ReVanced v")
                : value;
            if (value) control.appendChild(new Option(label, value));
            control.value = value;
        }
        byId("bundle-version-filter").disabled = !text("source");
        document.querySelectorAll(".filter-btn").forEach(button =>
            button.classList.toggle("active", button.dataset.filter === release));
        document.querySelectorAll(".catalog-tab").forEach(button => {
            const selected = button.id === tab + "-tab";
            button.classList.toggle("active", selected);
            button.setAttribute("aria-selected", String(selected));
            button.tabIndex = selected ? 0 : -1;
        });
        byId("results").setAttribute("aria-labelledby", tab + "-tab");
        byId("results").classList.toggle("grid-view", grid);
        document.querySelector(".container").classList.toggle("grid-view-enabled", grid);
        byId("view-toggle").setAttribute("aria-pressed", String(grid));
        byId("view-toggle").textContent = grid ? "List view" : "Grid view";
        byId("bundle-version-field").hidden = tab === "apps";
        document.querySelector(".filter-row").classList.toggle("apps-filters", tab === "apps");
        byId("apps-note").hidden = tab !== "apps";
        byId("status").textContent = tab === "apps" ? "Loading apps..." : "Loading bundles...";
        root.removeAttribute("data-browse-pending");
    }
    if (document.readyState === "loading")
        document.addEventListener("readystatechange", restore, { once: true });
    else restore();
})();
