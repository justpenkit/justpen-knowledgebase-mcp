"""Regression tests for protected-file approvals."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SHARED_GUARD = ROOT / "scripts" / "hooks" / "guard_config.py"


def call_hook(tool: str, tool_input: dict[str, Any], *, event="PreToolUse", cwd=ROOT) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(SHARED_GUARD), "--client", "codex"],
        input=json.dumps({"hook_event_name": event, "tool_name": tool, "tool_input": tool_input, "cwd": str(cwd)}),
        capture_output=True,
        text=True,
        check=True,
        cwd=cwd,
    )
    return json.loads(result.stdout) if result.stdout.strip() else {}


@pytest.mark.parametrize("operation", ["Add File", "Update File", "Delete File", "Move to"])
def test_patch_protects_every_file_operation(operation):
    patch = f"*** Begin Patch\n*** {operation}: uv.lock\n*** End Patch"
    output = call_hook("apply_patch", {"command": patch})
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_patch_content_mention_is_not_a_protected_destination():
    patch = "*** Begin Patch\n*** Add File: README.md\n+Read pyproject.toml first.\n*** End Patch"
    assert call_hook("apply_patch", {"command": patch}) == {}


def test_patch_through_a_symlink_is_guarded(tmp_path):
    alias = tmp_path / "settings"
    alias.symlink_to(ROOT / "pyproject.toml")
    patch = f"*** Begin Patch\n*** Update File: {alias}\n*** End Patch"
    output = call_hook("apply_patch", {"command": patch})
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "uv add rich",
        'uv add "fastmcp>=3"',
        "uv remove rich",
        "uv lock",
        "uv sync --locked --group dev",
        "uv version --bump patch",
    ],
)
def test_codex_automatically_approves_managed_uv(command):
    pinned = command.replace("uv ", f'uv --directory "{ROOT}" ', 1)
    output = call_hook("Bash", {"command": pinned}, event="PermissionRequest")
    assert output == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}


@pytest.mark.parametrize(
    "command",
    [
        "uv add rich && echo bad > pyproject.toml",
        "uv sync; touch uv.lock",
        "uv lock > pyproject.toml",
        "uv lock\npython edit.py",
        "uv add $(touch uv.lock)",
        "uv add `touch uv.lock`",
        "uv sync | sh",
        "uv run python edit.py",
        "uv pip install rich",
        "make bump-patch",
        "python edit.py",
        "./uv sync",
        "UV_PROJECT=/other uv sync",
        "uv sync --project /other",
        "uv add --script runner.py rich",
        "uv lock --directory=/other",
        "uv sync --config-file custom.toml",
        "uv sync --cache-dir /other",
        "uv sync --python ./custom-python",
        "uv sync -p./custom-python",
        'uv add "unterminated',
    ],
)
def test_codex_defers_other_escalations_to_user(command):
    assert call_hook("Bash", {"command": command}, event="PermissionRequest") == {}


def test_uv_in_another_directory_needs_user_review(tmp_path):
    assert call_hook("Bash", {"command": "uv sync"}, event="PermissionRequest", cwd=tmp_path) == {}


def test_codex_session_cwd_is_not_evidence_of_execution_cwd():
    # CLI 0.154 drops exec_command.workdir from PermissionRequest tool_input.
    assert call_hook("Bash", {"command": "uv sync"}, event="PermissionRequest", cwd=ROOT) == {}


@pytest.mark.parametrize("suffix", ["--directory /other", "--project /other", "--", "--cache-dir /other"])
def test_pinned_uv_cannot_override_its_target(suffix):
    command = f'uv --directory "{ROOT}" sync {suffix}'
    assert call_hook("Bash", {"command": command}, event="PermissionRequest") == {}


def test_pinned_uv_is_independent_of_session_cwd(tmp_path):
    command = f'uv --directory "{ROOT}" sync --locked'
    output = call_hook("Bash", {"command": command}, event="PermissionRequest", cwd=tmp_path)
    assert output["hookSpecificOutput"]["decision"]["behavior"] == "allow"


@pytest.mark.parametrize("target", ["format", "format-md", "format-toml", "format-yaml", "format-json"])
def test_codex_automatically_approves_root_pinned_formatting(target, tmp_path):
    command = f'make --directory "{ROOT}" {target}'
    output = call_hook("Bash", {"command": command}, event="PermissionRequest", cwd=tmp_path)
    assert output["hookSpecificOutput"]["decision"]["behavior"] == "allow"


@pytest.mark.parametrize(
    "suffix",
    [
        "-f other.mk",
        "MAKE=python",
        "--eval=anything",
        "; echo bad > pyproject.toml",
        "&& uv run python edit.py",
        "> uv.lock",
        "check",
    ],
)
def test_formatting_cannot_approve_command_overrides_or_extra_actions(suffix):
    command = f'make --directory "{ROOT}" format {suffix}'
    assert call_hook("Bash", {"command": command}, event="PermissionRequest") == {}


@pytest.mark.parametrize(
    "command", ["make format", "make --directory /other format", "make --directory . format", "./make format"]
)
def test_formatting_escalation_requires_a_proven_project_root(command):
    assert call_hook("Bash", {"command": command}, event="PermissionRequest") == {}


def test_workdir_override_is_checked(tmp_path):
    assert call_hook("Bash", {"command": "uv sync", "workdir": str(tmp_path)}, event="PermissionRequest") == {}


def test_codex_shell_write_uses_native_filesystem_gate():
    assert call_hook("Bash", {"command": "echo bad > uv.lock"}) == {}
    assert call_hook("Bash", {"command": "echo bad > uv.lock"}, event="PermissionRequest") == {}


@pytest.mark.parametrize("raw", ["not json", "[]", '{"tool_input": null}'])
def test_invalid_hook_input_blocks(raw):
    result = subprocess.run(
        [sys.executable, str(SHARED_GUARD), "--client", "codex"],
        input=raw,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stderr


def test_guard_no_longer_serves_claude():
    result = subprocess.run(
        [sys.executable, str(SHARED_GUARD), "--client", "claude"],
        input="{}",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0


@pytest.mark.integration
def test_registered_codex_hooks_run_from_subdirectory():
    # Git's pre-push exports GIT_DIR. Carrying it into a different cwd makes
    # rev-parse treat that cwd as the worktree root. Model a normal agent launch,
    # not a nested Git hook, by clearing Git's documented local environment.
    git_local_vars = subprocess.check_output(["git", "rev-parse", "--local-env-vars"], text=True).splitlines()
    environment = {key: value for key, value in os.environ.items() if key not in git_local_vars}
    hooks = json.loads((ROOT / ".codex/hooks.json").read_text())["hooks"]
    for event, entries in hooks.items():
        command = entries[0]["hooks"][0]["command"]
        if event == "PermissionRequest":
            tool = "Bash"
            tool_input = {"command": f'uv --directory "{ROOT}" sync --locked'}
        else:
            tool = "apply_patch"
            tool_input = {"command": f"*** Begin Patch\n*** Delete File: {ROOT / 'uv.lock'}\n*** End Patch"}
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            input=json.dumps(
                {"hook_event_name": event, "tool_name": tool, "tool_input": tool_input, "cwd": str(ROOT / "scripts")}
            ),
            cwd=ROOT / "scripts",
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        output = json.loads(result.stdout)["hookSpecificOutput"]
        assert output["hookEventName"] == event
        if event == "PermissionRequest":
            assert output["decision"]["behavior"] == "allow"
        else:
            assert output["permissionDecision"] == "deny"


def test_codex_profile_preserves_human_write_gate():
    config = tomllib.loads((ROOT / ".codex/config.toml").read_text())
    assert config["approval_policy"] == "on-request"
    assert config["approvals_reviewer"] == "user"
    assert "sandbox_mode" not in config
    profile = config["permissions"][config["default_permissions"]]
    assert profile["extends"] == ":workspace"
    assert profile["filesystem"][":workspace_roots"]["pyproject.toml"] == "read"
    assert profile["filesystem"][":workspace_roots"]["uv.lock"] == "read"


CLAUDE_SETTINGS = json.loads((ROOT / ".claude/settings.json").read_text())
# Commands that may change protected metadata or Git's own configuration, so they run outside
# Claude's sandbox. Each one is a known tool action; widening this list re-opens the write gate.
CLAUDE_UNSANDBOXED = {
    "uv add *",
    "uv remove *",
    "uv lock *",
    "uv sync *",
    "uv version *",
    "make format",
    "make format-project-text",
    "make format-toml",
    "make bump-patch",
    "make bump-minor",
    "make bump-major",
    "git switch *",
    "git checkout *",
    "git pull *",
    "git merge *",
    "git rebase *",
    "git restore *",
    "git stash *",
    "git reset *",
    "git cherry-pick *",
    "git push *",
    "git branch *",
}


def test_claude_permissions_keep_protected_edit_asks():
    permissions = CLAUDE_SETTINGS["permissions"]
    assert permissions["defaultMode"] == "acceptEdits"
    assert "Edit(/pyproject.toml)" in permissions["ask"]
    assert "Edit(/uv.lock)" in permissions["ask"]
    assert "Edit(/scripts/hooks/guard_config.py)" in permissions["ask"]
    assert "Bash" not in permissions["allow"]


def test_claude_sandbox_blocks_shell_writes_to_protected_files():
    """The OS sandbox, not a command classifier, keeps metadata read-only for Claude's shell."""
    sandbox = CLAUDE_SETTINGS["sandbox"]
    assert sandbox["enabled"] is True
    assert sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False
    assert sandbox["autoAllowBashIfSandboxed"] is False
    assert set(sandbox["filesystem"]["denyWrite"]) == {"./pyproject.toml", "./uv.lock"}


