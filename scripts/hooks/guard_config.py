"""Keep agents from changing the files that define this project's rules and tooling.

Claude Code and Codex run this hook around every tool call. It records a
last-approved baseline of the protected files, the Git index and hooks, and the
virtual environment, in the worktree's Git directory. After each call it restores
any change no approved route made and tells the agent why. Approved routes are the
allowlisted Make targets, a plain ``git commit``, plain branch-changing Git
commands, edits the user approved, and ``make guard-accept-changes``.

This is a guardrail against mistakes, not a security boundary: an agent that
deliberately rewrites this script is not stopped by it. Python 3.9+ standard
library only, because it runs as ``/usr/bin/python3 -I -B`` outside the project
environment.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable

# The registered hooks run this script only when it declares this protocol, so a
# checkout of an older commit, without this guard, does not lock the agent out.
GUARD_PROTOCOL = 2

PROTECTED_FILES = (
    "pyproject.toml",
    "uv.lock",
    "Makefile",
    "scripts/development.mk",
    ".claude/settings.json",
    ".codex/config.toml",
    ".codex/hooks.json",
    ".pre-commit-config.yaml",
    "scripts/format_files.py",
    "scripts/release.py",
    "scripts/install_taplo.py",
    "scripts/docs_version.py",
    "mkdocs.yml",
    ".taplo.toml",
    ".mdformat.toml",
    ".python-version",
    "AGENTS.md",
    "CLAUDE.md",
)
PROTECTED_DIRS = ("scripts/hooks", ".codex/rules")
FORBIDDEN_ANY_DEPTH = frozenset(
    {"ruff.toml", ".ruff.toml", "pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "tox.ini", "setup.cfg"}
)
FORBIDDEN_ROOT = frozenset(
    {
        "pyrightconfig.json",
        ".coveragerc",
        ".coveragerc.toml",
        "GNUmakefile",
        "makefile",
        ".cz.toml",
        "cz.toml",
        ".cz.json",
        "cz.json",
        ".cz.yaml",
        "cz.yaml",
        "uv.toml",
        ".gitattributes",
        "taplo.toml",
        "yamlfix.toml",
        ".yamlfix.toml",
    }
)
FORBIDDEN_BELOW_ROOT = frozenset({".mdformat.toml", "pyproject.toml"})
MAKEFILE_NAMES = frozenset({"Makefile", "GNUmakefile", "makefile"})
SKIPPED_DIRS = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache", ".cache"}
)
SKIPPED_DIRS_AT_ROOT = frozenset({"site", "htmlcov", "dist", "build"})
OPERATION_MARKERS = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply")
STATE_DIR_NAME = "protected-baseline"
SYMLINK_PREFIX = "symlink:"
TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024

PACKAGE_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~]*")
GROUP_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
ALLOWLISTED_TARGETS: dict[str, dict[str, re.Pattern[str]]] = {
    "uv-add": {"PKG": PACKAGE_VALUE, "GROUP": GROUP_VALUE},
    "uv-remove": {"PKG": PACKAGE_VALUE, "GROUP": GROUP_VALUE},
    "uv-upgrade": {"PKG": PACKAGE_VALUE},
    "uv-lock": {},
    "uv-reinstall": {},
    "format": {},
    "format-toml": {},
    "format-json": {},
    "format-md": {},
    "format-yaml": {},
    "format-project-text": {},
    "format-ruff": {},
    "lint-fix": {},
    "install": {},
    "setup": {},
    "install-taplo": {},
    "bump-patch": {},
    "bump-minor": {},
    "bump-major": {},
    "guard-accept-changes": {},
}
HISTORY_COMMANDS = frozenset(
    {"switch", "checkout", "pull", "merge", "rebase", "reset", "restore", "stash", "cherry-pick"}
)
SHELL_TOOLS = frozenset({"Bash", "PowerShell", "Monitor", "shell", "local_shell", "exec_command", "unified_exec"})
EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
CLAUDE_GUARD_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "ConfigChange", "Stop")
SHELL_EXPANSION = re.compile(r"[`$\n\r{}]")
SHELL_OPERATORS = frozenset(";&|<>()")
MAKE_TARGET_LINE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.\- ]*?)\s*::?(?!=)")
# A call that never reported back (a crashed session) stops blocking others after an hour;
# long single commands, such as a slow Codex make target, finish well within it.
STALE_SECONDS = 3600
SWITCH_OFF = (
    (
        re.compile(r"\bgit\b[^;&|\n]*\bcommit\b[^;&|\n]*\s-(?!-)[A-Za-z]*n[A-Za-z]*\b"),
        "`git commit -n` skips the Git hooks",
    ),
    (re.compile(r"\bgit\b[^;&|\n]*\s--no-verify\b"), "`--no-verify` skips the Git hooks"),
    (re.compile(r"(?:^|[\s;&|(`'\"])SKIP=|\bexport\s+SKIP\b"), "`SKIP=` skips pre-commit hooks"),
    (re.compile(r"core\.hooks[Pp]ath"), "changing `core.hooksPath` disables the Git hooks"),
    (
        re.compile(r"\bGIT_CONFIG_(?:COUNT|KEY_\w*|VALUE_\w*|PARAMETERS|GLOBAL|SYSTEM|NOSYSTEM)\b"),
        "`GIT_CONFIG_*` variables override the repository's Git configuration",
    ),
    (
        re.compile(
            r"\b(?:PYTEST_ADDOPTS|COVERAGE_RCFILE|COVERAGE_PROCESS_START|MAKEFLAGS|GNUMAKEFLAGS|MFLAGS|MAKEFILES)\s*="
            r"|\bexport\s+(?:PYTEST_ADDOPTS|COVERAGE_RCFILE|MAKEFLAGS|GNUMAKEFLAGS|MFLAGS|MAKEFILES)\b"
        ),
        "these variables change how the checks run",
    ),
    (
        re.compile(
            r"\bmake\b[^;&|\n]*\s(?:--file\b|--makefile\b|--directory\b|--environment-overrides\b"
            r"|-(?!-)[A-Za-z]*[fCe][A-Za-z]*\b)"
        ),
        "`make -f/-C/-e` replaces the project's Make rules",
    ),
    (re.compile(r"\bpre-commit\b[^;&|\n]*\buninstall\b"), "`pre-commit uninstall` removes the Git hooks"),
    (re.compile(re.escape(STATE_DIR_NAME)), "the guard's baseline belongs to the guard"),
)
# `git [global options] commit`, but not `git commit-tree`.
GIT_COMMIT = re.compile(r"\bgit(?:\s+-\S+(?:\s+[^\s-]\S*)?)*\s+commit(?![\w-])")
# Make goals the user approves each time; only their exact standalone form reaches the prompt.
USER_APPROVED_GOAL = re.compile(r"\bmake\b[^;&|\n]*\b(guard-accept-changes|bump-(?:patch|minor|major)|release-tag)\b")
HEREDOC = re.compile(r"(<<-?\s*(['\"]?)(\w+)\2[^\n]*)\n.*?^\t*\3[ \t]*$", re.DOTALL | re.MULTILINE)
QUOTED = re.compile(r"((?:^|\s)(?:-c|eval)\s+)?('[^']*'|\"(?:\\.|[^\"\\])*\")")
INTERPRETERS = re.compile(r"(?:^|/)(?:python[0-9.]*|exec|source|\.|sh|bash|zsh|env)$")
CODEX_HOOKS_OFF = re.compile(r"(?m)^\s*(?:codex_)?hooks\s*=\s*false\b")

RESTORED = (
    "Writing these files directly is forbidden for agents: {paths}. They were restored. "
    "Use the make targets (make uv-add PKG=..., make uv-remove PKG=..., make uv-upgrade, make uv-lock, "
    "make format) or ask the user to make or approve the change."
)
DRIFT = (
    "Protected project state changed outside an agent call: {paths}. Stop and ask the user whether they made "
    "this change; `git diff` shows it. If they did and want to keep it, they approve `make guard-accept-changes`."
)
VENV_DRIFT = (
    "The project .venv changed outside an allowlisted make target. Run `make uv-reinstall` to rebuild it "
    "from uv.lock, or ask the user to approve `make guard-accept-changes` if they made the change."
)
OPERATION_HELD = (
    "A merge, rebase or cherry-pick changed protected files it could not settle: {paths}. They were left as they "
    "are. Stop and ask the user to resolve them and finish the operation, then to approve `make guard-accept-changes`."
)
BUSY = "Another call that can change protected files is running. Retry after it finishes."
BACKGROUND = "Run allowlisted make targets in the foreground, so the guard can check their changes."
PLAIN_COMMIT = (
    "Run `git commit` as a standalone command with no chaining or `$(...)`, e.g. `git commit -F <message-file>`, "
    "so the guard can check what it commits."
)
STANDALONE_GOAL = "Run `make {goal}` as a standalone command with no other goals or variables, so the user is asked."


class GuardError(RuntimeError):
    """A condition the guard cannot evaluate safely."""


def shell_words(command: str) -> list[str]:
    """Tokenize without evaluating shell code; reject malformed quoting."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return []


