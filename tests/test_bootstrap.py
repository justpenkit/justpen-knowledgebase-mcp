"""Bootstrap decisions and file replacement with external tools isolated."""

from __future__ import annotations

import re
import shutil
import subprocess
from contextlib import nullcontext
from pathlib import Path

import pytest
from scripts import bootstrap as setup


@pytest.mark.parametrize(
    ("entries", "status", "error"),
    [
        ("H README.md\0", "", None),
        ("h README.md\0", "", "index flags"),
        ("S README.md\0", "", "index flags"),
        ("H README.md\0", " M README.md", "clean"),
        ("", "?? new.txt", "clean"),
    ],
)
def test_clean_state_policy_rejects_hidden_and_visible_changes(tmp_path, monkeypatch, entries, status, error):
    def git(repo, *arguments):
        assert repo == tmp_path
        return entries if arguments[0] == "ls-files" else status

    monkeypatch.setattr(setup, "_git", git)
    context = pytest.raises(ValueError, match=error) if error else nullcontext()
    with context:
        setup._require_clean(tmp_path)


@pytest.mark.parametrize(
    ("repository", "origin", "at_root", "error"),
    [
        ("acme/weather-mcp", "", True, None),
        ("acme/weather-mcp", "https://github.com/acme/weather-mcp.git", True, None),
        ("invalid", "", True, "owner/project"),
        (setup.TEMPLATE_REPOSITORY.upper(), "", True, "original template"),
        ("acme/weather-mcp", "", False, "repository root"),
        ("acme/weather-mcp", f"https://github.com/{setup.TEMPLATE_REPOSITORY}.git", True, "original template"),
        ("acme/weather-mcp", f"git@github.com:{setup.TEMPLATE_REPOSITORY}.git", True, "original template"),
    ],
)
def test_target_policy_uses_supplied_git_identity(tmp_path, monkeypatch, repository, origin, at_root, error):
    replies = {
        ("rev-parse", "--show-toplevel"): str(tmp_path if at_root else tmp_path.parent),
        ("remote",): "origin" if origin else "",
        ("remote", "get-url", "origin"): origin,
        ("ls-files", "-v", "-z"): "H README.md\0",
        ("status", "--porcelain", "--untracked-files=all"): "",
    }
    monkeypatch.setattr(setup, "_git", lambda repo, *arguments: replies[arguments])
    if error:
        with pytest.raises(ValueError, match=error):
            setup._validate_target(tmp_path, repository)
    else:
        assert setup._validate_target(tmp_path, repository) == ("acme", "weather-mcp")


@pytest.mark.parametrize("path", ["/outside.txt", "../outside.txt", ".git/config", "nested/.git/config"])
def test_path_policy_rejects_repository_escape(tmp_path, path):
    with pytest.raises(ValueError, match="Unsafe"):
        setup._check_paths(tmp_path, set(), {Path(path)})


@pytest.mark.parametrize("kind", ["untracked", "directory", "parent-file", "symlink"])
def test_path_policy_protects_existing_local_data(tmp_path, kind):
    destination = tmp_path / "src"
    relative = Path("src/app.py")
    if kind in {"untracked", "directory"}:
        destination.mkdir()
        if kind == "untracked":
            (destination / "app.py").write_text("personal")
        else:
            (destination / "app.py").mkdir()
    elif kind == "parent-file":
        destination.write_text("personal")
    else:
        destination.symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(ValueError, match=r"collision|symlink"):
        setup._check_paths(tmp_path, set(), {relative})


@pytest.mark.parametrize("change", ["identical", "content", "executable", "new"])
def test_workflow_policy_requires_identical_content_and_execution_mode(tmp_path, change):
    repo, generated = tmp_path / "repo", tmp_path / "generated"
    relative = Path(".github/workflows/ci.yml")
    for directory in (repo, generated):
        (directory / relative).parent.mkdir(parents=True)
        (directory / relative).write_text("original workflow\n")
        (directory / relative).chmod(0o644)
    if change == "content":
        (generated / relative).write_text("changed workflow\n")
    elif change == "executable":
        (generated / relative).chmod(0o755)
    elif change == "new":
        (repo / relative).unlink()
    context = nullcontext() if change == "identical" else pytest.raises(ValueError, match="workflow")
    with context:
        setup._check_workflows(repo, generated, {relative, Path("README.md")})


