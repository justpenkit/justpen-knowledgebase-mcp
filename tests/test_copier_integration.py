"""Run both setup routes and verify the emitted wheel in an isolated runtime environment."""

import shutil
import subprocess
import tomllib

import pytest
import yaml
from copier import run_copy
from scripts.bootstrap import bootstrap, validate_project

from tests.copier_helpers import git

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("route", ["local", "github"])
@pytest.mark.parametrize("python_version", ["3.11", "3.12", "3.13"])
def test_generated_project_end_to_end(template_source, tmp_path, monkeypatch, route, python_version):
    # uv commands inside Make and the consumer environment must use the matrix
    # interpreter instead of the generated .python-version default (3.13).
    monkeypatch.setenv("UV_PYTHON", python_version)
    for name in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PROJECT", "UV_WORKING_DIR", "PYTHONPATH"):
        monkeypatch.delenv(name, raising=False)
    destination = tmp_path / "project"
    name = "weather-mcp" if route == "local" else "justpen-browser-tools-mcp"
    description = 'A "quoted" MCP description with a backslash \\ and Unicode: ölçüm'
    if route == "github":
        shutil.copytree(template_source, destination, ignore=shutil.ignore_patterns(".git"))
        git(destination, "init", "-q", "-b", "main")
        git(destination, "add", "-A")
        git(destination, "commit", "-qm", "test: GitHub template copy")
        template_head = git(destination, "rev-parse", "HEAD")
        assert template_head != git(template_source, "rev-parse", "HEAD")
        subprocess.run(
            [
                "uv",
                "run",
                "--locked",
                "python",
                "scripts/bootstrap.py",
                "--repository",
                f"acme/{name}",
                "--description",
                description,
            ],
            cwd=destination,
            check=True,
            timeout=600,
        )
        assert not (destination / "copier.yml").exists()
        assert not (destination / "template").exists()
        assert not (destination / "scripts/bootstrap.py").exists()
        assert not bootstrap(destination, repository=f"acme/{name}")
        assert git(destination, "diff", "--name-only", "--diff-filter=AM", "--", ".github/workflows") == ""
    else:
        template_head = git(template_source, "rev-parse", "HEAD")
        run_copy(
            str(template_source),
            destination,
            data={"project_name": name, "repo_owner": "acme", "description": description},
            defaults=True,
            vcs_ref="HEAD",
            quiet=True,
        )
        validate_project(destination, build_docs=python_version == "3.13")
    metadata = tomllib.loads((destination / "pyproject.toml").read_text())
    assert metadata["project"]["description"] == description
    assert metadata["project"]["version"] == "0.0.0"
    assert metadata["tool"]["coverage"]["report"]["fail_under"] == 80
    answers = yaml.safe_load((destination / ".copier-answers.yml").read_text())
    source = destination if route == "github" else template_source
    assert git(source, "rev-parse", answers["_commit"]) == template_head
    assert answers["_src_path"] == ("." if route == "github" else str(template_source))
    assert (destination / "site/index.html").exists() == (python_version == "3.13" and route == "local")
    assert not (destination / "node_modules").exists()
    assert not (destination / "package.json").exists()
    _check_installed_wheel(destination, tmp_path / "consumer", name, python_version)
    if route == "github" and python_version == "3.13":
        # One rendered project verifies its shared integration suite. Other
        # matrix cases cover interpreter/package compatibility without repeating it.
        subprocess.run(["make", "test-integration"], cwd=destination, check=True, timeout=600)


def _check_installed_wheel(project, consumer, name, python_version):
    consumer.mkdir()
    wheels = consumer / "dist"
    # GitHub bootstrap discards its staging build, so build the installed output
    # for both routes instead of depending on a surviving dist/ directory.
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(wheels)], cwd=project, check=True, timeout=120)
    (wheel,) = wheels.glob("*.whl")
    requirements = consumer / "requirements.txt"
    subprocess.run(
        ["uv", "export", "--locked", "--no-default-groups", "--no-emit-project", "--output-file", str(requirements)],
        cwd=project,
        check=True,
        timeout=60,
    )
    environment = consumer / "venv"
    python = str(environment / "bin/python")
    commands = [
        ["uv", "venv", "--python", python_version, str(environment)],
        ["uv", "pip", "sync", "--python", python, "--require-hashes", str(requirements)],
        ["uv", "pip", "install", "--python", python, "--no-deps", str(wheel)],
        ["uv", "pip", "check", "--python", python],
    ]
    for command in commands:
        subprocess.run(command, cwd=consumer, check=True, timeout=120)
    # Run outside the checkout without uv run, editable installs, dev/docs groups,
    # PYTHONPATH or user site-packages. The child uses this venv's console script.
    probe = """
import asyncio
import importlib
import os
import sys
from pathlib import Path
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

async def probe():
    assert f"{sys.version_info.major}.{sys.version_info.minor}" == os.environ["UV_PYTHON"]
    name = sys.argv[1]
    package = importlib.import_module(name.replace("-", "_"))
    location = Path(package.__file__).resolve()
    assert location.is_relative_to(Path(sys.prefix).resolve()), f"Imported outside the consumer venv: {location}"
    assert location.with_name("py.typed").is_file(), "Wheel is missing py.typed"
    transport = StdioTransport(command=str(Path(sys.executable).with_name(name)), args=[], keep_alive=False)
    async with Client(transport, timeout=15, init_timeout=15) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert "echo" in names
        result = await client.call_tool("echo", {"message": "Installed wheel: ölçüm"})
        assert result.structured_content == {
            "status": "success",
            "data": {"echoed": "Installed wheel: ölçüm"},
        }

asyncio.run(probe())
"""
    subprocess.run([python, "-I", "-c", probe, name], cwd=consumer, check=True, timeout=60)
