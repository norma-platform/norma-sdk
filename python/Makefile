UV ?= uv
PYTHON := .venv/bin/python
.PHONY: sync install test lint typecheck check build
sync install:
	$(UV) sync --frozen --no-install-project --no-build --extra strands-agents --extra otel --extra pydantic-ai --extra openai-agents
	$(UV) pip install --python $(PYTHON) --no-deps --no-build-isolation -e .

test:
	$(PYTHON) -m pytest tests

lint:
	$(PYTHON) -m ruff check src tests

typecheck:
	$(PYTHON) -m mypy src/norma

check: lint typecheck test

build:
	$(PYTHON) -m build --no-isolation

.PHONY: lint-fix format test-wheel
lint-fix:
	$(PYTHON) -m ruff check src tests --fix

format:
	$(PYTHON) -m ruff format src tests

test-wheel: build
	$(PYTHON) scripts/check_installed_wheel.py

# Exact real-framework matrix; installs public packages into temporary environments.
ADK_VERSIONS ?= 1.7.0 2.12.0
.PHONY: test-google-adk
test-google-adk: build
	$(PYTHON) scripts/run_google_adk_checks.py $(ADK_VERSIONS)

OPENAI_AGENTS_VERSION ?= 0.22.3
.PHONY: test-openai-agents
test-openai-agents: build
	$(PYTHON) scripts/run_openai_agents_checks.py $(OPENAI_AGENTS_VERSION)
