// Fails when web/src uses raw Tailwind palette classes instead of theme tokens.
// Run: node scripts/no-raw-colors.mjs (from web/).
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = fileURLToPath(new URL("../src", import.meta.url));
const RAW = /(?<![\w-])(?:[\w-]+:)*(?:[a-z]+-)+(?:zinc|slate|gray|red|green|amber|blue)-\d+(?:\/\d+)?(?![\w-])|(?<![\w-])(?:[\w-]+:)*bg-white(?![\w-])/g;

function* files(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) yield* files(path);
    else if (/\.(tsx?|jsx?|mjs|css)$/.test(name)) yield path;
  }
}

let hits = 0;
for (const file of files(SRC)) {
  readFileSync(file, "utf8")
    .split("\n")
    .forEach((line, i) => {
      for (const m of line.matchAll(RAW)) {
        hits++;
        console.log(`${relative(process.cwd(), file)}:${i + 1}: ${m[0]}`);
      }
    });
}
if (hits) {
  console.error(`\n${hits} raw colour class(es). Use the theme tokens in globals.css instead.`);
  process.exit(1);
}
console.log("no-raw-colors: OK, no raw palette classes in src/");
