// Checks WCAG AA contrast for the theme token pairs in src/app/globals.css.
// Run: node scripts/contrast-check.mjs (from web/). Exits 1 on any failure.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const CSS = readFileSync(fileURLToPath(new URL("../src/app/globals.css", import.meta.url)), "utf8");
const BLOCKS = {
  plain: /(?:^|\n):root,\s*\[data-theme="plain"\]\s*\{([^}]*)\}/,
  parchment: /(?:^|\n)\[data-theme="parchment"\]\s*\{([^}]*)\}/,
};

// [text, background, minimum ratio]; "name@0.75" blends text over the background at that alpha.
const PAIRS = [
  ["ink", "surface", 4.5],
  ["ink", "surface-raised", 4.5],
  ["ink", "surface-muted", 4.5],
  ["ink-muted", "surface", 4.5],
  ["ink-muted", "surface-raised", 4.5],
  ["ink-muted", "surface-muted", 4.5],
  ["ink-faint", "surface", 4.5],
  ["ink-faint", "surface-raised", 4.5],
  ["accent-ink", "accent", 4.5],
  ["accent-ink", "accent-hover", 4.5],
  ["accent-ink@0.75", "accent", 4.5],
  ["success", "success-surface", 4.5],
  ["success", "surface", 4.5],
  ["warning", "warning-surface", 4.5],
  ["warning", "surface", 4.5],
  ["warning", "surface-raised", 4.5],
  ["danger", "danger-surface", 4.5],
  ["danger", "surface", 4.5],
  ["danger", "surface-raised", 4.5],
  ["focus", "surface", 3],
  ["focus", "surface-raised", 3],
];
// Parchment-only pairs: ribbon tab counts and the wax-seal fit score.
const EXTRA = { parchment: [["ink-faint", "surface-muted", 4.5], ["accent-ink", "danger", 4.5]] };

function tokens(name) {
  const m = CSS.match(BLOCKS[name]);
  if (!m) throw new Error(`theme block "${name}" not found in globals.css`);
  return Object.fromEntries([...m[1].matchAll(/--([\w-]+):\s*(#[0-9a-f]{6})\s*;/gi)].map(([, k, v]) => [k, v]));
}

const rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
const lin = (c) => ((c /= 255) <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const lum = ([r, g, b]) => 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
const blend = (fg, bg, a) => fg.map((c, i) => Math.round(c * a + bg[i] * (1 - a)));

let failed = 0;
for (const theme of Object.keys(BLOCKS)) {
  const t = tokens(theme);
  console.log(`\n${theme}`);
  for (const [fgName, bgName, min] of [...PAIRS, ...(EXTRA[theme] ?? [])]) {
    const [name, alpha] = fgName.split("@");
    if (!t[name] || !t[bgName]) throw new Error(`${theme}: missing --${name} or --${bgName}`);
    const bg = rgb(t[bgName]);
    const fg = alpha ? blend(rgb(t[name]), bg, Number(alpha)) : rgb(t[name]);
    const [hi, lo] = [lum(fg), lum(bg)].sort((a, b) => b - a);
    const ratio = (hi + 0.05) / (lo + 0.05);
    const ok = ratio >= min;
    if (!ok) failed++;
    console.log(`  ${ok ? "PASS" : "FAIL"}  ${ratio.toFixed(2).padStart(5)} (min ${min})  ${fgName} on ${bgName}`);
  }
}
if (failed) {
  console.error(`\n${failed} pair(s) below WCAG AA.`);
  process.exit(1);
}
console.log("\ncontrast-check: all pairs meet WCAG AA");
