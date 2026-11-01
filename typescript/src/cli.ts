#!/usr/bin/env node
import { readFile, readdir, stat } from "node:fs/promises";
import { extname, parse as parsePath, resolve } from "node:path";
import { assertInstrumentationAgentName, InstrumentationSyntaxError, InstrumentationValidationError, parseInstrumentation } from "./instrumentation";
import { TypeScriptCapabilityError, validateTypeScriptV2Capabilities } from "./instrumentation-v2";
import { SelectorValidationError, validateInstrumentationSelectors } from "./instrumentation-selectors";
import { analyzeRuleCoverage, parseLocalRules, type RuleCoverageItem } from "./rule-path-coverage";
import { InputSchemaConfigurationError } from "./instrumentation-input-schema";

interface Diagnostic {
  file: string;
  code: string;
  path: string;
  message: string;
  line: number | null;
  column: number | null;
}

interface Report {
  valid: boolean;
  language: "typescript";
  files_checked: number;
  diagnostics: Diagnostic[];
}

function usage(): never {
  throw new Error("Usage: norma config validate <file-or-directory> [--json]");
}

function checkUsage(): never { throw new Error("Usage: norma config check <file-or-directory> --rules <file-or-directory> [--json] [--allow-unknown]"); }

function checkEnvironmentConfiguration(): number {
  let valid = true;
  const runtimeUrl = process.env.NORMA_RUNTIME_URL;
  try {
    if (!runtimeUrl?.trim()) throw new Error();
    const endpoint = new URL(runtimeUrl);
    if ((endpoint.protocol !== "https:" && !(endpoint.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(endpoint.hostname)))
      || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) throw new Error();
    process.stdout.write("NORMA_RUNTIME_URL: valid\n");
  } catch {
    process.stdout.write("NORMA_RUNTIME_URL: missing or invalid\n");
    valid = false;
  }
  if (process.env.NORMA_AGENT_KEY?.trim()) process.stdout.write("NORMA_AGENT_KEY: valid\n");
  else { process.stdout.write("NORMA_AGENT_KEY: missing or invalid\n"); valid = false; }
  return valid ? 0 : 1;
}

function diagnostic(file: string, code: string, message: string, path = "", line: number | null = null, column: number | null = null): Diagnostic {
  return { file, code, path, message, line, column };
}

function compareText(left: string, right: string): number {
  const a = Array.from(left, value => value.codePointAt(0)!);
  const b = Array.from(right, value => value.codePointAt(0)!);
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    if (a[index] !== b[index]) return a[index]! < b[index]! ? -1 : 1;
  }
  return a.length - b.length;
}