def executable_text(command: str) -> str:
    """Drop heredoc bodies and quoted data, keeping only text the shell would run.

    Quoted code handed to `sh -c` or `eval` stays, because the shell runs it.
    """
    text = HEREDOC.sub(r"\1", command)
    return QUOTED.sub(lambda match: match.group(0) if match.group(1) else "''", text)


def simple_command(command: str) -> bool:
    """Reject shell evaluation, chaining and redirection while accepting quoted values."""
    words = shell_words(command)
    return (
        bool(words) and not SHELL_EXPANSION.search(command) and not any(set(word) <= SHELL_OPERATORS for word in words)
    )


def git(root: Path, *args: str, check: bool = True) -> str:
    """Run Git in the worktree and return its standard output."""
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if check and result.returncode != 0:
        raise GuardError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def is_forbidden_name(path: str) -> bool:
    """Classify a worktree-relative path against the forbidden override names."""
    name = Path(path).name
    at_root = "/" not in path
    return (
        name in FORBIDDEN_ANY_DEPTH
        or (at_root and name in FORBIDDEN_ROOT)
        or (not at_root and name in FORBIDDEN_BELOW_ROOT)
    )


def walk_files(root: Path, *, skip_caches: bool) -> list[str]:
    """List worktree files, never descending into Git data or nested checkouts."""
    found: list[str] = []
    for current, dirs, names in os.walk(root):
        here = Path(current)
        prefix = "" if here == root else here.relative_to(root).as_posix() + "/"
        dirs[:] = [
            name
            for name in dirs
            if name != ".git"
            and not (here / name).is_symlink()
            and not (here / name / ".git").exists()
            and not (skip_caches and (name in SKIPPED_DIRS or (here == root and name in SKIPPED_DIRS_AT_ROOT)))
        ]
        found.extend(prefix + name for name in names)
    return found


class Repo:
    """The worktree a hook event belongs to, and its guard state directory."""

    def __init__(self, start: Path) -> None:
        """Locate the worktree root and the per-worktree state directory."""
        top, git_dir, common_dir = git(
            start, "rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir"
        ).splitlines()[:3]
        self.root = Path(top).resolve()
        self.git_dir = Path(git_dir)
        self.common_dir = Path(common_dir)
        if not self.common_dir.is_absolute():
            self.common_dir = (Path(start).resolve() / self.common_dir).resolve()
        self.state_dir = self.git_dir / STATE_DIR_NAME
        self.blob_dir = self.state_dir / "blobs"
        self.hash_name = git(self.root, "rev-parse", "--show-object-format", check=False).strip() or "sha1"
        self._files: list[str] | None = None
        self._protected: dict[Path, str] | None = None

    def blob_id(self, content: bytes) -> str:
        """Compute the Git object id of a blob with this content."""
        header = f"blob {len(content)}\0".encode()
        return hashlib.new(self.hash_name, header + content).hexdigest()

    def store(self, content: bytes) -> str:
        """Keep a copy of approved content so it can be restored later."""
        blob = self.blob_id(content)
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileExistsError), (self.blob_dir / blob).open("xb") as handle:
            handle.write(content)
        return blob

    def files(self) -> list[str]:
        """List worktree files once per event, skipping caches and the virtual environment."""
        if self._files is None:
            self._files = walk_files(self.root, skip_caches=True)
        return self._files

    def resolved_protected(self) -> dict[Path, str]:
        """Map each protected path's resolved location to its name, once per event."""
        if self._protected is None:
            self._protected = {(self.root / path).resolve(): path for path in protected_paths(self, {})}
        return self._protected

    def operation_in_progress(self) -> bool:
        """Whether a merge, rebase, cherry-pick or revert is waiting to be finished."""
        return any((self.git_dir / marker).exists() for marker in OPERATION_MARKERS)


