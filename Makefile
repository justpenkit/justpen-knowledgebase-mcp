.DEFAULT_GOAL := help
PYTHON_PATHS := tests/ scripts/
COVERAGE_SOURCE := scripts.bootstrap

include scripts/development.mk
