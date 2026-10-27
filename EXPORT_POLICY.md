<!-- Modified for Norma. -->
# Public export policy

Export a reviewed source snapshot into a new repository. Do not copy the platform repository's `.git` directory, branches, tags, stash references, or combined history. Removing files in a later commit does not remove earlier versions from history.

## Allowed content

- Root README, Apache license, this policy, and minimal repository configuration.
- `python/`: hosted client source, public Rule and Decision models, internal wire adapters, package metadata, synthetic tests, packaging tools, README and NOTICE.
- `typescript/`: hosted client source, public Rule and Decision models, internal wire adapters, package metadata, synthetic tests, packaging tools, README and third-party notices.
- `docs/`: customer-facing onboarding, migration and tenancy guidance.
- `tools/verify_export.py` and reviewed check-only workflows under `.github/workflows/`.

Upstream credit is retained in the included notices, alongside required licenses, source copyright and modification notices. Do not export upstream or private changelogs, historical handoffs, internal qualification receipts, or sealed evidence. Synthetic protocol fixtures used by public package tests are permitted.

Select files explicitly. Do not recursively export the monorepo or copy cached build output. Exclude platform source, operator tools, native host installers, deployment files, private schemas, research notes, customer evidence, environment files, dependency trees and generated artifacts.

## Checks before publication

1. Verify imports and declared dependencies do not require platform code or private package indexes.
2. Review the public API model set and generated code. Removing a method export alone does not remove source files or types from a public repository. Public imports, constructors, enum names and values, docstrings, nested receipts, callback payloads and TypeScript declarations must use Rule and Check names. Wire adapters must use the same native Rule and Check contract; no retired domain aliases may ship. Test uninitialized checkpoints for zero side effects; a clean name scan is not an enforcement test.
3. Inspect actual wheels, source archives, JavaScript bundles, declarations and source maps. If maps include original source text, review those bytes as public source too.
4. Retain the complete license and applicable provenance/third-party notices. Review modification notices and copyright attribution when moving derived files.
5. Scan the complete selected snapshot and distributable bytes for credentials, private hostnames, operator paths, real customer records and unsupported product claims. Token-pattern checks alone are not a guarantee of clean content.
6. Build and test each package in isolation, including installation outside the source tree and denied/out-of-scope requests.
7. Review repository ownership, package-name ownership, versioning and release credentials separately. A GitHub organization does not reserve registry names.

Record the reviewed file selection in `EXPORT_MANIFEST.json` as a `files` array of objects with `path`, `bytes`, and `sha256`; exclude the manifest itself to avoid a recursive hash. Use Python 3.12 or newer. Run `python3 tools/verify_export.py PATH_TO_FRESH_SNAPSHOT` before initializing public Git history. The distributed verifier checks manifest hashes and unexpected files. Maintainers separately inspect source, declarations, dependencies, credentials, legal notices and actual network contracts before generating this manifest; installed-package tests cover behavior and transitive models. Its JSON output is an inventory of the inspected snapshot, not authorization to publish.

No publication, repository visibility change, transfer, deployment, or package release is part of preparing this export.

Internal wire adapters are shipped source. They use the native runtime contract; application code should use the supported models. Raw HTTP diagnostics may include caller payloads and require careful logging. See the [Python API guide](python/README.md), [TypeScript API guide](typescript/README.md), and [workspace tenancy guidance](docs/tenancy.md).
