import { bundleImportPath, reseamNativeImportPath, bundleTypeLabel } from "./catalog.js";
import { patchMatchesSearch, repositoryNames, countLabel } from "./site.js";
import { iconAction, importFormatAction, bindActions } from "./actions.js";
import { groupBundlePatches, filterPatchGroups } from "./details.js";

const disclosureArrow = '<span class="disclosure-arrow" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="m6 9 6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>';
export function readableOptionType(type) {
    const text = String(type || "").replace(/(?:kotlin\.(?:collections\.)?|java\.lang\.|java\.util\.)/g, "").replace(/\?/g, "");
    const names = {Boolean:"Yes / no",String:"Text",Char:"Character",Long:"Whole number",Int:"Whole number",
        Short:"Whole number",Byte:"Whole number",Float:"Decimal number",Double:"Decimal number"};
    if (names[text]) return names[text];
    const list = text.match(/^(?:List|Set|Array|Collection)<(.+)>$/);
    if (list) return "List of " + readableOptionType(list[1]).toLowerCase();
    if (/^(?:Map|HashMap)</.test(text)) return "Named values";
    return text || "Unspecified";
}
export function readableOptionValue(value, depth = 0) {
    if (value === undefined) return "Not provided";
    if (value === null) return "Not set";
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (value === "") return "Empty text";
    if (depth > 8) return "Nested values";
    if (Array.isArray(value)) return value.length ? value.map(item => "• " + readableOptionValue(item, depth + 1)).join("\n") : "None";
    if (typeof value === "object") {
        const entries = Object.entries(value);
        return entries.length ? entries.map(([key, item]) =>
            key + ": " + readableOptionValue(item, depth + 1)).join("\n") : "None";
    }
    return String(value);
}
export function renderPatchOptions(options, escape, heading = "Patch options", overview = "") {
    options = Array.isArray(options) ? options.filter(option => option && typeof option === "object" && !Array.isArray(option)) : [];
    if (!options.length) return "";
    const label = countLabel(options.length, "patch option");
    return `<details class="patch-options"><summary><span class="patch-heading">
        <span class="patch-name">${heading}</span><span class="option-toggle" title="${label}" aria-label="${label}">
            <span class="option-count">${options.length}</span>${disclosureArrow}</span></span>${overview}</summary>
        <div class="patch-options-list" aria-label="${label}">
        ${options.map(option =>
            `<div class="patch-option"><strong class="patch-option-heading">${escape(option.title || option.name || option.key || "Option")}</strong>
                ${option.description ? `<p>${escape(option.description)}</p>` : ""}
                <dl><div><dt>Type</dt><dd>${escape(readableOptionType(option.type))}</dd></div>
                    <div><dt>Required</dt><dd>${option.required ? "Yes" : "No"}</dd></div>
                    <div><dt>Default</dt><dd class="option-value">${escape(readableOptionValue(option.default))}</dd></div>
                    ${option.values != null ? `<div><dt>Choices</dt><dd class="option-value">${escape(readableOptionValue(option.values))}</dd></div>` : ""}
                </dl></div>`).join("")}</div></details>`;
}