@pytest.mark.parametrize("failure", [None, "install", "restore"])
def test_file_installation_preserves_local_data_and_recovers_on_failure(tmp_path, monkeypatch, failure):
    repo, generated, backup = (tmp_path / name for name in ("repo", "generated", "backup"))
    old = {Path("README.md"), Path("obsolete/nested/old.py")}
    new = {Path("README.md"), Path("src/app.py")}
    for base, paths, content in ((repo, old, "original"), (generated, new, "generated")):
        for relative in paths:
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    (repo / ".env").write_text("personal")
    monkeypatch.setattr(setup, "_git", lambda *_: "\0".join(str(path) for path in old))
    copy = shutil.copy2

    def fail_copy(source, destination, *args, **kwargs):
        source, destination = Path(source), Path(destination)
        if failure and source == generated / "src/app.py":
            raise OSError("install failed")
        if failure == "restore" and source.is_relative_to(backup):
            raise OSError("restore failed")
        return copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", fail_copy)
    error_type, message = {
        None: (None, ""),
        "install": (OSError, "install failed"),
        "restore": (setup.RecoveryError, re.escape(str(backup))),
    }[failure]
    context = pytest.raises(error_type, match=message) if error_type else nullcontext()
    with context:
        setup._install(repo, generated, backup, new)
    assert (repo / ".env").read_text() == "personal"
    assert all((backup / relative).read_text() == "original" for relative in old)
    if failure == "install":
        assert all((repo / relative).read_text() == "original" for relative in old)
        assert not (repo / "src").exists()
    elif not failure:
        assert all((repo / relative).read_text() == "generated" for relative in new)
        assert not (repo / "obsolete").exists()


@pytest.mark.parametrize("outcome", ["success", "already-complete", "validation-failure", "head-changed", "symlink"])
def test_bootstrap_installs_only_validated_unchanged_output(tmp_path, monkeypatch, outcome):
    sentinel = tmp_path / setup.SENTINEL
    sentinel.parent.mkdir()
    if outcome != "already-complete":
        sentinel.touch()
    monkeypatch.setattr(setup, "_validate_target", lambda *_: ("acme", "weather-mcp"))
    heads = iter(["original", "changed" if outcome == "head-changed" else "original"])
    monkeypatch.setattr(setup, "_git", lambda *_: next(heads))
    monkeypatch.setattr(setup, "_require_clean", lambda *_: None)
    installed = []
    validated = []

    def render(source, destination, **kwargs):
        assert source == "."
        assert kwargs["vcs_ref"] == "original"
        assert kwargs["data"] == {
            "project_name": "weather-mcp",
            "repo_owner": "acme",
            "description": "MCP server for weather-mcp.",
            "author": "acme",
        }
        destination.mkdir()
        (destination / "README.md").write_text("rendered")
        if outcome == "symlink":
            (destination / "alias").symlink_to("README.md")

    def validate(destination):
        if outcome == "validation-failure":
            raise RuntimeError("validation failed")
        validated.append(destination)
        (destination / "uv.lock").write_text("locked")

    def install(repo, generated, backup, files):
        assert validated == [generated]
        assert files == {Path("README.md"), Path("uv.lock")}
        installed.append(repo)

    monkeypatch.setattr(setup, "run_copy", render)
    monkeypatch.setattr(setup, "validate_project", validate)
    monkeypatch.setattr(setup, "_install", install)
    if outcome in {"success", "already-complete"}:
        assert setup.bootstrap(tmp_path, repository="acme/weather-mcp") is (outcome == "success")
    else:
        with pytest.raises((ValueError, RuntimeError), match=r"symlink|validation failed|HEAD changed"):
            setup.bootstrap(tmp_path, repository="acme/weather-mcp")
    assert installed == ([tmp_path] if outcome == "success" else [])