def symlink_id(target: str) -> str:
    """Identify a symlink by its target."""
    return SYMLINK_PREFIX + target


def file_id(repo: Repo, path: Path) -> str | None:
    """Identify a file's content; symlinks are identified by their target."""
    if path.is_symlink():
        return symlink_id(str(path.readlink()))
    try:
        return repo.blob_id(path.read_bytes())
    except (FileNotFoundError, IsADirectoryError):
        return None


def capture(repo: Repo, path: Path) -> str | None:
    """Identify a file and keep a copy of its content for later restoration."""
    if path.is_symlink():
        return file_id(repo, path)
    try:
        return repo.store(path.read_bytes())
    except (FileNotFoundError, IsADirectoryError):
        return None


def approve(approved: dict[str, list[str | None]], path: str, blob: str | None) -> None:
    """Add a content id to a path's approval history."""
    history = approved.setdefault(path, [])
    if blob not in history:
        history.append(blob)


def unapproved(
    ids: dict[str, str | None], reference: dict[str, str | None], approved: dict[str, list[str | None]]
) -> list[str]:
    """Paths whose content is neither the reference content nor previously approved."""
    return [path for path, blob in ids.items() if blob != reference[path] and blob not in approved.get(path, [])]


def forbidden_files(repo: Repo) -> set[str]:
    """Find override configuration files that would replace the project's rules."""
    return {path for path in repo.files() if is_forbidden_name(path)}


def extra_makefiles(repo: Repo) -> list[str]:
    """Find Make rule files anywhere outside the root Makefile, caches and build output included."""
    return sorted(
        path
        for path in walk_files(repo.root, skip_caches=False)
        if Path(path).name in MAKEFILE_NAMES and path != "Makefile"
    )


def declared_targets(repo: Repo) -> set[str]:
    """Read the target names the protected Makefiles declare."""
    targets: set[str] = set()
    for name in ("Makefile", "scripts/development.mk"):
        try:
            text = (repo.root / name).read_text(errors="replace")
        except FileNotFoundError:
            continue
        for line in text.splitlines():
            match = MAKE_TARGET_LINE.match(line)
            if match and not line.startswith(("override ", "export ", "\t")):
                targets.update(word for word in match.group(1).split() if not word.startswith("."))
    return targets


def protected_paths(repo: Repo, state: dict[str, Any]) -> list[str]:
    """List every protected path, including ones that appeared or disappeared."""
    paths = set(PROTECTED_FILES) | set(cast("dict[str, Any]", state.get("files", {})))
    for directory in PROTECTED_DIRS:
        base = repo.root / directory
        if base.is_dir():
            paths.update(
                path.relative_to(repo.root).as_posix()
                for path in base.rglob("*")
                if (path.is_file() or path.is_symlink()) and "__pycache__" not in path.parts
            )
    listed = git(repo.root, "ls-files", "-s", "--", *PROTECTED_DIRS).splitlines()
    paths.update(line.split("\t", 1)[1] for line in listed if "\t" in line)
    return sorted(paths)


def tree_paths(repo: Repo, revision: str) -> set[str]:
    """Protected-directory paths recorded in a commit."""
    return set(git(repo.root, "ls-tree", "-r", "--name-only", revision, "--", *PROTECTED_DIRS).splitlines())


def worktree_ids(repo: Repo, paths: list[str]) -> dict[str, str | None]:
    """Identify the working-tree content of each path."""
    return {path: file_id(repo, repo.root / path) for path in paths}


def entry_id(repo: Repo, mode: str, blob: str) -> str:
    """Identify an index or tree entry the same way as working-tree content."""
    return symlink_id(git(repo.root, "cat-file", "blob", blob)) if mode == "120000" else blob


def resolve(repo: Repo, revision: str) -> str | None:
    """Return the commit a revision names, or None when it does not exist."""
    return git(repo.root, "rev-parse", "--verify", "--quiet", revision, check=False).strip() or None


def tree_ids(repo: Repo, paths: list[str], revision: str = "HEAD") -> dict[str, str | None]:
    """Identify each path's blob in a commit; absent paths map to None."""
    ids: dict[str, str | None] = dict.fromkeys(paths)
    if resolve(repo, revision) is None:
        return ids
    for line in git(repo.root, "ls-tree", "-r", revision, "--", *paths).splitlines():
        meta, _, path = line.partition("\t")
        mode, _, blob = meta.split()
        if path in ids:
            ids[path] = entry_id(repo, mode, blob)
    return ids


def index_ids(repo: Repo, paths: list[str]) -> dict[str, str | None]:
    """Identify each path's staged blob; unstaged paths map to None."""
    ids: dict[str, str | None] = dict.fromkeys(paths)
    for line in git(repo.root, "ls-files", "-s", "--", *paths).splitlines():
        meta, _, path = line.partition("\t")
        mode, blob, _stage = meta.split()
        if path in ids:
            ids[path] = entry_id(repo, mode, blob)
    return ids


def unmerged(repo: Repo, paths: list[str]) -> set[str]:
    """Protected paths with conflict stages in the index."""
    return {line.split("\t", 1)[1] for line in git(repo.root, "ls-files", "-u", "--", *paths).splitlines()}


def head(repo: Repo) -> str | None:
    """Return the current commit, or None in an empty repository."""
    return resolve(repo, "HEAD")


def site_packages(repo: Repo) -> list[Path]:
    """Locate the virtual environment's site-packages directories."""
    return sorted((repo.root / ".venv").glob("lib/python*/site-packages"))


