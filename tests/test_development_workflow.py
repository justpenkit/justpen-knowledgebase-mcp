"""Check Make command routing without running the project toolchain."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_full_integration_suite_runs_on_python_313():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    integration = workflow["jobs"]["integration"]
    assert integration["strategy"]["matrix"]["python-version"] == ["3.13"]
    assert integration["strategy"]["fail-fast"] is False
    assert integration["env"]["UV_PYTHON"] == "${{ matrix.python-version }}"
    assert "${{ matrix.python-version }}" in integration["name"]
    assert integration["needs"] == "quality"
    assert integration["if"] == "needs.quality.outputs.generated == 'true'"
    assert any(step.get("run") == "make test-integration" for step in integration["steps"])


def test_documentation_deploy_waits_for_every_validation_job():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    deploy = workflow["jobs"]["deploy-docs"]
    assert set(deploy["needs"]) == {"quality", "check", "integration", "consumer-runtime"}
    condition = deploy["if"]
    for job in deploy["needs"]:
        assert f"needs.{job}.result == 'success'" in condition
    assert "github.event_name == 'push'" in condition
    assert "github.ref == 'refs/heads/main'" in condition
    assert deploy["environment"] == {
        "name": "production",
        "url": "https://justpen-knowledgebase-mcp.justpenkit.justmumu.com/",
    }


def test_documentation_deploy_checks_credentials_head_and_pages_project():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = workflow["jobs"]["deploy-docs"]["steps"]
    rendered = json.dumps(steps)
    assert "CLOUDFLARE_ACCOUNT_ID" in rendered
    assert "CLOUDFLARE_API_TOKEN" in rendered
    assert "make docs-build" in rendered
    assert "git rev-parse FETCH_HEAD" in rendered
    publish = next(step for step in steps if step.get("uses") == "cloudflare/wrangler-action@v4.0.0")
    assert publish["with"]["command"] == (
        "pages deploy site --project-name=justpen-knowledgebase-mcp --branch=main --commit-hash=${{ github.sha }}"
    )


@pytest.fixture
def make_project(tmp_path):
    return create_make_project(tmp_path)


def create_make_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    shutil.copyfile(ROOT / "Makefile", project / "Makefile")
    (project / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/development.mk", project / "scripts/development.mk")
    (project / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    binary = tmp_path / "bin"
    binary.mkdir()
    # Capture tool arguments, but delegate the formatter runner to real tools.
    uv = binary / "uv"
    uv.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "if sys.argv[1:5] == ['run', '--group', 'dev', 'python']:\n"
        "    os.execv(sys.executable, [sys.executable, *sys.argv[5:]])\n"
        "print(json.dumps(sys.argv[1:]))\n"
    )
    uv.chmod(0o755)
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    # A parent `make test-one TEST=…` must not override this fixture's selector.
    for name in ("MAKEFLAGS", "MAKEOVERRIDES", "MFLAGS", "MAKELEVEL"):
        environment.pop(name, None)
    environment["PATH"] = os.pathsep.join((str(binary), str(Path(sys.executable).parent), environment["PATH"]))
    environment.pop("CODEX_TEST_BINARY", None)
    environment.pop("TEST", None)
    return project, environment


@pytest.mark.parametrize("existing_lock", [False, True])
def test_install_initializes_only_a_missing_lock(make_project, existing_lock):
    project, environment = make_project
    if existing_lock:
        (project / "uv.lock").write_text("fixture\n")
    result = subprocess.run(
        ["make", "-s", "install"], cwd=project, env=environment, text=True, capture_output=True, check=True
    )
    calls = [json.loads(line) for line in result.stdout.splitlines()]
    assert calls == ([] if existing_lock else [["lock"]]) + [["sync", "--locked", "--group", "dev", "--group", "docs"]]


def test_typecheck_uses_only_the_active_python(make_project):
    project, environment = make_project
    result = subprocess.run(
        ["make", "-s", "typecheck"], cwd=project, env=environment, text=True, capture_output=True, check=True
    )
    calls = [json.loads(line) for line in result.stdout.splitlines()]
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert calls == [["run", "--group", "dev", "pyright", "--pythonversion", version, *make_paths()]]


def make_paths():
    return ["src/", "tests/", "scripts/"] if (ROOT / "src").exists() else ["tests/", "scripts/"]


def test_python_gate_runs_typing_and_unit_coverage_once(make_project):
    project, environment = make_project
    result = subprocess.run(
        ["make", "-s", "check-python"], cwd=project, env=environment, text=True, capture_output=True, check=True
    )
    calls = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(calls) == 2
    assert calls[0][3] == "pyright"
    assert calls[1][:10] == [
        "run",
        "--group",
        "dev",
        "--group",
        "docs",
        "pytest",
        "tests/",
        "-v",
        "-m",
        "not integration",
    ]
    assert any(argument.startswith("--cov=") for argument in calls[1])
    assert "--cov-report=term-missing" in calls[1]


def test_lock_gate_rejects_a_missing_lock_without_creating_it(make_project):
    project, environment = make_project
    result = subprocess.run(
        ["make", "-s", "lock-check"], cwd=project, env=environment, text=True, capture_output=True, check=False
    )
    assert result.returncode != 0
    assert "uv.lock" in result.stderr
    assert not (project / "uv.lock").exists()


@pytest.mark.parametrize("selection_source", ["environment", "argument"])
@pytest.mark.parametrize(
    "selector",
    [
        "tests/test_example.py",
        "tests/test_example.py::test_case[with space]",
        "tests/test_example.py;touch injected",
        "tests/test_example.py::test_case[$(shell touch injected)]",
    ],
)
def test_selected_test_is_one_literal_argument(make_project, selector, selection_source):
    project, environment = make_project
    arguments = ["make", "-s", "test-one"]
    if selection_source == "environment":
        environment["TEST"] = selector
    else:
        arguments.append(f"TEST={selector}")
    result = subprocess.run(arguments, cwd=project, env=environment, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["run", "--group", "dev", "--group", "docs", "pytest", selector, "-v"]
    assert not (project / "injected").exists()


@pytest.mark.parametrize(
    "selector", ["", "--override-ini=addopts=", "/outside/unrelated.py", "tests/../../unrelated.py"]
)
def test_selected_test_rejects_missing_or_option_input(make_project, selector):
    project, environment = make_project
    environment["TEST"] = selector
    result = subprocess.run(
        ["make", "-s", "test-one"], cwd=project, env=environment, capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "TEST=tests/" in result.stderr
    assert result.stdout == ""


def test_integration_target_runs_the_complete_suite(make_project):
    project, environment = make_project
    result = subprocess.run(
        ["make", "-s", "test-integration"], cwd=project, env=environment, capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == [
        "run",
        "--locked",
        "--group",
        "dev",
        "--group",
        "docs",
        "pytest",
        "-v",
        "-m",
        "integration",
    ]


def test_permission_target_requires_an_actual_codex_binary(make_project):
    project, environment = make_project
    result = subprocess.run(
        ["make", "-s", "test-permissions"], cwd=project, env=environment, capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "CODEX_TEST_BINARY" in result.stderr
