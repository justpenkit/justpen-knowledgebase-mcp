"""The rendered project, rather than internal replacement helpers, is the contract."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml
from copier import run_copy
from yamlfix import fix_code
from yamlfix.model import YamlfixConfig

from tests.copier_helpers import ROOT, git

pytestmark = pytest.mark.integration


def render(source: Path, destination: Path, **answers: str) -> Path:
    data = {"project_name": "example-mcp", "repo_owner": "acme", **answers}
    run_copy(str(source), destination, data=data, defaults=True, vcs_ref="HEAD", quiet=True)
    return destination


@pytest.mark.parametrize("project_name", ["weather-mcp", "justpen-browser-tools-mcp", "logging-mcp", "pytest-mcp"])
def test_rendered_project_contract(template_source, tmp_path, project_name):
    destination = render(template_source, tmp_path / "project", project_name=project_name)
    package = project_name.replace("-", "_")
    metadata = tomllib.loads((destination / "pyproject.toml").read_text())
    assert metadata["project"]["name"] == project_name
    assert metadata["project"]["version"] == "0.0.0"
    assert metadata["project"]["requires-python"] == ">=3.11"
    assert "docs" in metadata["dependency-groups"]
    assert "pythonVersion" not in metadata["tool"]["pyright"]
    assert metadata["project"]["scripts"] == {project_name: f"{package}.__main__:cli"}
    assert metadata["project"]["urls"]["Repository"] == f"https://github.com/acme/{project_name}"
    assert metadata["project"]["dependencies"] == ["fastmcp>=2.0"]
    assert metadata["tool"]["pyright"]["typeCheckingMode"] == "strict"
    assert metadata["tool"]["coverage"]["report"]["fail_under"] == 80
    root_metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    root_ruff = {key: value for key, value in root_metadata["tool"]["ruff"].items() if key != "src"}
    output_ruff = {key: value for key, value in metadata["tool"]["ruff"].items() if key != "src"}
    assert output_ruff == root_ruff
    path_keys = {"include", "extraPaths"}
    assert {key: value for key, value in metadata["tool"]["pyright"].items() if key not in path_keys} == {
        key: value for key, value in root_metadata["tool"]["pyright"].items() if key not in path_keys
    }
    assert metadata["tool"]["coverage"]["report"] == root_metadata["tool"]["coverage"]["report"]
    assert metadata["tool"]["coverage"]["run"]["branch"] == root_metadata["tool"]["coverage"]["run"]["branch"]
    assert (destination / "src" / package / "__main__.py").is_file()
    assert not (destination / "src/your_package").exists()
    for filename in (
        "AGENTS.md",
        "CLAUDE.md",
        ".claude/settings.json",
        ".codex/config.toml",
        ".codex/hooks.json",
        "scripts/hooks/guard_config.py",
        "tests/test_agent_permissions.py",
        "Makefile",
        ".vscode/tasks.json",
        "tests/test_development_integration.py",
        "tests/test_release_workflow.py",
        ".github/workflows/ci.yml",
    ):
        assert (destination / filename).is_file(), filename
    assert (destination / "AGENTS.md").read_bytes() == (ROOT / "AGENTS.md").read_bytes()
    for workflow in (destination / ".github/workflows").glob("*.yml"):
        assert workflow.read_bytes() == (ROOT / ".github/workflows" / workflow.name).read_bytes()
    for filename in (
        "copier.yml",
        "template",
        "scripts/bootstrap.py",
        ".github/.template-pending",
        ".github/workflows/template-cleanup.yml",
        ".github/workflows/docs-deploy.yml",
    ):
        assert not (destination / filename).exists(), filename
    recorded = yaml.safe_load((destination / ".copier-answers.yml").read_text())
    assert recorded["project_name"] == project_name
    assert Path(recorded["_src_path"]).resolve() == template_source.resolve()
    assert git(template_source, "rev-parse", recorded["_commit"]) == git(template_source, "rev-parse", "HEAD")


def test_metadata_answers_are_escaped(template_source, tmp_path):
    description = 'A "quoted" description with a backslash \\ and Unicode: ölçüm 🚀'
    author = 'Ayd\u0131n "Engineering"'
    destination = render(template_source, tmp_path / "project", description=description, author=author)
    metadata = tomllib.loads((destination / "pyproject.toml").read_text())
    assert metadata["project"]["description"] == description
    assert metadata["project"]["authors"] == [{"name": author}]


@pytest.mark.parametrize("revision", ["123e456", "1e6", "1234567"])
def test_answers_preserve_strings_after_yaml_formatting(template_source, tmp_path, revision):
    source = tmp_path / "source"
    git(tmp_path, "clone", "--no-hardlinks", str(template_source), str(source))
    git(source, "tag", revision)
    destination = tmp_path / "project"
    data = {"project_name": "example-mcp", "repo_owner": "acme", "description": "1e6", "author": "123e456"}
    run_copy(str(source), destination, data=data, defaults=True, vcs_ref=revision, quiet=True)
    answer_file = destination / ".copier-answers.yml"
    recorded = yaml.safe_load(answer_file.read_text())
    assert recorded["_commit"] == revision
    metadata = tomllib.loads((destination / "pyproject.toml").read_text())
    formatted = fix_code(answer_file.read_text(), config=YamlfixConfig(**metadata["tool"]["yamlfix"]))
    assert yaml.safe_load(formatted) == recorded
    assert git(source, "rev-parse", yaml.safe_load(formatted)["_commit"]) == git(source, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "project_name",
    [
        "../escape-mcp",
        "Bad-Name-mcp",
        "class",
        "bad--mcp",
        "9-start-mcp",
        "sys",
        "logging",
        "argparse",
        "typing",
        "pytest",
        "tests",
        "scripts",
    ],
)
def test_invalid_project_names_are_rejected(template_source, tmp_path, project_name):
    with pytest.raises(ValueError):
        render(template_source, tmp_path / "project", project_name=project_name)
    assert not (tmp_path / "escape-mcp").exists()


def test_invalid_owner_is_rejected(template_source, tmp_path):
    with pytest.raises(ValueError):
        render(template_source, tmp_path / "project", repo_owner="bad/owner")


@pytest.mark.parametrize(("answer", "value"), [("project_name", "weather-mcp\n"), ("repo_owner", "acme\n")])
def test_names_with_trailing_newline_are_rejected(template_source, tmp_path, answer, value):
    with pytest.raises(ValueError):
        render(template_source, tmp_path / "project", **{answer: value})


def test_copy_has_no_implicit_tasks(template_source, tmp_path):
    destination = render(template_source, tmp_path / "project")
    assert not (destination / ".venv").exists()
    assert not (destination / "node_modules").exists()
    assert not (destination / "uv.lock").exists()