def test_claude_sandbox_exempts_only_named_tool_actions():
    assert set(CLAUDE_SETTINGS["sandbox"]["excludedCommands"]) == CLAUDE_UNSANDBOXED


def test_claude_sandbox_reaches_only_caches_and_package_hosts():
    sandbox = CLAUDE_SETTINGS["sandbox"]
    assert set(sandbox["filesystem"]["allowWrite"]) == {"~/.cache/uv", "~/.cache/pre-commit", "~/.cache/pyright-python"}
    assert set(sandbox["network"]["allowedDomains"]) == {
        "github.com",
        "api.github.com",
        "pypi.org",
        "files.pythonhosted.org",
    }


def test_claude_asks_before_uv_writes_a_named_script():
    """`uv add --script pyproject.toml` would write a PEP 723 block into the file, unsandboxed."""
    assert "Bash(uv *--script*)" in CLAUDE_SETTINGS["permissions"]["ask"]


def test_claude_sandbox_paths_are_machine_independent():
    filesystem = CLAUDE_SETTINGS["sandbox"]["filesystem"]
    for path in filesystem["allowWrite"] + filesystem["denyWrite"]:
        assert path.startswith(("./", "~/")), path


def test_claude_no_longer_runs_the_guard_hook():
    assert "guard_config.py" not in json.dumps(CLAUDE_SETTINGS.get("hooks", {}))


