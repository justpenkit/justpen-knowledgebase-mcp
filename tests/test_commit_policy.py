"""Integrate project configuration with Commitizen's real checker."""

from pathlib import Path

import pytest
from commitizen.commands.check import Check
from commitizen.config import read_cfg
from commitizen.exceptions import CommitizenException

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("message", "accepted"),
    [
        ("feat: add a tool", True),
        ("fix(browser tools)!: correct lifecycle", True),
        ("docs: ölçüm örneği", True),
        ("chore: " + "a" * 65, True),
        ("chore: " + "a" * 66, False),
        ("fix: trailing period.", False),
        ("bump: 0.1.0", False),
        ("unknown: a message", False),
        ("fix(): empty scope", False),
        ("fix((nested)): scope", False),
        ("fix:subject", False),
        ("", False),
        ("fix: subject\nBody without a blank separator", True),
        ("fix: subject\n\nBody with a separator", True),
        ("Merge branch 'feature'", True),
        ('Revert "old change"', True),
        ("fixup! old subject", True),
        ("squash! old subject", True),
        ("amend! old subject", True),
        ("Mergeanything", False),
        ("Pull request anything", False),
    ],
)
def test_commitizen_checks_project_policy(message, accepted):
    config = read_cfg(str(ROOT / "pyproject.toml"))
    assert config.settings.get("name") == "cz_conventional_commits"
    assert config.settings.get("version_provider") == "pep621"
    config.settings["name"] = "cz_customize"
    check = Check(config, {"message": message})
    if accepted:
        check()
    else:
        with pytest.raises(CommitizenException):
            check()


@pytest.mark.parametrize(
    ("raw", "accepted"),
    [
        pytest.param("# Git comment\nfix: message\n", True, id="git-comment"),
        # The previous script discarded indented comments as well.
        pytest.param("  # comment\nfix: message\n", False, id="indented-comment-is-content"),
        # Commitizen trims the outside of a message; the previous script rejected
        # a leading blank line and counted the trailing space in this 73-char line.
        pytest.param("\nfix: message\n", True, id="leading-blank-normalized"),
        pytest.param("fix: " + "a" * 67 + " \n", True, id="trailing-space-normalized"),
        # The period is terminal after normalization, unlike the previous script.
        pytest.param("fix: message. \n", False, id="period-before-trailing-space"),
        pytest.param(
            "fix: message\n# ------------------------ >8 ------------------------\ndiff --git a/a b/a\n",
            True,
            id="verbose-diff-excluded",
        ),
        # The previous script ignored the scissors comment and accepted the
        # following subject; Commitizen correctly treats that content as excluded.
        pytest.param(
            "# ------------------------ >8 ------------------------\nfix: message\n",
            False,
            id="subject-after-scissors-excluded",
        ),
    ],
)
def test_commitizen_normalizes_git_message_files(tmp_path, raw, accepted):
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text(raw)
    config = read_cfg(str(ROOT / "pyproject.toml"))
    config.settings["name"] = "cz_customize"
    check = Check(config, {"commit_msg_file": str(message)})
    if accepted:
        check()
    else:
        with pytest.raises(CommitizenException):
            check()
