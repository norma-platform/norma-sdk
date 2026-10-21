# Modified for Norma.
"""Distribution-boundary regressions using synthetic package files only."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

SDK = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qualify_packed_sdk", SDK / "scripts/qualify_packed_sdk.py"
)
assert SPEC and SPEC.loader
QUALIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUALIFIER)


class PackagePayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        # Given: the explicit public package boundary, without app or customer source.
        self.files = {name: b"synthetic public SDK" for name in QUALIFIER.PAYLOAD_FILES}
        self.files["package/package.json"] = json.dumps({
            "name": "norma-sdk", "version": "3.2.1-dev.1", "dependencies": {"zod": "^4.1.11"},
        }).encode()
        for name in ("LICENSE", "THIRD_PARTY_NOTICES"):
            self.files[f"package/{name}"] = (SDK / name).read_bytes()

    def inspect(self, *, symlink: str | None = None) -> dict[str, object]:
        # Given: archive internals are necessary to verify unsafe distribution members.
        with tempfile.TemporaryDirectory(prefix="norma-package-boundary-") as temp:
            path = Path(temp) / "synthetic.tgz"
            with tarfile.open(path, "w:gz") as archive:
                for name, value in self.files.items():
                    member = tarfile.TarInfo(name)
                    if name == symlink:
                        member.type = tarfile.SYMTYPE
                        member.linkname = "../../" + "server/private.py"
                        archive.addfile(member)
                    else:
                        member.size = len(value)
                        archive.addfile(member, io.BytesIO(value))
            # When: inspect the actual archive bytes consumers would receive.
            return QUALIFIER.inspect_package(path)

    def test_accepts_reviewed_public_payload(self) -> None:
        result = self.inspect()
        # Then: every public file is accounted for by content hash.
        self.assertEqual(result["name"], "norma-sdk")
        self.assertEqual(len(result["files"]), len(self.files))

    def test_rejects_transitive_wire_declaration_leaks(self) -> None:
        for token in ("NormaSDKCore", "RuleDefinitionOutput", "namespaceKey",
                      "serverURL", "apiKeyHeader", "SemanticAuthoringVocabulary"):
            with self.subTest(token=token):
                self.files["package/dist/index.d.ts"] = f"type Hidden = {{ {token}: string }};".encode()
                with self.assertRaisesRegex(RuntimeError, "wire type leaked"):
                    self.inspect()

    def test_rejects_extra_app_or_fixture_content(self) -> None:
        for name in ("package/server/private.py", "package/tests/customer.json", "package/.env"):
            with self.subTest(name=name):
                self.files[name] = b"must not ship"
                # Then: an unexpected file fails before package installation.
                with self.assertRaisesRegex(RuntimeError, "allowlist"):
                    self.inspect()
                del self.files[name]

    def test_rejects_private_source_map_origin(self) -> None:
        self.files["package/dist/index.js.map"] = json.dumps({
            "sources": ["../../" + "server/private.py"],
        }).encode()
        with self.assertRaisesRegex(RuntimeError, "allowlist"):
            self.inspect()

    def test_rejects_workspace_runtime_dependency(self) -> None:
        self.files["package/package.json"] = json.dumps({
            "name": "norma-sdk", "version": "0.0.0", "dependencies": {"zod": "workspace" + ":*"},
        }).encode()
        with self.assertRaisesRegex(RuntimeError, "independent of the app"):
            self.inspect()

    def test_rejects_symlinks(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "regular files only"):
            self.inspect(symlink="package/dist/index.js")

    def test_requires_distributed_notices(self) -> None:
        self.files["package/THIRD_PARTY_NOTICES"] = b"Missing bundled MIT licenses"
        with self.assertRaisesRegex(RuntimeError, "reviewed THIRD_PARTY_NOTICES"):
            self.inspect()

    def test_rejects_credential_shaped_content_without_echoing_it(self) -> None:
        self.files["package/dist/index.js"] = b"-----BEGIN " + b"PRIVATE KEY-----"
        with self.assertRaisesRegex(RuntimeError, "Credential-shaped content"):
            self.inspect()


class InstalledDependencyTests(unittest.TestCase):
    def test_archives_installed_bytes_independent_of_pnpm_store_location(self) -> None:
        # Given: an installed package under the same nested layout used by pnpm.
        with tempfile.TemporaryDirectory(prefix="norma-dependency-") as temp:
            root = Path(temp)
            source = root / "node_modules/.pnpm/zod@4.3.6/node_modules/zod"
            source.mkdir(parents=True)
            metadata = b'{"name":"zod","version":"4.3.6","license":"MIT"}'
            (source / "package.json").write_bytes(metadata)
            (source / "LICENSE").write_bytes(b"synthetic unchanged license")
            (source / "index.js").write_bytes(b"export const synthetic = true;")
            (source / "identical.js").hardlink_to(source / "index.js")
            # When: create a dependency archive without invoking npm on its install directory.
            packed = QUALIFIER.pack_installed_dependency(source, root, "zod")
            # Then: npm's expected package prefix preserves exact installed bytes and notices.
            with tarfile.open(packed) as archive:
                self.assertEqual(set(archive.getnames()), {
                    "package/package.json", "package/LICENSE", "package/index.js",
                    "package/identical.js",
                })
                self.assertTrue(all(member.isfile() for member in archive.getmembers()))
                self.assertEqual(archive.extractfile("package/identical.js").read(),
                                 (source / "index.js").read_bytes())
                self.assertEqual(archive.extractfile("package/package.json").read(), metadata)
                self.assertEqual(archive.extractfile("package/LICENSE").read(),
                                 b"synthetic unchanged license")

    def test_rejects_dependency_symlinks_and_private_files(self) -> None:
        with tempfile.TemporaryDirectory(prefix="norma-dependency-") as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "package.json").write_text('{"name":"zod","version":"4.3.6"}')
            (source / "private.js").symlink_to(root / "outside.js")
            with self.assertRaisesRegex(RuntimeError, "regular files"):
                QUALIFIER.pack_installed_dependency(source, root, "zod")
            (source / "private.js").unlink()
            (source / ".npmrc").write_text("synthetic private configuration")
            with self.assertRaisesRegex(RuntimeError, "private or nested"):
                QUALIFIER.pack_installed_dependency(source, root, "zod")

    def test_requires_explicit_transitive_dependency_set(self) -> None:
        with tempfile.TemporaryDirectory(prefix="norma-dependency-") as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            (source / "package.json").write_text(json.dumps({
                "name": "zod", "version": "4.3.6", "dependencies": {"unreviewed": "*"},
            }))
            with self.assertRaisesRegex(RuntimeError, "transitive dependency set"):
                QUALIFIER.pack_installed_dependency(source, root, "zod")

    def test_failure_diagnostics_exclude_subprocess_payloads(self) -> None:
        # Given: npm may print config values or test assertions with private payloads.
        result = subprocess.CompletedProcess(["npm"], 1,
            stdout="synthetic-private-payload", stderr="npm error code ENOENT\n"
            "npm error private-url-and-credential\n")
        # When: convert the failure into a CI-safe diagnostic.
        diagnostic = QUALIFIER.failure_diagnostic("install-consumer", result)
        # Then: only the step and machine code are emitted.
        self.assertEqual(diagnostic, {
            "status": "failed", "stage": "install-consumer", "exit_code": 1,
            "npm_error_code": "ENOENT", "tool_error": None,
        })
        self.assertNotIn("private", json.dumps(diagnostic))
        result.stderr = "npm error Exit handler never called!\nprivate output"
        diagnostic = QUALIFIER.failure_diagnostic("pack-sdk", result)
        self.assertEqual(diagnostic["tool_error"], "npm-exit-handler-not-called")
        self.assertNotIn("private", json.dumps(diagnostic))


if __name__ == "__main__":
    unittest.main()
