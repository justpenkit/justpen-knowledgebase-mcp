"""Exercise Copier's real three-way update with committed user changes."""

import shutil
import subprocess
import tomllib
from contextlib import chdir

import pytest
import yaml
from copier import run_copy, run_update
from plumbum import local
from scripts import bootstrap as setup

from tests.copier_helpers import git

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def foreground_copier_maintenance():
    # Copier also creates temporary Git repositories internally. Its subprocess
    # environment is separate from os.environ; keep maintenance synchronous so
    # it cannot rewrite objects during a later clone or directory cleanup.
    with local.env(
        GIT_CONFIG_COUNT="2",
        GIT_CONFIG_KEY_0="gc.autoDetach",
        GIT_CONFIG_VALUE_0="false",
        GIT_CONFIG_KEY_1="maintenance.autoDetach",
        GIT_CONFIG_VALUE_1="false",
    ):
        yield


@pytest.mark.parametrize("conflicting", [False, True])
def test_github_copy_updates_from_its_own_history_without_setup_credentials(
    template_source, tmp_path, monkeypatch, conflicting
):
    source = tmp_path / "upstream"
    shutil.copytree(template_source, source)
    original = "setting = original\n" + "unchanged context\n" * 12 + "footer = original\n"
    (source / "template/update-example.txt").write_text(original)
    git(source, "add", "-A")
    git(source, "commit", "-qm", "test: upstream template")
    project = tmp_path / "weather-mcp"
    # GitHub copies files, but creates unrelated history in the new repository.
    shutil.copytree(source, project, ignore=shutil.ignore_patterns(".git"))
    git(project, "init", "-q", "-b", "main")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "test: GitHub initial commit")
    initial = git(project, "rev-parse", "HEAD")
    assert initial != git(source, "rev-parse", "HEAD")
    # First-run setup must not look up the source repository over the network.
    original_run = setup._run

    def local_setup_only(arguments, **kwargs):
        assert arguments[:2] != ["git", "ls-remote"], "Setup must not query an upstream repository"
        return original_run(arguments, **kwargs)

    monkeypatch.setattr(setup, "_run", local_setup_only)
    monkeypatch.setattr(setup, "validate_project", lambda path: (path / "uv.lock").write_text("fixture\n"))
    assert setup.bootstrap(project, repository="acme/weather-mcp")
    answers = yaml.safe_load((project / ".copier-answers.yml").read_text())
    assert answers["_src_path"] == "."
    assert git(project, "rev-parse", answers["_commit"]) == initial
    assert not (project / "copier.yml").exists()
    user_file = project / "update-example.txt"
    user_file.write_text(
        original.replace("setting = original" if conflicting else "footer = original", "user customization")
    )
    (project / "src/weather_mcp/custom_tool.py").write_text('"""User-owned tool."""\n')
    git(project, "add", "-A")
    git(project, "commit", "-qm", "test: generated and customized")
    # Move to a fresh clone: no source path from the setup runner may be needed.
    fresh = tmp_path / "fresh"
    git(tmp_path, "clone", "--no-local", str(project), str(fresh))
    shutil.rmtree(project)
    (source / "template/update-example.txt").write_text(original.replace("setting = original", "setting = updated"))
    git(source, "add", "-A")
    git(source, "commit", "-qm", "test: template update")
    git(source, "tag", "v1.0.0")
    git(fresh, "fetch", "--no-tags", str(source), "main:refs/remotes/template/main")
    revision = git(fresh, "rev-parse", "refs/remotes/template/main")
    with chdir(fresh):
        run_update(vcs_ref=revision, defaults=True, overwrite=True, quiet=True)
    result = (fresh / "update-example.txt").read_text()
    assert "user customization" in result
    assert "setting = updated" in result
    assert (fresh / "src/weather_mcp/custom_tool.py").is_file()
    assert ("<<<<<<<" in result) is conflicting
    assert git(fresh, "tag") == ""
    answers = yaml.safe_load((fresh / ".copier-answers.yml").read_text())
    assert answers["_src_path"] == "."
    assert git(fresh, "rev-parse", answers["_commit"]) == revision
    if not conflicting:
        git(fresh, "add", "-A")
        git(fresh, "commit", "-qm", "test: first update")
        second = tmp_path / "second"
        git(tmp_path, "clone", "--no-local", str(fresh), str(second))
        shutil.rmtree(fresh)
        (source / "template/new-template-file.txt").write_text("second update\n")
        git(source, "add", "-A")
        git(source, "commit", "-qm", "test: second template update")
        git(second, "fetch", "--no-tags", str(source), "main:refs/remotes/template/main")
        with chdir(second):
            run_update(
                vcs_ref=git(second, "rev-parse", "refs/remotes/template/main"),
                defaults=True,
                overwrite=True,
                quiet=True,
            )
        assert (second / "new-template-file.txt").read_text() == "second update\n"
        assert "user customization" in (second / "update-example.txt").read_text()


@pytest.fixture
def update_project(template_source, tmp_path):
    source = tmp_path / "upstream"
    shutil.copytree(template_source, source)
    upstream_file = source / "template/update-example.txt"
    upstream_file.write_text("setting = original\n" + "unchanged context\n" * 12 + "footer = original\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "test: template v1")
    git(source, "tag", "v1.0.0")
    project = tmp_path / "weather-mcp"
    run_copy(
        str(source),
        project,
        data={"project_name": "weather-mcp", "repo_owner": "acme"},
        defaults=True,
        vcs_ref="v1.0.0",
        quiet=True,
    )
    git(project, "init", "-q", "-b", "main")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "test: generated project")
    return source, project


