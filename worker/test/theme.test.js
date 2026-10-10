import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

const script = readFileSync(new URL("../../web/theme.js", import.meta.url), "utf8");
const key = "patch-bundle-registry-theme";

function page({ readyState = "loading", stored = null, dark = true, blocked = false } = {}) {
  const attributes = new Map();
  const button = Object.assign(new EventTarget(), {
    setAttribute: (key, value) => attributes.set(key, value),
    getAttribute: key => attributes.get(key)
  });
  const document = Object.assign(new EventTarget(), {
    readyState, documentElement: { dataset: {} },
    getElementById: () => document.readyState === "loading" ? null : button
  });
  const media = Object.assign(new EventTarget(), { matches: dark });
  const storage = new Map(stored === null ? [] : [[key, stored]]);
  runInNewContext(script, { document, window: new EventTarget(), matchMedia: () => media,
    localStorage: {
      getItem: key => { if (blocked) throw Error("Storage disabled"); return storage.get(key); },
      setItem: (key, value) => { if (blocked) throw Error("Storage disabled"); storage.set(key, value); }
    }
  });
  return { document, button, attributes, media, storage,
    theme: () => document.documentElement.dataset.theme,
    parsed: () => {
      document.readyState = "interactive";
      document.dispatchEvent(new Event("readystatechange"));
    },
    click: () => button.dispatchEvent(new Event("click")) };
}

test("theme toggle works after HTML parsing while catalog modules are still pending", () => {
  const view = page();
  assert.equal(view.theme(), "dark"); // Applied before the button exists.
  view.parsed(); // DOMContentLoaded is deliberately never dispatched.
  assert.equal(view.attributes.get("aria-pressed"), "true");
  assert.equal(view.button.title, "Switch to light mode");
  view.click();
  assert.equal(view.theme(), "light");
  assert.equal(view.storage.get(key), "light");
  // Later document readiness changes must not install a second click handler.
  view.document.readyState = "complete";
  view.document.dispatchEvent(new Event("readystatechange"));
  view.click();
  assert.equal(view.theme(), "dark");
});

test("a theme script loaded after parsing initializes immediately and respects saved preference", () => {
  const view = page({ readyState: "interactive", stored: "light", dark: true });
  assert.equal(view.theme(), "light");
  assert.equal(view.button.title, "Switch to dark mode");
  view.media.dispatchEvent(new Event("change"));
  assert.equal(view.theme(), "light");
  view.click();
  assert.equal(view.storage.get(key), "dark");
  assert.equal(page({ stored: view.storage.get(key), dark: false }).theme(), "dark");
});

test("theme toggles remain usable when browser storage is disabled", () => {
  const view = page({ blocked: true, dark: false });
  view.parsed();
  view.click();
  assert.equal(view.theme(), "dark");
  assert.equal(view.attributes.get("aria-label"), "Switch to light mode");
  view.click();
  assert.equal(view.theme(), "light");
});