@pytest.mark.parametrize("changed", [True, False])
def test_cli_passes_explicit_answers_without_running_setup(tmp_path, monkeypatch, capsys, changed):
    calls = []

    def bootstrap(repo, **answers):
        calls.append((repo, answers))
        return changed

    monkeypatch.setattr(setup, "bootstrap", bootstrap)
    setup.main(
        [
            "--repo",
            str(tmp_path),
            "--repository",
            "acme/weather-mcp",
            "--description",
            "Weather tools",
            "--author",
            "Weather Team",
        ]
    )
    assert calls == [
        (
            tmp_path,
            {
                "repository": "acme/weather-mcp",
                "description": "Weather tools",
                "author": "Weather Team",
            },
        )
    ]
    assert ("generated and verified" if changed else "already complete") in capsys.readouterr().out


@pytest.mark.parametrize("returncode", [0, 1])
def test_command_adapter_keeps_failure_diagnostics(tmp_path, monkeypatch, capsys, returncode):
    def run(arguments, **options):
        assert options["cwd"] == tmp_path
        assert options["check"] is False
        assert "GIT_DIR" not in options["env"]
        return subprocess.CompletedProcess(arguments, returncode, "stdout\n", "stderr\n")

    monkeypatch.setenv("GIT_DIR", "outside")
    monkeypatch.setattr(subprocess, "run", run)
    if returncode:
        with pytest.raises(subprocess.CalledProcessError):
            setup._run(["tool"], cwd=tmp_path)
        assert "stderr" in capsys.readouterr().out
    else:
        assert setup._run(["tool"], cwd=tmp_path) == "stdout"


def test_failed_recovery_keeps_staged_backup_available():
    directories = []

    def fail_recovery():
        with setup._staging_directory() as staging:
            directories.append(staging)
            (staging / "backup").mkdir()
            raise setup.RecoveryError("restore failed")

    try:
        with pytest.raises(setup.RecoveryError):
            fail_recovery()
        assert len(directories) == 1
        assert (directories[0] / "backup").is_dir()
    finally:
        for staging in directories:
            shutil.rmtree(staging)


@pytest.mark.parametrize("build_docs", [True, False])
def test_validation_uses_make_without_node(tmp_path, monkeypatch, build_docs):
    target = tmp_path
    commands = []

    def record(arguments, *, cwd=None):
        assert cwd == target
        commands.append(arguments)
        return ""

    monkeypatch.setattr(setup, "_run", record)
    setup.validate_project(target, build_docs=build_docs)
    expected = [
        ["git", "init", "-q", "-b", "main"],
        ["make", "install"],
        ["make", "format"],
        ["make", "check"],
    ]
    if build_docs:
        expected.append(["make", "docs-build"])
    expected.append(["uv", "build"])
    assert commands == expected


def test_nested_environment_keeps_python_selection_without_other_project_context(monkeypatch):
    monkeypatch.setenv("UV_PYTHON", "3.11")
    for name in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PROJECT", "UV_WORKING_DIR", "PYTHONPATH", "GIT_DIR"):
        monkeypatch.setenv(name, "outer-project")
    environment = setup._environment()
    assert environment["UV_PYTHON"] == "3.11"
    assert all(
        name not in environment
        for name in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PROJECT", "UV_WORKING_DIR", "PYTHONPATH", "GIT_DIR")
    )


@pytest.mark.parametrize("fails", [True, False])
def test_staging_directory_is_removed_after_ordinary_exit(fails):
    directories = []
    context = pytest.raises(ValueError, match="validation failed") if fails else nullcontext()
    with context, setup._staging_directory() as staging:
        directories.append(staging)
        assert staging.is_dir()
        if fails:
            raise ValueError("validation failed")
    assert len(directories) == 1
    assert not directories[0].exists()
