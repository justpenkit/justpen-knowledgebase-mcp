"""Render this project's Git installation pins from canonical project metadata."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from mkdocs.config.defaults import MkDocsConfig
    from mkdocs.structure.pages import Page


def on_config(config: MkDocsConfig) -> MkDocsConfig:
    """Read version and repository beside the active MkDocs configuration."""
    metadata_path = Path(config.config_file_path).parent / "pyproject.toml"
    with metadata_path.open("rb") as metadata_file:
        project = tomllib.load(metadata_file)["project"]
    version = project["version"]
    repository = project["urls"]["Repository"]
    if not isinstance(version, str) or not version.strip():
        raise ValueError("project.version must be a non-empty string")
    if not isinstance(repository, str) or not repository.strip():
        raise ValueError("project.urls.Repository must be a non-empty string")
    config.extra["project_version"] = version
    config.extra["project_repository"] = repository.rstrip("/").removesuffix(".git")
    return config


def on_page_markdown(markdown: str, *, config: MkDocsConfig, page: Page, **_kwargs: object) -> str:
    """Update complete version tags for this exact repository in rendered pages."""
    if PurePosixPath(page.file.src_uri).name.casefold() == "changelog.md":
        return markdown
    version = cast("str", config.extra["project_version"])
    repository = re.escape(cast("str", config.extra["project_repository"]))
    # Accept release, prerelease and local-version suffixes as one token. The
    # final boundary prevents replacing only a prefix of a different Git ref.
    install_pin = (
        rf"(?<![\w+./:@%-])(?P<source>git\+{repository}(?:\.git)?@v)"
        r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?"
        r"(?:[.-][0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
        r"(?:\+[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?(?![\w.+/-])"
    )
    return re.sub(install_pin, lambda match: match["source"] + version, markdown)
