const esbuild = require("esbuild");
const path = require("path");

const production = process.argv.includes("--production");
const watch = process.argv.includes("--watch");

// Resolve paths relative to this file so the script works regardless of
// whether npm runs it from SerenMemoryVSCode/ or seren_memory-vscode/.
const root = __dirname;

/** @type {import('esbuild').BuildOptions} */
const buildOptions = {
  entryPoints: [path.join(root, "src/extension.ts")],
  bundle: true,
  outfile: path.join(root, "dist/extension.js"),
  external: ["vscode"],
  format: "cjs",
  platform: "node",
  target: "node18",
  sourcemap: !production,
  minify: production,
  logLevel: "info",
};

if (watch) {
  esbuild.context(buildOptions).then((ctx) => {
    ctx.watch();
    console.log("Watching for changes...");
  });
} else {
  esbuild.build(buildOptions).catch(() => process.exit(1));
}
