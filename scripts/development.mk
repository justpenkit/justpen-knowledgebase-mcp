.PHONY: help version install setup clean test test-one test-permissions lint lint-fix format format-check format-ruff format-ruff-check format-md format-md-check format-toml format-toml-check format-yaml format-yaml-check format-json format-json-check typecheck audit check docs-build docs-catalog docs-serve bump-patch bump-minor bump-major release-tag changelog pre-commit lock-check format-project-text
.PHONY: format-html format-html-check format-css format-css-check
.PHONY: test-integration check-static check-python install-taplo
.PHONY: uv-add uv-remove uv-upgrade uv-lock uv-reinstall guard-accept-changes

VENV := .venv
# Preserve literal test IDs instead of evaluating Make expressions supplied in TEST.
override TEST := $(value TEST)
export TEST
# Same for dependency names, which may contain spaces, quotes or `$`.
override PKG := $(value PKG)
export PKG
override GROUP := $(value GROUP)
export GROUP

help::
	@echo "Makefile targets:"
	@echo "  install                Install locked dev/docs dependencies; initialize a missing lock"
	@echo "  setup                  Install dependencies and all Git hook stages"
	@echo "  install-taplo          Install the pinned taplo binary on linux aarch64 (no wheel)"
	@echo "  uv-add PKG=… [GROUP=…] Add a dependency, optionally to a group"
	@echo "  uv-remove PKG=… [GROUP=…]  Remove a dependency, optionally from a group"
	@echo "  uv-upgrade [PKG=…]     Upgrade the lock (one package or all) and sync"
	@echo "  uv-lock                Relock after a pyproject.toml change and sync"
	@echo "  uv-reinstall           Delete .venv and rebuild it from the lock"
	@echo "  guard-accept-changes   Record the current protected files as approved (asks the user)"
	@echo "  clean                  Remove venv, build outputs and caches"
	@echo "  test                   Run unit tests with coverage in the active Python"
	@echo "  test-integration       Run real tool and project integration scenarios (CI)"
	@echo "  test-one TEST=tests/…   Run one test file or node for focused feedback"
	@echo "  test-permissions       Verify agent policy and Codex rules (CODEX_TEST_BINARY required)"
	@echo "  lint | lint-fix        Run Ruff checks or safe fixes"
	@echo "  format | format-check  Format all supported files or check without rewriting"
	@echo "  format-{ruff,md,toml,yaml,json,html,css}[-check]   Individual formatters"
	@echo "  typecheck              Strict Pyright for the active uv Python"
	@echo "  audit                  Audit locked runtime, dev and docs dependencies"
	@echo "  lock-check             Verify uv.lock agrees with pyproject.toml"
	@echo "  check                  lock-check, formatting, lint, typing and tests"
	@echo "  check-static           Formatting and lint (shared CI checks)"
	@echo "  check-python           Active Python typing and unit tests (per CI Python)"
	@echo "  pre-commit             Run all pre-commit hooks"
	@echo "  docs-build             Build MkDocs with strict link and anchor checks"
	@echo "  docs-catalog           Regenerate the catalog reference page from the manifest"
	@echo "  docs-serve             Serve MkDocs locally with live reload"
	@echo "  changelog              Generate the changelog with Commitizen"
	@echo "  bump-patch|bump-minor|bump-major   Prepare version, changelog and commit for review"
	@echo "  release-tag            Annotate the reviewed release merge on updated main"
	@echo "  version                Print current version"

version:
	@uv version --short

install:
	@if [ ! -f uv.lock ]; then uv lock; fi
	uv sync --locked --group dev --group docs

install-taplo:
	uv run --group dev python scripts/install_taplo.py

uv-add:
	@test -n "$$PKG" || { echo "Usage: make uv-add PKG=<spec> [GROUP=<group>]" >&2; exit 2; }
	@if [ -n "$$GROUP" ]; then uv add --group "$$GROUP" "$$PKG"; else uv add "$$PKG"; fi
	$(MAKE) format-toml

uv-remove:
	@test -n "$$PKG" || { echo "Usage: make uv-remove PKG=<name> [GROUP=<group>]" >&2; exit 2; }
	@if [ -n "$$GROUP" ]; then uv remove --group "$$GROUP" "$$PKG"; else uv remove "$$PKG"; fi
	$(MAKE) format-toml

uv-upgrade:
	@if [ -n "$$PKG" ]; then uv lock --upgrade-package "$$PKG"; else uv lock --upgrade; fi
	uv sync --locked --group dev --group docs
	$(MAKE) format-toml

uv-lock:
	uv lock
	uv sync --locked --group dev --group docs
	$(MAKE) format-toml

# Delete first: syncing in place would keep a planted .pth file. The sync cannot
# restore taplo on linux aarch64, where it lives outside the lock.
uv-reinstall:
	rm -rf $(VENV)
	uv sync --locked --group dev --group docs
	$(MAKE) install-taplo

