import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { afterEach, describe, expect, it } from "vitest";

const packageRoot = resolve(import.meta.dirname, "..");
const cliPath = join(packageRoot, "dist", "cli.cjs");
const temporaryPaths: string[] = [];
interface SharedFixtureManifest {
  cases: Array<{ name: string; format: "json" | "yaml"; source: string; valid: boolean; code?: string; path?: string; line?: number | null; column?: number | null }>;
}
interface SharedCoverageManifest {
  cases: Array<{
    name: string;
    config: { format: "json" | "yaml"; source: string };
    rules: { format: "json" | "yaml"; source: string };
    allow_unknown?: boolean;
    expect: { typescript: { valid: boolean; coverage_complete: boolean; diagnostics: Array<{ code: string; path: string }>; coverage: Array<{ rule_path: string; checkpoint: string; stage: string; selector: string; status: string }> } };
  }>;
}
interface SharedInputSchemaManifest {
  cases: Array<{ name: string; format: "json" | "yaml"; source: string; parse_valid: boolean; config_valid: boolean; config_path?: string }>;
}

function tempDirectory(): string {
  const path = mkdtempSync(join(tmpdir(), "norma-validate-cli-"));
  temporaryPaths.push(path);
  return path;
}

function run(args: string[]) {
  return spawnSync(process.execPath, [cliPath, "config", "validate", ...args], {
    cwd: packageRoot,
    encoding: "utf8",
    env: {},
  });
}

function runCheck(args: string[]) {
  return spawnSync(process.execPath, [cliPath, "config", "check", ...args], {
    cwd: packageRoot,
    encoding: "utf8",
    env: {},
  });
}

afterEach(() => {
  for (const path of temporaryPaths.splice(0)) rmSync(path, { recursive: true, force: true });
});

