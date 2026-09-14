# Getting started

If you're contributing to this project for the first time, here's the minimum
to get up and running.

## Prerequisites

Install [uv](https://docs.astral.sh/uv/), Git and Make on macOS, Linux or WSL.
uv installs the selected Python interpreter and project tools; you do not need
to install Node.js, npm or a separate documentation toolchain. Pyright's Python
package manages its own runtime.

## Clone and set up

```bash
git clone https://github.com/justpenkit/justpen-mcp-dev-template.git
cd justpen-mcp-dev-template
make setup
```

`make setup` completes the initial setup:

- `make install` installs the locked development and documentation dependencies
    into `.venv/`. A freshly generated project initializes its missing `uv.lock`
    through uv first.
- The project formatters normalize generated Markdown, TOML, YAML and JSON.
- Git hooks are installed for pre-commit, pre-push and commit-msg.

Use `make install` when you only need to refresh dependencies, as CI does.
Commit `uv.lock` and `.copier-answers.yml` in generated projects.

## Finish a new project's setup

If you just created an MCP project from this template:

- Replace the example tools with your server's behavior.
- Customize the README, docs landing page, logo and colors.
- Add the `mcp` and `python` repository topics and enable **Discussions** under
    Settings → Features; the issue-template contact link points to Discussions.
- If you publish the docs, set `site_url` in `mkdocs.yml` to their public URL and
    expand `nav` as you add pages.
- Enable regular merge commits and automatic branch deletion. Configure branch
    protection when your repository visibility and GitHub plan support it; it is
    not a prerequisite for local development.
- Keep `main` as the default branch; CI push triggers and the release workflow use it.
- Keep Actions enabled. GitHub's automated setup commit does not trigger another
    CI run; the setup workflow validates the project before creating that commit.
    Normal CI runs on pushes to `main` and on pull requests.

Keep `.copier-answers.yml` committed so you can apply future template updates.
Activate the agent setup below if you use Claude Code or Codex. The installed
hooks provide the development gates when you commit and push.

## The development gate

Git hooks run the routine checks automatically:

| Stage      | Checks                                                                                                                                           |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| Pre-commit | Conflict/whitespace checks; lint and active-Python typing for Python changes; formatting for text changes; lock consistency for metadata changes |
| Commit-msg | Commitizen validates the project commit-message rules                                                                                            |
| Pre-push   | `make check` and one strict `make docs-build`                                                                                                    |

`make check` covers lock consistency, all supported formatters, lint, strict
typing and unit tests with 80% branch coverage. The local interpreter defaults
to Python 3.13; typing and tests each use the active uv Python once.
There is no additional manual gate before starting development or opening a PR.
A passing pre-push hook already supplies that verification.

CI runs shared formatting, lint and the docs build once on Python 3.13. Its
matrix checks typing and unit tests once per Python 3.11, 3.12 and 3.13.
It rejects a missing or stale committed lock before installing dependencies.
Initial project setup may create a new lockfile.

Infrastructure tests remain in generated projects. Real hook, formatter,
release and documentation scenarios run through `make test-integration` in CI.
The generator's Template CI additionally tests Copier generation and updates,
including both setup routes on all three Python versions. These tests are
excluded from local commit/push gates.

Use `make test-one TEST=tests/test_file.py::test_name` for focused development.
When changing an integration test or its harness, run that relevant scenario
locally through the same target; the full generation matrix remains CI's job.
Focused runs do not apply the suite-wide coverage threshold; pre-push does.

## Editor tests

VS Code's default **Run Test Task** invokes `make check`. The Python Test
Explorer's **Run All Tests** button runs pytest's unit suite with
`-m "not integration"`; it does not invoke Make or run integration scenarios.
Formatting on save remains enabled.

## Documentation

`make docs-serve` starts the local MkDocs preview. `make docs-build` builds the site
into `site/` in strict mode. All pages use normal Markdown under `docs/`, and
navigation lives in `mkdocs.yml`. Generated MCP projects also render their
Python API reference from docstrings through mkdocstrings.

## Use a coding agent

Claude Code and Codex share the rules in root `AGENTS.md`. Follow the
[agent setup guide](agents.md) to activate project permissions and the protected
metadata gate before development.

## Make a change

1. Open a feature branch: `git switch -c type/short-description`
    (e.g. `feat/foo-bar`), or `codex/short-description` for Codex work.
2. Implement the change with a focused failing test, a minimal fix, and
    verification before committing.
3. Walk the [Pre-PR checklist](pr-checklist.md) before opening the PR.
4. Use Conventional Commits for every commit subject
    (`type(scope): subject`, ≤72 chars).

For lint and type-check rules, including the suppression protocol, see
[Lint & typing](lint-typing.md).
