"""Exercise the published documentation and its strict local-link gate."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastmcp import Client

from justpen_knowledgebase_mcp.app import create_app
from justpen_knowledgebase_mcp.config import ServerConfig
from justpen_knowledgebase_mcp.evidence import IngestRequest
from justpen_knowledgebase_mcp.models import SearchRequest, TypesRequest, WriteRequest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.version_info[:2] != (3, 13), reason="Documentation builds are validated on Python 3.13"),
]


@pytest.fixture
def documentation_project(tmp_path):
    shutil.copy2(ROOT / "mkdocs.yml", tmp_path / "mkdocs.yml")
    shutil.copy2(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    shutil.copytree(ROOT / "scripts", tmp_path / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "docs", tmp_path / "docs")
    if (ROOT / "src").exists():
        shutil.copytree(ROOT / "src", tmp_path / "src")
    return tmp_path


def build_documentation(project, *, cwd=None):
    return subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--config-file", str(project / "mkdocs.yml")],
        cwd=project if cwd is None else cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


def test_documentation_builds_strictly(documentation_project):
    result = build_documentation(documentation_project)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (documentation_project / "site" / "index.html").is_file()
    tools = rendered_text(documentation_project / "site" / "tools" / "index.html")
    assert "exactly 11 tools" in tools
    assert "kb_ingest_evidence" in tools


async def test_mcp_reference_covers_actual_public_tools_once(tmp_path):
    async with Client(create_app(ServerConfig(workspace_dir=tmp_path))) as client:
        actual = {tool.name for tool in await client.list_tools()}
    reference = "\n".join(path.read_text() for path in (ROOT / "docs" / "tools").glob("*.md"))
    documented = re.findall(r"^## `([a-z_]+)`$", reference, flags=re.MULTILINE)
    assert len(documented) == len(set(documented)) == 11
    assert set(documented) == actual


def test_public_json_examples_parse_and_match_request_schemas():
    sources = [
        ROOT / "docs" / "quickstart.md",
        ROOT / "docs" / "guides" / "graph.md",
        ROOT / "docs" / "guides" / "evidence-search.md",
    ]
    examples = [
        json.loads(block)
        for source in sources
        for block in re.findall(r"```json\n(.*?)\n```", source.read_text(), flags=re.DOTALL)
    ]
    discovery = next(example for example in examples if set(example) == {"kind", "type"})
    ingest = next(example for example in examples if "path" in example)
    write = next(example for example in examples if set(example) == {"nodes", "relations"})
    search = next(example for example in examples if example.get("kind") == "nodes" and "properties" in example)
    assert TypesRequest.model_validate(discovery).type == "endpoint"
    assert IngestRequest.model_validate(ingest).effective_media_type == "application/x-ndjson"
    assert WriteRequest.model_validate(write).nodes[1].properties["value"] == "api.example.com"
    assert SearchRequest.model_validate(search).properties is not None


def test_readme_install_pin_matches_release_metadata():
    readme = (ROOT / "README.md").read_text()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    repository = project["urls"]["Repository"].removesuffix(".git")
    pins = re.findall(r'git\+https://[^\s"]+@v([^\s"]+)', readme)
    assert pins == [project["version"]]
    assert f"git+{repository}.git@v{project['version']}" in readme


async def test_quickstart_client_configuration_starts_stdio_server(tmp_path):
    quickstart = (ROOT / "docs" / "quickstart.md").read_text()
    block = re.findall(r"```json\n(.*?)\n```", quickstart, flags=re.DOTALL)[0]
    block = block.replace("/absolute/path/to/justpen-knowledgebase-mcp/workspace", str(tmp_path))
    block = block.replace("/absolute/path/to/justpen-knowledgebase-mcp", str(ROOT))
    async with Client(json.loads(block), timeout=10) as client:
        result = await client.call_tool("kb_status", {})
    assert result.structured_content is not None
    assert result.structured_content["status"] == "ok"
    assert result.structured_content["data"]["deployment_scope"] == "single_workspace"


def test_site_is_mcp_focused_and_uses_canonical_publication_settings():
    configuration = (ROOT / "mkdocs.yml").read_text()
    assert "site_url: https://justpen-knowledgebase-mcp.justpenkit.justmumu.com/" in configuration
    assert "mkdocstrings" not in configuration
    assert "api.md" not in configuration
    assert not (ROOT / "docs" / "api.md").exists()
    assert not (ROOT / "docs" / "guides" / "template-updates.md").exists()
    assert (ROOT / "docs" / "contributing" / "template-updates.md").is_file()


@pytest.mark.parametrize("target", ["missing-guide.md", "index.md#missing-anchor"])
def test_documentation_rejects_broken_internal_links(documentation_project, target):
    index = documentation_project / "docs" / "index.md"
    index.write_text(index.read_text() + f"\n[Broken link]({target})\n")
    result = build_documentation(documentation_project)
    assert result.returncode != 0, result.stdout + result.stderr
    assert target in result.stdout + result.stderr


def rendered_text(path):
    parser = HTMLParser()
    parts = []

    def capture(data: str) -> None:
        parts.append(data)

    parser.handle_data = capture
    parser.feed(path.read_text())
    return "".join(parts)


@pytest.mark.parametrize("version", [None, "7.8.9rc2"])
def test_documentation_renders_own_install_pins_on_every_page(documentation_project, version):
    metadata = documentation_project / "pyproject.toml"
    project = tomllib.loads(metadata.read_text())["project"]
    repository = project["urls"]["Repository"]
    expected = project["version"] if version is None else version
    if version is not None:
        metadata.write_text(
            metadata.read_text().replace(f'version = "{project["version"]}"', f'version = "{version}"', 1)
        )
    references = [
        (f"git+{repository}@v1.2.3", f"git+{repository}@v{expected}"),
        (f"git+{repository}.git@v2.3.4-rc.1+build.7", f"git+{repository}.git@v{expected}"),
        (f"git+{repository}@v3.4.5rc2#subdirectory=package", f"git+{repository}@v{expected}#subdirectory=package"),
    ]
    sources = {}
    for source in (documentation_project / "docs").rglob("*.md"):
        if source.name.casefold() == "changelog.md":
            continue
        examples = "\n".join(f'uv add "{project["name"]} @ {before}"' for before, _ in references)
        source.write_text(source.read_text() + f"\n```text\n{examples}\n```\n")
        sources[source] = source.read_bytes()

    result = build_documentation(documentation_project, cwd=ROOT)

    assert result.returncode == 0, result.stdout + result.stderr
    for source, original in sources.items():
        relative = source.relative_to(documentation_project / "docs")
        output = relative if relative.name == "index.md" else relative.with_suffix("") / "index.md"
        text = rendered_text(documentation_project / "site" / output.with_suffix(".html"))
        for before, after in references:
            expected_command = f'uv add "{project["name"]} @ {after}"'
            original_command = f'uv add "{project["name"]} @ {before}"'
            assert expected_command in text.splitlines(), (relative, after)
            if original_command != expected_command:
                assert original_command not in text.splitlines(), (relative, before)
        assert source.read_bytes() == original


def test_documentation_uses_metadata_repository_and_preserves_other_refs(documentation_project):
    metadata = documentation_project / "pyproject.toml"
    project = tomllib.loads(metadata.read_text())["project"]
    original_repository = project["urls"]["Repository"]
    repository = "https://git.example.test/maintainer/docs-mcp"
    metadata.write_text(
        metadata.read_text().replace(f'Repository = "{original_repository}"', f'Repository = "{repository}.git/"')
    )
    unchanged = [
        f"git+{original_repository}@v1.2.3",
        "git+https://git.example.test/foreign/docs-mcp@v1.2.3",
        "git+https://git.example.test.evil/maintainer/docs-mcp@v1.2.3",
        f"git+{repository}-extra@v1.2.3",
        f"git+{repository}/child@v1.2.3",
        f"git+{repository}@main",
        f"git+{repository}@v1.2.3/docs",
        f"git+{repository}@v1.2.3_extra",
        f"git+{repository}@v1.2.3rc1/branch",
        f"prefixgit+{repository}@v1.2.3",
        f"other@git+{repository}@v1.2.3",
        f"other%git+{repository}@v1.2.3",
        f"{repository}@v1.2.3",
    ]
    own_reference = f"git+{repository}@v1.2.3"
    index = documentation_project / "docs" / "index.md"
    examples = "\n".join([own_reference, *unchanged])
    index.write_text(index.read_text() + f"\n```text\n{examples}\n```\n")
    original_source = index.read_bytes()

    result = build_documentation(documentation_project, cwd=ROOT)

    assert result.returncode == 0, result.stdout + result.stderr
    text = rendered_text(documentation_project / "site" / "index.html")
    assert f"git+{repository}@v{project['version']}" in text
    for reference in unchanged:
        assert reference in text, reference
    assert index.read_bytes() == original_source


def test_documentation_preserves_rendered_changelog_history(documentation_project):
    project = tomllib.loads((documentation_project / "pyproject.toml").read_text())["project"]
    reference = f"git+{project['urls']['Repository']}@v1.2.3"
    directory = documentation_project / "docs" / "history"
    directory.mkdir()
    changelog = directory / "CHANGELOG.md"
    changelog.write_text(f"# Changelog\n\n```text\n{reference}\n```\n")
    configuration = documentation_project / "mkdocs.yml"
    configuration.write_text(
        configuration.read_text().replace("nav:\n", "nav:\n  - Changelog: history/CHANGELOG.md\n", 1)
    )

    result = build_documentation(documentation_project)

    assert result.returncode == 0, result.stdout + result.stderr
    text = rendered_text(documentation_project / "site" / "history" / "CHANGELOG" / "index.html")
    assert reference in text


@pytest.mark.parametrize(
    ("version", "repository"),
    [("", "https://example.test/repo"), (123, "https://example.test/repo"), ("1.2.3", ""), ("1.2.3", 123)],
)
def test_documentation_rejects_invalid_version_metadata(documentation_project, version, repository):
    metadata = documentation_project / "pyproject.toml"
    metadata.write_text(f"[project]\nversion = {version!r}\n[project.urls]\nRepository = {repository!r}\n")

    result = build_documentation(documentation_project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "must be a non-empty string" in result.stdout + result.stderr