async function collectFiles(input: string): Promise<{ files: string[]; errors: Diagnostic[] }> {
  const absolute = resolve(input);
  let info;
  try { info = await stat(absolute); } catch { return { files: [], errors: [diagnostic(absolute, "io", "Input path cannot be read.")] }; }
  if (!info.isDirectory()) {
    const extension = extname(absolute).toLowerCase();
    try {
      if (!info.isFile() || !new Set([".json", ".yaml", ".yml"]).has(extension)) throw new Error();
      assertInstrumentationAgentName(parsePath(absolute).name);
    } catch {
      const supportedRegularFile = info.isFile() && new Set([".json", ".yaml", ".yml"]).has(extension);
      return { files: supportedRegularFile ? [absolute] : [], errors: [diagnostic(absolute, "identity", "Input filename must use a valid agent name and a supported extension.")] };
    }
    return { files: [absolute], errors: [] };
  }
  let names;
  try { names = await readdir(absolute, { withFileTypes: true }); } catch { return { files: [], errors: [diagnostic(absolute, "io", "Input directory cannot be read.")] }; }
  const files: string[] = [];
  const errors: Diagnostic[] = [];
  const seenStems = new Set<string>();
  for (const entry of names.filter(item => item.isFile()).sort((a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0)) {
    const name = entry.name;
    const full = resolve(absolute, name);
    const extension = extname(name).toLowerCase();
    if (!new Set([".json", ".yaml", ".yml"]).has(extension)) continue;
    const stem = parsePath(name).name;
    try { assertInstrumentationAgentName(stem); } catch {
      errors.push(diagnostic(full, "identity", "Input filename must use a valid agent name and a supported extension."));
      files.push(full);
      continue;
    }
    if (seenStems.has(stem)) {
      errors.push(diagnostic(full, "identity", "Input directory contains duplicate agent filenames."));
      files.push(full);
      continue;
    }
    seenStems.add(stem);
    files.push(full);
  }
  if (!files.length && !errors.length) errors.push(diagnostic(absolute, "io", "Input directory contains no supported configuration files."));
  return { files, errors };
}

async function validateFile(file: string): Promise<Diagnostic[]> {
  let source: string;
  try {
    source = await readFile(file, "utf8");
  } catch {
    return [diagnostic(file, "io", "Input file cannot be read.")];
  }
  try {
    const extension = extname(file).toLowerCase();
    const config = parseInstrumentation(source, extension === ".json" ? "json" : "yaml");
    try { validateInstrumentationSelectors(config); } catch (error) {
      if (error instanceof SelectorValidationError) return [diagnostic(file, error.code, error.message, error.path)];
      return [diagnostic(file, "selector", "Selector is incompatible with its target phase or declared parameters.")];
    }
    try { validateTypeScriptV2Capabilities(config); } catch (error) {
      if (error instanceof TypeScriptCapabilityError) return [diagnostic(file, error.code, error.message, error.path)];
      return [diagnostic(file, "capability", "Configuration uses a feature unavailable in the TypeScript SDK.")];
    }
    return [];
  } catch (error) {
    if (error instanceof InputSchemaConfigurationError) return [diagnostic(file, error.code, error.message, error.path)];
    if (error instanceof InstrumentationSyntaxError) {
      return [diagnostic(file, error.code, error.message, error.path, error.line, error.column)];
    }
    if (error instanceof InstrumentationValidationError) {
      if (error.issues.length > 1) return error.issues.map(issue => diagnostic(file, issue.code, issue.message, issue.path, error.line, error.column));
      return [diagnostic(file, error.code, error.message, error.path, error.line, error.column)];
    }
    if (error instanceof SyntaxError) return [diagnostic(file, "syntax", "Invalid instrumentation syntax.")];
    if (error instanceof TypeError) return [diagnostic(file, "schema", "Instrumentation configuration is invalid.")];
    return [diagnostic(file, "schema", "Instrumentation configuration is invalid.")];
  }
}

export async function runCli(args: string[]): Promise<number> {
  try {
    if (args.includes("--help") || args.includes("-h")) {
      process.stdout.write("Validate Norma instrumentation JSON/YAML or check local Rule selector coverage without loading application code or accessing the network. Environment references and authorization are checked at startup.\n\nUsage: norma config validate <file-or-directory> [--json]\n       norma config check <file-or-directory> --rules <file-or-directory> [--json] [--allow-unknown]\n");
      return 0;
    }
    if (args[0] === "config" && args[1] === "check") return await runCoverageCheck(args.slice(2));
    if (args[0] !== "config" || args[1] !== "validate") usage();
    const remaining = args.slice(2);
    const jsonFlags = remaining.filter(argument => argument === "--json");
    const paths = remaining.filter(argument => argument !== "--json");
    if (jsonFlags.length > 1 || paths.length !== 1 || paths[0]!.startsWith("-")) usage();
    const json = jsonFlags.length === 1;
    const { files, errors } = await collectFiles(paths[0]!);
    const diagnostics = [...errors];
    for (const file of files) diagnostics.push(...await validateFile(file));
    diagnostics.sort((a, b) => a.file < b.file ? -1 : a.file > b.file ? 1 : a.path < b.path ? -1 : a.path > b.path ? 1 : a.code < b.code ? -1 : a.code > b.code ? 1 : 0);
    const report: Report = { valid: diagnostics.length === 0, language: "typescript", files_checked: files.length, diagnostics };
    if (json) process.stdout.write(`${JSON.stringify(report)}\n`);
    else if (report.valid) process.stdout.write(`Validated ${files.length} configuration document${files.length === 1 ? "" : "s"} offline. Bindings and authorization are checked at startup.\n`);
    else for (const item of diagnostics) process.stderr.write(`${item.file}${item.line ? `:${item.line}:${item.column}` : ""}${item.path ? ` ${item.path}` : ""} [${item.code}]: ${item.message}\n`);
    return report.valid ? 0 : 1;
  } catch {
    process.stderr.write(args[0] === "config" && args[1] === "check"
      ? "Usage: norma config check <file-or-directory> --rules <file-or-directory> [--json] [--allow-unknown]\n"
      : "Usage: norma config validate <file-or-directory> [--json]\n");
    return 2;
  }
}

interface CoverageReport extends Report {
  rules_checked: number;
  coverage_complete: boolean;
  coverage: Array<RuleCoverageItem & { file: string; rule_file: string }>;
}

async function runCoverageCheck(args: string[]): Promise<number> {
  if (args.length === 0) return checkEnvironmentConfiguration();
  const jsonCount = args.filter(value => value === "--json").length;
  const allowUnknownCount = args.filter(value => value === "--allow-unknown").length;
  const rulesIndexes = args.flatMap((value, index) => value === "--rules" ? [index] : []);
  if (jsonCount > 1 || allowUnknownCount > 1 || rulesIndexes.length !== 1) checkUsage();
  const rulesIndex = rulesIndexes[0]!;
  const rulesPath = args[rulesIndex + 1];
  const remaining = args.filter((_, index) => index !== rulesIndex && index !== rulesIndex + 1 && args[index] !== "--json" && args[index] !== "--allow-unknown");
  if (!rulesPath || rulesPath.startsWith("-") || remaining.length !== 1 || remaining[0]!.startsWith("-")) checkUsage();
  const json = jsonCount === 1;
  const allowUnknown = allowUnknownCount === 1;
  const selectedConfigs = await collectFiles(remaining[0]!);
  const diagnostics: Diagnostic[] = [...selectedConfigs.errors];
  const coverage: CoverageReport["coverage"] = [];
  let rulesChecked = 0;
  const rulePath = resolve(rulesPath);
  let ruleInfo;
  try { ruleInfo = await stat(rulePath); } catch { ruleInfo = undefined; }
  const commonRuleFile = ruleInfo?.isFile() ? rulePath : undefined;
  const ruleDirectoryFiles = new Map<string, string>();
  if (!ruleInfo) diagnostics.push(diagnostic(rulePath, "io", "Rule path cannot be read."));
  else if (!commonRuleFile) {
    if (!ruleInfo.isDirectory()) diagnostics.push(diagnostic(rulePath, "io", "Rule path must be a JSON/YAML file or directory."));
    else {
      try {
        const entries = await readdir(rulePath, { withFileTypes: true });
        const seen = new Set<string>();
        for (const entry of entries.filter(item => item.isFile()).sort((a, b) => compareText(a.name, b.name))) {
          const extension = extname(entry.name).toLowerCase();
          if (![".json", ".yaml", ".yml"].includes(extension)) continue;
          const stem = parsePath(entry.name).name;
          if (seen.has(stem)) diagnostics.push(diagnostic(resolve(rulePath, entry.name), "identity", "Rule directory contains duplicate agent filenames."));
          seen.add(stem);
          ruleDirectoryFiles.set(stem, resolve(rulePath, entry.name));
        }
      } catch { diagnostics.push(diagnostic(rulePath, "io", "Rule directory cannot be read.")); }
    }
  }
  for (const file of selectedConfigs.files) {
    const identity = parsePath(file).name;
    const selectedRuleFile = commonRuleFile ?? ruleDirectoryFiles.get(identity);
    if (!selectedRuleFile) { diagnostics.push(diagnostic(file, "io", "No matching local Rule file exists for this configuration.")); continue; }
    let source: string;
    try { source = await readFile(file, "utf8"); } catch { diagnostics.push(diagnostic(file, "io", "Input file cannot be read.")); continue; }
    let config;
    try {
      const extension = extname(file).toLowerCase();
      config = parseInstrumentation(source, extension === ".json" ? "json" : "yaml");
    } catch (error) {
      if (error instanceof InputSchemaConfigurationError) { diagnostics.push(diagnostic(file, error.code, error.message, error.path)); continue; }
      if (error instanceof InstrumentationSyntaxError) diagnostics.push(diagnostic(file, error.code, error.message, error.path, error.line, error.column));
      else if (error instanceof InstrumentationValidationError) {
        const issues = error.issues.length > 1 ? error.issues : [error];
        for (const issue of issues) diagnostics.push(diagnostic(file, issue.code, issue.message, issue.path, error.line, error.column));
      } else diagnostics.push(diagnostic(file, "schema", "Instrumentation configuration is invalid."));
      continue;
    }
    try { validateInstrumentationSelectors(config); } catch (error) {
      if (error instanceof SelectorValidationError) diagnostics.push(diagnostic(file, error.code, error.message, error.path));
      else diagnostics.push(diagnostic(file, "selector", "Selector is incompatible with its target phase or declared parameters."));
      continue;
    }
    try { validateTypeScriptV2Capabilities(config); } catch (error) {
      if (error instanceof TypeScriptCapabilityError) diagnostics.push(diagnostic(file, error.code, error.message, error.path));
      else diagnostics.push(diagnostic(file, "capability", "Configuration uses a feature unavailable in the TypeScript SDK."));
    }
    let ruleSource: string;
    try { ruleSource = await readFile(selectedRuleFile, "utf8"); } catch { diagnostics.push(diagnostic(selectedRuleFile, "io", "Rule file cannot be read.")); continue; }
    let rules;
    try {
      const extension = extname(selectedRuleFile).toLowerCase();
      rules = parseLocalRules(ruleSource, extension === ".json" ? "json" : "yaml", identity);
      rulesChecked += rules.length;
    } catch (error) {
      const info = error as Error & { code?: string; path?: string };
      diagnostics.push(diagnostic(selectedRuleFile, info.code === "rules_scope" ? "rules_scope" : "rules_schema", "Local Rule document is invalid.", info.path ?? ""));
      continue;
    }
    const result = analyzeRuleCoverage(config, rules, identity);
    coverage.push(...result.coverage.map(item => ({ ...item, file, rule_file: selectedRuleFile })));
    for (const item of result.diagnostics) {
      if (item.code === "coverage_unknown" && allowUnknown) continue;
      diagnostics.push(diagnostic(selectedRuleFile, item.code, item.message, item.path));
    }
  }
  coverage.sort((a, b) => compareText(a.file, b.file) || a.rule_index - b.rule_index || compareText(a.checkpoint, b.checkpoint) || (a.stage === b.stage ? 0 : a.stage === "pre" ? -1 : 1) || compareText(a.rule_path, b.rule_path));
  diagnostics.sort((a, b) => compareText(a.file, b.file) || compareText(a.path, b.path) || compareText(a.code, b.code));
  const coverageComplete = coverage.every(item => item.status === "covered") && diagnostics.length === 0;
  const report: CoverageReport = { valid: diagnostics.length === 0, language: "typescript", files_checked: selectedConfigs.files.length, diagnostics, rules_checked: rulesChecked, coverage_complete: coverageComplete, coverage };
  if (json) process.stdout.write(`${JSON.stringify(report)}\n`);
  else if (report.valid) process.stdout.write(`Checked ${selectedConfigs.files.length} configuration document${selectedConfigs.files.length === 1 ? "" : "s"} against ${rulesChecked} local Rule${rulesChecked === 1 ? "" : "s"} offline.${coverageComplete ? "" : " Unknown coverage was allowed."}\n`);
  else {
    for (const item of diagnostics) process.stderr.write(`${item.file}${item.path ? ` ${item.path}` : ""} [${item.code}]: ${item.message}\n`);
    if (allowUnknown && coverage.some(item => item.status === "unknown")) process.stderr.write("Warning: unknown Rule coverage was allowed; coverage_complete remains false.\n");
  }
  return report.valid ? 0 : 1;
}

void runCli(process.argv.slice(2)).then(code => { process.exitCode = code; });