guard-accept-changes:
	/usr/bin/python3 -I -B scripts/hooks/guard_config.py --event accept

setup: install install-taplo
	uv run --group dev pre-commit install --install-hooks
	@# Install the plugins .claude/settings.json declares, in project scope. The CLI
	@# rewrites that tracked file, so its exact bytes are restored afterwards.
	@if command -v claude >/dev/null 2>&1; then \
	    backup=$$(mktemp) && cp .claude/settings.json "$$backup" && cmp -s .claude/settings.json "$$backup" || \
	        { rm -f "$$backup"; echo "Could not back up .claude/settings.json; not installing Claude Code plugins." >&2; exit 1; }; \
	    trap 'cp "$$backup" .claude/settings.json && rm -f "$$backup"' EXIT; trap 'exit 130' INT TERM; \
	    { claude plugin marketplace add anthropics/claude-plugins-official --scope project && \
	      claude plugin marketplace add EveryInc/compound-engineering-plugin --scope project && \
	      claude plugin install pyright-lsp@claude-plugins-official --scope project && \
	      claude plugin install compound-engineering@compound-engineering-plugin --scope project || \
	      echo "Claude Code plugin install failed; install the project plugins from /plugin." >&2; }; \
	fi
	@# Download the marketplace .codex/config.toml declares. Codex reads project tables
	@# only for a trusted project, so trust is granted for this one command only.
	@if command -v codex >/dev/null 2>&1; then \
	    codex plugin marketplace upgrade compound-engineering-plugin \
	        -c 'projects={"$(CURDIR)"={trust_level="trusted"}}' || \
	    echo "Codex plugin download failed; trust this project in Codex, then run: codex plugin marketplace upgrade compound-engineering-plugin" >&2; \
	fi
	$(MAKE) format

# Rendered user descriptions and answers may need quoting/wrapping normalization.
format-project-text: format-md format-toml format-yaml format-json

pre-commit:
	uv run --group dev pre-commit run --all-files

clean:
	rm -rf $(VENV) dist/ build/ htmlcov/ site/ .pytest_cache .ruff_cache
	rm -f .coverage .coverage.* coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "Cleaned."

test:
	uv run --group dev --group docs pytest tests/ -v -m "not integration" \
	    --cov=$(COVERAGE_SOURCE) --cov-report=term-missing

test-integration:
	uv run --locked --group dev --group docs pytest -v -m integration

test-one:
	@case "$${TEST%%::*}" in */../*|*/..) ;; tests/?*) exit 0 ;; esac; \
	    echo "Usage: make test-one TEST=tests/test_file.py[::test_name]" >&2; exit 2
	uv run --group dev --group docs pytest "$$TEST" -v

test-permissions:
	@test -n "$$CODEX_TEST_BINARY" || { \
	    echo "Set CODEX_TEST_BINARY to an installed Codex CLI executable." >&2; exit 2; }
	uv run --group dev pytest tests/test_agent_permissions.py -v

lint:
	uv run --group dev ruff check $(PYTHON_PATHS)

lint-fix:
	uv run --group dev ruff check --fix $(PYTHON_PATHS)

format: format-ruff format-project-text format-html format-css

format-check: format-ruff-check format-md-check format-toml-check format-yaml-check format-json-check format-html-check format-css-check

format-ruff:
	uv run --group dev ruff format $(PYTHON_PATHS)

format-ruff-check:
	uv run --group dev ruff format --check $(PYTHON_PATHS)

format-md format-toml format-yaml format-json format-html format-css:
	uv run --group dev python scripts/format_files.py $(patsubst format-%,%,$@)

format-md-check format-toml-check format-yaml-check format-json-check format-html-check format-css-check:
	uv run --group dev python scripts/format_files.py $(patsubst %-check,%,$(patsubst format-%,%,$@)) --check

typecheck:
	@project_python=$$(uv run --group dev python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")') || exit $$?; \
	    uv run --group dev pyright --pythonversion "$$project_python" $(PYTHON_PATHS)

audit:
	@mkdir -p .cache
	uv export --locked --all-groups --no-emit-project --output-file .cache/audit-requirements.txt
	uv tool run pip-audit --strict --disable-pip --require-hashes -r .cache/audit-requirements.txt

lock-check:
	@test -f uv.lock || { echo "uv.lock is missing; initialize it with make setup and commit it." >&2; exit 1; }
	uv lock --check

check-static:
	$(MAKE) format-check lint

check-python:
	$(MAKE) typecheck test

check: lock-check
	$(MAKE) check-static check-python

docs-build:
	uv run --group docs mkdocs build --strict

docs-serve:
	uv run --group docs mkdocs serve

docs-catalog:
	uv run python -m scripts.catalog_reference
	$(MAKE) format-md

changelog:
	uv run --group dev python scripts/release.py changelog

bump-patch bump-minor bump-major:
	uv run --group dev python scripts/release.py bump $(patsubst bump-%,%,$@)

release-tag:
	uv run --group dev python scripts/release.py tag
