SHELL := /bin/bash
PYTHON ?= python3
ARTIFACT_DIR ?= $(CURDIR)/artifacts

.PHONY: help install install-no-frozen lint typecheck test build check release-check publish-dry-run publish clean pack-check

help:
	@echo "TypeScript SDK Makefile targets"
	@echo "  make install            - install dependencies (frozen lockfile)"
	@echo "  make lint               - run ESLint"
	@echo "  make typecheck          - run tsc --noEmit"
	@echo "  make test               - run Vitest"
	@echo "  make build              - build package with tsup"
	@echo "  make pack-check         - qualify a built tarball in an offline temporary consumer"
	@echo "  make check              - lint, typecheck, test, build, and qualify the tarball"
	@echo "  make release-check      - prepare qualified artifacts without publishing"

install:
	pnpm install --frozen-lockfile

install-no-frozen:
	pnpm install

lint:
	pnpm run lint

typecheck:
	$(PYTHON) scripts/check_public_contract.py
	pnpm run typecheck

test:
	pnpm run test
	$(PYTHON) -m unittest discover -s tests -p 'test_package_payload.py'

build:
	pnpm run build

check:
	$(MAKE) lint
	$(MAKE) typecheck
	$(MAKE) test
	$(MAKE) build
	$(MAKE) pack-check

release-check: check

# Preserve target names used by old automation, but never silently publish.
publish-dry-run: release-check
	@echo "Prepared local artifacts only; registry publication is disabled."

publish:
	@echo "Registry publication is disabled. See RELEASING.md for the approval boundary." >&2
	@exit 1

clean:
	rm -rf dist

pack-check:
	$(PYTHON) scripts/qualify_packed_sdk.py --output-dir "$(ARTIFACT_DIR)"
