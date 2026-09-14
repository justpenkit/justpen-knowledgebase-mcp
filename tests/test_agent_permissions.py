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


def call_hook(client: str, tool: str, tool_input: dict[str, Any], *, event="PreToolUse", cwd=ROOT) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(SHARED_GUARD), "--client", client],
        input=json.dumps({"hook_event_name": event, "tool_name": tool, "tool_input": tool_input, "cwd": str(cwd)}),
        capture_output=True,
        text=True,
        check=True,
        cwd=cwd,
    )
    return json.loads(result.stdout) if result.stdout.strip() else {}


def call_claude(command: str) -> dict[str, Any]:
    return call_hook("claude", "Bash", {"command": command})


@pytest.mark.parametrize(
    "command",
    [
        "cat pyproject.toml",
        "head -20 uv.lock",
        "rg fastmcp pyproject.toml",
        "sed -n '1,40p' pyproject.toml",
        "cat pyproject.toml | head -20",
        "cat < uv.lock",
        "cat pyproject.toml && git diff -- uv.lock",
    ],
)
def test_reading_protected_files_does_not_request_approval(command: str) -> None:
    assert call_claude(command) == {}


@pytest.mark.parametrize(
    "command",
    [
        "uv add rich && printf bad > pyproject.toml",
        "uv sync; printf bad > uv.lock",
        "uv lock > pyproject.toml",
    ],
)
def test_uv_prefix_cannot_approve_a_protected_shell_write(command: str) -> None:
    assert call_claude(command)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize(
    "command",
    [
        "cat uv.lock | tee pyproject.toml",
        "sed -n '1w uv.lock' pyproject.toml",
        "sed -i '' 's/a/b/' pyproject.toml",
        "sed -n '1p' -e 'w uv.lock' pyproject.toml",
        "rg --pre='./writer' fastmcp pyproject.toml",
        "cat pyproject.toml && python writer.py",
        "cat < uv.lock > pyproject.toml",
    ],
)
def test_read_prefixes_cannot_hide_writes(command):
    assert call_claude(command)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit"])
