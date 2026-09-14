"""Release operations use real disposable Git repositories, uv and Commitizen."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent
RELEASE_SPEC = importlib.util.spec_from_file_location("release", ROOT / "scripts/release.py")
assert RELEASE_SPEC is not None
assert RELEASE_SPEC.loader is not None
release = importlib.util.module_from_spec(RELEASE_SPEC)
RELEASE_SPEC.loader.exec_module(release)


def git(repo, *arguments):
    return subprocess.check_output(["git", *arguments], cwd=repo, text=True).strip()


def commit(repo, message):
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


def finalize_reviewed(project):
    branch = git(project, "branch", "--show-current")
    git(project, "switch", "main")
    git(project, "merge", "--no-ff", branch, "-m", "Merge reviewed release")
    git(project, "update-ref", "refs/remotes/origin/main", "HEAD")
    release.finalize(project)


@pytest.fixture
def project(tmp_path, monkeypatch):
    for key in os.environ:
        if key.startswith(("GIT_", "UV_")) or key in {"VIRTUAL_ENV", "MAKEFLAGS", "MAKEOVERRIDES", "MFLAGS"}:
            monkeypatch.delenv(key)
    monkeypatch.setenv("UV_PYTHON", sys.executable)
    monkeypatch.setenv("UV_OFFLINE", "1")
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "release-fixture"\nversion = "0.0.0"\nrequires-python = ">=3.11"\n'
        "[dependency-groups]\ndev = []\n"
        '[tool.commitizen]\nname = "cz_conventional_commits"\nversion_provider = "pep621"\ntag_format = "v$version"\n'
    )
    (repo / ".gitignore").write_text(".venv/\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Release Test")
    git(repo, "config", "user.email", "release@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "tag.gpgsign", "false")
    commit(repo, "feat: inherited template feature")
    git(repo, "tag", "-a", "v9.0.0", "-m", "template release")
    (repo / ".copier-answers.yml").write_text("project_name: release-fixture\n")
    commit(repo, "chore: initialize application")
    git(repo, "switch", "-qc", "release/first")
    (repo / "app.py").write_text('"""Application."""\n')
    commit(repo, "feat: application feature")
    return repo


def test_bump_uses_uv_changelog_and_defers_tag_until_review(project):
    release.bump(project, "patch")
    assert tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"] == "0.0.1"
    assert tomllib.loads((project / "uv.lock").read_text())["package"][0]["version"] == "0.0.1"
    changelog = (project / "CHANGELOG.md").read_text()
    assert "## v0.0.1" in changelog
    assert "application feature" in changelog
    assert "inherited template" not in changelog
    assert "v9.0.0" not in changelog
    assert "v0.0.1" not in git(project, "tag").splitlines()
    assert git(project, "log", "-1", "--format=%s") == "chore: bump version to v0.0.1"
    assert git(project, "status", "--porcelain") == ""
    assert git(project, "remote") == ""


@pytest.mark.parametrize("state", ["main", "master", "detached", "tracked", "untracked", "staged"])
def test_bump_requires_clean_feature_branch(project, state):
    if state in {"main", "master"}:
        git(project, "branch", "-m", state) if state == "master" else git(project, "switch", "main")
    elif state == "detached":
        git(project, "checkout", "--detach", "-q")
    else:
        (project / ("app.py" if state == "tracked" else "new.py")).write_text("# local edit\n")
        if state == "staged":
            git(project, "add", "new.py")
    original = (project / "pyproject.toml").read_bytes()
    with pytest.raises(ValueError, match=r"feature branch|clean"):
        release.bump(project, "patch")
    assert (project / "pyproject.toml").read_bytes() == original
    assert "v0.0.1" not in git(project, "tag").splitlines()


@pytest.mark.parametrize("hook", ["pre-commit", "commit-msg"])
def test_failed_git_hook_stops_before_tag_without_disabling_hooks(project, hook):
    hook_path = project / ".git/hooks" / hook
    hook_path.write_text("#!/bin/sh\nexit 1\n")
    hook_path.chmod(0o755)
    head = git(project, "rev-parse", "HEAD")
    with pytest.raises(subprocess.CalledProcessError):
        release.bump(project, "patch")
    assert git(project, "rev-parse", "HEAD") == head
    assert "v0.0.1" not in git(project, "tag").splitlines()
    assert (project / "CHANGELOG.md").exists()


@pytest.mark.parametrize("phase", ["version", "changelog", "format"])
def test_command_failure_stops_before_commit_and_tag(project, monkeypatch, phase):
    original_run = release._run
    head = git(project, "rev-parse", "HEAD")
    failed = False

    def fail_step(repo, *arguments):
        nonlocal failed
        matches = {
            "version": arguments == ("uv", "version", "--bump", "patch"),
            "changelog": "cz" in arguments,
            "format": "mdformat" in arguments,
        }
        if matches[phase]:
            failed = True
            raise subprocess.CalledProcessError(1, arguments)
        return original_run(repo, *arguments)

    monkeypatch.setattr(release, "_run", fail_step)
    with pytest.raises(subprocess.CalledProcessError):
        release.bump(project, "patch")
    assert failed
    assert git(project, "rev-parse", "HEAD") == head
    assert "v0.0.1" not in git(project, "tag").splitlines()


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_edits_are_not_a_clean_release(project, flag):
    git(project, "update-index", flag, "app.py")
    (project / "app.py").write_text("# hidden edit\n")
    assert git(project, "status", "--porcelain") == ""
    with pytest.raises(ValueError, match="index flags"):
        release.bump(project, "patch")
    assert "v0.0.1" not in git(project, "tag").splitlines()


def test_post_commit_edits_stop_tag_creation(project):
    hook = project / ".git/hooks/post-commit"
    hook.write_text("#!/bin/sh\nprintf '\\n# hook change\\n' >> app.py\n")
    hook.chmod(0o755)
    with pytest.raises(ValueError, match="clean"):
        release.bump(project, "patch")
    assert git(project, "log", "-1", "--format=%s") == "chore: bump version to v0.0.1"
    assert "v0.0.1" not in git(project, "tag").splitlines()


def test_existing_tag_is_rejected_before_metadata_changes(project):
    git(project, "tag", "-a", "v0.0.1", "-m", "already exists")
    original = (project / "pyproject.toml").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        release.bump(project, "patch")
    assert (project / "pyproject.toml").read_bytes() == original
    assert git(project, "status", "--porcelain") == ""


def test_standalone_changelog_preserves_version_and_scopes_history(project):
    original = (project / "pyproject.toml").read_bytes()
    release.changelog(project)
    text = (project / "CHANGELOG.md").read_text()
    assert "Unreleased" in text
    assert "application feature" in text
    assert "inherited template" not in text
    assert (project / "pyproject.toml").read_bytes() == original
    assert "v0.0.1" not in git(project, "tag").splitlines()


def test_generator_changelog_includes_its_own_history(project):
    git(project, "rm", ".copier-answers.yml")
    commit(project, "test: generator fixture without answers")
    release.changelog(project)
    text = (project / "CHANGELOG.md").read_text()
    assert "inherited template feature" in text
    assert "v9.0.0" in text


def test_notes_cli_writes_only_the_verified_section(project, monkeypatch, tmp_path):
    release.bump(project, "patch")
    finalize_reviewed(project)
    output = tmp_path / "notes.md"
    monkeypatch.chdir(project)
    release.main(["notes", "--tag", "v0.0.1", "--output", str(output)])
    assert "application feature" in output.read_text()
    assert "inherited template" not in output.read_text()


def test_consecutive_release_keeps_earlier_application_section(project):
    release.bump(project, "patch")
    finalize_reviewed(project)
    git(project, "switch", "-c", "release/second")
    (project / "app.py").write_text('"""Corrected application."""\n')
    (project / ".copier-answers.yml").write_text("project_name: release-fixture\n_commit: updated-template\n")
    commit(project, "fix: correct application feature")
    release.bump(project, "minor")
    changelog = (project / "CHANGELOG.md").read_text()
    assert "## v0.1.0" in changelog
    assert "## v0.0.1" in changelog
    assert "inherited template" not in changelog
    finalize_reviewed(project)
    notes = release.release_notes(project, "v0.1.0")
    assert "correct application feature" in notes
    assert "## v0.0.1" not in notes


@pytest.mark.parametrize("problem", ["lightweight", "unmerged", "metadata", "section"])
def test_release_notes_reject_invalid_release_identity(project, problem):
    release.bump(project, "patch")
    git(project, "tag", "-a", "v0.0.1", "-m", "fixture")
    if problem == "lightweight":
        git(project, "tag", "-d", "v0.0.1")
        git(project, "tag", "v0.0.1")
    if problem == "section":
        (project / "CHANGELOG.md").write_text("## v0.0.10\n\nWrong version.\n")
        commit(project, "test: wrong release notes")
        git(project, "tag", "-fa", "v0.0.1", "-m", "fixture")
    if problem == "metadata":
        git(project, "tag", "-a", "v0.0.2", "-m", "wrong version")
    git(project, "update-ref", "refs/remotes/origin/main", "HEAD~1" if problem == "unmerged" else "HEAD")
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        release.release_notes(project, "v0.0.2" if problem == "metadata" else "v0.0.1")


def test_release_rejects_tag_that_omits_post_bump_review_fixes(project):
    release.bump(project, "patch")
    if "v0.0.1" not in git(project, "tag").splitlines():
        git(project, "tag", "-a", "v0.0.1", "-m", "premature release")
    (project / "app.py").write_text('"""Corrected during PR review."""\n')
    commit(project, "fix: address release review")
    git(project, "switch", "main")
    git(project, "merge", "--no-ff", "release/first", "-m", "Merge reviewed release")
    git(project, "update-ref", "refs/remotes/origin/main", "HEAD")

    with pytest.raises(ValueError, match="reviewed"):
        release.release_notes(project, "v0.0.1")


def test_finalize_tags_reviewed_merge_and_preserves_future_main_changes(project):
    release.bump(project, "patch")
    (project / "app.py").write_text('"""Corrected during PR review."""\n')
    commit(project, "fix: address release review")
    git(project, "switch", "main")
    git(project, "merge", "--no-ff", "release/first", "-m", "Merge reviewed release")
    git(project, "update-ref", "refs/remotes/origin/main", "HEAD")
    reviewed = git(project, "rev-parse", "HEAD")

    release.finalize(project)

    assert git(project, "cat-file", "-t", "refs/tags/v0.0.1") == "tag"
    assert git(project, "rev-parse", "v0.0.1^{commit}") == reviewed
    assert "Corrected during PR review" in git(project, "show", "v0.0.1:app.py")
    (project / "later.py").write_text('"""Next release work."""\n')
    commit(project, "feat: later development")
    git(project, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert "application feature" in release.release_notes(project, "v0.0.1")


@pytest.mark.parametrize("state", ["branch", "behind", "dirty", "existing", "not_merge"])
def test_finalize_rejects_unreviewed_or_ambiguous_state(project, state):
    release.bump(project, "patch")
    if state != "branch":
        git(project, "switch", "main")
        git(project, "merge", "--no-ff", "release/first", "-m", "Merge reviewed release")
        git(project, "update-ref", "refs/remotes/origin/main", "HEAD")
    if state == "behind":
        git(project, "update-ref", "refs/remotes/origin/main", "HEAD~1")
    elif state == "dirty":
        (project / "new.py").write_text("# unreviewed\n")
    elif state == "existing":
        git(project, "tag", "-a", "v0.0.1", "-m", "existing release")
    elif state == "not_merge":
        (project / "new.py").write_text("# unreviewed\n")
        commit(project, "feat: unreviewed next change")
        git(project, "update-ref", "refs/remotes/origin/main", "HEAD")
    previous_tags = git(project, "show-ref", "--tags")

    with pytest.raises(ValueError):
        release.finalize(project)

    assert git(project, "show-ref", "--tags") == previous_tags


def test_historical_branch_tag_with_identical_reviewed_tree_stays_valid(project):
    release.bump(project, "patch")
    git(project, "tag", "-a", "v0.0.1", "-m", "historical release")
    git(project, "switch", "main")
    git(project, "merge", "--no-ff", "release/first", "-m", "Merge reviewed release")
    git(project, "update-ref", "refs/remotes/origin/main", "HEAD")

    assert "application feature" in release.release_notes(project, "v0.0.1")


def test_make_release_tag_finalizes_reviewed_merge(project):
    release.bump(project, "patch")
    (project / "scripts").mkdir()
    for relative in ("Makefile", "scripts/development.mk", "scripts/release.py"):
        shutil.copyfile(ROOT / relative, project / relative)
    commit(project, "fix: include reviewed release tooling")
    git(project, "switch", "main")
    git(project, "merge", "--no-ff", "release/first", "-m", "Merge reviewed release")
    git(project, "update-ref", "refs/remotes/origin/main", "HEAD")

    subprocess.run(["make", "release-tag"], cwd=project, check=True)

    assert git(project, "cat-file", "-t", "refs/tags/v0.0.1") == "tag"
    assert git(project, "rev-parse", "v0.0.1^{commit}") == git(project, "rev-parse", "HEAD")


@pytest.mark.parametrize("history", ["all", "selected"])
def test_configured_changelog_boundary_overrides_copier_default(project, history):
    (project / "boundary.txt").write_text("Application history boundary.\n")
    commit(project, "chore: choose application history boundary")
    start = "" if history == "all" else git(project, "rev-parse", "HEAD")
    metadata = project / "pyproject.toml"
    metadata.write_text(metadata.read_text() + f'changelog_start_rev = "{start}"\n')
    commit(project, "chore: configure explicit changelog history")
    (project / "app.py").write_text('"""Corrected application."""\n')
    commit(project, "fix: correct latest filtering")

    release.changelog(project)

    text = (project / "CHANGELOG.md").read_text()
    assert "correct latest filtering" in text
    assert ("application feature" in text) is (history == "all")
    assert ("inherited template feature" in text) is (history == "all")
    assert ("v9.0.0" in text) is (history == "all")


@pytest.mark.parametrize("repository_suffix", ["", ".git"])
def test_bump_commits_only_tracked_own_repository_install_pins(project, repository_suffix):
    repository = "https://github.com/acme/release-fixture"
    metadata = project / "pyproject.toml"
    metadata.write_text(metadata.read_text() + f'\n[project.urls]\nRepository = "{repository}{repository_suffix}"\n')
    (project / "docs/guides").mkdir(parents=True)
    (project / "examples").mkdir()
    ignored = project / ".gitignore"
    ignored.write_text(ignored.read_text() + "docs/private.md\n")
    versions = ("0.0.0", "0.0.9", "0.0.0rc1", "0.0.0.post1", "0.0.0+local", "0.0.0-dev")
    pins = "".join(
        f'uv add "release-fixture @ git+{repository}{suffix}@v{version}"\n'
        for version in versions
        for suffix in ("", ".git")
    )
    unrelated = (
        "git+https://github.com/another-owner/release-fixture@v0.0.0\n"
        "git+https://notgithub.com/acme/release-fixture@v0.0.0\n"
        f"git+{repository}-fork@v0.0.0\n"
        f"https://mirror.invalid/git+{repository}@v0.0.0\n"
        f"git+https://mirror.invalid@git+{repository}@v0.0.0\n"
        f"%git+{repository}@v0.0.0\n"
        f"prefixgit+{repository}@v0.0.0\n"
        f"{repository}@v0.0.0\n"
        f"git+{repository}@v0.0.0/branch\n"
        "Unrelated example version: 0.0.0\n"
    )
    document_names = ("README.md", "docs/index.md", "docs/guides/install example.md")
    for name in (*document_names, "docs/example.txt", "docs/CHANGELOG.md", "examples/README.md"):
        (project / name).write_text(pins + unrelated)
    external = project.parent / "external.md"
    external.write_text(pins)
    (project / "docs/external.md").symlink_to(external)
    commit(project, "docs: add versioned installation references")
    (project / "docs/private.md").write_text(pins)

    release.bump(project, "patch")

    expected = (
        "".join(
            f'uv add "release-fixture @ git+{repository}{suffix}@v0.0.1"\n'
            for _version in versions
            for suffix in ("", ".git")
        )
        + unrelated
    )
    for name in document_names:
        assert (project / name).read_text() == expected
        assert git(project, "show", f"HEAD:{name}") == expected.rstrip()
    for name in ("docs/example.txt", "docs/CHANGELOG.md", "examples/README.md"):
        assert (project / name).read_text() == pins + unrelated
    assert (project / "docs/private.md").read_text() == pins
    assert external.read_text() == pins
    assert (project / "docs/external.md").is_symlink()
    assert git(project, "status", "--porcelain") == ""
    assert "v0.0.1" not in git(project, "tag").splitlines()

    finalize_reviewed(project)

    for name in document_names:
        assert git(project, "show", f"v0.0.1:{name}") == expected.rstrip()
