"""Render and validate a fresh GitHub template copy before installing its files."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import chdir, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from copier import run_copy

if TYPE_CHECKING:
    from collections.abc import Generator

TEMPLATE_REPOSITORY = "justpenkit/justpen-mcp-dev-template"
SENTINEL = ".github/.template-pending"


class RecoveryError(OSError):
    """An installation backup must be retained because automatic restore failed."""


@contextmanager
def _staging_directory() -> Generator[Path, None, None]:
    staging = Path(tempfile.mkdtemp(prefix="justpen-bootstrap-"))
    preserve = False
    try:
        yield staging
    except RecoveryError:
        preserve = True
        raise
    finally:
        if not preserve:
            shutil.rmtree(staging)


def _environment() -> dict[str, str]:
    """Avoid inherited Git/uv context redirecting commands into another checkout."""
    excluded = {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_PROJECT", "UV_WORKING_DIR", "PYTHONPATH"}
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_") and key not in excluded}


def _run(arguments: list[str], *, cwd: Path | None = None) -> str:
    """Run a checked command, retaining output for diagnostics on failure."""
    print(f"+ {' '.join(arguments)}", flush=True)
    result = subprocess.run(arguments, cwd=cwd, env=_environment(), check=False, text=True, capture_output=True)
    if result.returncode:
        print(result.stdout, end="")
        print(result.stderr, end="")
        result.check_returncode()
    return result.stdout.strip()


def _git(repo: Path, *arguments: str) -> str:
    return _run(["git", *arguments], cwd=repo)


def _require_clean(repo: Path) -> None:
    entries = _git(repo, "ls-files", "-v", "-z").split("\0")
    if any(entry and (entry[0].islower() or entry[0] == "S") for entry in entries):
        raise ValueError("Template setup refuses assume-unchanged/skip-worktree index flags that can hide local edits.")
    if _git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Template setup requires a clean working tree and index; commit or move local changes first.")


def _validate_target(repo: Path, repository: str) -> tuple[str, str]:
    if repository.count("/") != 1:
        raise ValueError("Repository must be owner/project-mcp.")
    if repository.casefold() == TEMPLATE_REPOSITORY.casefold():
        raise ValueError("Refusing to transform the original template repository.")
    if Path(_git(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise ValueError("Run setup at the repository root.")
    if "origin" in _git(repo, "remote").splitlines():
        origin = _git(repo, "remote", "get-url", "origin").removesuffix(".git").rstrip("/")
        if origin.casefold().endswith((f"/{TEMPLATE_REPOSITORY}", f":{TEMPLATE_REPOSITORY}")):
            raise ValueError("Refusing to transform a checkout of the original template repository.")
    _require_clean(repo)
    owner, project = repository.split("/")
    return owner, project


def validate_project(project: Path, *, build_docs: bool = sys.version_info[:2] == (3, 13)) -> None:
    """Initialize the lock and run the same quality gates as generated-project CI."""
    _git(project, "init", "-q", "-b", "main")
    _run(["make", "install"], cwd=project)
    _run(["make", "format"], cwd=project)
    _run(["make", "check"], cwd=project)
    if build_docs:
        _run(["make", "docs-build"], cwd=project)
    _run(["uv", "build"], cwd=project)


def _check_paths(repo: Path, old: set[Path], new: set[Path]) -> None:
    for relative in old | new:
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise ValueError(f"Unsafe generated path: {relative}")
        path = repo / relative
        for candidate in (path, *path.parents):
            if candidate == repo:
                break
            if candidate.is_symlink():
                raise ValueError(f"Refusing to replace or traverse a symlink: {relative}")
        if relative in new and path.exists() and (relative not in old or not path.is_file()):
            raise ValueError(f"Generated file collision with local path: {relative}")
        if any(parent.is_file() for parent in path.parents if parent != repo):
            raise ValueError(f"Generated directory collision with local file: {relative}")


def _prune_empty(repo: Path, files: set[Path]) -> None:
    parents = {parent for relative in files for parent in relative.parents if parent != Path()}
    for relative in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        path = repo / relative
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def _check_workflows(repo: Path, generated: Path, files: set[Path]) -> None:
    """Keep first-run pushes within GITHUB_TOKEN's workflow permissions."""
    for relative in files:
        if relative.parts[:2] != (".github", "workflows"):
            continue
        original, rendered = repo / relative, generated / relative
        if (
            not original.is_file()
            or original.read_bytes() != rendered.read_bytes()
            or original.stat().st_mode & 0o111 != rendered.stat().st_mode & 0o111
        ):
            raise ValueError(
                f"Setup would create or change workflow {relative}; GITHUB_TOKEN cannot push it. "
                "Keep shared workflows identical in the generator and emitted project."
            )


