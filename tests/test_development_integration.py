"""Exercise real commit hooks and formatters on disposable projects."""

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from markdown import markdown

from .test_development_workflow import create_make_project

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.integration


@pytest.fixture
def make_project(tmp_path):
    return create_make_project(tmp_path)


def test_commit_message_hook_works_without_global_python(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    shutil.copyfile(ROOT / ".pre-commit-config.yaml", project / ".pre-commit-config.yaml")
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(ROOT / name, project / name)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ("uv", "git", "bash", "dirname"):
        executable = shutil.which(name)
        assert executable is not None, f"Required test executable: {name}"
        (binaries / name).symlink_to(executable)
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "UV_")) and key != "VIRTUAL_ENV"
    }
    # Reuse the installed dev tools; package installation has separate consumer
    # coverage. The hook itself must still locate Commitizen through uv.
    environment.update(PATH=str(binaries), UV_PYTHON=sys.executable, UV_PROJECT_ENVIRONMENT=sys.prefix, UV_NO_SYNC="1")
    assert shutil.which("python", path=environment["PATH"]) is None
    subprocess.run(["git", "init", "-q"], cwd=project, env=environment, check=True)
    subprocess.run(["git", "add", "."], cwd=project, env=environment, check=True)
    subprocess.run(
        [sys.executable, "-m", "pre_commit", "install", "--hook-type", "commit-msg"],
        cwd=project,
        env=environment,
        check=True,
    )
    for message, accepted in (
        ("chore: initialize project", True),
        ("invalid message", False),
        ("fix: trailing dot.", False),
    ):
        result = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Hook Test",
                "-c",
                "user.email=hook-test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--allow-empty",
                "-m",
                message,
            ],
            cwd=project,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert (result.returncode == 0) is accepted, result.stdout + result.stderr
        assert "Conventional Commits (Commitizen)" in result.stdout + result.stderr
        if not accepted:
            assert "commit validation: failed" in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("README.md", "# Heading\n\nhello    \n"),
        ("settings.yml", 'answer:   "yes"\n'),
        ("page.html", "<html><body><h1>Heading</h1><p>hello</p></body></html>\n"),
        ("style.css", "body{color:red;}\n"),
    ],
)
def test_non_python_change_runs_format_hook_and_preserves_metadata_values(make_project, filename, content):
    project, environment = make_project
    for name in (".pre-commit-config.yaml", ".mdformat.toml", ".taplo.toml", ".gitignore"):
        shutil.copyfile(ROOT / name, project / name)
    shutil.copyfile(ROOT / "scripts/format_files.py", project / "scripts/format_files.py")
    # Trusted formatting may rewrite whitespace in metadata, never its values.
    metadata = project / "pyproject.toml"
    metadata.write_text('[project]\nversion="0.1.0"\n[tool.ruff.lint]\nselect=["F","E"]\n')
    expected = tomllib.loads(metadata.read_text())
    (project / filename).write_text(content)
    subprocess.run(["git", "init", "-q"], cwd=project, env=environment, check=True)
    subprocess.run(["git", "add", filename, "pyproject.toml"], cwd=project, env=environment, check=True)
    result = subprocess.run(
        [sys.executable, "-m", "pre_commit", "run", "format", "--files", filename],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert "files were modified by this hook" in result.stdout, result.stdout + result.stderr
    assert (project / filename).read_text() != content
    assert 'version = "0.1.0"' in metadata.read_text()
    assert tomllib.loads(metadata.read_text()) == expected


def test_formatter_handles_spaces_and_skips_private_files(make_project):
    project, environment = make_project
    shutil.copyfile(ROOT / "scripts/format_files.py", project / "scripts/format_files.py")
    shutil.copyfile(ROOT / ".gitignore", project / ".gitignore")
    shutil.copyfile(ROOT / ".mdformat.toml", project / ".mdformat.toml")
    (project / "space name.md").write_text("# Heading\n\nhello    \n")
    (project / ".superpowers").mkdir()
    private = project / ".superpowers/private.md"
    private.write_text("# private    \n")
    subprocess.run(["git", "init", "-q"], cwd=project, env=environment, check=True)
    check = subprocess.run(
        ["make", "-s", "format-md-check"], cwd=project, env=environment, capture_output=True, check=False
    )
    assert check.returncode != 0
    assert (project / "space name.md").read_text().endswith("hello    \n")
    subprocess.run(["make", "-s", "format-md"], cwd=project, env=environment, check=True)
    assert (project / "space name.md").read_text().endswith("hello\n")
    assert private.read_text() == "# private    \n"


def test_markdown_formatter_preserves_mkdocs_admonition_content(make_project):
    """Formatting must retain the rendered structure of MkDocs callouts."""
    project, environment = make_project
    shutil.copyfile(ROOT / "scripts/format_files.py", project / "scripts/format_files.py")
    shutil.copyfile(ROOT / ".mdformat.toml", project / ".mdformat.toml")
    content = (
        '# Guide\n\n!!! note "Development"\n\n'
        "    **Keep the checks enabled.**\n\n"
        "    - Run `make check`.\n"
        "    - Run `make docs-build`.\n"
    )
    page = project / "guide.md"
    page.write_text(content)
    expected = markdown(content, extensions=["admonition"])
    assert '<div class="admonition note">' in expected
    subprocess.run(["git", "init", "-q"], cwd=project, env=environment, check=True)
    subprocess.run(["make", "-s", "format-md"], cwd=project, env=environment, check=True)
    assert markdown(page.read_text(), extensions=["admonition"]) == expected
    formatted = page.read_bytes()
    subprocess.run(["make", "-s", "format-md-check"], cwd=project, env=environment, check=True)
    assert page.read_bytes() == formatted


@pytest.mark.parametrize(
    ("kind", "content"),
    [
        (
            "html",
            "<html><head><style>body{color:red;}</style></head><body><h1>Heading</h1>"
            "<script>const x={a:1};</script></body></html>\n",
        ),
        ("css", "body{color:red;}\n"),
    ],
)
def test_asset_formatters_check_then_rewrite_without_node(make_project, tmp_path, kind, content):
    project, environment = make_project
    binaries = Path(environment["PATH"].split(os.pathsep)[0])
    for name in ("make", "git"):
        executable = shutil.which(name)
        assert executable is not None
        (binaries / name).symlink_to(executable)
    environment["PATH"] = os.pathsep.join((str(binaries), str(Path(sys.executable).parent)))
    if shutil.which("taplo", path=environment["PATH"]) is None:
        pytest.skip("taplo is not a dev dependency on linux aarch64; see pyproject.toml")
    assert shutil.which("node", path=environment["PATH"]) is None
    assert shutil.which("npm", path=environment["PATH"]) is None
    shutil.copyfile(ROOT / "scripts/format_files.py", project / "scripts/format_files.py")
    shutil.copyfile(ROOT / ".gitignore", project / ".gitignore")
    asset = project / f"space name.{kind}"
    asset.write_text(content)
    excluded = [project / folder / f"untouched.{kind}" for folder in (".superpowers", "site", "template")]
    excluded.append(tmp_path / f"linked.{kind}")
    for path in excluded:
        path.parent.mkdir(exist_ok=True)
        path.write_text(content)
    (project / f"symlink.{kind}").symlink_to(excluded[-1])
    subprocess.run(["git", "init", "-q"], cwd=project, env=environment, check=True)
    # Make the unrelated TOML fixture compliant so it cannot cause a false
    # positive when testing the aggregate formatting gate below.
    subprocess.run(["make", "-s", "format-toml"], cwd=project, env=environment, check=True)
    for target in (f"format-{kind}-check", "format-check"):
        check = subprocess.run(
            ["make", "-s", target], cwd=project, env=environment, capture_output=True, text=True, check=False
        )
        assert check.returncode != 0
        assert asset.name in check.stdout + check.stderr
        assert asset.read_text() == content
    formatted = subprocess.run(
        ["make", "-s", "format"], cwd=project, env=environment, capture_output=True, text=True, check=False
    )
    assert formatted.returncode == 0, formatted.stdout + formatted.stderr
    assert asset.read_text() != content
    assert "color: red;" in asset.read_text()
    if kind == "html":
        assert "const x = {" in asset.read_text()
        lines = asset.read_text().splitlines()
        for parent, child in (("body {", "color: red;"), ("const x = {", "a: 1")):
            parent_line = next(line for line in lines if line.strip() == parent)
            child_line = next(line for line in lines if line.strip() == child)
            assert child_line.index(child) - parent_line.index(parent) == 2
    for target in (f"format-{kind}", f"format-{kind}-check", "format-check"):
        subprocess.run(["make", "-s", target], cwd=project, env=environment, check=True)
    assert all(path.read_text() == content for path in excluded)
