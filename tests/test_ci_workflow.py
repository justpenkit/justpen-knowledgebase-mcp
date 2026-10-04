"""CI spends one billed job on checks: every Python version on pull requests, 3.13 on main."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
PULL_REQUEST_ONLY = "github.event_name == 'pull_request'"


def workflow(name: str) -> dict[Any, Any]:
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def triggers(config: dict[Any, Any]) -> Any:
    # PyYAML's YAML 1.1 loader interprets the unquoted Actions key `on` as True.
    return config.get("on", config.get(True))


def python_checks(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map each Python version to the step that runs its typing and unit tests."""
    checks: dict[str, dict[str, Any]] = {}
    for step in job["steps"]:
        if "make check-python" in step.get("run", ""):
            version = step.get("env", {}).get("UV_PYTHON", job["env"]["UV_PYTHON"])
            checks[version] = step
    return checks


def test_ci_keeps_its_name_and_triggers_for_docs_deployment():
    config = workflow("ci.yml")
    assert config["name"] == "CI"
    assert triggers(config) == {"pull_request": None, "push": {"branches": ["main"]}}


def test_one_job_runs_the_checks_and_integration_waits_for_it():
    jobs = workflow("ci.yml")["jobs"]
    assert set(jobs) == {"check", "integration", "consumer-runtime", "deploy-docs"}
    assert jobs["integration"]["needs"] == "check"
    assert jobs["integration"]["if"] == "needs.check.outputs.generated == 'true'"


def test_the_check_job_keeps_every_static_gate():
    runs = [step.get("run", "") for step in workflow("ci.yml")["jobs"]["check"]["steps"]]
    for gate in ("make lock-check", "make install", "make check-static", "make docs-build"):
        assert gate in runs, gate


def test_pull_requests_test_every_version_and_main_tests_313():
    checks = python_checks(workflow("ci.yml")["jobs"]["check"])
    assert set(checks) == {"3.11", "3.12", "3.13"}
    assert "if" not in checks["3.13"]
    for version in ("3.11", "3.12"):
        assert checks[version]["if"] == PULL_REQUEST_ONLY, version


def test_each_extra_version_installs_its_own_environment_before_checking():
    checks = python_checks(workflow("ci.yml")["jobs"]["check"])
    for version in ("3.11", "3.12"):
        run = checks[version]["run"]
        assert run.index("make install") < run.index("make check-python"), version


def test_template_ci_runs_only_for_pull_requests():
    path = ROOT / ".github/workflows/template-ci.yml"
    if not path.is_file():
        pytest.skip("Template CI belongs to the generator only.")
    assert triggers(workflow("template-ci.yml")) == {"pull_request": None}
