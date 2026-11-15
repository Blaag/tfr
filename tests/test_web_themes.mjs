import assert from "node:assert/strict";
import test from "node:test";

import { applyTheme, normalizeTheme, THEME_PALETTES } from "../src/tfr/web/themes.mjs";

test("exposes every terminal theme preset to the PWA", () => {
  assert.deepEqual(Object.keys(THEME_PALETTES).sort(), [
    "1976",
    "abyss",
    "carbon",
    "catppuccin-frappe",
    "catppuccin-latte",
    "catppuccin-macchiato",
    "catppuccin-mocha",
    "default",
    "dracula",
    "gruvbox",
    "kanagawa",
    "nightfly",
    "nord",
    "oled",
    "solarized-dark",
    "tokyo-night",
  ]);
  assert.equal(normalizeTheme("unknown"), "default");
});

test("applies semantic palette roles and light color scheme", () => {
  const values = new Map();
  const root = {
    dataset: {},
    style: {
      colorScheme: "",
      setProperty(key, value) { values.set(key, value); },
    },
  };
  const meta = { content: "" };

  const selected = applyTheme(root, "catppuccin-latte", meta);

  assert.equal(selected, "catppuccin-latte");
  assert.equal(root.dataset.theme, "catppuccin-latte");
  assert.equal(root.style.colorScheme, "light");
  assert.equal(values.get("--ink"), "#eff1f5");
  assert.equal(values.get("--signal"), "#1e66f5");
  assert.equal(meta.content, "#eff1f5");
});

test("deep dark themes use near-black panels", () => {
  for (const name of ["oled", "abyss", "carbon"]) {
    const values = new Map();
    const root = {
      dataset: {},
      style: {
        colorScheme: "",
        setProperty(key, value) { values.set(key, value); },
      },
    };

    applyTheme(root, name);

    assert.equal(root.style.colorScheme, "dark");
    assert.match(values.get("--panel"), /82%/);
    assert.match(values.get("--panel-2"), /62%/);
  }
});
