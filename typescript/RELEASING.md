# Release preparation

Run `make install` and `make release-check` from this standalone directory. This uses only the checked-in public client contract and local SDK source. No private server, database, or schema generator is required.

The package version and transport metadata version must agree. `make check` validates the route allowlist, unit tests, lint and types, builds ESM/CommonJS and declarations without source maps, and installs the packed SDK and its sole runtime dependency in a temporary consumer outside the repository. Qualification receipts and SHA-256 manifests are written under `artifacts/`.

Publication is deliberately disabled. Before enabling a release workflow, verify registry ownership of `norma-sdk`, review the exact tarball manifest, preserve LICENSE and THIRD_PARTY_NOTICES, and run compatibility checks against the intended hosted release. Unit tests use synthetic in-memory transports; they do not establish live gateway authorization or tenant isolation.