export function createDetailsBrowser({ getApplications, getSummaries, transformBundle,
    bundlePatches, bundleChangelog, renderMarkdown, renderPatch, escapeHtml: escape, installAvatarFallback, formatDate, getApi,
    onNavigate = () => {}, onClose = () => {} }) {
    let dialog, revision = 0, previousFocus;
    function close({ notify = true } = {}) {
        if (!dialog?.open) return;
        revision++;
        dialog.close();
        if (notify) onClose();
    }
    function shell(title, subtitle, icon, description = "", ownerAvatar = "", copyApp = false) {
        revision++;
        if (!dialog) {
            dialog = document.createElement("dialog");
            dialog.className = "catalog-dialog";
            document.body.appendChild(dialog);
            dialog.addEventListener("click", event => { if (event.target === dialog) close(); });
            dialog.addEventListener("cancel", event => { event.preventDefault(); close(); });
            dialog.addEventListener("close", () => {
                // A queued close event must not invalidate a newly opened route.
                if (dialog.open) return;
                revision++;
                document.documentElement.classList.remove("details-open");
                document.querySelectorAll(".card-selected").forEach(card => card.classList.remove("card-selected"));
                if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
                else document.querySelector(".catalog-tab.active")?.focus({ preventScroll: true });
            });
        }
        previousFocus = document.activeElement;
        dialog.innerHTML = `<div class="details-heading">
            <span class="owner-avatar-link app-icon"><img class="owner-avatar" src="${escape(icon || "")}" alt=""></span>
            <div><h2 id="details-title">${escape(title)}${copyApp ? iconAction("copy", "Copy app name", escape, {copy:title}) : ""}</h2>
            <p class="app-package">${escape(subtitle)}${copyApp ? iconAction("copy", "Copy package name", escape, {copy:subtitle}) : ""}</p>
            ${description ? `<p class="details-description">${escape(description)}</p>` : ""}</div>
            <button class="dialog-close" type="button" aria-label="Close details">×</button></div>
            <div class="details-sections" hidden aria-label="Bundle details">
                <button type="button" data-details-section="patches" aria-pressed="true">Apps and patches</button>
                <button type="button" data-details-section="changelog" aria-pressed="false">Changelog</button></div>
            <div class="details-toolbar"><input type="search" class="details-search"
                aria-label="Search apps, bundles and patches" placeholder="Search apps, patches, descriptions, packages or versions...">
                <span class="details-count" aria-live="polite"></span></div>
            <div class="details-body"><p role="status">Loading compatibility and patches...</p></div>
            <div class="details-body details-changelog changelog-content" tabindex="0" aria-label="Release changelog" hidden></div>`;
        bindActions(dialog);
        dialog.setAttribute("aria-labelledby", "details-title");
        dialog.querySelector(".dialog-close").addEventListener("click", close);
        delete dialog.dataset.ownerAvatar;
        if (ownerAvatar && ownerAvatar !== icon) dialog.dataset.ownerAvatar = ownerAvatar;
        installAvatarFallback(dialog, title);
        document.documentElement.classList.add("details-open");
        if (!dialog.open) dialog.showModal();
        dialog.querySelector(".details-search").focus();
        return { token: revision, body: dialog.querySelector(".details-body"),
            search: dialog.querySelector(".details-search"), count: dialog.querySelector(".details-count"),
            toolbar: dialog.querySelector(".details-toolbar"), sections: dialog.querySelector(".details-sections"),
            changelog: dialog.querySelector(".details-changelog") };
    }
    function alive(view) { return view.token === revision && dialog.open; }
    function activateCard(card, open) {
        card.classList.add("interactive-card");
        card.tabIndex = 0;
        card.setAttribute("role", "button");
        card.setAttribute("aria-haspopup", "dialog");
        const activate = () => {
            card.classList.add("card-selected");
            card.focus({ preventScroll: true });
            open();
        };
        card.addEventListener("click", event => {
            if (event.target.closest("a,button,input,select,summary,.changelog-preview,.changelog-content,.patch-options")) return;
            if (window.getSelection()?.toString()) return;
            activate();
        });
        card.addEventListener("keydown", event => {
            if (event.target !== card || !["Enter", " "].includes(event.key)) return;
            event.preventDefault();
            activate();
        });
    }
    function image(icon) {
        return `<span class="owner-avatar-link app-icon"><img class="owner-avatar" src="${escape(icon || "")}" alt="" loading="lazy"></span>`;
    }
    function patchesHtml(patches, query) {
        const matching = query ? patches.filter(patch => patchMatchesSearch(patch, query)) : patches;
        return matching.length ? matching.map(renderPatch).join("") :
            '<p class="patch-empty">No patches match this search.</p>';
    }
    function bindImages(element) {
        element.querySelectorAll(".details-group").forEach(group =>
            installAvatarFallback(group, group.dataset.label || "?"));
    }
    async function openBundle(bundle, { section = "patches", notify = true } = {}) {
        const name = repositoryNames(bundle).tracked;
        const view = shell(name, bundle.version + " · " + bundleTypeLabel(bundle.bundleType),
            bundle.iconUrl, bundle.repoDescription, bundle.ownerAvatarUrl);
        let patchesPending, changelogPending;
        const loadPatches = () => patchesPending ??= (async () => {
            try {
                const patches = await bundlePatches({ ...bundle, appPackage: undefined });
                if (!alive(view)) return;
                const groups = groupBundlePatches(patches, getApplications());
                const render = () => {
                    const matches = filterPatchGroups(groups, view.search.value, patchMatchesSearch);
                    view.count.textContent = countLabel(matches.length, "app group");
                    view.body.innerHTML = matches.map(group =>
                        `<details class="details-group" data-label="${escape(group.app_name)}">
                            <summary>${image(group.icon_url)}<span class="details-group-name">
                                <strong>${escape(group.app_name)}</strong>
                                <span class="app-package">${escape(group.package_name || "Applies across applications")}</span></span>
                                <span class="bundle-badge">${countLabel(group.patches.length, "patch", "patches")}</span>${disclosureArrow}</summary>
                            <div class="details-patches">${group.patches.map(renderPatch).join("")}</div></details>`).join("") ||
                        '<p role="status">No app groups match. This release may have no available compatibility metadata.</p>';
                    if (bundle.metadataStatus !== "verified")
                        view.body.insertAdjacentHTML("afterbegin", '<p class="metadata-note">Compatibility metadata for this release is unverified.</p>');
                    bindImages(view.body);
                    if (view.search.value.trim()) view.body.querySelectorAll("details.details-group").forEach(group => { group.open = true; });
                };
                view.search.addEventListener("input", render);
                render();
            } catch (error) { if (alive(view)) view.body.textContent = "Unable to load patches: " + error.message; }
        })();
        const loadChangelog = () => changelogPending ??= (async () => {
            view.changelog.innerHTML = '<p role="status">Loading changelog...</p>';
            try {
                const description = await bundleChangelog(bundle);
                if (!alive(view)) return;
                view.changelog.innerHTML = description ? renderMarkdown(description) :
                    '<p role="status">No changelog was provided for this release.</p>';
            } catch (error) {
                if (alive(view)) view.changelog.textContent = "Unable to load changelog: " + error.message;
            }
        })();
        const patchesButton = view.sections.querySelector('[data-details-section="patches"]');
        const changelogButton = view.sections.querySelector('[data-details-section="changelog"]');
        const selectSection = (selected, updateUrl = true) => {
            if (updateUrl) onNavigate({ kind: "bundle", id: bundle.id,
                source: bundle.sourceUrl, version: bundle.version, section: selected });
            const changelog = selected === "changelog";
            view.body.hidden = changelog;
            view.toolbar.hidden = changelog;
            view.changelog.hidden = !changelog;
            patchesButton.setAttribute("aria-pressed", String(!changelog));
            changelogButton.setAttribute("aria-pressed", String(changelog));
            return changelog ? loadChangelog() : loadPatches();
        };
        view.sections.hidden = false;
        patchesButton.addEventListener("click", () => { void selectSection("patches"); });
        changelogButton.addEventListener("click", () => { void selectSection("changelog"); });
        const pending = selectSection(section, notify);
        if (section === "changelog") changelogButton.focus();
        await pending;
    }
    function openApp(app, { notify = true } = {}) {
        if (notify) onNavigate({ kind: "app", package: app.package_name });
        const view = shell(app.app_name, app.package_name, app.icon_url, "", "", true);
        const summaries = new Map(getSummaries().map(bundle => [bundle.id, bundle]));
        const groups = app.bundles.flatMap(reference => {
            const summary = summaries.get(reference.bundle_id);
            return summary ? [{ reference, bundle: transformBundle(summary), patches: null, pending: null, error: null }] : [];
        });
        const load = group => {
            if (!group.pending) group.pending = bundlePatches({ ...group.bundle, appPackage: app.package_name })
                .then(patches => { group.patches = patches; })
                .catch(error => { group.error = error.message; });
            return group.pending;
        };
        const opened = new Set();
        const label = group => repositoryNames(group.bundle).tracked;
        function render() {
            if (!alive(view)) return;
            const query = view.search.value.trim();
            const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
            const visible = groups.filter(group => {
                const text = (label(group) + " " + group.bundle.version).toLowerCase();
                return !query || terms.every(term => text.includes(term)) || !group.patches ||
                    group.patches.some(patch => patchMatchesSearch(patch, query));
            });
            view.count.textContent = countLabel(visible.length, "bundle");
            view.body.innerHTML = visible.map(group => {
                const bundle = group.bundle, expanded = opened.has(bundle.id) || Boolean(query);
                const url = new URL(bundleImportPath(bundle, true), getApi()).href;
                return `<details class="details-group" data-bundle="${escape(bundle.id)}" data-label="${escape(label(group))}" data-owner-avatar="${escape(bundle.ownerAvatarUrl || "")}" ${expanded ? "open" : ""}>
                    <summary>${image(bundle.iconUrl)}<span class="details-group-name">
                        <strong>${escape(label(group))}</strong>
                        <span class="app-package">${escape(bundle.version)} · ${escape(bundleTypeLabel(bundle.bundleType))} · ${escape(formatDate(bundle.createdAt))}</span></span>
                        <span class="bundle-badge">${group.reference.compatibility_unknown ? "Count unavailable" : countLabel(group.reference.patch_count, "patch", "patches")}</span>
                        <span class="details-group-actions">
                            ${iconAction("repository", "Open repository", escape, {href:bundle.sourceUrl})}
                            ${bundle.downloadUrl ? iconAction("download", "Download bundle", escape, {href:bundle.downloadUrl}) : ""}
                            ${bundle.bundleType === "Reseam"
                                ? importFormatAction(escape, url,
                                    new URL(reseamNativeImportPath(bundle, true), getApi()).href,
                                    bundle.metadataStatus !== "verified")
                                : iconAction("link", "Copy this version URL", escape, {copy:url})}</span>${disclosureArrow}</summary>
                    ${bundle.metadataStatus !== "verified" ? '<p class="metadata-note">Compatibility metadata for this release is unverified.</p>' : ""}
                    <div class="details-patches">${group.error ? `<p role="status">Unable to load patches: ${escape(group.error)}</p>` :
                        group.patches ? patchesHtml(group.patches, terms.every(term => (label(group) + " " + bundle.version).toLowerCase().includes(term)) ? "" : query) :
                        '<p role="status">Loading patches...</p>'}</div></details>`;
            }).join("") || '<p role="status">No bundles or patches match this search.</p>';
            bindImages(view.body);
            bindActions(view.body);
            view.body.querySelectorAll("[data-bundle]").forEach(element => {
                const group = groups.find(group => group.bundle.id === element.dataset.bundle);
                element.addEventListener("toggle", () => {
                    if (!element.isConnected) return;
                    if (element.open) {
                        opened.add(group.bundle.id);
                        if (!view.search.value.trim() && !group.pending)
                            void load(group).then(() => updateLoadedGroup(group));
                    } else opened.delete(group.bundle.id);
                });
            });
        }
        function updateLoadedGroup(group) {
            if (!alive(view)) return;
            const query = view.search.value.trim();
            const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
            const text = (label(group) + " " + group.bundle.version).toLowerCase();
            const labelMatches = terms.every(term => text.includes(term));
            // Keep summary focus and other groups' option/scroll state intact,
            // including while background search results are arriving.
            const element = view.body.querySelector(`[data-bundle="${group.bundle.id}"]`);
            if (!labelMatches && group.patches &&
                !group.patches.some(patch => patchMatchesSearch(patch, query))) {
                element?.remove();
                const remaining = view.body.querySelectorAll("[data-bundle]").length;
                view.count.textContent = countLabel(remaining, "bundle");
                if (!remaining) view.body.innerHTML = '<p role="status">No bundles or patches match this search.</p>';
                return;
            }
            const target = element?.querySelector(".details-patches");
            if (target) target.innerHTML = group.error
                ? `<p role="status">Unable to load patches: ${escape(group.error)}</p>`
                : patchesHtml(group.patches || [], labelMatches ? "" : query);
        }
        let searchRevision = 0;
        view.search.addEventListener("input", () => {
            const sequence = ++searchRevision;
            render();
            if (!view.search.value.trim()) return;
            const queue = groups.filter(group => !group.pending);
            // Browser reads use the local/published cache, never app metadata providers.
            const worker = async () => {
                while (queue.length && alive(view) && sequence === searchRevision && view.search.value.trim()) {
                    const group = queue.shift();
                    await load(group);
                    updateLoadedGroup(group);
                }
            };
            void Promise.all(Array.from({ length: Math.min(3, queue.length) }, worker));
        });
        render();
    }
    return { activateCard, openBundle, openApp, close };
}