@pytest.mark.parametrize("conflicting", [False, True])
def test_update_preserves_changes_or_reports_real_conflict(update_project, conflicting):
    source, project = update_project
    user_file = project / "update-example.txt"
    original = user_file.read_text()
    user_file.write_text(
        original.replace("setting = original" if conflicting else "footer = original", "user customization")
    )
    (project / "src/weather_mcp/custom_tool.py").write_text('"""User-owned tool, preserved by updates."""\n')
    git(project, "add", "-A")
    git(project, "commit", "-qm", "test: user customization")
    (source / "template/update-example.txt").write_text(original.replace("setting = original", "setting = updated"))
    (source / "template/new-template-file.txt").write_text("new template feature\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "test: template v2")
    git(source, "tag", "v1.1.0")
    run_update(project, vcs_ref="v1.1.0", defaults=True, overwrite=True, quiet=True)
    result = user_file.read_text()
    assert "user customization" in result
    assert "setting = updated" in result
    assert (project / "src/weather_mcp/custom_tool.py").is_file()
    assert (project / "new-template-file.txt").read_text() == "new template feature\n"
    answers = yaml.safe_load((project / ".copier-answers.yml").read_text())
    assert answers["_src_path"] == str(source)
    assert answers["_commit"] == "v1.1.0"
    if conflicting:
        assert "<<<<<<<" in result
        assert "update-example.txt" in git(project, "diff", "--name-only", "--diff-filter=U")
        # The generated pre-commit gate rejects markers even after `git add`.
        hooks = yaml.safe_load((project / ".pre-commit-config.yaml").read_text())["repos"][0]["hooks"]
        conflict_hook = next(hook for hook in hooks if hook["id"] == "merge-conflict-check")
        git(project, "add", "update-example.txt")
        with pytest.raises(subprocess.CalledProcessError):
            git(project, *conflict_hook["entry"].split()[1:])
    else:
        assert "<<<<<<<" not in result
        assert git(project, "diff", "--name-only", "--diff-filter=U") == ""


@pytest.mark.parametrize("legacy", [False, True], ids=["new-project", "legacy-project"])
@pytest.mark.parametrize("released_version", [None, "2.4.1"], ids=["untouched", "released"])
def test_template_updates_preserve_initial_and_released_versions(template_source, tmp_path, legacy, released_version):
    source = tmp_path / "versioned-template"
    shutil.copytree(template_source, source)
    configuration_path = source / "copier.yml"
    metadata_path = source / "template/pyproject.toml.jinja"
    current_configuration = configuration_path.read_text()
    current_metadata = metadata_path.read_text()
    initial_version = "0.1.0" if legacy else "0.0.0"
    if legacy:
        # Publish a real old template without the new question. Copier itself
        # creates its answers, just as it did before initial_version existed.
        configuration = yaml.safe_load(current_configuration)
        configuration.pop("initial_version")
        configuration_path.write_text(yaml.safe_dump(configuration, sort_keys=False))
        version_expression = "version = {{ initial_version | to_json(ensure_ascii=False) }}"
        assert version_expression in current_metadata
        metadata_path.write_text(current_metadata.replace(version_expression, 'version = "0.1.0"'))
    (source / "template/version-update.txt").write_text("original template\n")
    git(source, "add", "-A")
    git(source, "commit", "-qm", "test: publish initial version template")
    git(source, "tag", "v1.0.0")
    project = tmp_path / "projects [work]" / "weather-mcp"
    run_copy(
        str(source),
        project,
        data={"project_name": "weather-mcp", "repo_owner": "acme"},
        defaults=True,
        vcs_ref="v1.0.0",
        quiet=True,
    )
    metadata = tomllib.loads((project / "pyproject.toml").read_text())
    assert metadata["project"]["version"] == initial_version
    answers = yaml.safe_load((project / ".copier-answers.yml").read_text())
    assert answers.get("initial_version") == (None if legacy else initial_version)
    git(project, "init", "-q", "-b", "main")
    git(project, "add", "-A")
    git(project, "commit", "-qm", "test: create application from tagged template")
    if released_version:
        # Only the version is relevant to this merge fixture; avoid resolving
        # the application's dependencies while simulating a uv-managed release.
        version_result = subprocess.run(
            ["uv", "version", released_version, "--frozen"],
            cwd=project,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert version_result.returncode == 0, version_result.stdout + version_result.stderr
        git(project, "add", "pyproject.toml")
        git(project, "commit", "-qm", "chore: release application")

    configuration_path.write_text(current_configuration)
    metadata_path.write_text(current_metadata)
    for revision in ("v1.1.0", "v1.2.0"):
        (source / "template/version-update.txt").write_text(f"template {revision}\n")
        git(source, "add", "-A")
        git(source, "commit", "-qm", f"test: publish template {revision}")
        git(source, "tag", revision)
        run_update(project, vcs_ref=revision, defaults=True, overwrite=True, quiet=True)
        assert git(project, "diff", "--name-only", "--diff-filter=U") == ""
        metadata = tomllib.loads((project / "pyproject.toml").read_text())
        assert metadata["project"]["version"] == (released_version or initial_version)
        answers = yaml.safe_load((project / ".copier-answers.yml").read_text())
        assert answers["initial_version"] == initial_version
        assert answers["_commit"] == revision
        assert (project / "version-update.txt").read_text() == f"template {revision}\n"
        git(project, "add", "-A")
        git(project, "commit", "-qm", f"test: apply template {revision}")
