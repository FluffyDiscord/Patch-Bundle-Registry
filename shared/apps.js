import { CatalogError } from "./catalog.js";

// One application can occur in several versions of the same source.
export function appVersionMatches(reference, query) {
  if (!query) return true;
  return reference.unrestricted || reference.versions.some(version =>
    String(version).toLowerCase().includes(query.toLowerCase()));
}

export function filterApps(apps, bundles, { q = "", packageQuery = "", appVersion = "", exactAppVersion = false } = {}) {
  if (q.length > 512 || packageQuery.length > 512 || appVersion.length > 512)
    throw new CatalogError("Search text too long");
  const eligible = new Map(bundles.map(bundle => [bundle.id, bundle]));
  const terms = q.toLowerCase().trim().split(/\s+/).filter(Boolean);
  const [packagePart, exactVersion] = packageQuery.toLowerCase().split("@", 2);
  return apps.flatMap(app => {
    // Some legacy lists contain human labels instead of Android package IDs.
    if (!/^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+$/.test(app.package_name)) return [];
    const text = (app.app_name + " " + app.package_name).toLowerCase();
    if (!terms.every(term => text.includes(term))) return [];
    const nameMatches = app.package_name.toLowerCase().includes(packagePart);
    const references = app.bundles.filter(reference => {
      if (!eligible.has(reference.bundle_id)) return false;
      if (exactAppVersion && appVersion && !reference.unrestricted &&
          !reference.versions.includes(appVersion)) return false;
      if (!exactAppVersion && !appVersionMatches(reference, appVersion)) return false;
      if (exactVersion !== undefined) return nameMatches && (!exactVersion ||
        reference.unrestricted || reference.versions.some(version =>
          String(version).toLowerCase() === exactVersion));
      return !packageQuery || nameMatches || reference.versions.some(version =>
        String(version).toLowerCase().includes(packagePart));
    }).map(reference => ({ ...reference,
      metadata_status: eligible.get(reference.bundle_id).metadata_status }));
    if (!references.length) return [];
    return [{ ...app, id: app.package_name, bundles: references,
      bundle_count: references.length,
      source_count: new Set(references.map(reference => eligible.get(reference.bundle_id).source_id)).size,
      patch_count: references.some(reference => reference.compatibility_unknown) ? null :
        references.reduce((total, reference) => total + reference.patch_count, 0),
      versions: [...new Set(references.flatMap(reference => reference.versions))].sort(),
      unrestricted: references.some(reference => reference.unrestricted) }];
  }).sort((left, right) => left.app_name.localeCompare(right.app_name) ||
    left.package_name.localeCompare(right.package_name));
}
