"""Agent configuration: the protected-files guard, approvals and plugins."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
# Loaded by path: generated projects do not put the repository root on the type checker's import path.
_GUARD_SPEC = importlib.util.spec_from_file_location("guard_config", ROOT / "scripts/hooks/guard_config.py")
assert _GUARD_SPEC is not None
assert _GUARD_SPEC.loader is not None
guard_config = importlib.util.module_from_spec(_GUARD_SPEC)
_GUARD_SPEC.loader.exec_module(guard_config)
CLAUDE_SETTINGS = json.loads((ROOT / ".claude/settings.json").read_text())
CODEX_CONFIG = tomllib.loads((ROOT / ".codex/config.toml").read_text())
CODEX_HOOKS = json.loads((ROOT / ".codex/hooks.json").read_text())["hooks"]
CLAUDE_GUARD_EVENTS = {
    "PreToolUse": "pre",
    "PostToolUse": "post",
    "PostToolUseFailure": "post-failure",
    "ConfigChange": "config-change",
    "Stop": "stop",
}
# Every tool that can write files: the shell tools, Claude's file tools and MCP tools.
CLAUDE_GUARDED_TOOLS = ("Bash", "Monitor", "PowerShell", "Edit", "Write", "MultiEdit", "NotebookEdit", "mcp__.*")


def test_claude_runs_without_an_os_sandbox():
    assert "sandbox" not in CLAUDE_SETTINGS


@pytest.mark.parametrize(("event", "argument"), CLAUDE_GUARD_EVENTS.items())
def test_claude_runs_the_guard_on_every_event(event, argument):
    [entry] = CLAUDE_SETTINGS["hooks"][event]
    [hook] = entry["hooks"]
    assert hook["command"] == guard_command('"$CLAUDE_PROJECT_DIR/scripts/hooks/guard_config.py"', "claude", argument)
    if event.startswith(("Pre", "Post")):
        assert entry["matcher"].split("|") == list(CLAUDE_GUARDED_TOOLS)


def guard_command(script: str, client: str, event: str) -> str:
    """The registered hook: skip a checkout whose guard is missing or speaks another protocol."""
    return (
        f"f={script}; grep -qs '^GUARD_PROTOCOL = 2$' \"$f\" || exit 0; "
        f'exec /usr/bin/python3 -I -B "$f" --client {client} --event {event}'
    )


def test_guard_declares_the_protocol_its_hooks_check():
    assert guard_config.GUARD_PROTOCOL == 2
    assert "\nGUARD_PROTOCOL = 2\n" in (ROOT / "scripts/hooks/guard_config.py").read_text()


@pytest.mark.parametrize("guard", [None, "import sys\nsys.exit(2)\n"], ids=["missing", "older-protocol"])
@pytest.mark.parametrize("event", sorted(CLAUDE_GUARD_EVENTS))
def test_claude_hooks_pass_on_a_checkout_without_this_guard(tmp_path, guard, event):
    # Switching to an older commit must not lock the agent out of every tool.
    if guard is not None:
        (tmp_path / "scripts/hooks").mkdir(parents=True)
        (tmp_path / "scripts/hooks/guard_config.py").write_text(guard)
    command = CLAUDE_SETTINGS["hooks"][event][0]["hooks"][0]["command"]
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        input="{}",
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(tmp_path)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode, result.stdout) == (0, "")


def test_claude_guard_hooks_satisfy_the_guards_own_check():
    assert guard_config.guard_registered(CLAUDE_SETTINGS)


@pytest.mark.parametrize("path", guard_config.PROTECTED_FILES)
def test_claude_asks_before_editing_a_protected_file(path):
    ask = CLAUDE_SETTINGS["permissions"]["ask"]
    assert f"Edit(/{path})" in ask or any(f"Edit(/{path.split('/')[0]}/**)" == rule for rule in ask)


@pytest.mark.parametrize("directory", guard_config.PROTECTED_DIRS)
def test_claude_asks_before_editing_a_protected_directory(directory):
    parts = directory.split("/")
    globs = {f"Edit(/{'/'.join(parts[:depth])}/**)" for depth in range(1, len(parts) + 1)}
    assert globs & set(CLAUDE_SETTINGS["permissions"]["ask"])


def test_claude_keeps_acceptance_and_protected_edits_with_the_user():
    permissions = CLAUDE_SETTINGS["permissions"]
    assert permissions["defaultMode"] == "acceptEdits"
    assert "Bash(make guard-accept-changes)" in permissions["ask"]
    assert "Bash" not in permissions["allow"]


@pytest.mark.parametrize(
    "rule",
    [
        "Bash(make uv-add *)",
        "Bash(make uv-remove *)",
        "Bash(make uv-upgrade)",
        "Bash(make uv-upgrade *)",
        "Bash(make uv-lock)",
        "Bash(make uv-reinstall)",
    ],
)
def test_claude_runs_dependency_targets_without_asking(rule):
    assert rule in CLAUDE_SETTINGS["permissions"]["allow"]


def test_claude_does_not_preapprove_direct_uv_writes():
    assert not [rule for rule in CLAUDE_SETTINGS["permissions"]["allow"] if rule.startswith("Bash(uv ")]
    assert "Bash(uv *--script*)" in CLAUDE_SETTINGS["permissions"]["ask"]


# Commands that rewrite the working tree from another ref; the guard accepts their
# protected-file changes, so the user approves each one.
WORKTREE_REWRITING = (
    "git switch *",
    "git checkout *",
    "git pull",
    "git pull *",
    "git merge *",
    "git rebase *",
    "git restore *",
    "git stash",
    "git stash *",
    "git reset *",
    "git cherry-pick *",
    "gh pr checkout *",
)


@pytest.mark.parametrize("command", WORKTREE_REWRITING)
def test_claude_asks_before_git_rewrites_the_worktree(command):
    assert f"Bash({command})" in CLAUDE_SETTINGS["permissions"]["ask"]


def test_codex_runs_without_a_sandbox():
    assert CODEX_CONFIG["sandbox_mode"] == "danger-full-access"
    for key in ("default_permissions", "permissions", "features"):
        assert key not in CODEX_CONFIG


@pytest.mark.parametrize(("event", "argument"), [("PreToolUse", "pre"), ("PostToolUse", "post"), ("Stop", "stop")])
def test_codex_runs_the_guard_on_every_event(event, argument):
    [entry] = CODEX_HOOKS[event]
    [hook] = entry["hooks"]
    script = '"$(git rev-parse --show-toplevel)/scripts/hooks/guard_config.py"'
    assert hook["command"] == guard_command(script, "codex", argument)
    if event != "Stop":
        assert entry["matcher"] == "Bash|apply_patch"
    assert set(CODEX_HOOKS) == {"PreToolUse", "PostToolUse", "Stop"}


@pytest.mark.integration
def test_registered_codex_pre_hook_runs_from_subdirectory():
    # Git's pre-push exports GIT_DIR. Carrying it into a different cwd makes
    # rev-parse treat that cwd as the worktree root. Model a normal agent launch,
    # not a nested Git hook, by clearing Git's documented local environment.
    git_local_vars = subprocess.check_output(["git", "rev-parse", "--local-env-vars"], text=True).splitlines()
    environment = {key: value for key, value in os.environ.items() if key not in git_local_vars}
    command = CODEX_HOOKS["PreToolUse"][0]["hooks"][0]["command"]
    patch = f"*** Begin Patch\n*** Delete File: {ROOT / 'uv.lock'}\n*** End Patch"
    payload = {"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "tool_input": {"command": patch}}
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        input=json.dumps({**payload, "cwd": str(ROOT / "scripts")}),
        cwd=ROOT / "scripts",
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_codex_enables_compound_engineering_from_its_marketplace():
    assert CODEX_CONFIG["plugins"] == {"compound-engineering@compound-engineering-plugin": {"enabled": True}}
    marketplace = CODEX_CONFIG["marketplaces"]["compound-engineering-plugin"]
    assert marketplace["source_type"] == "git"
    assert marketplace["source"].endswith("EveryInc/compound-engineering-plugin.git")


def make_recipe(target: str) -> list[str]:
    text = (ROOT / "scripts/development.mk").read_text()
    body = text.split(f"\n{target}:", 1)[1].split("\n\n", 1)[0]
    return [line.strip() for line in body.splitlines()[1:]]


def test_accept_target_runs_the_guard_with_the_system_interpreter():
    assert make_recipe("guard-accept-changes") == [
        "/usr/bin/python3 -I -B scripts/hooks/guard_config.py --event accept"
    ]


@pytest.mark.parametrize("target", sorted(guard_config.ALLOWLISTED_TARGETS))
def test_every_allowlisted_target_exists(target):
    declared = {
        name
        for line in (ROOT / "scripts/development.mk").read_text().splitlines()
        if (match := guard_config.MAKE_TARGET_LINE.match(line))
        for name in match.group(1).split()
    }
    assert target in declared


def test_commit_hook_checks_staged_protected_files_first():
    hooks = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())["repos"][0]["hooks"]
    assert hooks[0]["id"] == "guard-staged"
    assert hooks[0]["entry"] == "/usr/bin/python3 -I -B scripts/hooks/guard_config.py --event pre-commit"
    assert hooks[0]["always_run"] is True


# Each of these runs CI or publishes a release, so the user decides every time.
PUBLISHING = (
    "git push",
    "git push *",
    "gh pr create",
    "gh pr create *",
    "gh pr merge",
    "gh pr merge *",
    "make bump-patch",
    "make bump-minor",
    "make bump-major",
    "make release-tag",
)


@pytest.mark.parametrize("command", PUBLISHING)
def test_claude_asks_before_publishing(command):
    assert f"Bash({command})" in CLAUDE_SETTINGS["permissions"]["ask"]


CODEX_RULES = ROOT / ".codex/rules/publishing.rules"
# The same publishing commands, as Codex sees them; prefix rules match their argv.
CODEX_PUBLISHING = (
    ["git", "push"],
    ["git", "push", "origin", "feature"],
    ["gh", "pr", "create", "--fill"],
    ["gh", "pr", "merge", "52", "--merge"],
    ["make", "bump-patch"],
    ["make", "bump-minor"],
    ["make", "bump-major"],
    ["make", "release-tag"],
    ["make", "guard-accept-changes"],
)


def codex_prefix_rules() -> list[tuple[list[Any], str]]:
    """Each prefix_rule's pattern and decision, read without executing the rules file."""
    rules = []
    for node in ast.walk(ast.parse(CODEX_RULES.read_text())):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "prefix_rule":
            fields = {keyword.arg: ast.literal_eval(keyword.value) for keyword in node.keywords}
            # Codex treats a rule without a decision as "allow".
            rules.append((fields["pattern"], fields.get("decision", "allow")))
    return rules


