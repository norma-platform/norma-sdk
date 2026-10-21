# Modified for Norma.
"""Qualify the built SDK as an offline npm tarball consumer; never publish or fetch."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

SDK = Path(__file__).resolve().parents[1]


def pack_installed_dependency(source: Path, output: Path, expected_name: str) -> Path:
    """Archive installed dependency bytes without npm interpreting a pnpm store path."""
    metadata = json.loads((source / "package.json").read_text())
    version = metadata.get("version", "")
    if metadata.get("name") != expected_name or not re.fullmatch(r"[0-9A-Za-z.+-]+", version):
        raise RuntimeError("Installed dependency identity is invalid")
    if metadata.get("dependencies") or metadata.get("optionalDependencies"):
        raise RuntimeError(
            "Offline dependency packing requires an explicit transitive dependency set"
        )
    members = sorted(source.rglob("*"))
    for member in members:
        relative = member.relative_to(source)
        if any(part in {"node_modules", ".git", ".npmrc"} or part.startswith(".env")
               for part in relative.parts):
            raise RuntimeError("Installed dependency contains unexpected private or nested files")
        if member.is_symlink() or not (member.is_file() or member.is_dir()):
            raise RuntimeError("Installed dependency must contain regular files and directories")
    path = output / f"{expected_name}-{version}.tgz"
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
}


def inspect_package(path: Path) -> dict[str, object]:
    """Inspect the distributable bytes, excluding source trees, fixtures and app internals."""
    with tarfile.open(path) as package:
        members = package.getmembers()
        names = {member.name for member in members}
        shared = {name for name in names if re.fullmatch(r"package/dist/client-[A-Za-z0-9_-]+\.d\.(?:ts|cts)", name)}
        if shared and (len(shared) != 2 or len({name.rsplit(".", 1)[0] for name in shared}) != 1):
            raise RuntimeError("Unexpected shared declaration graph")
        if names - shared != PAYLOAD_FILES or len(members) != len(names):
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
        dependencies = metadata.get("dependencies", {})
        if set(dependencies) != {"zod"} or any(
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

    def run(args: list[str], cwd: Path = root, *, stage: str) -> str:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
        diagnostic = failure_diagnostic(stage, result)
        with log.open("a") as stream:
            # npm and failed assertions can echo config values or customer receipt payloads.
            # Keep raw subprocess output in memory only; persisted diagnostics are structural.
            stream.write(json.dumps({**diagnostic, "status": "failed" if result.returncode
                                     else "passed"}) + "\n")
        if result.returncode:
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
    (consumer / "package.json").write_text(
        json.dumps({"name": "norma-offline-consumer", "private": True, "type": "module"})
    )
    run(
        [
            npm,
            "install",
            str(sdk_package),
            str(zod_package),
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

    shutil.copyfile(SDK / "tests/fixtures/packed-consumer/consumer.cjs", consumer / "consumer.cjs")

    shutil.copyfile(SDK / "tests/fixtures/packed-consumer/consumer.mts", consumer / "consumer.mts")

    shutil.copyfile(SDK / "tests/fixtures/packed-consumer/consumer.cts", consumer / "consumer.cts")
    shutil.copyfile(SDK / "tests/fixtures/packed-consumer/identity.mjs", consumer / "identity.mjs")

    run([node, "consumer.mjs"], consumer, stage="esm-consumer")
    run([node, "consumer.cjs"], consumer, stage="commonjs-consumer")
    run([node, "identity.mjs"], consumer, stage="agent-identity-consumer")
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
            "ESM and CommonJS public imports",
            "strict NodeNext ESM and CommonJS declarations",
            "semantic authoring, operations and guarded call contract",
            "independent per-agent credentials and 403 fail-closed behavior",
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
