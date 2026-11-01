import { defineConfig } from "tsup";

export default defineConfig({
  entry: ["src/index.ts", "src/openai-agents.ts", "src/cli.ts"],
  splitting: true,
  external: ["@openai/agents"],
  format: ["esm", "cjs"],
  // Emit through one TypeScript program before bundling. Per-module emission can
  // reorder inferred unions and change declaration hashes between clean builds.
  dts: {
    entry: {
      index: ".declarations/index.d.ts",
      "openai-agents": ".declarations/openai-agents.d.ts",
      cli: ".declarations/cli.d.ts",
    },
  },
  sourcemap: false,
  clean: true,
  target: "es2020",
  outDir: "dist",
});