def codex_rule_matches(pattern: list[Any], argv: list[str]) -> bool:
    return len(argv) >= len(pattern) and all(
        token in part if isinstance(part, list) else token == part for part, token in zip(pattern, argv, strict=False)
    )


def test_codex_publishing_rules_only_prompt():
    rules = codex_prefix_rules()
    # "allow" would skip the user's approval.
    assert {decision for _, decision in rules} == {"prompt"}
    # The file reaches generated projects through a Jinja include.
    text = CODEX_RULES.read_text()
    for delimiter in ("{" + "{", "{" + "%", "{" + "#"):
        assert delimiter not in text


@pytest.mark.parametrize("command", CODEX_PUBLISHING, ids=" ".join)
def test_codex_rules_cover_every_publishing_command(command):
    assert any(codex_rule_matches(pattern, command) for pattern, _ in codex_prefix_rules())


@pytest.mark.parametrize("command", [["git", "status"], ["make", "check"], ["gh", "pr", "view"]], ids=" ".join)
def test_codex_rules_leave_other_commands_alone(command):
    assert not any(codex_rule_matches(pattern, command) for pattern, _ in codex_prefix_rules())


def test_codex_prompts_reach_a_person():
    assert CODEX_CONFIG["approval_policy"] == "on-request"
    assert CODEX_CONFIG["approvals_reviewer"] == "user"


