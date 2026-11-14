import { build, context } from "esbuild";
import { cpSync, mkdirSync, rmSync, readFileSync, writeFileSync } from "node:fs";

const watch = process.argv.includes("--watch");
rmSync("dist", { recursive: true, force: true });
mkdirSync("dist", { recursive: true });
const options = {
  entryPoints: ["src/main.jsx"],
  bundle: true, minify: !watch, sourcemap: false, target: ["es2020"],
  outfile: "dist/app.js", jsx: "automatic", loader: { ".jsx": "jsx" },
  define: { "process.env.NODE_ENV": '"production"' }, logLevel: "info",
};
if (watch) { const ctx = await context(options); await ctx.watch(); }
else await build(options);
cpSync("src/styles.css", "dist/styles.css");
cpSync("assets", "dist/assets", { recursive: true });
// Self-hosted Poppins (the company site's typeface); the CSP only allows same-origin fonts.
mkdirSync("dist/fonts", { recursive: true });
for (const w of [400, 500, 600, 700]) cpSync(`node_modules/@fontsource/poppins/files/poppins-latin-${w}-normal.woff2`, `dist/fonts/poppins-${w}.woff2`);
writeFileSync("dist/index.html", readFileSync("index.html", "utf8"));
