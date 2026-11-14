// Frontend smoke test: bundles the real components for Node and server-renders every page + Markdown safety checks.
//   cd web && npm install && node test/run.mjs
import { build } from "esbuild";
import { execFileSync } from "node:child_process";
await build({ entryPoints: ["test/render_smoke.jsx"], bundle: true, outfile: "test/.out.cjs", platform: "node", format: "cjs", jsx: "automatic", logLevel: "error", define: { "process.env.NODE_ENV": '"production"' } });
const out = JSON.parse(execFileSync("node", ["test/.out.cjs"]).toString());
const bad = Object.entries(out).filter(([, v]) => v !== true);
console.log(JSON.stringify(out, null, 1));
if (bad.length) { console.error("FAILED:", bad.map(([k]) => k).join(", ")); process.exit(1); }
console.log("frontend smoke test: all checks passed");
