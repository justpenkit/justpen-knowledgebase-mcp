"""Real Git fixtures for generation, updates, and first-run setup."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git(repo: Path, *arguments: str) -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    # Wait for maintenance before fixtures clone or remove this repository.
    return subprocess.check_output(
        [
            "git",
            "-c",
            "user.name=Template Test",
            "-c",
            "user.email=template-test@example.invalid",
            "-c",
            "gc.autoDetach=false",
            "-c",
            "maintenance.autoDetach=false",
            *arguments,
        ],
        cwd=repo,
        env=environment,
        text=True,
    ).strip()


def snapshot_template(destination: Path) -> Path:
    shutil.copytree(
        ROOT,
        destination,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "node_modules",
            ".pytest_cache",
            ".ruff_cache",
            "__pycache__",
            ".superpowers",
            ".worktrees",
            ".astro",
            "site",
            "dist",
            "htmlcov",
            ".coverage*",
            "coverage.xml",
        ),
    )
    git(destination, "init", "-q", "-b", "main")
    git(destination, "add", "-A")
    git(destination, "commit", "-qm", "test: seed template")
    return destination