def _restore(repo: Path, backup: Path, old: set[Path], files: set[Path]) -> None:
    for relative in files - old:
        (repo / relative).unlink(missing_ok=True)
    for relative in old:
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup / relative, destination)
    _prune_empty(repo, files - old)


def _install(repo: Path, generated: Path, backup: Path, files: set[Path]) -> None:
    old = {Path(name) for name in _git(repo, "ls-files", "-z").split("\0") if name}
    _check_paths(repo, old, files)
    for relative in old:
        target = backup / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / relative, target)
    try:
        for relative in sorted(files):
            destination = repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(generated / relative, destination)
        for relative in old - files:
            (repo / relative).unlink()
        _prune_empty(repo, old - files)
    except OSError:
        try:
            _restore(repo, backup, old, files)
        except OSError as error:
            raise RecoveryError(
                f"Automatic restore failed; original tracked files remain in backup: {backup}"
            ) from error
        raise


def bootstrap(
    repo: Path,
    *,
    repository: str,
    description: str | None = None,
    author: str | None = None,
) -> bool:
    """Replace a clean first-run template checkout after successful staged validation.

    Returns False if setup already ran. Leaves Git history and ignored files in
    place; I/O errors during installation restore the original tracked files.
    """
    repo = repo.resolve()
    if not (repo / SENTINEL).is_file():
        return False
    owner, project = _validate_target(repo, repository)
    original_head = _git(repo, "rev-parse", "HEAD")
    data = {
        "project_name": project,
        "repo_owner": owner,
        "description": description or f"MCP server for {project}.",
        "author": author or owner,
    }
    with _staging_directory() as staging:
        generated = staging / "project"
        # GitHub already copied the source. Record '.' and the actual local
        # revision: the template remains available in this project's history.
        with chdir(repo):
            run_copy(".", generated, data=data, vcs_ref=original_head, defaults=True, quiet=True)
        paths = list(generated.rglob("*"))
        if any(path.is_symlink() for path in paths):
            raise ValueError("Generated symlinks are not supported by first-run setup.")
        files = {path.relative_to(generated) for path in paths if path.is_file()}
        validate_project(generated)
        files.add(Path("uv.lock"))
        _check_workflows(repo, generated, files)
        _require_clean(repo)
        if _git(repo, "rev-parse", "HEAD") != original_head:
            raise ValueError("Repository HEAD changed during validation; retry setup.")
        _install(repo, generated, staging / "backup", files)
    return True


def main(argv: list[str] | None = None) -> None:
    """Run the one-time GitHub setup from the files already copied by GitHub."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--repository", default=os.environ.get("GITHUB_REPOSITORY"), required="GITHUB_REPOSITORY" not in os.environ
    )
    parser.add_argument("--description", default=os.environ.get("PROJECT_DESCRIPTION"))
    parser.add_argument("--author", default=os.environ.get("PROJECT_AUTHOR"))
    args = parser.parse_args(argv)
    changed = bootstrap(
        args.repo,
        repository=args.repository,
        description=args.description,
        author=args.author,
    )
    print(
        "Project generated and verified. Review and commit the setup changes." if changed else "Setup already complete."
    )


if __name__ == "__main__":
    main()
