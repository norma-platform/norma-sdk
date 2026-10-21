import { defineConfig } from "tsup";

export default defineConfig({
  entry: ["src/index.ts", "src/openai-agents.ts"],
  splitting: false,
  external: ["@openai/agents"],
  format: ["esm", "cjs"],
  dts: true,
  sourcemap: false,
  clean: true,
  target: "es2020",
  outDir: "dist",
});