@pytest.mark.parametrize("path", ["pyproject.toml", "./uv.lock", str(ROOT / "pyproject.toml")])
def test_claude_direct_writes_request_user_approval(tool, path):
    assert call_hook("claude", tool, {"file_path": path})["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("tool", ["Read", "Edit", "Write"])
@pytest.mark.parametrize("path", ["src/app.py", "pyproject.toml.example", "uv.lock.bak", "nested/pyproject.toml"])
def test_unprotected_files_are_not_gated(tool, path):
    assert call_hook("claude", tool, {"file_path": path}) == {}


def test_read_tool_can_inspect_protected_file():
    assert call_hook("claude", "Read", {"file_path": "pyproject.toml"}) == {}


def test_claude_allows_only_the_complete_selected_test_command():
    output = call_claude("make test-one TEST=tests/test_example.py::test_case")
    assert output["hookSpecificOutput"]["permissionDecision"] == "allow"


@pytest.mark.parametrize("suffix", ["-f other.mk", "--eval=anything", "format", "&& python edit.py"])
def test_claude_selected_test_does_not_approve_additional_make_options(suffix):
    output = call_claude(f"make test-one TEST=tests/test_example.py {suffix}")
    assert output.get("hookSpecificOutput", {}).get("permissionDecision") != "allow"
    permissions = json.loads((ROOT / ".claude/settings.json").read_text())["permissions"]
    assert not any("make test-one" in rule for rule in permissions["allow"])


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("operation", ["Add File", "Update File", "Delete File", "Move to"])
def test_patch_protects_every_file_operation(client, operation):
    patch = f"*** Begin Patch\n*** {operation}: uv.lock\n*** End Patch"
    output = call_hook(client, "apply_patch", {"command": patch})
    assert output["hookSpecificOutput"]["permissionDecision"] == ("ask" if client == "claude" else "deny")


def test_patch_content_mention_is_not_a_protected_destination():
    patch = "*** Begin Patch\n*** Add File: README.md\n+Read pyproject.toml first.\n*** End Patch"
    assert call_hook("codex", "apply_patch", {"command": patch}) == {}


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
    output = call_hook("codex", "Bash", {"command": pinned}, event="PermissionRequest")
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
    assert call_hook("codex", "Bash", {"command": command}, event="PermissionRequest") == {}


def test_uv_in_another_directory_needs_user_review(tmp_path):
    assert call_hook("codex", "Bash", {"command": "uv sync"}, event="PermissionRequest", cwd=tmp_path) == {}


def test_codex_session_cwd_is_not_evidence_of_execution_cwd():
    # CLI 0.154 drops exec_command.workdir from PermissionRequest tool_input.
    assert call_hook("codex", "Bash", {"command": "uv sync"}, event="PermissionRequest", cwd=ROOT) == {}


@pytest.mark.parametrize("suffix", ["--directory /other", "--project /other", "--", "--cache-dir /other"])
def test_pinned_uv_cannot_override_its_target(suffix):
    command = f'uv --directory "{ROOT}" sync {suffix}'
    assert call_hook("codex", "Bash", {"command": command}, event="PermissionRequest") == {}


def test_pinned_uv_is_independent_of_session_cwd(tmp_path):
    command = f'uv --directory "{ROOT}" sync --locked'
    output = call_hook("codex", "Bash", {"command": command}, event="PermissionRequest", cwd=tmp_path)
    assert output["hookSpecificOutput"]["decision"]["behavior"] == "allow"


@pytest.mark.parametrize("target", ["format", "format-md", "format-toml", "format-yaml", "format-json"])
def test_codex_automatically_approves_root_pinned_formatting(target, tmp_path):
    command = f'make --directory "{ROOT}" {target}'
    output = call_hook("codex", "Bash", {"command": command}, event="PermissionRequest", cwd=tmp_path)
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
    assert call_hook("codex", "Bash", {"command": command}, event="PermissionRequest") == {}


@pytest.mark.parametrize(
    "command", ["make format", "make --directory /other format", "make --directory . format", "./make format"]
)
def test_formatting_escalation_requires_a_proven_project_root(command):
    assert call_hook("codex", "Bash", {"command": command}, event="PermissionRequest") == {}


def test_workdir_override_is_checked(tmp_path):
    assert call_hook("codex", "Bash", {"command": "uv sync", "workdir": str(tmp_path)}, event="PermissionRequest") == {}


def test_codex_shell_write_uses_native_filesystem_gate():
    assert call_hook("codex", "Bash", {"command": "echo bad > uv.lock"}) == {}
    assert call_hook("codex", "Bash", {"command": "echo bad > uv.lock"}, event="PermissionRequest") == {}


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("raw", ["not json", "[]", '{"tool_input": null}'])
def test_invalid_hook_input_blocks(client, raw):
    result = subprocess.run(
        [sys.executable, str(SHARED_GUARD), "--client", client],
        input=raw,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stderr


def test_symlink_write_is_guarded(tmp_path):
    alias = tmp_path / "settings"
    alias.symlink_to(ROOT / "pyproject.toml")
    output = call_hook("claude", "Write", {"file_path": str(alias)})
    assert output["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.integration
@pytest.mark.parametrize("client", ["claude", "codex"])
def test_registered_hooks_run_from_subdirectory(client):
    # Git's pre-push exports GIT_DIR. Carrying it into a different cwd makes
    # rev-parse treat that cwd as the worktree root. Model a normal agent launch,
    # not a nested Git hook, by clearing Git's documented local environment.
    git_local_vars = subprocess.check_output(["git", "rev-parse", "--local-env-vars"], text=True).splitlines()
    environment = {key: value for key, value in os.environ.items() if key not in git_local_vars}
    environment["CLAUDE_PROJECT_DIR"] = str(ROOT)
    config_path = ROOT / (".claude/settings.json" if client == "claude" else ".codex/hooks.json")
    hooks = json.loads(config_path.read_text())["hooks"]
    for event, entries in hooks.items():
        command = entries[0]["hooks"][0]["command"]
        if event == "PermissionRequest":
            tool = "Bash"
            tool_input = {"command": f'uv --directory "{ROOT}" sync --locked'}
            expected = "allow"
        elif client == "codex":
            tool = "apply_patch"
            tool_input = {"command": f"*** Begin Patch\n*** Delete File: {ROOT / 'uv.lock'}\n*** End Patch"}
            expected = "deny"
        else:
            tool = "Write"
            tool_input = {"file_path": str(ROOT / "pyproject.toml")}
            expected = "ask"
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
            assert output["decision"]["behavior"] == expected
        else:
            assert output["permissionDecision"] == expected


def test_codex_profile_preserves_human_write_gate():
    config = tomllib.loads((ROOT / ".codex/config.toml").read_text())
    assert config["approval_policy"] == "on-request"
    assert config["approvals_reviewer"] == "user"
    assert "sandbox_mode" not in config
    profile = config["permissions"][config["default_permissions"]]
    assert profile["extends"] == ":workspace"
    assert profile["filesystem"][":workspace_roots"]["pyproject.toml"] == "read"
    assert profile["filesystem"][":workspace_roots"]["uv.lock"] == "read"


def test_claude_permissions_keep_protected_edit_asks():
    permissions = json.loads((ROOT / ".claude/settings.json").read_text())["permissions"]
    assert permissions["defaultMode"] == "acceptEdits"
    assert "Edit(/pyproject.toml)" in permissions["ask"]
    assert "Edit(/uv.lock)" in permissions["ask"]
    assert "Bash" not in permissions["allow"]


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