def venv_fingerprint(repo: Repo) -> str:
    """Fingerprint the parts of .venv that change which code the checks run."""
    venv = repo.root / ".venv"
    if not venv.is_dir():
        return "absent"
    entries: list[str] = []
    candidates = list((venv / "bin").glob("*"))
    for packages in site_packages(repo):
        candidates += list(packages.glob("*.pth"))
        candidates += [packages / "sitecustomize.py", packages / "usercustomize.py"]
        entries += sorted(path.name for path in packages.glob("*.dist-info"))
    for path in candidates:
        with contextlib.suppress(OSError):
            stat = path.lstat()
            entries.append(f"{path.relative_to(venv)}:{stat.st_size}:{stat.st_mtime_ns}")
    return hashlib.sha256("\n".join(sorted(entries)).encode()).hexdigest()


def hooks_state(repo: Repo, *, keep: bool = False) -> dict[str, Any]:
    """Describe the installed Git hooks, their file modes and the effective hooks path.

    Copies of the scripts are kept only when the state becomes the baseline.
    """
    hooks_path = git(repo.root, "config", "--get", "core.hooksPath", check=False).strip() or None
    scripts: dict[str, str] = {}
    directory = repo.common_dir / "hooks"
    if directory.is_dir():
        for path in sorted(directory.iterdir()):
            if path.is_file() and not path.name.endswith(".sample"):
                content = path.read_bytes()
                blob = repo.store(content) if keep else repo.blob_id(content)
                scripts[path.name] = f"{blob}:{path.stat().st_mode & 0o777:o}"
    return {"path": hooks_path, "scripts": scripts}


def codex_home() -> Path:
    """Return the user's Codex configuration directory."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def user_files() -> dict[str, Path]:
    """The user-level Codex files that can switch the project's hooks off."""
    home = codex_home()
    return {"config.toml": home / "config.toml", "hooks.json": home / "hooks.json"}


def store_user_files(repo: Repo) -> dict[str, str | None]:
    """Keep copies of the user-level Codex files as they are now."""
    return {name: capture(repo, path) for name, path in user_files().items()}


def disables_hooks(name: str, content: bytes | None, root: Path) -> bool:
    """Recognize user-level Codex settings that switch hooks off or untrust the project."""
    if content is None:
        return False
    text = content.decode(errors="replace")
    if name == "hooks.json":
        try:
            json.loads(text)
        except ValueError:
            return True
        return False
    untrusted = re.search(r'\[projects\."' + re.escape(str(root)) + r'"\][^\[]*trust_level\s*=\s*"(?!trusted")', text)
    return bool(CODEX_HOOKS_OFF.search(text) or untrusted)