@pytest.mark.parametrize("action", ["checkout", "restore"])
@pytest.mark.parametrize("name", ["pyproject.toml", "uv.lock"])
def test_claude_asks_before_git_rewrites_protected_files(action, name):
    """Unsandboxed Git can restore metadata from any ref, so naming a protected file asks."""
    assert f"Bash(git {action} *{name}*)" in CLAUDE_SETTINGS["permissions"]["ask"]


def test_claude_selected_test_stays_sandboxed():
    rules = [rule for rule in CLAUDE_SETTINGS["permissions"]["allow"] if "make test-one" in rule]
    assert rules == ["Bash(make test-one TEST=tests/*)"]
    assert not any(command.startswith("make test") for command in CLAUDE_UNSANDBOXED)


def test_claude_imports_shared_rules():
    assert "@AGENTS.md" in (ROOT / "CLAUDE.md").read_text()
    assert (ROOT / "AGENTS.md").is_file()


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("CODEX_TEST_BINARY"), reason="opt-in: needs an installed Codex sandbox")
def test_real_codex_sandbox_enforces_protected_files(tmp_path):
    """Exercise OS permissions against disposable files, without a model call."""
    fixture = tmp_path / "permission-fixture"
    fixture.mkdir()
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run(["git", "init", "-q", str(fixture)], env=environment, check=True)
    assert (fixture / ".git").is_dir()
    (fixture / ".codex").mkdir()
    shutil.copyfile(ROOT / ".codex/config.toml", fixture / ".codex/config.toml")
    for name in ("pyproject.toml", "uv.lock"):
        (fixture / name).write_text("sentinel\n")
    probe = """
from pathlib import Path
for name in ("pyproject.toml", "uv.lock"):
    path = Path(name)
    assert path.read_text() == "sentinel\\n"
    replacement = Path("replacement")
    replacement.write_text("bad")
    for operation in (lambda: path.write_text("bad"), path.unlink, lambda: replacement.replace(path)):
        try:
            operation()
        except PermissionError:
            pass
        else:
            raise AssertionError(f"Protected operation unexpectedly permitted: {name}")
    assert path.read_text() == "sentinel\\n"
Path("source.py").write_text("# ordinary edits work\\n")
"""
    result = subprocess.run(
        [
            os.environ["CODEX_TEST_BINARY"],
            "sandbox",
            "-c",
            "projects={" + json.dumps(str(fixture)) + '={trust_level="trusted"}' + "}",
            "-C",
            str(fixture),
            "-P",
            "justpen-dev",
            "--",
            sys.executable,
            "-c",
            probe,
        ],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (fixture / "source.py").is_file()


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("CODEX_TEST_BINARY"), reason="opt-in: needs an installed Codex sandbox")
def test_real_codex_sandbox_ignores_inherited_worktree(tmp_path, monkeypatch):
    """A pre-push GIT_DIR must not redirect the probe into the shared repository."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    repository = tmp_path / "repository"
    linked = tmp_path / "linked"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repository)], env=environment, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Sandbox Test",
            "-c",
            "user.email=sandbox@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-qm",
            "test: initialize isolated repository",
        ],
        cwd=repository,
        env=environment,
        check=True,
    )
    subprocess.run(["git", "worktree", "add", "-qb", "probe", str(linked)], cwd=repository, env=environment, check=True)
    git_dir = subprocess.check_output(
        ["git", "rev-parse", "--absolute-git-dir"], cwd=linked, env=environment, text=True
    ).strip()
    config = repository / ".git/config"
    original_config = config.read_bytes()
    monkeypatch.setenv("GIT_DIR", git_dir)

    test_real_codex_sandbox_enforces_protected_files(tmp_path)

    assert config.read_bytes() == original_config
    assert (tmp_path / "permission-fixture/.git").is_dir()
