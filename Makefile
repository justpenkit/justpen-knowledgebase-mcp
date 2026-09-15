.DEFAULT_GOAL := help
PYTHON_PATHS := src/ tests/ scripts/
COVERAGE_SOURCE := justpen_knowledgebase_mcp

include scripts/development.mk

.PHONY: test-consumer benchmark-kb test-native

test-consumer:
	uv run --no-project python scripts/runtime_validation.py consumer

test-native:
	uv run --no-project python scripts/runtime_validation.py native

SCALE ?= smoke
BENCHMARK_SECONDS ?= 1200
BENCHMARK_OUTPUT ?= .superpowers/benchmarks/$(SCALE)
BENCHMARK_PHASE ?= all
BENCHMARK_RESUME ?= 0
BENCHMARK_CORPUS_OUTPUT ?=
benchmark-kb:
	uv run --no-project python scripts/runtime_validation.py benchmark --scale "$(SCALE)" --output "$(BENCHMARK_OUTPUT)" --max-seconds "$(BENCHMARK_SECONDS)" --phase "$(BENCHMARK_PHASE)" $(if $(filter 1,$(BENCHMARK_RESUME)),--resume,) $(if $(BENCHMARK_CORPUS_OUTPUT),--corpus-output "$(BENCHMARK_CORPUS_OUTPUT)",)
