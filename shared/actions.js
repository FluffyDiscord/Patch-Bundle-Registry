// Shared accessible icon actions for cards and detail dialogs.
const paths = {
    download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    link: '<path d="m10 13 4-4m-7 7-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m0 7a4 4 0 0 0 6 0l4-4a4 4 0 0 0-6-6l-1 1" transform="translate(2 2) scale(.85)"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M15 8V4H4v11h4"/>',
    repository: '<path d="M5 3h14v18H5zM8 7h8M8 11h8M8 15h4"/>',
    signature: '<path d="m12 3 8 4v5c0 5-8 9-8 9s-8-4-8-9V7zM8 12l3 3 5-6"/>'
};
export function iconAction(kind, label, escape, {href, copy, disabled = false} = {}) {
    const tag = href ? "a" : "button";
    const target = href ? `href="${escape(href)}" target="_blank" rel="noopener noreferrer"` :
        `type="button" ${disabled ? "disabled" : ""} data-copy-text="${escape(copy ?? "")}"`;
    return `<${tag} class="icon-action" ${target} aria-label="${escape(label)}" data-tooltip="${escape(label)}">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[kind]}</svg>
        <span class="action-tooltip" role="tooltip">${escape(label)}</span></${tag}>`;
}
export function bindActions(container) {
    container.querySelectorAll(".icon-action").forEach(action => {
        let holdTimer, hideTimer, held = false;
        const clearHold = () => clearTimeout(holdTimer);
        const placeTooltip = () => {
            // Virtual rows are transformed; card labels stay relative to their buttons.
            if (!action.closest?.("dialog")) return;
            const rect = action.getBoundingClientRect?.();
            if (!rect) return;
            const tip = action.querySelector(".action-tooltip");
            // Fixed positioning keeps labels outside scrolling/overflow clips.
            tip.style.position = "fixed";
            tip.style.right = "auto";
            tip.style.width = "max-content";
            tip.style.maxWidth = Math.min(280, window.innerWidth - 16) + "px";
            tip.style.whiteSpace = "normal";
            const size = tip.getBoundingClientRect();
            tip.style.left = Math.max(8, Math.min(rect.right - size.width, window.innerWidth - size.width - 8)) + "px";
            tip.style.top = Math.max(8, rect.bottom + size.height + 12 <= window.innerHeight
                ? rect.bottom + 6 : rect.top - size.height - 6) + "px";
        };
        action.addEventListener("pointerenter", placeTooltip);
        action.addEventListener("focus", placeTooltip);
        action.addEventListener("pointerdown", event => {
            if (event.pointerType === "mouse") return;
            held = false;
            clearTimeout(hideTimer);
            holdTimer = setTimeout(() => { held = true; placeTooltip(); action.classList.add("tooltip-visible"); }, 500);
        });
        action.addEventListener("pointerup", () => {
            clearHold();
            hideTimer = setTimeout(() => action.classList.remove("tooltip-visible"), 1800);
        });
        action.addEventListener("pointercancel", () => {
            clearHold(); held = false; action.classList.remove("tooltip-visible");
        });
        action.addEventListener("pointerleave", clearHold);
        action.addEventListener("contextmenu", event => { if (held) event.preventDefault(); });
        action.addEventListener("click", async event => {
            // Actions embedded in a disclosure header must not toggle its contents.
            event.stopPropagation();
            if (held) { event.preventDefault(); held = false; return; }
            if (!action.hasAttribute("data-copy-text")) return;
            event.preventDefault();
            const tip = action.querySelector(".action-tooltip");
            let message;
            try { await navigator.clipboard.writeText(action.dataset.copyText); message = "Copied!"; }
            catch { message = "Unable to copy"; }
            tip.textContent = message;
            action.setAttribute("aria-label", message);
            placeTooltip();
            action.classList.add("tooltip-visible");
            clearTimeout(hideTimer);
            hideTimer = setTimeout(() => {
                tip.textContent = action.dataset.tooltip;
                action.setAttribute("aria-label", action.dataset.tooltip);
                action.classList.remove("tooltip-visible");
            }, 1800);
        });
    });
}