def codex_policy(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(
        [os.environ["CODEX_TEST_BINARY"], "execpolicy", "check", "--rules", str(CODEX_RULES), *command],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("CODEX_TEST_BINARY"), reason="opt-in: needs an installed Codex CLI")
@pytest.mark.parametrize("command", CODEX_PUBLISHING, ids=" ".join)
def test_real_codex_prompts_before_publishing(command):
    assert codex_policy(command)["decision"] == "prompt"


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("CODEX_TEST_BINARY"), reason="opt-in: needs an installed Codex CLI")
@pytest.mark.parametrize("command", [["git", "status"], ["make", "check"], ["gh", "pr", "view"]], ids=" ".join)
def test_real_codex_leaves_other_commands_alone(command):
    assert codex_policy(command)["matchedRules"] == []


def test_claude_runs_a_selected_test_only_under_tests():
    rules = [rule for rule in CLAUDE_SETTINGS["permissions"]["allow"] if "make test-one" in rule]
    assert rules == ["Bash(make test-one TEST=tests/*)"]


def test_claude_enables_only_the_sanctioned_plugins():
    assert CLAUDE_SETTINGS["enabledPlugins"] == {
        "pyright-lsp@claude-plugins-official": True,
        "compound-engineering@compound-engineering-plugin": True,
    }


def test_claude_declares_a_marketplace_for_every_enabled_plugin():
    """A fresh clone can only install a plugin whose marketplace the project declares."""
    marketplaces = CLAUDE_SETTINGS["extraKnownMarketplaces"]
    for plugin in CLAUDE_SETTINGS["enabledPlugins"]:
        source = marketplaces[plugin.split("@", 1)[1]]["source"]
        assert source["source"] == "github"
        assert source["repo"]


@pytest.mark.parametrize("path", [".claude/settings.json", ".claude/CLAUDE.md"])
def test_claude_configuration_names_no_retired_plugins(path):
    text = (ROOT / path).read_text().lower()
    assert "superpowers" not in text
    assert "caveman" not in text


def test_setup_installs_every_declared_plugin_in_project_scope():
    """make setup must stay in step with the plugins both hosts declare."""
    setup = (ROOT / "scripts/development.mk").read_text().split("\nsetup:", 1)[1].split("\n\n", 1)[0]
    for plugin in CLAUDE_SETTINGS["enabledPlugins"]:
        assert f"claude plugin install {plugin} --scope project" in setup, plugin
    for marketplace in CLAUDE_SETTINGS["extraKnownMarketplaces"].values():
        assert f"claude plugin marketplace add {marketplace['source']['repo']} --scope project" in setup
    assert 'cp "$$backup" .claude/settings.json' in setup
    for marketplace in CODEX_CONFIG["marketplaces"]:
        assert f"codex plugin marketplace upgrade {marketplace}" in setup
    assert "codex plugin add" not in setup, "Codex plugins stay project-scoped"


def test_claude_imports_shared_rules():
    assert "@AGENTS.md" in (ROOT / "CLAUDE.md").read_text()
    assert (ROOT / "AGENTS.md").is_file()