@contextlib.contextmanager
def locked(repo: Repo) -> Generator[None, None, None]:
    """Serialize hook runs for one worktree."""
    repo.state_dir.mkdir(parents=True, exist_ok=True)
    with (repo.state_dir / "lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_state(repo: Repo) -> dict[str, Any] | None:
    """Read the baseline, or None before the first run."""
    try:
        raw: object = json.loads((repo.state_dir / "state.json").read_text())
    except FileNotFoundError:
        return None
    if not isinstance(raw, dict):
        raise GuardError("guard state is not an object")
    return cast("dict[str, Any]", raw)


def save_state(repo: Repo, state: dict[str, Any]) -> None:
    """Write the baseline atomically."""
    path = repo.state_dir / "state.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=1, sort_keys=True))
    temporary.replace(path)


def snapshot(repo: Repo, state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record the current protected state, keeping the approval history."""
    previous = state or {}
    paths = protected_paths(repo, previous)
    files = {path: capture(repo, repo.root / path) for path in paths}
    approved = cast("dict[str, list[str | None]]", previous.get("approved", {}))
    for ids in (files, index_ids(repo, paths)):
        for path, blob in ids.items():
            approve(approved, path, blob)
    hooks = hooks_state(repo, keep=True)
    hook_blobs = [entry.split(":", 1)[0] for entry in cast("dict[str, str]", hooks["scripts"]).values()]
    users = store_user_files(repo)
    prune_blobs(repo, [*files.values(), *hook_blobs, *users.values()])
    return {
        "version": 1,
        "files": files,
        "approved": approved,
        "forbidden_known": sorted(forbidden_files(repo)),
        "venv": venv_fingerprint(repo),
        "venv_blocked": False,
        "hooks": hooks,
        "user": users,
        "head": head(repo),
        "blocked": None,
        "inflight": previous.get("inflight", {}),
        "window": [],
    }


def prune_blobs(repo: Repo, keep: Iterable[str | None]) -> None:
    """Drop stored copies the new baseline no longer needs; approval history keeps ids only."""
    wanted = {blob for blob in keep if blob}
    if repo.blob_dir.is_dir():
        for path in repo.blob_dir.iterdir():
            if path.name not in wanted:
                path.unlink()


def restore(repo: Repo, path: str, blob: str | None) -> None:
    """Put a protected path back to its approved content."""
    target = repo.root / path
    if target.is_symlink() or (blob is None and target.exists()):
        target.unlink()
    if blob is None:
        return
    if blob.startswith(SYMLINK_PREFIX):
        target.symlink_to(blob.removeprefix(SYMLINK_PREFIX))
        return
    source = repo.blob_dir / blob
    if not source.is_file():
        raise GuardError(f"no stored copy of {path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def reset_index(repo: Repo, paths: list[str]) -> None:
    """Return staged entries of the given paths to their committed state."""
    if paths:
        git(repo.root, "reset", "-q", "--", *paths)


def transcript_finished(entry: dict[str, Any], call_id: str) -> bool:
    """Recognize a call whose result already reached the transcript, e.g. a denied one."""
    if not entry.get("transcript"):
        return False
    path = Path(str(entry["transcript"]))
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - TRANSCRIPT_TAIL_BYTES))
            data = handle.read()
    except OSError:
        return False
    needle = call_id.encode()
    if needle not in data:
        return False
    for line in data.splitlines():
        if needle not in line:
            continue
        with contextlib.suppress(ValueError):
            if _has_result(json.loads(line), call_id):
                return True
    return False


def _has_result(node: object, call_id: str) -> bool:
    """Search a transcript record for a tool result that answers the call."""
    if isinstance(node, dict):
        record = cast("dict[str, Any]", node)
        kinds = {"tool_result", "function_call_output", "custom_tool_call_output"}
        if record.get("type") in kinds and call_id in (record.get("tool_use_id"), record.get("call_id")):
            return True
        return any(_has_result(value, call_id) for value in record.values())
    if isinstance(node, list):
        return any(_has_result(value, call_id) for value in cast("list[object]", node))
    return False


def live_calls(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Drop calls that finished without a post event, then return the rest."""
    inflight = cast("dict[str, dict[str, Any]]", state.setdefault("inflight", {}))
    now = time.time()
    for call_id, entry in list(inflight.items()):
        if now - float(entry.get("started", 0)) > STALE_SECONDS or transcript_finished(entry, call_id):
            del inflight[call_id]
    return inflight


def relative_protected(repo: Repo, raw: str, cwd: Path) -> str | None:
    """Map a tool path to a protected path, following symlinks."""
    if not raw:
        return None
    try:
        resolved = (cwd / raw.replace("\\", "/")).resolve()
    except (OSError, RuntimeError):
        return None
    protected = repo.resolved_protected()
    if resolved in protected:
        return protected[resolved]
    for directory in PROTECTED_DIRS:
        base = (repo.root / directory).resolve()
        if base in resolved.parents:
            return resolved.relative_to(repo.root.resolve()).as_posix()
    return None


class Call:
    """One tool call and the protected changes it may make."""

    def __init__(self, payload: dict[str, Any], repo: Repo, client: str) -> None:
        """Classify the call once from its tool name and input."""
        self.client = client
        self.repo = repo
        self.tool = str(payload.get("tool_name", ""))
        self.input = cast("dict[str, Any]", payload.get("tool_input") or {})
        self.command = str(self.input.get("command", "")) if self.tool in SHELL_TOOLS else ""
        self.cwd = Path(str(payload.get("cwd") or repo.root))
        self.words = shell_words(self.command) if self.command else []
        self.simple = bool(self.command) and simple_command(self.command)
        self.background = bool(self.input.get("run_in_background")) or self.tool == "Monitor"
        self.target = self._make_target()
        self.git_kind = self._git_kind()
        self.edit_path = self._edit_path()

    def _edit_path(self) -> str | None:
        """The protected path a Claude file tool edits, if any."""
        if self.tool not in EDIT_TOOLS:
            return None
        raw = str(self.input.get("file_path") or self.input.get("notebook_path") or "")
        return relative_protected(self.repo, raw, self.cwd)

    def _is_make(self) -> bool:
        return self.simple and len(self.words) >= 2 and self.words[0] == "make"

    def _make_target(self) -> str | None:
        """Return the target of an exact allowlisted make command."""
        if not self._is_make():
            return None
        target, assignments = self.words[1], self.words[2:]
        variables = ALLOWLISTED_TARGETS.get(target)
        if variables is None:
            return None
        for word in assignments:
            name, equals, value = word.partition("=")
            pattern = variables.get(name)
            if not equals or pattern is None or not pattern.fullmatch(value):
                return None
        if self.client == "claude" and self.cwd.resolve() != self.repo.root:
            return None
        return target

    def plain_make(self) -> bool:
        """Recognize a plain make command for a target the project declares."""
        if not self._is_make() or any(word.startswith("-") for word in self.words[1:]):
            return False
        targets = [word for word in self.words[1:] if "=" not in word]
        return bool(targets) and set(targets) <= declared_targets(self.repo)

    def _git_kind(self) -> str | None:
        """Return 'commit' or 'history' for a plain Git command the guard accepts."""
        if not self.simple or len(self.words) < 2:
            return None
        if self.words[0] == "git" and self.words[1] == "commit":
            return "commit"
        if self.words[0] == "git" and self.words[1] in HISTORY_COMMANDS:
            return "history"
        return "history" if self.words[:3] == ["gh", "pr", "checkout"] else None

    def runs_guard(self) -> bool:
        """Whether the command executes the guard script rather than mentioning it."""
        text = executable_text(self.command)
        words = shell_words(text)
        if not words:
            return "guard_config" in text
        for index, word in enumerate(words):
            if "guard_config" not in word:
                continue
            if index == 0 or INTERPRETERS.search(words[index - 1]):
                return True
            if any(marker in word for marker in ("import", "runpy", "exec(")):
                return True
        return "--event" in words and any("guard_config" in word for word in words)

    def exclusive(self) -> bool:
        """Calls that may change protected state run alone."""
        return bool(self.target or self.git_kind or self.edit_path)

    def allowance(self, *, failed: bool) -> dict[str, Any]:
        """What this call may change, recorded for the post-call check."""
        target = self.target if not self.background else None
        kind = self.git_kind
        return {
            "files": "any" if target else kind,
            "edit": self.edit_path,
            "make": bool(target),
            "venv": bool(target or kind or self.plain_make()) and not (target == "uv-reinstall" and failed),
            "hooks": target in {"setup", "install"},
            "head": bool(kind) or (target or "").startswith("bump-"),
            "accept": target == "guard-accept-changes" and not failed,
            "client": self.client,
        }


def deny(reason: str) -> dict[str, Any]:
    """A PreToolUse refusal both hosts understand."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def switch_off_reason(call: Call) -> str | None:
    """Explain why a command that would switch checks off or skip an approval is refused."""
    command = executable_text(call.command)
    for pattern, reason in SWITCH_OFF:
        if pattern.search(command):
            return f"Refused: {reason}. Run the checks as the project defines them."
    if call.runs_guard():
        return "Refused: the guard runs only from its hooks and `make guard-accept-changes`."
    if GIT_COMMIT.search(command) and call.git_kind != "commit":
        return f"Refused: {PLAIN_COMMIT}"
    goal = USER_APPROVED_GOAL.search(command)
    if goal and call.words != ["make", goal.group(1)]:
        return f"Refused: {STANDALONE_GOAL.format(goal=goal.group(1))}"
    return None


def protected_patch(repo: Repo, patch: str, cwd: Path) -> bool:
    """Inspect destinations as well as sources in the apply_patch wire format."""
    prefixes = ("*** Add File: ", "*** Update File: ", "*** Delete File: ", "*** Move to: ")
    return any(
        relative_protected(repo, line.removeprefix(prefix).strip(), cwd)
        for line in patch.splitlines()
        for prefix in prefixes
        if line.startswith(prefix)
    )


def drift(repo: Repo, state: dict[str, Any]) -> list[str]:
    """List protected state that differs from the baseline."""
    baseline = cast("dict[str, str | None]", state["files"])
    paths = protected_paths(repo, state)
    current = worktree_ids(repo, paths)
    changed = [path for path in paths if current[path] != baseline.get(path)]
    approved = cast("dict[str, list[str | None]]", state["approved"])
    staged = unapproved(index_ids(repo, paths), tree_ids(repo, paths), approved)
    changed += [f"{path} (staged)" for path in staged]
    changed += sorted(forbidden_files(repo) - set(state["forbidden_known"]))
    if hooks_state(repo) != state["hooks"]:
        changed.append("Git hooks")
    return changed


def pre_tool(payload: dict[str, Any], repo: Repo, client: str) -> dict[str, Any]:
    """Refuse switch-off commands, conflicting calls and calls on drifted state."""
    call = Call(payload, repo, client)
    reason = switch_off_reason(call) if call.command else None
    if reason:
        return deny(reason)
    if call.tool in EDIT_TOOLS and STATE_DIR_NAME in str(call.input.get("file_path", "")):
        return deny("Refused: the guard's baseline belongs to the guard.")
    if call.tool == "apply_patch" and protected_patch(repo, str(call.input.get("command", "")), call.cwd):
        return deny(
            "Agents may not patch protected files. Ask the user to make this change; for dependencies use "
            "make uv-add/uv-remove/uv-upgrade/uv-lock."
        )
    if call.target and call.background:
        return deny(BACKGROUND)
    with locked(repo):
        return admit(call, payload)


def admit(call: Call, payload: dict[str, Any]) -> dict[str, Any]:
    """Check concurrency and drift, then register the call as running."""
    repo, target = call.repo, call.target
    state = load_state(repo)
    if state is None:
        # The accept target creates the baseline itself once the user approves it;
        # Claude runs this hook before asking, so nothing may be recorded here.
        if target == "guard-accept-changes":
            return {}
        if drift_from_head(repo):
            return deny(DRIFT.format(paths="protected files differ from HEAD on the guard's first run"))
        state = snapshot(repo)
    others = live_calls(state)
    if not others and state.get("window"):
        # Calls that ended without a post event (a denied permission) left changes to judge.
        problems = evaluate(repo, state)
        if problems:
            save_state(repo, state)
            return deny(" ".join(problems))
    exclusive = call.exclusive()
    if others and (exclusive or any(entry.get("exclusive") for entry in others.values())):
        save_state(repo, state)
        return deny(BUSY)
    if not others and target != "guard-accept-changes":
        refusal = pre_drift_refusal(repo, state, target)
        if refusal:
            save_state(repo, state)
            return deny(refusal)
    call_id = str(payload.get("tool_use_id") or f"anonymous-{time.time()}")
    others[call_id] = {
        "started": time.time(),
        "exclusive": exclusive,
        "target": target if not call.background else None,
        "client": call.client,
        "transcript": payload.get("transcript_path"),
    }
    save_state(repo, state)
    return {}


def pre_drift_refusal(repo: Repo, state: dict[str, Any], target: str | None) -> str | None:
    """Refuse a call while protected state differs from the baseline."""
    # User-level Codex files changed between calls belong to the user.
    state["user"] = store_user_files(repo)
    if state.get("blocked"):
        return str(state["blocked"])
    changed = drift(repo, state)
    if changed:
        return DRIFT.format(paths=", ".join(changed))
    if venv_fingerprint(repo) != state["venv"]:
        state["venv_blocked"] = True
    if state.get("venv_blocked") and target != "uv-reinstall":
        return VENV_DRIFT
    state["head"] = head(repo)
    return None


def drift_from_head(repo: Repo) -> bool:
    """Whether protected files or their staged copies differ from HEAD."""
    paths = protected_paths(repo, {})
    committed = tree_ids(repo, paths)
    return worktree_ids(repo, paths) != committed or index_ids(repo, paths) != committed


def post_tool(payload: dict[str, Any], repo: Repo, client: str, *, failed: bool) -> dict[str, Any]:
    """Evaluate finished calls; restore what no approved route changed."""
    call = Call(payload, repo, client)
    with locked(repo):
        state = load_state(repo)
        if state is None:
            return {}
        inflight = cast("dict[str, dict[str, Any]]", state.setdefault("inflight", {}))
        inflight.pop(str(payload.get("tool_use_id") or ""), None)
        cast("list[dict[str, Any]]", state.setdefault("window", [])).append(call.allowance(failed=failed))
        if live_calls(state):
            save_state(repo, state)
            return {}
        problems = evaluate(repo, state)
        save_state(repo, state)
    if not problems:
        return {}
    reason = " ".join(problems)
    if failed:
        return {"hookSpecificOutput": {"hookEventName": "PostToolUseFailure", "additionalContext": reason}}
    return {"decision": "block", "reason": reason}


def evaluate(repo: Repo, state: dict[str, Any]) -> list[str]:
    """Compare the worktree with the baseline after the last running call ended."""
    window = cast("list[dict[str, Any]]", state.get("window", []))
    state["window"] = []
    if any(allowance["accept"] for allowance in window):
        refreshed = snapshot(repo, state)
        state.clear()
        state.update(refreshed)
        return []
    paths = protected_paths(repo, state)
    committed = tree_ids(repo, paths)
    # A history command that stopped half-way leaves its result for the user to settle.
    hold = repo.operation_in_progress() or bool(unmerged(repo, paths))
    hold = hold and any(allowance["files"] == "history" for allowance in window)
    problems: list[str] = []
    restored, held = check_files(repo, state, window, paths, committed, hold=hold)
    if restored:
        problems.append(RESTORED.format(paths=", ".join(restored)))
    if held:
        state["blocked"] = OPERATION_HELD.format(paths=", ".join(held))
        problems.append(str(state["blocked"]))
    removed = sorted(forbidden_files(repo) - set(state["forbidden_known"]))
    for path in removed:
        (repo.root / path).unlink()
    if removed:
        problems.append(
            f"Removed override configuration files: {', '.join(removed)}. They would replace the project's "
            "tool settings; ask the user if a setting must change."
        )
    problems += check_index(repo, state, removed, [path for path in paths if path not in held], committed)
    problems += check_head(repo, state, window, paths)
    problems += check_hooks(repo, state, window)
    problems += check_venv(repo, state, window)
    problems += check_user_files(repo, state)
    return problems


def check_files(
    repo: Repo,
    state: dict[str, Any],
    window: list[dict[str, Any]],
    paths: list[str],
    committed: dict[str, str | None],
    *,
    hold: bool,
) -> tuple[list[str], list[str]]:
    """Accept approved changes to protected files, restore the rest, or hold them mid-operation."""
    baseline = cast("dict[str, str | None]", state["files"])
    approved = cast("dict[str, list[str | None]]", state["approved"])
    current = worktree_ids(repo, paths)
    restored: list[str] = []
    held: list[str] = []
    for path in paths:
        if current[path] == baseline.get(path):
            continue
        if file_allowed(repo, path, current[path], committed[path], approved.get(path, []), window):
            blob = capture(repo, repo.root / path)
            baseline[path] = blob
            approve(approved, path, blob)
        elif hold:
            held.append(path)
        else:
            restore(repo, path, baseline.get(path))
            restored.append(path)
    return restored, held


def file_allowed(
    repo: Repo,
    path: str,
    blob: str | None,
    committed: str | None,
    history: list[str | None],
    window: list[dict[str, Any]],
) -> bool:
    """Decide whether any call in the window was allowed to make this change."""
    for allowance in window:
        if allowance["edit"] == path or allowance["files"] == "commit":
            return True
        # Codex reports no working directory; a make run elsewhere could use other rules.
        if allowance["files"] == "any" and (allowance["client"] != "codex" or not extra_makefiles(repo)):
            return True
        if allowance["files"] == "history" and (blob == committed or blob in history):
            return True
    return False


def check_index(
    repo: Repo,
    state: dict[str, Any],
    removed: list[str],
    paths: list[str],
    committed: dict[str, str | None],
) -> list[str]:
    """Unstage protected content that was never approved, and removed overrides."""
    approved = cast("dict[str, list[str | None]]", state["approved"])
    staged_unapproved = unapproved(index_ids(repo, paths), committed, approved)
    staged = set(git(repo.root, "diff", "--cached", "--name-only").splitlines())
    reset_index(repo, staged_unapproved + [path for path in removed if path in staged])
    if not staged_unapproved:
        return []
    return [f"Unstaged unapproved changes to {', '.join(staged_unapproved)}; they cannot be committed."]


def check_head(repo: Repo, state: dict[str, Any], window: list[dict[str, Any]], paths: list[str]) -> list[str]:
    """Block when new commits carry protected content no approved route produced."""
    current = head(repo)
    previous = state.get("head")
    state["head"] = current
    if current == previous or current is None or any(allowance["head"] for allowance in window):
        return []
    # Files that exist only in a commit are invisible to the worktree and index listing.
    paths = sorted(set(paths) | tree_paths(repo, current) | (tree_paths(repo, previous) if previous else set()))
    approved = cast("dict[str, list[str | None]]", state["approved"])
    before = tree_ids(repo, paths, previous) if previous else dict.fromkeys(paths)
    committed_unapproved = unapproved(tree_ids(repo, paths), before, approved)
    if not committed_unapproved:
        return []
    state["blocked"] = (
        f"A commit recorded unapproved changes to {', '.join(committed_unapproved)}. Stop and ask the user to "
        "review and undo it, or to approve `make guard-accept-changes`."
    )
    return [str(state["blocked"])]


def check_hooks(repo: Repo, state: dict[str, Any], window: list[dict[str, Any]]) -> list[str]:
    """Restore Git hook scripts and modes that no setup target installed."""
    current = hooks_state(repo)
    baseline = cast("dict[str, Any]", state["hooks"])
    if current == baseline:
        return []
    if any(allowance["hooks"] for allowance in window):
        state["hooks"] = hooks_state(repo, keep=True)
        return []
    directory = repo.common_dir / "hooks"
    scripts = cast("dict[str, str]", baseline["scripts"])
    for name in set(cast("dict[str, str]", current["scripts"])) - set(scripts):
        (directory / name).unlink()
    for name, entry in scripts.items():
        blob, _, mode = entry.partition(":")
        shutil.copyfile(repo.blob_dir / blob, directory / name)
        (directory / name).chmod(int(mode or "755", 8))
    if current["path"] != baseline["path"]:
        state["blocked"] = "core.hooksPath changed. Stop and ask the user to restore the Git hooks configuration."
        return [str(state["blocked"])]
    return ["The Git hooks were changed and have been restored; they run the project's checks."]


def check_venv(repo: Repo, state: dict[str, Any], window: list[dict[str, Any]]) -> list[str]:
    """Flag a virtual environment changed outside the project's make targets."""
    current = venv_fingerprint(repo)
    if current == state["venv"]:
        return []
    if any(allowance["venv"] for allowance in window):
        state["venv"] = current
        if any(allowance["make"] for allowance in window):
            state["venv_blocked"] = False
        return []
    state["venv_blocked"] = True
    return [VENV_DRIFT]


def check_user_files(repo: Repo, state: dict[str, Any]) -> list[str]:
    """Restore user-level Codex settings an agent changed to switch hooks off."""
    users = cast("dict[str, str | None]", state.setdefault("user", {}))
    restored: list[str] = []
    for name, path in user_files().items():
        try:
            content: bytes | None = path.read_bytes()
        except FileNotFoundError:
            content = None
        blob = repo.blob_id(content) if content is not None else None
        if blob == users.get(name):
            continue
        previous = users.get(name)
        if disables_hooks(name, content, repo.root) and previous and (repo.blob_dir / previous).is_file():
            shutil.copyfile(repo.blob_dir / previous, path)
            restored.append(str(path))
        else:
            users[name] = repo.store(content) if content is not None else None
    if not restored:
        return []
    return [f"Restored {', '.join(restored)}: agents may not switch the project's hooks off."]


def stop(payload: dict[str, Any], repo: Repo) -> dict[str, Any]:
    """Report protected state that still differs from the baseline at the end of a turn."""
    with locked(repo):
        state = load_state(repo)
        if state is None:
            return {}
        if live_calls(state):
            save_state(repo, state)
            return {}
        changed = evaluate(repo, state) if state.get("window") else []
        changed += [DRIFT.format(paths=", ".join(found))] if (found := drift(repo, state)) else []
        save_state(repo, state)
    if not changed:
        return {}
    message = " ".join(changed)
    if payload.get("stop_hook_active"):
        return {"systemMessage": message}
    return {"decision": "block", "reason": message}


def config_change(payload: dict[str, Any], repo: Repo) -> dict[str, Any]:
    """Block Claude settings changes that switch the guard off."""
    source = str(payload.get("source", ""))
    files = {
        "user_settings": Path.home() / ".claude" / "settings.json",
        "project_settings": repo.root / ".claude" / "settings.json",
        "local_settings": repo.root / ".claude" / "settings.local.json",
    }
    path = Path(str(payload.get("file_path") or files.get(source, "")))
    if not path.is_file():
        return {}
    try:
        raw: object = json.loads(path.read_text())
    except ValueError:
        return {"decision": "block", "reason": "Settings that cannot be parsed would drop the guard."}
    settings = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    if settings.get("disableAllHooks"):
        if path.resolve() != (repo.root / ".claude" / "settings.json").resolve():
            del settings["disableAllHooks"]
            path.write_text(json.dumps(settings, indent=2) + "\n")
        return {"decision": "block", "reason": "disableAllHooks would switch the protected-files guard off."}
    if source == "project_settings" and not guard_registered(settings):
        return {"decision": "block", "reason": "The project settings must keep the protected-files guard hooks."}
    return {}


def guard_registered(settings: dict[str, Any]) -> bool:
    """Whether every guard event still runs this script."""
    hooks = cast("dict[str, Any]", settings.get("hooks") or {})
    return all("guard_config.py" in json.dumps(hooks.get(event, [])) for event in CLAUDE_GUARD_EVENTS)


def pre_commit(repo: Repo) -> int:
    """Fail an agent's commit whose staged protected content was never approved."""
    with locked(repo):
        state = load_state(repo)
        if state is None:
            return 0
        running = live_calls(state)
        save_state(repo, state)
        if not running:
            return 0
        # An allowlisted target (a version bump) commits its own approved output.
        if any(entry.get("target") and not codex_make_elsewhere(repo, entry) for entry in running.values()):
            return 0
        paths = protected_paths(repo, state)
        approved = cast("dict[str, list[str | None]]", state["approved"])
        staged = unapproved(index_ids(repo, paths), tree_ids(repo, paths), approved)
        added = git(repo.root, "diff", "--cached", "--name-only", "--diff-filter=A").splitlines()
        overrides = sorted(path for path in added if is_forbidden_name(path))
    problems = staged + overrides
    if problems:
        sys.stderr.write(f"Unapproved protected changes are staged: {', '.join(problems)}.\n")
        return 1
    return 0


def codex_make_elsewhere(repo: Repo, entry: dict[str, Any]) -> bool:
    """A Codex make run may have used another directory's rules when any other Makefile exists."""
    return entry.get("client") == "codex" and bool(extra_makefiles(repo))


def accept(repo: Repo) -> int:
    """Record the current protected state as approved; the user runs or approves this."""
    with locked(repo):
        try:
            previous = load_state(repo) or {}
        except (ValueError, GuardError):
            previous = {}
        changed = drift(repo, previous) if previous else []
        state = snapshot(repo, previous)
        save_state(repo, state)
    print(f"Accepted the current protected state{': ' + ', '.join(changed) if changed else ''}.")
    return 0


def read_payload() -> dict[str, Any]:
    """Validate the protocol envelope before inspecting tool arguments."""
    raw: object = json.load(sys.stdin)
    if not isinstance(raw, dict):
        raise TypeError("hook payload must be an object")
    payload = cast("dict[str, Any]", raw)
    if "tool_input" in payload and not isinstance(payload["tool_input"], dict):
        raise TypeError("tool_input must be an object")
    return payload


def dispatch(event: str, client: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Route one hook event to its handler."""
    repo = Repo(Path(str(payload.get("cwd") or Path.cwd())))
    if event == "pre":
        return pre_tool(payload, repo, client)
    if event in {"post", "post-failure"}:
        return post_tool(payload, repo, client, failed=event == "post-failure")
    if event == "stop":
        return stop(payload, repo)
    if event == "config-change":
        return config_change(payload, repo)
    raise GuardError(f"unknown event {event}")


def main() -> int:
    """Run one hook event; input the guard cannot evaluate blocks instead of passing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("claude", "codex"), default="claude")
    parser.add_argument(
        "--event",
        choices=("pre", "post", "post-failure", "stop", "config-change", "pre-commit", "accept"),
        required=True,
    )
    arguments = parser.parse_args()
    try:
        if arguments.event in {"pre-commit", "accept"}:
            repo = Repo(Path.cwd())
            return pre_commit(repo) if arguments.event == "pre-commit" else accept(repo)
        output = dispatch(arguments.event, arguments.client, read_payload())
    except (ValueError, TypeError, OSError, GuardError) as error:
        sys.stderr.write(f"Protected-files guard cannot evaluate this event: {error}\n")
        # Blocking a Stop would re-prompt the agent every turn while the error lasts.
        return 1 if arguments.event == "stop" else 2
    if output:
        json.dump(output, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
