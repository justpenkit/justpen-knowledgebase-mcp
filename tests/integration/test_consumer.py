"""Isolated wheel, locked and lowest-direct compatible dependency installations."""

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def run(command, cwd):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=180, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    target = tmp_path_factory.mktemp("wheel-build")
    run(["uv", "build", "--wheel", "--out-dir", str(target)], ROOT)
    wheels = list(target.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


@pytest.mark.parametrize("resolution", ["locked", "minimum"])
def test_installed_wheel_all_eleven_tools(wheel, tmp_path, resolution):
    installation = tmp_path / "installation"
    run(["uv", "venv", "--python", sys.executable, str(installation)], tmp_path)
    python = installation / "bin/python"
    if resolution == "locked":
        requirements = tmp_path / "requirements.txt"
        run(
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--no-default-groups",
                "--no-emit-project",
                "--output-file",
                str(requirements),
            ],
            ROOT,
        )
        run(["uv", "pip", "sync", "--python", str(python), str(requirements)], tmp_path)
        run(["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheel)], tmp_path)
    else:
        dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
        run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--resolution",
                "lowest-direct",
                *dependencies,
                str(wheel),
            ],
            tmp_path,
        )
    flow = tmp_path / "consumer_flow.py"
    shutil.copyfile(Path(__file__).with_name("consumer_flow.py"), flow)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "VIRTUAL_ENV"} and not key.startswith(("OTEL_", "JUSTPEN_"))
    }
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [str(python), "-I", "-B", str(flow), str(workspace)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(json.loads(result.stdout)["tools"]) == 11
    assert not list(workspace.rglob("*.log"))