describe("norma config validate", () => {
  it.each((JSON.parse(readFileSync(new URL("../../schema/validation-fixtures/manifest.json", import.meta.url), "utf8")) as SharedFixtureManifest).cases)(
    "matches shared validation fixture $name",
    ({ name, format, source, valid, code, path, line, column }) => {
      const dir = tempDirectory();
      const file = join(dir, `norma-${name}.${format}`);
      writeFileSync(file, source);
      const result = run([file, "--json"]);
      const report = JSON.parse(result.stdout) as { valid: boolean; diagnostics: Array<Record<string, unknown>> };
      expect(report.valid).toBe(valid);
      expect(result.status).toBe(valid ? 0 : 1);
      if (!valid) {
        expect(report.diagnostics).toContainEqual(expect.objectContaining({ file, code, ...(path === undefined ? {} : { path }), ...(line === undefined ? {} : { line }), ...(column === undefined ? {} : { column }) }));
      }
    },
  );

  it.each((JSON.parse(readFileSync(new URL("../../schema/validation-fixtures/input-schema.json", import.meta.url), "utf8")) as SharedInputSchemaManifest).cases)(
    "matches shared input-schema offline validation fixture $name",
    ({ name, format, source, config_valid, config_path }) => {
      const dir = tempDirectory();
      const file = join(dir, `agent-${name}.${format}`);
      writeFileSync(file, source);
      const result = run([file, "--json"]);
      const report = JSON.parse(result.stdout) as { valid: boolean; diagnostics: Array<{ code: string; path: string }> };
      expect(report.valid).toBe(config_valid);
      expect(result.status).toBe(config_valid ? 0 : 1);
      if (!config_valid && config_path) {
        expect(report.diagnostics).toContainEqual(expect.objectContaining({ code: "input_schema", path: config_path }));
      }
    },
  );

  it("accepts an offline JSON config and emits the exact machine report", () => {
    const dir = tempDirectory();
    const file = join(dir, "norma-agent.json");
    writeFileSync(file, '{"version":1,"entrypoints":["app.run"],"checkpoints":{}}');

    const result = run([file, "--json"]);

    expect(result.status).toBe(0);
    expect(result.stderr).toBe("");
    expect(JSON.parse(result.stdout)).toEqual({
      valid: true,
      language: "typescript",
      files_checked: 1,
      diagnostics: [],
    });
  });

  it("validates environment references without resolving them or using the network", () => {
    const dir = tempDirectory();
    const file = join(dir, "norma-env-agent.json");
    writeFileSync(file, JSON.stringify({
      version: 2,
      entrypoints: ["app.run"],
      checkpoints: {},
      connection: { runtime_url: { env: "MISSING_RUNTIME_URL" }, agent_key: { env: "MISSING_AGENT_KEY" } },
    }));

    const result = run([file, "--json"]);

    expect(result.status).toBe(0);
    expect(JSON.parse(result.stdout)).toEqual({ valid: true, language: "typescript", files_checked: 1, diagnostics: [] });
  });

  it("sorts supported files in a directory and aggregates diagnostics", () => {
    const dir = tempDirectory();
    writeFileSync(join(dir, "norma-bravo.yaml"), "version: 1\nentrypoints: [app.run]\ncheckpoints: {}\n");
    writeFileSync(join(dir, "norma-alpha.json"), "{\n  \"version\": 99\n}");
    writeFileSync(join(dir, "ignored.txt"), "secret-looking-content");

    const result = run([dir, "--json"]);

    expect(result.status).toBe(1);
    expect(result.stderr).toBe("");
    const report = JSON.parse(result.stdout) as {
      valid: boolean;
      language: string;
      files_checked: number;
      diagnostics: Array<{ file: string; code: string; path: string; message: string; line: number | null; column: number | null }>;
    };
    expect(report.valid).toBe(false);
    expect(report.language).toBe("typescript");
    expect(report.files_checked).toBe(2);
    expect(report.diagnostics).toContainEqual(expect.objectContaining({ file: join(dir, "norma-alpha.json"), code: "schema", line: null, column: null }));
    expect(result.stdout).not.toContain("secret-looking-content");
  });

  it("reports duplicate YAML keys with a source location and redacts their values", () => {
    const dir = tempDirectory();
    const file = join(dir, "norma-duplicate.yaml");
    writeFileSync(file, "version: 1\nentrypoints: [app.run]\ncheckpoints: {}\nversion: private-secret\n");

    const result = run([file, "--json"]);
    const report = JSON.parse(result.stdout) as { diagnostics: Array<Record<string, unknown>> };

    expect(result.status).toBe(1);
    expect(report.diagnostics).toEqual([
      expect.objectContaining({ file, code: "syntax", path: "", line: 4, column: 1 }),
    ]);
    expect(result.stdout).not.toContain("private-secret");
  });

  it("rejects empty directories and unsupported file stems as input errors", () => {
    const dir = tempDirectory();
    const empty = join(dir, "empty");
    mkdirSync(empty);
    const invalidStem = join(dir, "bad name.json");
    writeFileSync(invalidStem, '{"version":1,"entrypoints":["app.run"],"checkpoints":{}}');

    const emptyResult = run([empty, "--json"]);
    const invalidResult = run([invalidStem, "--json"]);
    expect(emptyResult.status).toBe(1);
    expect(JSON.parse(emptyResult.stdout)).toEqual(expect.objectContaining({ valid: false, files_checked: 0, diagnostics: [expect.objectContaining({ code: "io" })] }));
    expect(invalidResult.status).toBe(1);
    expect(JSON.parse(invalidResult.stdout)).toEqual(expect.objectContaining({ valid: false, diagnostics: [expect.objectContaining({ code: "identity" })] }));
  });

  it("reports duplicate agent stems while checking both supported files", () => {
    const dir = tempDirectory();
    const source = '{"version":1,"entrypoints":["app.run"],"checkpoints":{}}';
    writeFileSync(join(dir, "norma-duplicate-agent.json"), source);
    writeFileSync(join(dir, "norma-duplicate-agent.yaml"), "version: 1\nentrypoints: [app.run]\ncheckpoints: {}\n");

    const result = run([dir, "--json"]);
    const report = JSON.parse(result.stdout) as { files_checked: number; diagnostics: Array<{ code: string }> };

    expect(result.status).toBe(1);
    expect(report.files_checked).toBe(2);
    expect(report.diagnostics).toEqual([expect.objectContaining({ code: "identity" })]);
  });

  it("uses usage status for malformed command lines", () => {
    const result = run([]);
    expect(result.status).toBe(2);
    expect(result.stdout).toBe("");
    expect(result.stderr).not.toBe("");
  });

  it("checks explicit selector slots without inspecting literal payload path keys", () => {
    const dir = tempDirectory();
    const file = join(dir, "norma-selector-agent.json");
    writeFileSync(file, JSON.stringify({
      version: 2,
      entrypoints: [{ target: "app.run", context: { path: ["result", "id"] } }],
      checkpoints: { send: { target: "app.send", state: { path: ["args", 0] } } },
    }));

    const result = run([file, "--json"]);
    const report = JSON.parse(result.stdout) as { diagnostics: Array<{ code: string; path: string }> };

    expect(result.status).toBe(1);
    expect(report.diagnostics).toEqual([expect.objectContaining({ code: "selector", path: "/entrypoints/0/context" })]);
  });

  it.each([
    ["entrypoint context cannot read its own context root", { version: 2, entrypoints: [{ target: "app.run", context: { path: ["context"] } }], checkpoints: {} }, "/entrypoints/0/context"],
    ["stream text cannot select the absent error root", {
      version: 2,
      entrypoints: [{ target: "app.run", returns: "async_iterable", turn: { session_id: { value: "s" }, turn_id: { value: "t" }, finish: { stream: { text: { path: ["error", "message"] } }, on_hold: "raise" } } }],
      checkpoints: {},
    }, "/entrypoints/0/turn/finish/stream/text"],
  ])("rejects impossible selector: %s", (_title, config, pointer) => {
    const dir = tempDirectory();
    const file = join(dir, "norma-impossible-selector.json");
    writeFileSync(file, JSON.stringify(config));
    const result = run(["--json", file]);
    const report = JSON.parse(result.stdout) as { diagnostics: Array<{ code: string; path: string }> };
    expect(result.status).toBe(1);
    expect(report.diagnostics).toEqual([expect.objectContaining({ code: "selector", path: pointer })]);
  });

  it("prints offline help with success status", () => {
    const result = run(["--help"]);
    expect(result.status).toBe(0);
    expect(result.stdout).toContain("without loading application code or accessing the network");
    expect(result.stderr).toBe("");
  });

  it("reports a language-specific finish driver as a TypeScript capability", () => {
    const dir = tempDirectory();
    const file = join(dir, "norma-native-finish.json");
    writeFileSync(file, JSON.stringify({
      version: 2,
      entrypoints: [{ target: "app.run", turn: { session_id: { value: "s" }, turn_id: { value: "t" }, finish: { driver: "pydantic_ai", adapter: "agent" } } }],
      checkpoints: {},
    }));
    const result = run([file, "--json"]);
    const report = JSON.parse(result.stdout) as { diagnostics: Array<{ code: string; path: string }> };
    expect(result.status).toBe(1);
    expect(report.diagnostics).toEqual([expect.objectContaining({ code: "capability", path: "/entrypoints/0/turn/finish/driver" })]);
  });

  it.each([
    ["nested unknown field", { version: 2, entrypoints: [{ target: "app.run", parametr_names: ["request"] }], checkpoints: {} }, "/entrypoints/0/parametr_names", "schema"],
    ["missing nested required field", { version: 2, entrypoints: [{ target: "app.run", turn: { session_id: { value: "session" } } }], checkpoints: {} }, "/entrypoints/0/turn/turn_id", "schema"],
    ["successful observation cannot read error", { version: 2, entrypoints: ["app.run"], checkpoints: {}, operations: { create: { target: "app.create", returns: "promise", operation_key: { value: "k" }, observations: { succeeded: { event_key: { value: "done" }, summary: { path: ["error", "message"] } } } } } }, "/operations/create/observations/succeeded/summary", "selector"],
    ["failed observation cannot read result", { version: 2, entrypoints: ["app.run"], checkpoints: {}, operations: { create: { target: "app.create", returns: "promise", operation_key: { value: "k" }, observations: { failed: { event_key: { value: "failed" }, summary: { path: ["result", "id"] } } } } } }, "/operations/create/observations/failed/summary", "selector"],
  ])("reports only the actionable validation issue for %s", (_label, config, path, code) => {
    const dir = tempDirectory();
    const file = join(dir, "norma-schema-diagnostic.json");
    writeFileSync(file, JSON.stringify(config));
    const result = run([file, "--json"]);
    const report = JSON.parse(result.stdout) as { diagnostics: Array<{ path: string; code: string }> };
    expect(result.status).toBe(1);
    expect(report.diagnostics).toEqual([expect.objectContaining({ path, code })]);
  });
});

