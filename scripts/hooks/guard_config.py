"""Translate protected-file policy into host hook responses.

This conservative command classifier is not a shell sandbox. Codex also uses
filesystem permissions to catch indirect writes. Only complete, simple uv
commands and the project's exact formatter targets can bypass its native approval
prompt.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[2]
PROTECTED = frozenset({"pyproject.toml", "uv.lock"})
PROTECTED_TEXT = re.compile(r"(?<![\w.-])(?:pyproject\.toml|uv\.lock)(?![\w.-])")
SHELL_EXPANSION = re.compile(r"[`$\n\r{}]")
SHELL_OPERATORS = frozenset(";&|<>()")
UV_ACTIONS = frozenset({"add", "remove", "lock", "sync", "version"})
UV_TARGET_OPTIONS = frozenset(
    {"--directory", "--project", "--script", "--config-file", "--cache-dir", "--python", "-p"}
)
READ_COMMANDS = frozenset({"cat", "head", "tail", "rg", "grep", "wc", "stat", "ls", "nl"})
# Inspection commands that cannot create a file from their positional arguments. A command whose
# usage is "[INPUT [OUTPUT]]" can never appear here: uniq and xxd write their second operand, so no
# option list could gate them. Each entry lists the short letters and long options that would make
# the command write a file or execute another program; a short letter is matched inside a cluster,
# because `sort -uo FILE` writes exactly as `sort -o FILE` does.
INSPECTION_OPTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    # awk is deliberately absent: its pattern position evaluates arbitrary expressions, so
    # `awk 'system("...")'` and `awk '"cmd" | getline x'` execute with no option, no brace and
    # no directive. A classifier cannot carve a safe subset out of a programming language.
    "basename": ("", ()),
    "cksum": ("", ()),
    "cmp": ("", ()),
    "column": ("", ()),
    "comm": ("", ()),
    "cut": ("", ()),
    "diff": ("", ()),
    "dirname": ("", ()),
    "du": ("", ()),
    "file": ("C", ("--compile",)),
    "fold": ("", ()),
    "jq": ("", ()),
    "md5sum": ("", ()),
    "od": ("", ()),
    "paste": ("", ()),
    "realpath": ("", ()),
    "sha1sum": ("", ()),
    "sha256sum": ("", ()),
    "sha512sum": ("", ()),
    "sort": ("o", ("--output", "--compress-program")),
}
GLOB_CHARACTERS = frozenset("*?[")
REASON = (
    "Direct writes to pyproject.toml or uv.lock require user approval. "
    "Use a simple uv add/remove/lock/sync/version command for managed changes. "
    "For other changes, show the exact diff and request approval; do not weaken the gate."
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


def path_candidates(word: str) -> list[str]:
    """Yield the spellings of a path a single argument can carry.

    A short option may carry its value with no separator, so `-opyproject.toml` names the file
    while neither the word nor the text around it looks like a path. Every suffix is cheap to test
    and a suffix only equals a protected path when the argument really carries one.
    """
    candidates = [word]
    if "=" in word:
        candidates.append(word.split("=", 1)[1])
    if word.startswith("-") and not word.startswith("--"):
        candidates.extend(word[index:] for index in range(1, len(word)))
    return candidates


def matches_protected(word: str, cwd: Path) -> bool:
    """Report whether one argument can name a protected file, literally or through a glob.

    True means "ask"; every caller must keep that polarity, because an answer this function
    cannot determine is reported as True.

    A pattern such as `py*.toml` names no protected file literally while the shell still
    resolves it onto pyproject.toml. The pattern is matched against the two known protected
    paths rather than expanded against the filesystem: expanding would walk the tree for a
    pattern that matches nothing, follow the working directory's current contents instead of
    the command's meaning, and answer differently than the shell does for `**`.
    """
    protected_paths = {str(ROOT / name) for name in PROTECTED}
    for candidate in path_candidates(word):
        if is_protected(candidate, cwd):
            return True
        if not (GLOB_CHARACTERS & set(candidate)):
            continue
        if "[[:" in candidate:
            # The shell expands a POSIX class; fnmatch reads it as literal text and would miss.
            return True
        pattern = Path(candidate.replace("\\", "/")).expanduser()
        if not pattern.is_absolute():
            pattern = cwd / pattern
        # normpath collapses `..` textually; resolve() would touch the filesystem instead.
        if any(fnmatch.fnmatch(target, os.path.normpath(pattern)) for target in protected_paths):
            return True
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


def writes_a_file(word: str, letters: str, long_options: tuple[str, ...]) -> bool:
    """Detect a write option, including one bundled inside a short-option cluster."""
    if word.startswith("--"):
        # GNU accepts any unambiguous abbreviation, so `--out` writes exactly as `--output` does.
        name = word.split("=", 1)[0]
        return len(name) > 2 and any(option.startswith(name) for option in long_options)
    if word.startswith("-") and word != "-":
        return any(letter in letters for letter in word[1:])
    return False


def read_segment(words: list[str]) -> bool:
    """Recognize one inspection command, excluding options that execute or write."""
    if not words:
        return False
    if words[0] in {"rg", "grep"}:
        return not any(word.startswith(("--pre", "--hostname-bin")) for word in words[1:])
    if words[0] == "git":
        return (
            len(words) > 1
            and words[1] in {"diff", "show", "status", "log"}
            and not any(word.startswith(("--output", "--ext-diff", "--textconv")) for word in words[2:])
        )
    if words[0] == "sed":
        return (
            len(words) >= 3
            and words[1] == "-n"
            and bool(re.fullmatch(r"(?:\d+|\$)(?:,(?:\d+|\$))?p", words[2]))
            and all(not word.startswith("-") for word in words[3:])
        )
    if words[0] in READ_COMMANDS:
        return True
    entry = INSPECTION_OPTIONS.get(words[0])
    if entry is None:
        return False
    letters, long_options = entry
    return not any(writes_a_file(word, letters, long_options) for word in words[1:])


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


def selected_test(command: str, cwd: Path) -> bool:
    """Allow a single Make test selector, never extra flags or Make expressions."""
    if not simple_command(command) or cwd.resolve() != ROOT:
        return False
    words = shell_words(command)
    if len(words) != 3 or words[:2] != ["make", "test-one"] or not words[2].startswith("TEST=tests/"):
        return False
    path = Path(words[2].removeprefix("TEST=").split("::", 1)[0])
    return ".." not in path.parts and (ROOT / path).resolve().is_relative_to(ROOT / "tests")


def reads_only(command: str) -> bool:
    """Accept inspection pipelines and input redirects, never output redirects."""
    if SHELL_EXPANSION.search(command):
        return False
    segments: list[list[str]] = [[]]
    words = iter(shell_words(command))
    for word in words:
        if word in {"|", "&&"}:
            segments.append([])
        elif word == "<":
            target = next(words, "")
            if not target or set(target) <= SHELL_OPERATORS:
                return False
        elif set(word) <= SHELL_OPERATORS:
            return False
        else:
            segments[-1].append(word)
    return all(read_segment(segment) for segment in segments)


def protected_shell_write(command: str, cwd: Path) -> bool:
    """Ask about opaque commands mentioning protected paths; allow simple reads."""
    mentions_file = bool(PROTECTED_TEXT.search(command)) or any(
        matches_protected(word, cwd) for word in shell_words(command)
    )
    return mentions_file and not (managed_uv(command, cwd) or reads_only(command))


def protected_patch(command: str, cwd: Path) -> bool:
    """Inspect destinations as well as sources in the apply_patch wire format."""
    prefixes = ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: ")
    return any(
        is_protected(line.removeprefix(prefix).strip(), cwd)
        for line in command.splitlines()
        for prefix in prefixes
        if line.startswith(prefix)
    )


def pre_tool_decision(tool: str, tool_input: dict[str, Any], cwd: Path, client: str) -> dict[str, Any]:
    """Use Claude's ask, or block Codex patches pending a reviewed shell edit."""
    command = str(tool_input.get("command", ""))
    if tool == "Bash":
        if client == "claude" and selected_test(command, cwd):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "Run one test through the project's Make target.",
                }
            }
        # Codex's filesystem profile catches writes, including indirect ones.
        # Leave escalations to PermissionRequest; PreToolUse 'ask' fails open there.
        guarded = client == "claude" and protected_shell_write(command, cwd)
    elif tool == "apply_patch":
        guarded = protected_patch(command, cwd)
    elif tool in {"Edit", "Write", "MultiEdit"}:
        guarded = is_protected(str(tool_input.get("file_path", "")), cwd)
    else:
        return {}
    if not guarded:
        return {}
    reason = REASON
    if client == "codex":
        reason += " Retry the exact change as a shell command with user-reviewed sandbox escalation."
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask" if client == "claude" else "deny",
            "permissionDecisionReason": reason,
        }
    }


def decide(payload: dict[str, Any], client: str) -> dict[str, Any]:
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
        if client == "codex" and tool == "Bash" and (managed_uv(command, cwd, pinned=True) or managed_format(command)):
            return {"hookSpecificOutput": {"hookEventName": event, "decision": {"behavior": "allow"}}}
        return {}
    if event == "PreToolUse":
        return pre_tool_decision(tool, tool_input, cwd, client)
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


def main(client: str | None = None) -> int:
    """Read one hook event; invalid input blocks instead of silently approving."""
    if client is None:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--client", choices=("claude", "codex"), required=True)
        client = str(parser.parse_args().client)
    try:
        output = decide(read_payload(), client)
    except (ValueError, TypeError, OSError, RuntimeError) as error:
        sys.stderr.write(f"Protected-file hook cannot validate this request: {error}\n")
        return 2
    if output:
        json.dump(output, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
