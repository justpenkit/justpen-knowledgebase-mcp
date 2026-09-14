"""Generator-only publication policy and isolated current-commit checks."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = Path(".github/workflows/docs-deploy.yml")
SITE_URL = "https://justpen-mcp-dev-template.justpenkit.justmumu.com/"


def deployment_workflow():
    assert (ROOT / WORKFLOW).is_file(), "The generator needs its own publication workflow."
    return yaml.safe_load((ROOT / WORKFLOW).read_text())


def test_publication_requires_successful_main_ci_in_the_original_repository():
    workflow = deployment_workflow()
    # PyYAML's YAML 1.1 loader interprets the unquoted Actions key `on` as True.
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_run"}
    assert triggers["workflow_run"] == {"workflows": ["CI"], "types": ["completed"], "branches": ["main"]}
    deployment = workflow["jobs"]["deploy-docs"]
    condition = deployment["if"]
    assert "github.repository == 'justpenkit/justpen-mcp-dev-template'" in condition
    assert "github.event.workflow_run.event == 'push'" in condition
    assert "github.event.workflow_run.conclusion == 'success'" in condition
    assert "github.event.workflow_run.head_repository.full_name == github.repository" in condition
    assert "github.event.workflow_run.head_branch == 'main'" in condition
    assert not (ROOT / "template" / WORKFLOW).exists()
    assert workflow["permissions"] == {"contents": "read"}
    assert deployment["permissions"] == {"contents": "read", "deployments": "write"}


def test_publication_identity_matches_the_template_documentation():
    deployment = deployment_workflow()["jobs"]["deploy-docs"]
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text())
    assert config["site_url"] == SITE_URL
    assert SITE_URL in (ROOT / "README.md").read_text()
    assert deployment["environment"] == {"name": "production", "url": SITE_URL}
    assert deployment["env"]["DOCS_COMMIT"] == "${{ github.event.workflow_run.head_sha }}"
    steps = deployment["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.event.workflow_run.head_sha }}"
    publication = next(step for step in steps if step.get("uses", "").startswith("cloudflare/wrangler-action@"))
    assert publication["with"]["accountId"] == "${{ secrets.CLOUDFLARE_ACCOUNT_ID }}"
    assert publication["with"]["apiToken"] == "${{ secrets.CLOUDFLARE_API_TOKEN }}"
    assert publication["with"]["command"] == (
        "pages deploy site --project-name=justpen-mcp-dev-template --branch=main "
        "--commit-hash=${{ github.event.workflow_run.head_sha }}"
    )
    commands = [step.get("run") for step in steps]
    assert commands.index("make lock-check") < commands.index("make install") < commands.index("make docs-build")


@pytest.mark.parametrize("current", [True, False])
def test_current_commit_guard_rejects_obsolete_ci_runs(tmp_path, current):
    steps = deployment_workflow()["jobs"]["deploy-docs"]["steps"]
    guard = next(step for step in steps if step["name"] == "Require current main before publication")
    binary = tmp_path / "git"
    binary.write_text(
        '#!/bin/sh\ncase "$1" in\nfetch) exit 0 ;;\nrev-parse) printf "%s\\n" "$LATEST_MAIN" ;;\n*) exit 2 ;;\nesac\n'
    )
    binary.chmod(0o755)
    environment = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DOCS_COMMIT": "successful-ci-commit",
        "GITHUB_SHA": "workflow-run-default-branch-sha",
        "LATEST_MAIN": "successful-ci-commit" if current else "newer-main-commit",
    }
    result = subprocess.run(
        ["sh", "-eu", "-c", guard["run"]], env=environment, capture_output=True, text=True, check=False
    )
    assert (result.returncode == 0) is current
    if not current:
        assert "newer main commit" in result.stdout
