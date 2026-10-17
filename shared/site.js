import { canonicalBundleType, compareReleaseDates } from "./catalog.js";

export function countLabel(count, singular, plural = singular + "s") {
    return Number(count).toLocaleString() + " " + (Number(count) === 1 ? singular : plural);
}

// Website-only presentation rules; API is_latest keeps its category semantics.
export function latestReleaseIds(bundles) {
    const newest = new Map();
    for (const bundle of bundles.filter(bundle => bundle.is_latest)) {
        const key = bundle.source_id || bundle.source?.url;
        const previous = newest.get(key);
        if (!previous || compareReleaseDates(bundle, previous) < 0 ||
            (compareReleaseDates(bundle, previous) === 0 &&
             bundle.channels?.includes("latest") && !previous.channels?.includes("latest"))) {
            newest.set(key, bundle);
        }
    }
    return new Set([...newest.values()].map(bundle => bundle.id));
}

export function catalogStats(bundles) {
    const stats = Object.fromEntries(["All bundles", "ReVanced", "Morphe", "Reseam"]
        .map(name => [name, { bundles: 0, patches: 0 }]));
    const latest = latestReleaseIds(bundles);
    for (const bundle of bundles.filter(bundle => latest.has(bundle.id))) {
        const type = canonicalBundleType(bundle.bundle_type) || "";
        const family = type.startsWith("ReVanced:") ? "ReVanced" :
            type.startsWith("Morphe:") ? "Morphe" :
            type === "Reseam" || type.startsWith("Reseam:") ? "Reseam" : null;
        for (const name of ["All bundles", family].filter(Boolean)) {
            stats[name].bundles++;
            stats[name].patches += Math.max(0, Number(bundle.patch_count) || 0);
        }
    }
    return stats;
}

export function patchDisplayName(patch) {
    return [patch.name, patch.display_name].find(value =>
        typeof value === "string" && value.trim()) || (patch.hidden === true ? "Internal patch" : "Name unavailable");
}

export function patchMatchesSearch(patch, query) {
    const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const text = [patch.name, patch.display_name, patch.description,
        ...(patch.compatiblePackages || []).flatMap(pkg => [pkg.name, pkg.appName || pkg.app_name, ...(pkg.versions || [])])]
        .filter(value => value != null).join(" ").toLowerCase();
    return terms.every(term => text.includes(term));
}

export function repositoryNames(bundle) {
    let tracked;
    try { tracked = decodeURIComponent(new URL(bundle.sourceUrl).pathname).replace(/^\/+|\/+$/g, ""); }
    catch { tracked = [bundle.ownerName, bundle.repoName].filter(Boolean).join("/"); }
    const current = bundle.ownerName && bundle.repoName ? bundle.ownerName + "/" + bundle.repoName : "";
    // Show the current repository name once, including after a rename.
    return { tracked: current || tracked, renamed: "" };
}

export function matchesReleaseFilter(bundle, filter) {
    if (filter === "latest") return bundle.isLatest;
    if (filter === "release") return !bundle.isPrerelease;
    if (filter === "prerelease") return bundle.isPrerelease;
    return true;
}
