"""Translate protected-file policy into Codex hook responses.

Codex's filesystem profile keeps root metadata read-only, including against indirect
shell writes. This hook blocks `apply_patch` edits to that metadata and lets only
complete, root-pinned uv commands and the project's exact formatter targets bypass
the native approval prompt. Claude Code enforces the same policy with its OS sandbox
(see `.claude/settings.json`) and does not run this hook.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
PROTECTED = frozenset({"pyproject.toml", "uv.lock"})
SHELL_EXPANSION = re.compile(r"[`$\n\r{}]")
SHELL_OPERATORS = frozenset(";&|<>()")
UV_ACTIONS = frozenset({"add", "remove", "lock", "sync", "version"})
UV_TARGET_OPTIONS = frozenset(
    {"--directory", "--project", "--script", "--config-file", "--cache-dir", "--python", "-p"}
)
REASON = (
    "Direct writes to pyproject.toml or uv.lock require user approval. "
    "Use a simple uv add/remove/lock/sync/version command for managed changes. "
    "For other changes, show the exact diff and request approval; do not weaken the gate. "
    "Retry the exact change as a shell command with user-reviewed sandbox escalation."
)


def shell_words(command: str) -> list[str]:
    """Tokenize without evaluating shell code; reject malformed quoting."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return []


def simple_command(command: str) -> bool:
    """Reject shell evaluation while accepting quoted dependency constraints."""
    words = shell_words(command)
    return (
        bool(words) and not SHELL_EXPANSION.search(command) and not any(set(word) <= SHELL_OPERATORS for word in words)
    )


def is_protected(path: str, cwd: Path) -> bool:
    """Recognize root metadata and existing aliases to those files."""
    candidate = Path(path.replace("\\", "/"))
    try:
        return (cwd / candidate).resolve() in {ROOT / name for name in PROTECTED}
    except (OSError, RuntimeError):
        return False


def managed_uv(command: str, cwd: Path, *, pinned: bool = False) -> bool:
    """Allow only standalone uv operations on this repository's project."""
    if not simple_command(command):
        return False
    words = shell_words(command)
    if len(words) < 2 or words[0] != "uv":
        return False
    if words[1] == "--directory" and len(words) >= 4:
        target = Path(words[2])
        if not target.is_absolute() or target.resolve() != ROOT:
            return False
        arguments = words[3:]
    elif pinned or cwd.resolve() != ROOT:
        return False
    else:
        arguments = words[1:]
    if arguments[0] not in UV_ACTIONS or "--" in arguments:
        return False
    return not any(
        word.split("=", 1)[0] in UV_TARGET_OPTIONS or (word.startswith("-p") and not word.startswith("--"))
        for word in arguments[1:]
    )


def managed_format(command: str) -> bool:
    """Recognize trusted formatting pinned to this repository, without overrides."""
    if not simple_command(command):
        return False
    words = shell_words(command)
    if len(words) != 4 or words[:2] != ["make", "--directory"]:
        return False
    target = Path(words[2])
    return (
        target.is_absolute()
        and target.resolve() == ROOT
        and words[3] in {"format", "format-md", "format-toml", "format-yaml", "format-json"}
    )


def protected_patch(command: str, cwd: Path) -> bool:
    """Inspect destinations as well as sources in the apply_patch wire format."""
    prefixes = ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: ")
    return any(
        is_protected(line.removeprefix(prefix).strip(), cwd)
        for line in command.splitlines()
        for prefix in prefixes
        if line.startswith(prefix)
    )


def pre_tool_decision(tool: str, tool_input: dict[str, Any], cwd: Path) -> dict[str, Any]:
    """Block protected patches pending a reviewed shell edit.

    Shell writes are left to the filesystem profile, which also catches indirect ones.
    """
    if tool != "apply_patch" or not protected_patch(str(tool_input.get("command", "")), cwd):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REASON,
        }
    }


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    """Return no decision unless this policy can safely handle the event."""
    tool = str(payload.get("tool_name", ""))
    tool_input = cast("dict[str, Any]", payload["tool_input"])
    cwd = Path(str(tool_input.get("workdir") or tool_input.get("cwd") or payload.get("cwd") or ROOT))
    event = str(payload.get("hook_event_name", "PreToolUse"))
    if event == "PermissionRequest":
        # CLI 0.154 omits execution cwd; its common cwd is only the session root.
        # Require a global --directory before the subcommand, not an apparent
        # flag hidden in another option's value or after the '--' terminator.
        command = str(tool_input.get("command", ""))
        if tool == "Bash" and (managed_uv(command, cwd, pinned=True) or managed_format(command)):
            return {"hookSpecificOutput": {"hookEventName": event, "decision": {"behavior": "allow"}}}
        return {}
    if event == "PreToolUse":
        return pre_tool_decision(tool, tool_input, cwd)
    return {}


def read_payload() -> dict[str, Any]:
    """Validate the protocol envelope before inspecting tool arguments."""
    raw: object = json.load(sys.stdin)
    if not isinstance(raw, dict):
        raise TypeError("hook payload must be an object")
    payload = cast("dict[str, Any]", raw)
    if not isinstance(payload.get("tool_input"), dict):
        raise TypeError("tool_input must be an object")
    return payload


def main() -> int:
    """Read one hook event; invalid input blocks instead of silently approving."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("codex",), required=True)
    parser.parse_args()
    try:
        output = decide(read_payload())
    except (ValueError, TypeError, OSError, RuntimeError) as error:
        sys.stderr.write(f"Protected-file hook cannot validate this request: {error}\n")
        return 2
    if output:
        json.dump(output, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