describe("norma config check", () => {
  const manifest = JSON.parse(readFileSync(new URL("../../schema/validation-fixtures/rule-coverage.json", import.meta.url), "utf8")) as SharedCoverageManifest;

  it("preserves the bare offline environment check", () => {
    const result = runCheck([]);
    expect(result.status).toBe(1);
    expect(result.stderr).toBe("");
    expect(result.stdout).toBe("NORMA_RUNTIME_URL: missing or invalid\nNORMA_AGENT_KEY: missing or invalid\n");
  });

  it.each(manifest.cases)("matches shared Rule path coverage case $name", ({ name, config, rules, allow_unknown, expect: expected }) => {
    const dir = tempDirectory();
    const configFile = join(dir, `agent-${name}.${config.format}`);
    const ruleFile = join(dir, `local-rules.${rules.format}`);
    writeFileSync(configFile, config.source);
    writeFileSync(ruleFile, rules.source);
    const result = runCheck([configFile, "--rules", ruleFile, "--json", ...(allow_unknown ? ["--allow-unknown"] : [])]);
    const report = JSON.parse(result.stdout) as {
      valid: boolean;
      rules_checked: number;
      coverage_complete: boolean;
      diagnostics: Array<Record<string, unknown>>;
      coverage: Array<Record<string, unknown>>;
    };
    expect(result.status).toBe(expected.typescript.valid ? 0 : 1);
    expect(report.valid).toBe(expected.typescript.valid);
    expect(report.coverage_complete).toBe(expected.typescript.coverage_complete);
    expect(report.diagnostics.map(({ code, path }) => ({ code, path }))).toEqual(expected.typescript.diagnostics);
    expect(report.coverage.map(({ rule_path, checkpoint, stage, selector, status }) => ({ rule_path, checkpoint, stage, selector, status }))).toEqual(expected.typescript.coverage);
    expect(report.rules_checked).toBeGreaterThanOrEqual(0);
  });

  it("rejects all_kwargs as a TypeScript capability before network or application access", () => {
    const dir = tempDirectory();
    const configFile = join(dir, "agent-typescript-all-kwargs.json");
    const rulesFile = join(dir, "rules.json");
    writeFileSync(configFile, JSON.stringify({ version: 2, entrypoints: ["app.run"], checkpoints: { send: { target: "app.send", input_from: "all_kwargs" } } }));
    writeFileSync(rulesFile, JSON.stringify({ rules: [{ condition: { selector: { path: "kwargs.recipient" }, evaluator: { name: "json", config: {} } } }] }));
    const result = runCheck([configFile, "--rules", rulesFile, "--json", "--allow-unknown"]);
    const report = JSON.parse(result.stdout) as { valid: boolean; diagnostics: Array<{ code: string; path: string }>; coverage_complete: boolean };
    expect(result.status).toBe(1);
    expect(report.valid).toBe(false);
    expect(report.coverage_complete).toBe(false);
    expect(report.diagnostics).toContainEqual(expect.objectContaining({ code: "capability", path: "/checkpoints/send/input_from" }));
  });

  it("accepts all_params with declared names and reports its input shape as unknown coverage", () => {
    const dir = tempDirectory();
    const configFile = join(dir, "agent-all-params.json");
    const rulesFile = join(dir, "rules.json");
    writeFileSync(configFile, JSON.stringify({
      version: 2,
      entrypoints: ["app.run"],
      checkpoints: { send: { target: "app.send", parameter_names: ["recipient", "options"], input_from: "all_params" } },
    }));
    writeFileSync(rulesFile, JSON.stringify({ rules: [{ condition: { selector: { path: "input.recipient" }, evaluator: { name: "json", config: {} } }, scope: { stages: ["pre"] } }] }));

    const validation = run([configFile, "--json"]);
    const strict = runCheck([configFile, "--rules", rulesFile, "--json"]);
    const waived = runCheck([configFile, "--rules", rulesFile, "--json", "--allow-unknown"]);
    const strictReport = JSON.parse(strict.stdout) as { valid: boolean; coverage_complete: boolean; coverage: Array<{ status: string }>; diagnostics: Array<{ code: string }> };
    const waivedReport = JSON.parse(waived.stdout) as { valid: boolean; coverage_complete: boolean; coverage: Array<{ status: string }>; diagnostics: Array<{ code: string }> };

    expect(validation.status).toBe(0);
    expect(strict.status).toBe(1);
    expect(strictReport.valid).toBe(false);
    expect(strictReport.coverage_complete).toBe(false);
    expect(strictReport.coverage).toEqual([expect.objectContaining({ status: "unknown", selector: "input.recipient", stage: "pre" })]);
    expect(strictReport.diagnostics).toContainEqual(expect.objectContaining({ code: "coverage_unknown", path: "/rules/0/condition/selector/path" }));
    expect(waived.status).toBe(0);
    expect(waivedReport.valid).toBe(true);
    expect(waivedReport.coverage_complete).toBe(false);
    expect(waivedReport.coverage).toEqual(strictReport.coverage);
    expect(waivedReport.diagnostics).toEqual([]);
  });

  it("requires parameter_names for all_params at the checkpoint input_from path", () => {
    const dir = tempDirectory();
    const file = join(dir, "agent-all-params-missing-names.json");
    writeFileSync(file, JSON.stringify({ version: 2, entrypoints: ["app.run"], checkpoints: { send: { target: "app.send", input_from: "all_params" } } }));

    const result = run([file, "--json"]);
    const report = JSON.parse(result.stdout) as { valid: boolean; diagnostics: Array<{ code: string; path: string }> };

    expect(result.status).toBe(1);
    expect(report.valid).toBe(false);
    expect(report.diagnostics).toEqual([expect.objectContaining({ code: "selector", path: "/checkpoints/send/input_from" })]);
  });

  it("never waives a proven missing path with --allow-unknown", () => {
    const dir = tempDirectory();
    const configFile = join(dir, "agent-missing.json");
    const rulesFile = join(dir, "rules.json");
    writeFileSync(configFile, JSON.stringify({ version: 2, entrypoints: ["app.run"], checkpoints: { send: { target: "app.send", input_from: { value: { subject: "Hello" } } } } }));
    writeFileSync(rulesFile, JSON.stringify({ rules: [{ condition: { selector: { path: "input.to" }, evaluator: { name: "json", config: {} } } }] }));
    const result = runCheck([configFile, "--rules", rulesFile, "--json", "--allow-unknown"]);
    const report = JSON.parse(result.stdout) as { valid: boolean; diagnostics: Array<{ code: string; path: string }>; coverage_complete: boolean };
    expect(result.status).toBe(1);
    expect(report.valid).toBe(false);
    expect(report.coverage_complete).toBe(false);
    expect(report.diagnostics).toContainEqual(expect.objectContaining({ code: "coverage_missing", path: "/rules/0/condition/selector/path" }));
  });

  it("keeps dynamic state_from paths unknown", () => {
    const dir = tempDirectory();
    const configFile = join(dir, "agent-dynamic-state.json");
    const rulesFile = join(dir, "rules.json");
    writeFileSync(configFile, JSON.stringify({ version: 2, entrypoints: ["app.run"], checkpoints: { send: { target: "app.send", state_from: { provider: "app.state" } } } }));
    writeFileSync(rulesFile, JSON.stringify({ rules: [{ condition: { selector: { path: "context.state.balance" }, evaluator: { name: "json", config: {} } } }] }));

    const result = runCheck([configFile, "--rules", rulesFile, "--json"]);
    const report = JSON.parse(result.stdout) as { coverage: Array<{ status: string }>; diagnostics: Array<{ code: string }> };

    expect(result.status).toBe(1);
    expect(report.coverage.every(item => item.status === "unknown")).toBe(true);
    expect(report.diagnostics).toContainEqual(expect.objectContaining({ code: "coverage_unknown" }));
    expect(report.diagnostics).not.toContainEqual(expect.objectContaining({ code: "coverage_missing" }));
  });

  it("does not use one entrypoint's literal context to prove a shared checkpoint", () => {
    const dir = tempDirectory();
    const configFile = join(dir, "agent-multiple-entrypoints.json");
    const rulesFile = join(dir, "rules.json");
    writeFileSync(configFile, JSON.stringify({
      version: 2,
      entrypoints: [
        { target: "app.run_a", context: { value: { state: { balance: 10 } } } },
        { target: "app.run_b" },
      ],
      checkpoints: { send: { target: "app.send" } },
    }));
    writeFileSync(rulesFile, JSON.stringify({ rules: [{ condition: { selector: { path: "context.state.balance" }, evaluator: { name: "json", config: {} } } }] }));

    const result = runCheck([configFile, "--rules", rulesFile, "--json"]);
    const report = JSON.parse(result.stdout) as { coverage: Array<{ status: string }>; diagnostics: Array<{ code: string }> };

    expect(result.status).toBe(1);
    expect(report.coverage.every(item => item.status === "unknown")).toBe(true);
    expect(report.diagnostics).toContainEqual(expect.objectContaining({ code: "coverage_unknown" }));
    expect(report.diagnostics).not.toContainEqual(expect.objectContaining({ code: "coverage_missing" }));
  });

  it("rejects cyclic YAML aliases as a bounded Rule schema error", () => {
    const dir = tempDirectory();
    const configFile = join(dir, "agent-cyclic-rules.json");
    const rulesFile = join(dir, "rules.yaml");
    writeFileSync(configFile, JSON.stringify({ version: 1, entrypoints: ["app.run"], checkpoints: { send: { target: "app.send" } } }));
    writeFileSync(rulesFile, "rule: &self\n  rule: *self\n");

    const result = runCheck([configFile, "--rules", rulesFile, "--json"]);
    const report = JSON.parse(result.stdout) as { valid: boolean; diagnostics: Array<{ code: string }> };

    expect(result.status).toBe(1);
    expect(report.valid).toBe(false);
    expect(report.diagnostics).toContainEqual(expect.objectContaining({ code: "rules_schema" }));
  });

  it("keeps JSON field-map names opaque for non-JSON evaluators", () => {
    const dir = tempDirectory();
    const configFile = join(dir, "agent-opaque-evaluator.json");
    const rulesFile = join(dir, "rules.json");
    writeFileSync(configFile, JSON.stringify({ version: 2, entrypoints: ["app.run"], checkpoints: { send: { target: "app.send", input_from: { value: { person: { name: "Ada" } } } } } }));
    writeFileSync(rulesFile, JSON.stringify({ rules: [{ condition: {
      selector: { path: "input.person.name" },
      evaluator: { name: "custom", config: { field_types: null, field_constraints: "opaque" } },
    }, scope: { stages: ["pre"] } }] }));

    const result = runCheck([configFile, "--rules", rulesFile, "--json"]);
    const report = JSON.parse(result.stdout) as { valid: boolean; diagnostics: Array<{ code: string; path: string }>; coverage: Array<{ status: string; selector: string }> };

    expect(result.status).toBe(1);
    expect(report.valid).toBe(false);
    expect(report.diagnostics.map(({ code }) => code)).toEqual(["coverage_unknown"]);
    expect(report.coverage).toEqual(expect.arrayContaining([expect.objectContaining({ status: "covered", selector: "input.person.name" }), expect.objectContaining({ status: "unknown", selector: "*" })]));
  });
});
