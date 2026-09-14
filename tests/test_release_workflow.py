"""Release configuration and isolated publication-script contracts."""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
# This shared test is also rendered by Copier; keep its Jinja delimiters inert.
GITHUB_EXPRESSION = "$" + "{" * 2


@pytest.mark.parametrize("exists", [True, False])
def test_github_release_step_is_idempotent_and_treats_notes_as_data(tmp_path, exists):
    workflow = Path(__file__).resolve().parent.parent / ".github/workflows/release.yml"
    script = yaml.safe_load(workflow.read_text())["jobs"]["release"]["steps"][-1]["run"]
    executable = tmp_path / "gh"
    executable.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$RELEASE_CALLS"\n'
        'printf "%s" "$GH_REPO" > "$RELEASE_REPO"\n'
        'if [ "$2" = view ]; then exit "$RELEASE_EXISTS_STATUS"; fi\n'
    )
    executable.chmod(0o755)
    calls = tmp_path / "calls.txt"
    notes = "## v0.1.0\n\nLiteral $(touch injected) and `touch injected` and ${HOME}.\n"
    environment = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "RELEASE_CALLS": str(calls),
        "RELEASE_EXISTS_STATUS": "0" if exists else "1",
        "RELEASE_NOTES": base64.b64encode(notes.encode()).decode(),
        "RELEASE_REPO": str(tmp_path / "repository.txt"),
        "GH_REPO": "acme/example-mcp",
        "GITHUB_REF_NAME": "v0.1.0",
        "RUNNER_TEMP": str(tmp_path),
    }
    subprocess.run(["sh", "-eu", "-c", script], cwd=tmp_path, env=environment, check=True)
    assert (tmp_path / "release-notes.md").read_text() == notes
    assert (tmp_path / "repository.txt").read_text() == "acme/example-mcp"
    assert not (tmp_path / "injected").exists()
    commands = calls.read_text().splitlines()
    assert commands[0] == "release view v0.1.0"
    if exists:
        assert len(commands) == 1
    else:
        assert commands[1] == (
            f"release create v0.1.0 --verify-tag --title v0.1.0 --notes-file {tmp_path}/release-notes.md"
        )


def test_release_validation_uses_main_without_publication_permissions():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    validation = workflow["jobs"]["validate"]
    assert validation.get("permissions", workflow["permissions"]) == {"contents": "read"}
    checkout = next(step for step in validation["steps"] if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"] == {"ref": "main", "fetch-depth": 0, "persist-credentials": False}
    assert validation["outputs"]["notes"] == GITHUB_EXPRESSION + " steps.notes.outputs.notes }}"

    publication = workflow["jobs"]["release"]
    assert publication["needs"] == "validate"
    assert publication["permissions"] == {"contents": "write"}
    assert len(publication["steps"]) == 1
    publish = publication["steps"][0]
    assert "uses" not in publish
    assert "scripts/" not in publish["run"]
    assert GITHUB_EXPRESSION not in publish["run"]
    assert publish["env"]["GH_REPO"] == GITHUB_EXPRESSION + " github.repository }}"
    assert publish["env"]["RELEASE_NOTES"] == GITHUB_EXPRESSION + " needs.validate.outputs.notes }}"
