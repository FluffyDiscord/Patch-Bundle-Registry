/* SPDX-License-Identifier: GPL-3.0-only */
// Apply the theme before styles load, independently of catalog/CDN requests.
(() => {
    const key = "patch-bundle-registry-theme";
    const root = document.documentElement;
    const systemTheme = matchMedia("(prefers-color-scheme: dark)");
    let preference = null;
    try { preference = localStorage.getItem(key); } catch { /* Storage may be disabled. */ }
    if (!["light", "dark"].includes(preference)) preference = null;

    function applyTheme(theme) {
        root.dataset.theme = theme;
        const button = document.getElementById("theme-toggle");
        if (!button) return;
        const dark = theme === "dark";
        button.setAttribute("aria-pressed", String(dark));
        button.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
        button.title = button.getAttribute("aria-label");
    }

    function updateTheme() {
        applyTheme(preference ?? (systemTheme.matches ? "dark" : "light"));
    }
    updateTheme();
    systemTheme.addEventListener("change", updateTheme);

    function initializeToggle() {
        updateTheme();
        document.getElementById("theme-toggle").addEventListener("click", () => {
            preference = root.dataset.theme === "dark" ? "light" : "dark";
            applyTheme(preference);
            try { localStorage.setItem(key, preference); } catch { /* Keep the in-page choice. */ }
        });
    }
    // "interactive" precedes deferred/module execution; CDN imports can delay DOMContentLoaded.
    if (document.readyState === "loading")
        document.addEventListener("readystatechange", initializeToggle, { once: true });
    else initializeToggle();

    window.addEventListener("storage", event => {
        if (event.key !== key && event.key !== null) return;
        preference = ["light", "dark"].includes(event.newValue) ? event.newValue : null;
        updateTheme();
    });
})();
