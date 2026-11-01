# Modified for Norma.
"""Qualify the built SDK as an offline npm tarball consumer; never publish or fetch."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

SDK = Path(__file__).resolve().parents[1]


def pack_installed_dependency(
    source: Path, output: Path, expected_name: str, *, expected_dependencies: set[str] | None = None,
    allow_optional_dependencies: bool = False,
) -> Path:
    """Archive installed dependency bytes without npm interpreting a pnpm store path."""
    metadata = json.loads((source / "package.json").read_text())
    version = metadata.get("version", "")
    if metadata.get("name") != expected_name or not re.fullmatch(r"[0-9A-Za-z.+-]+", version):
        raise RuntimeError("Installed dependency identity is invalid")
    dependencies = metadata.get("dependencies", {})
    if (set(dependencies) != (expected_dependencies or set()) or
            (metadata.get("optionalDependencies") and not allow_optional_dependencies)):
        raise RuntimeError("Installed dependency graph differs from the reviewed offline dependency set")
    # pnpm exposes package executables through a store-local `.bin` symlink.
    # npm consumers recreate executable links from package metadata, so do not
    # copy that generated directory into the dependency tarball.
    generated_bins = source / "node_modules"
    if generated_bins.exists():
        children = list(generated_bins.iterdir())
        if len(children) != 1 or children[0].name != ".bin" or not children[0].is_dir():
            raise RuntimeError("Installed dependency contains unexpected private or nested files")
        if any(not (child.is_symlink() or child.is_file()) for child in children[0].iterdir()):
            raise RuntimeError("Installed dependency contains unexpected private or nested files")
    members = sorted(
        member for member in source.rglob("*")
        if member.relative_to(source).parts[:1] != ("node_modules",)
    )
    for member in members:
        relative = member.relative_to(source)
        if any(part in {"node_modules", ".git", ".npmrc"} or part.startswith(".env")
               for part in relative.parts):
            raise RuntimeError("Installed dependency contains unexpected private or nested files")
        if member.is_symlink() or not (member.is_file() or member.is_dir()):
            raise RuntimeError("Installed dependency must contain regular files and directories")
    archive_name = expected_name.replace("/", "-")
    path = output / f"{archive_name}-{version}.tgz"
    # pnpm's content store hardlinks identical files. Emit regular file bytes for
    # every path; npm's archive extraction does not preserve package hardlinks.
    with tarfile.open(path, "w:gz", dereference=True) as archive:
        for member in members:
            if member.is_file():
                info = archive.gettarinfo(member, arcname=f"package/{member.relative_to(source)}")
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                info.mode = 0o644
                with member.open("rb") as stream:
                    archive.addfile(info, stream)
    return path


def installed_pnpm_package(name: str) -> Path:
    """Locate one dependency package in the local pnpm store without network access."""
    direct = SDK / "node_modules" / name
    candidates = [direct, *(entry / "node_modules" / name for entry in (SDK / "node_modules/.pnpm").iterdir())]
    for candidate in candidates:
        metadata_path = candidate / "package.json"
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text())
            if metadata.get("name") == name:
                return candidate.resolve(strict=True)
    raise RuntimeError("Required optional-peer fixture dependency is not installed")


def failure_diagnostic(stage: str, result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    """Expose only allowlisted machine codes, never subprocess payloads or config values."""
    code = re.search(r"^npm (?:ERR!|error) code (E[A-Z0-9_]{1,40})$", result.stderr, re.MULTILINE)
    return {
        "status": "failed", "stage": stage, "exit_code": result.returncode,
        "npm_error_code": code.group(1) if code else None,
        "tool_error": "npm-exit-handler-not-called"
        if "npm error Exit handler never called!" in result.stderr else None,
    }


PAYLOAD_FILES = {
    "package/package.json", "package/README.md", "package/LICENSE",
    "package/THIRD_PARTY_NOTICES", "package/dist/index.js", "package/dist/index.cjs",
    "package/dist/index.d.ts", "package/dist/index.d.cts",
    "package/dist/openai-agents.js", "package/dist/openai-agents.cjs",
    "package/dist/openai-agents.d.ts", "package/dist/openai-agents.d.cts",
    "package/dist/cli.js", "package/dist/cli.cjs", "package/dist/cli.d.ts", "package/dist/cli.d.cts",
}


def inspect_package(path: Path) -> dict[str, object]:
    """Inspect the distributable bytes, excluding source trees, fixtures and app internals."""
    with tarfile.open(path) as package:
        members = package.getmembers()
        names = {member.name for member in members}
        shared = {
            name
            for name in names
            if re.fullmatch(
                r"package/dist/(?:client|checkpoint)(?:\.d)?-[A-Za-z0-9_-]+\.d\.(?:ts|cts)",
                name,
            )
        }
        runtime_chunks = {
            name
            for name in names
            if re.fullmatch(r"package/dist/chunk-[A-Za-z0-9_-]+\.(?:js|cjs)", name)
        }
        if shared and (len(shared) != 2 or len({name.rsplit(".", 1)[0] for name in shared}) != 1):
            raise RuntimeError("Unexpected shared declaration graph")
        counts = {extension: sum(name.endswith(f".{extension}") for name in runtime_chunks) for extension in ("js", "cjs")}
        if runtime_chunks and (counts["js"] != counts["cjs"] or counts["js"] not in {1, 2}):
            raise RuntimeError("Unexpected runtime chunk graph")
        if names - shared - runtime_chunks != PAYLOAD_FILES or len(members) != len(names):
            raise RuntimeError("Packed SDK payload differs from the reviewed file allowlist")
        if any(not member.isfile() for member in members):
            raise RuntimeError("Packed SDK must contain regular files only")
        files: dict[str, bytes] = {}
        for member in members:
            stream = package.extractfile(member)
            if stream is None:
                raise RuntimeError("Packed SDK member is unreadable")
            files[member.name] = stream.read()
        metadata = json.loads(files["package/package.json"])
        if metadata.get("name") != "norma-sdk":
            raise RuntimeError("Packed SDK must be installed as norma-sdk")
        if metadata.get("bin") != {"norma": "dist/cli.cjs"} or "./cli" in metadata.get("exports", {}):
            raise RuntimeError("Packed CLI must be registered as a binary without adding a public module export")
        if not files["package/dist/cli.cjs"].startswith(b"#!/usr/bin/env node"):
            raise RuntimeError("Packed CLI entry must retain its Node executable header")
        dependencies = metadata.get("dependencies", {})
        if set(dependencies) != {"ajv", "yaml", "zod"} or any(
            value.startswith(("file" + ":", "link" + ":", "workspace" + ":")) for value in dependencies.values()
        ):
            raise RuntimeError("Packed SDK runtime dependencies must stay independent of the app")
        for filename in ("LICENSE", "THIRD_PARTY_NOTICES"):
            if files[f"package/{filename}"] != (SDK / filename).read_bytes():
                raise RuntimeError(f"Packed SDK must retain the reviewed {filename}")
        notices = files["package/THIRD_PARTY_NOTICES"].decode()
        for filename in ("is-plain-object.ts",):
            source = (SDK / "src/generated/lib" / filename).read_text()
            notice = "MIT License\n" + source.split("/*\nMIT License\n", 1)[1].split("*/", 1)[0]
            if notice not in notices:
                raise RuntimeError("Bundled third-party source notice is missing")
        for name, content in files.items():
            retired_surface = re.compile(
                rb"\b(?:TemplateDefinition(?:Input|Output)?|TemplateParameterDefinition|"
                rb"(?:Boolean|Enum|Regex|String|StringList)TemplateParameter|TemplateValue|"
                rb"UnrenderedTemplateRule|RULE_TEMPLATE_CONFLICT|TEMPLATE_PARAMETER_INVALID|"
                rb"TEMPLATE_RENDER_ERROR|ParameterBinding|policy-parameters-v1)\b"
            )
            if retired_surface.search(content):
                raise RuntimeError("Retired generic template surface leaked into the packed SDK")
            if name.endswith((".d.ts", ".d.cts")) and re.search(
                rb"\b(?:NormaSDKCore|RuleDefinitionOutput|namespaceKey|serverURL|apiKeyHeader|SemanticAuthoringVocabulary)\b", content
            ):
                raise RuntimeError("Internal wire type leaked into public declarations")
            # High-confidence credential forms only; no customer or provider fixtures belong here.
            if re.search(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
                         rb"(?:ghp_|github_pat_)[A-Za-z0-9_]{30,}|"
                         rb"AKIA[A-Z0-9]{16}|sk-(?:proj-)?[A-Za-z0-9_-]{32,}", content):
                raise RuntimeError("Credential-shaped content found in SDK payload")
        return {
            "name": metadata["name"], "version": metadata["version"],
            "dependencies": dependencies,
            "files": [{"path": name, "sha256": hashlib.sha256(content).hexdigest(),
                       "bytes": len(content)} for name, content in sorted(files.items())],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retained-receipts", type=Path)
    parser.add_argument("--output-dir", type=Path, default=SDK / "artifacts")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not (SDK / "dist/index.js").is_file():
        raise RuntimeError("Build the SDK before packed-consumer qualification")
    root = Path(tempfile.mkdtemp(prefix="norma-sdk-consumer-"))
    consumer = root / "consumer"
    consumer.mkdir()
    log = root / "qualification.log"
    npm = shutil.which("npm")
    node = shutil.which("node")
    if npm is None or node is None:
        raise RuntimeError("Local npm and Node are required; this script does not install them")
    (root / "user-npmrc").write_text("")
    (root / "global-npmrc").write_text("")
    common = [
        "--offline",
        "--ignore-scripts",
        "--no-update-notifier",
        "--cache",
        str(root / "cache"),
        "--userconfig",
        str(root / "user-npmrc"),
        "--globalconfig",
        str(root / "global-npmrc"),
    ]

    def run(args: list[str], cwd: Path = root, *, stage: str, expected_exit_code: int = 0,
            env: dict[str, str] | None = None) -> str:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False, env=env)
        diagnostic = failure_diagnostic(stage, result)
        with log.open("a") as stream:
            # npm and failed assertions can echo config values or customer receipt payloads.
            # Keep raw subprocess output in memory only; persisted diagnostics are structural.
            stream.write(json.dumps({**diagnostic, "status": "passed" if result.returncode == expected_exit_code
                                     else "failed"}) + "\n")
        if result.returncode != expected_exit_code:
            (output / "qualification-failure.json").write_text(
                json.dumps(diagnostic, indent=2) + "\n"
            )
            raise RuntimeError(f"Qualification failed: {json.dumps(diagnostic)}; local log: {log}")
        return result.stdout

    def pack(path: Path) -> Path:
        result = json.loads(
            run([npm, "pack", str(path), "--json", "--pack-destination", str(root), *common],
                stage="pack-sdk")
        )
        filename = result[0]["filename"]
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise RuntimeError("Unexpected npm tarball filename")
        return root / filename

    sdk_package = pack(SDK)
    manifest = inspect_package(sdk_package)
    zod_package = pack_installed_dependency(
        (SDK / "node_modules/zod").resolve(strict=True), root, "zod"
    )
    ajv_package = pack_installed_dependency(
        (SDK / "node_modules/ajv").resolve(strict=True), root, "ajv",
        expected_dependencies={"fast-deep-equal", "fast-uri", "json-schema-traverse", "require-from-string"},
    )
    yaml_package = pack_installed_dependency(
        (SDK / "node_modules/yaml").resolve(strict=True), root, "yaml"
    )
    native_peer_sources: dict[str, Path] = {}
    pending_peer_names = ["@openai/agents"]
    while pending_peer_names:
        name = pending_peer_names.pop()
        if name in native_peer_sources:
            continue
        source = installed_pnpm_package(name)
        metadata = json.loads((source / "package.json").read_text())
        native_peer_sources[name] = source
        peer_optional = {
            peer_name
            for peer_name, peer_metadata in metadata.get("peerDependenciesMeta", {}).items()
            if peer_metadata.get("optional") is True
        }
        pending_peer_names.extend(metadata.get("dependencies", {}).keys())
        for optional_name in metadata.get("optionalDependencies", {}):
            try:
                installed_pnpm_package(optional_name)
            except RuntimeError:
                continue
            pending_peer_names.append(optional_name)
        pending_peer_names.extend(
            peer_name
            for peer_name in metadata.get("peerDependencies", {})
            if peer_name not in peer_optional
        )
    native_peer_packages = [
        pack_installed_dependency(
            source,
            root,
            name,
            expected_dependencies=set(json.loads((source / "package.json").read_text()).get("dependencies", {})),
            allow_optional_dependencies=True,
        )
        for name, source in sorted(native_peer_sources.items())
    ]
    ajv_dependencies = [
        pack_installed_dependency(
            (SDK / "node_modules/.pnpm" / package_dir / "node_modules" / name).resolve(strict=True),
            root, name,
        )
        for package_dir, name in [
            ("fast-deep-equal@3.1.3", "fast-deep-equal"),
            ("fast-uri@3.1.9", "fast-uri"),
            ("json-schema-traverse@1.0.0", "json-schema-traverse"),
            ("require-from-string@2.0.2", "require-from-string"),
        ]
    ]
    (consumer / "package.json").write_text(
        json.dumps({"name": "norma-offline-consumer", "private": True, "type": "module",
                    "scripts": {"config:validate": "norma config validate", "config:check": "norma config check"}})
    )
    run(
        [
            npm,
            "install",
            str(sdk_package),
            str(ajv_package),
            str(yaml_package),
            str(zod_package),
            *(str(package) for package in ajv_dependencies),
            "--no-audit",
            "--no-fund",
            "--package-lock=false",
            *common,
        ],
        consumer,
        stage="install-consumer",
    )
    installed_metadata = json.loads(
        (consumer / "node_modules/norma-sdk/package.json").read_text()
    )
    if installed_metadata["name"] != "norma-sdk":
        raise RuntimeError("Consumer did not install the real Norma package")
    shutil.copyfile(SDK / "tests/fixtures/operation-receipt.json", consumer / "receipt.json")
    shutil.copyfile(SDK / "tests/fixtures/packed-consumer/consumer.mjs", consumer / "consumer.mjs")
    shutil.copyfile(
        SDK / "tests/fixtures/packed-consumer/control-consumer.mjs",
        consumer / "control-consumer.mjs",
    )

    shutil.copyfile(SDK / "tests/fixtures/packed-consumer/consumer.cjs", consumer / "consumer.cjs")

    shutil.copyfile(SDK / "tests/fixtures/packed-consumer/consumer.mts", consumer / "consumer.mts")

    shutil.copyfile(SDK / "tests/fixtures/packed-consumer/consumer.cts", consumer / "consumer.cts")
    shutil.copyfile(SDK / "tests/fixtures/packed-consumer/identity.mjs", consumer / "identity.mjs")
    shutil.copyfile(SDK / "tests/fixtures/packed-consumer/instrumentation.mjs", consumer / "instrumentation.mjs")
    shutil.copyfile(SDK / "tests/fixtures/packed-consumer/instrumentation.cjs", consumer / "instrumentation.cjs")
    shutil.copyfile(
        SDK / "tests/fixtures/packed-consumer/instrumentation-stream-proof.mjs",
        consumer / "instrumentation-stream-proof.mjs",
    )

    run([node, "consumer.mjs"], consumer, stage="esm-consumer")
    run([node, "control-consumer.mjs"], consumer, stage="controls-consumer")
    run([node, "consumer.cjs"], consumer, stage="commonjs-consumer")
    run([node, "identity.mjs"], consumer, stage="agent-identity-consumer")
    run([node, "instrumentation.mjs"], consumer, stage="instrumentation-esm-consumer")
    run([node, "instrumentation.cjs"], consumer, stage="instrumentation-commonjs-consumer")

    # Exercise the installed executable and package-local npm script with no
    # Norma credentials. The fixture points at an app module with a visible
    # side effect; validation must not import it or make a network request.
    cli_bin = consumer / "node_modules/.bin/norma"
    if not cli_bin.exists():
        raise RuntimeError("Offline consumer did not install the norma executable")
    marker = consumer / "application-imported.marker"
    app_module = consumer / "app-sentinel.mjs"
    app_module.write_text(
        "import { writeFileSync } from 'node:fs';\n"
        f"writeFileSync({json.dumps(str(marker))}, 'loaded');\n"
        "export function run() {}\n"
    )
    valid_config = consumer / "norma-offline-valid.json"
    valid_config.write_text(json.dumps({
        "version": 2,
        "entrypoints": [{"target": "app.run", "parameter_names": ["request"],
                         "context": {"path": ["args", 0]}}],
        "checkpoints": {},
        "connection": {"runtime_url": {"env": "NORMA_RUNTIME_URL"},
                       "agent_key": {"env": "NORMA_AGENT_KEY"}},
    }))
    invalid_config = consumer / "norma-offline-invalid.json"
    invalid_config.write_text(json.dumps({
        "version": 2,
        "entrypoints": [{"target": "app.run", "context": {"vale": "private"}}],
        "checkpoints": {},
    }))
    coverage_config = consumer / "norma-offline-coverage.json"
    coverage_config.write_text(json.dumps({
        "version": 2,
        "entrypoints": ["app.run"],
        "checkpoints": {"send": {"target": "app.send", "input_from": {"value": {"to": "person@example.test"}}}},
    }))
    local_rules = consumer / "norma-offline-rules.json"
    local_rules.write_text(json.dumps({
        "rules": [{"condition": {"selector": {"path": "input.to"}, "evaluator": {"name": "json", "config": {}}}}],
    }))
    network_attempt = consumer / "network-attempt.marker"
    preload = consumer / "offline-guard.cjs"
    preload.write_text(
        "const fs = require('node:fs');\n"
        f"const marker = {json.dumps(str(network_attempt))};\n"
        "const attempted = () => { fs.writeFileSync(marker, 'blocked'); throw new Error('network disabled'); };\n"
        "global.fetch = attempted;\n"
        "for (const name of ['node:http', 'node:https', 'node:net']) {\n"
        "  const mod = require(name);\n"
        "  for (const key of ['request', 'get', 'connect', 'createConnection']) {\n"
        "    if (typeof mod[key] === 'function') mod[key] = attempted;\n"
        "  }\n"
        "}\n"
    )
    offline_env = os.environ.copy()
    for key in list(offline_env):
        if key.startswith("NORMA_"):
            offline_env.pop(key)
    offline_env["NODE_OPTIONS"] = f"--require={preload}"
    json_output = run([str(cli_bin), "config", "validate", str(valid_config), "--json"], consumer,
                     stage="installed-cli-offline-valid", env=offline_env)
    valid_report = json.loads(json_output)
    if valid_report != {"valid": True, "language": "typescript", "files_checked": 1, "diagnostics": []}:
        raise RuntimeError("Installed CLI did not return the valid offline report")
    script_output = run([npm, "run", "--silent", "config:validate", "--", str(valid_config)], consumer,
                        stage="installed-npm-script-offline-valid", env=offline_env)
    if "Validated 1 configuration document offline." not in script_output:
        raise RuntimeError("Package-local npm validation script did not report success")
    coverage_output = run([str(cli_bin), "config", "check", str(coverage_config), "--rules", str(local_rules), "--json"], consumer,
                         stage="installed-cli-rule-coverage", env=offline_env)
    coverage_report = json.loads(coverage_output)
    if (coverage_report.get("valid") is not True or coverage_report.get("coverage_complete") is not True
            or coverage_report.get("rules_checked") != 1
            or [item.get("status") for item in coverage_report.get("coverage", [])] != ["covered", "covered"]):
        raise RuntimeError("Installed CLI did not prove local Rule path coverage offline")
    coverage_script_output = run([npm, "run", "--silent", "config:check", "--", str(coverage_config), "--rules", str(local_rules), "--json"], consumer,
                                 stage="installed-npm-script-rule-coverage", env=offline_env)
    if json.loads(coverage_script_output).get("valid") is not True:
        raise RuntimeError("Package-local npm Rule coverage script did not report success")
    all_params_config = consumer / "norma-offline-all-params.json"
    all_params_config.write_text(json.dumps({
        "version": 2,
        "entrypoints": ["app.run"],
        "checkpoints": {"send": {"target": "app.send", "parameter_names": ["recipient", "options"], "input_from": "all_params"}},
    }))
    all_params_rules = consumer / "norma-offline-all-params-rules.json"
    all_params_rules.write_text(json.dumps({
        "rules": [{"condition": {"selector": {"path": "input.recipient"}, "evaluator": {"name": "json", "config": {}}}, "scope": {"stages": ["pre"]}}],
    }))
    all_params_output = run([str(cli_bin), "config", "check", str(all_params_config), "--rules", str(all_params_rules), "--json"], consumer,
                            stage="installed-cli-all-params-coverage-unknown", expected_exit_code=1, env=offline_env)
    all_params_report = json.loads(all_params_output)
    if (all_params_report.get("valid") is not False or all_params_report.get("coverage_complete") is not False
            or [item.get("status") for item in all_params_report.get("coverage", [])] != ["unknown"]
            or [item.get("code") for item in all_params_report.get("diagnostics", [])] != ["coverage_unknown"]):
        raise RuntimeError("Installed CLI did not preserve unknown all_params coverage offline")
    invalid_output = run([str(cli_bin), "config", "validate", "--json", str(invalid_config)], consumer,
                         stage="installed-cli-offline-invalid", expected_exit_code=1, env=offline_env)
    invalid_report = json.loads(invalid_output)
    if (invalid_report.get("valid") is not False or
            [item.get("path") for item in invalid_report.get("diagnostics", [])] != ["/entrypoints/0/context/vale"] or
            any("private" in json.dumps(item) for item in invalid_report.get("diagnostics", []))):
        raise RuntimeError("Installed CLI did not return the single redacted schema diagnostic")
    help_output = run([str(cli_bin), "config", "validate", "--help"], consumer,
                      stage="installed-cli-help", env=offline_env)
    if "without loading application code or accessing the network" not in help_output:
        raise RuntimeError("Installed CLI help did not describe its offline boundary")
    if marker.exists() or network_attempt.exists():
        raise RuntimeError("Offline validation imported the application or attempted network access")

    native_consumer = root / "native-consumer"
    native_consumer.mkdir()
    (native_consumer / "package.json").write_text(
        json.dumps({"name": "norma-native-adapter-consumer", "private": True, "type": "module"})
    )
    for fixture_name in ("native-adapter.mjs", "native-adapter.cjs"):
        shutil.copyfile(SDK / "tests/fixtures/packed-consumer" / fixture_name, native_consumer / fixture_name)
    run(
        [
            npm,
            "install",
            str(sdk_package),
            str(ajv_package),
            str(yaml_package),
            str(zod_package),
            *(str(package) for package in ajv_dependencies),
            *(str(package) for package in native_peer_packages),
            "--no-audit",
            "--no-fund",
            "--package-lock=false",
            "--omit=optional",
            "--offline",
            *common,
        ],
        native_consumer,
        stage="install-native-optional-peer-consumer",
    )
    run([node, "native-adapter.mjs"], native_consumer, stage="native-adapter-esm-consumer")
    run([node, "native-adapter.cjs"], native_consumer, stage="native-adapter-commonjs-consumer")
    # Install the compiler independently, too: no ancestor node_modules or workspace links.
    tooling = root / "tooling"
    tooling.mkdir()
    (tooling / "package.json").write_text('{"name":"norma-consumer-tooling","private":true}')
    compiler_package = pack_installed_dependency(
        (SDK / "node_modules/typescript").resolve(strict=True), root, "typescript"
    )
    run([npm, "install", str(compiler_package), "--no-audit", "--no-fund", *common], tooling,
        stage="install-typescript-tooling")
    compiler = tooling / "node_modules/typescript/bin/tsc"
    run(
        [
            node,
            str(compiler),
            "--noEmit",
            "--strict",
            "--module",
            "NodeNext",
            "--moduleResolution",
            "NodeNext",
            "--target",
            "ES2022",
            "--lib",
            "ES2022,DOM,DOM.Iterable",
            "consumer.mts",
            "consumer.cts",
        ],
        consumer,
        stage="strict-nodenext-types",
    )
    if args.retained_receipts is not None:
        shutil.copyfile(args.retained_receipts, consumer / "retained.json")
        shutil.copyfile(
            SDK / "tests/fixtures/packed-consumer/retained.mjs", consumer / "retained.mjs"
        )
        run([node, "retained.mjs"], consumer, stage="retained-receipt-consumer")
    artifact = output / sdk_package.name
    shutil.copyfile(sdk_package, artifact)
    (output / "package-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    receipt = {
        "status": "passed",
        "package": "norma-sdk",
        "version": manifest["version"],
        "sdk_tarball": artifact.name,
        "sdk_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "checks": [
            "payload allowlist, dependency boundary and absence of source maps",
            "Apache license and bundled MIT notices",
            "fresh offline npm consumer with no workspace links",
            "installed local executable and npm script validate, and local Rule coverage check, with no Norma environment, app import, or network access",
            "ESM and CommonJS public imports",
            "ESM and CommonJS declarative parsing, two-agent directory configuration, pre-effect input-schema rejection, safe source failure diagnostics, and reassigned-slot guards",
            "ESM and CommonJS streamed finish: string and structured collection, terminal hold, early return, factory call shapes, and 8,000-codepoint Unicode",
            "configured OpenAI Agents native tool execution from the installed tarball in ESM and CommonJS with the optional peer present",
            "strict NodeNext ESM and CommonJS declarations",
            "semantic authoring, operations and guarded call contract",
            "independent per-agent credentials and 403 fail-closed behavior",
            "installed controls helper plus nonempty Agent receipt readback",
            "opt-in check, operation and finish receipt preservation with dual acknowledgement",
            "one-call rejection for missing contract acknowledgement",
        ],
        "transport": "synthetic in-memory HTTP; no backend or inference service",
        "node_version": run([node, "--version"], stage="node-version").strip(),
        "npm_version": run([npm, "--version"], stage="npm-version").strip(),
    }
    (output / "qualification.json").write_text(json.dumps(receipt, indent=2) + "\n")
    (output / "qualification-failure.json").unlink(missing_ok=True)
    print(json.dumps({**receipt, "artifact_directory": str(output),
                      "consumer_directory": str(consumer), "log": str(log)}, indent=2))


if __name__ == "__main__":
    main()
