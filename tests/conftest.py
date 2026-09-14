"""Shared isolated Git fixtures for the template's contract tests."""

import os

import pytest

from tests.copier_helpers import snapshot_template


def pytest_configure():
    # Copier's subprocess library captures the environment when imported during
    # collection. Clear hook-local Git variables before that import, not only
    # in function fixtures, so each temporary repository owns its Git context.
    for name in os.environ:
        if name.startswith("GIT_"):
            os.environ.pop(name)


@pytest.fixture(autouse=True)
def isolated_git_environment(monkeypatch):
    for name in os.environ:
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)


@pytest.fixture(scope="session")
def template_source(tmp_path_factory):
    return snapshot_template(tmp_path_factory.mktemp("copier-source") / "template-repo")
