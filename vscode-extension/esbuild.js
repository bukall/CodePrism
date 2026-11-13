const esbuild = require("esbuild");
const { rmSync } = require("node:fs");

const production = process.argv.includes("--production");
const watch = process.argv.includes("--watch");

async function main() {
  const ctx = await esbuild.context({
    entryPoints: ["src/extension.ts"],
    bundle: true,
    format: "cjs",
    minify: production,
    sourcemap: !production,
    sourcesContent: false,
    platform: "node",
    target: "node18",
    outfile: "dist/extension.js",
    external: ["vscode"],
    logLevel: "info",
  });
  if (watch) {
    await ctx.watch();
  } else {
    await ctx.rebuild();
    await ctx.dispose();
    if (production) {
      // production 不生成 sourcemap，但上一次 dev/compile 可能留下过期
      // map（与压缩后的 js 不匹配，会误导本地 F5 调试）。
      rmSync("dist/extension.js.map", { force: true });
    }
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
