"""Install a lock-constrained native/consumer harness without optional formatter tools."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITES = {"native": "tests/integration/test_workspace_containment.py", "consumer": "tests/integration/test_consumer.py"}


def run(command: list[str], *, env: dict[str, str] | None = None, quiet: bool = False) -> None:
    """Run only fixed harness commands; errors propagate without silent skips."""
    subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL if quiet else None, check=True)


def execute(suite: str, arguments: list[str]) -> None:
    """Isolate runtime sync; add only locked tooling for test suites."""
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    versions = {package["name"]: package["version"] for package in lock["package"]}
    with tempfile.TemporaryDirectory(prefix="kb-runtime-validation-") as temporary:
        folder = Path(temporary)
        environment = folder / "venv"
        python = environment / "bin/python"
        constraints = folder / "locked-all.txt"
        settings = {
            **{key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"},
            "UV_PROJECT_ENVIRONMENT": str(environment),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        run(["uv", "sync", "--locked", "--no-default-groups", "--python", sys.executable], env=settings)
        if suite == "benchmark":
            run([str(python), "-B", str(ROOT / "scripts/benchmark_knowledgebase.py"), *arguments], env=settings)
            return
        run(
            ["uv", "export", "--locked", "--all-groups", "--no-emit-project", "--output-file", str(constraints)],
            quiet=True,
        )
        # Every possible tool transitive dependency is constrained to its locked
        # version; no unconstrained uv --with overlay can shadow runtime versions.
        requirements = [f"{name}=={versions[name]}" for name in ["pytest", "pytest-asyncio", "pytest-cov"]]
        run(["uv", "pip", "install", "--python", str(python), "--constraint", str(constraints), *requirements])
        run([str(python), "-B", "-m", "pytest", SUITES[suite], "-v"], env=settings)


def main() -> None:
    """Use a separate venv, unchanged pytest config, and lock-derived tool constraints."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=[*SUITES, "benchmark"])
    options, arguments = parser.parse_known_args()
    if arguments and options.suite != "benchmark":
        parser.error("extra arguments are allowed only for the benchmark")
    execute(options.suite, arguments)


if __name__ == "__main__":
    main()
