# Development rules

These rules apply to contributors, Claude Code, and Codex. Host-specific files
must reference this policy instead of maintaining a second copy.

## Environment and commands

Install uv first. Run `make setup` once to install locked development and docs
dependencies and all Git hook stages; `make install` installs dependencies only.
uv manages Python 3.11–3.13, with 3.13 as the local default. Use the Make targets below
for routine development checks; their recipes select the tools and arguments.
Change dependencies through the `uv-*` Make targets below and run the
application with uv. Do not install project dependencies into system Python.
The stdlib-only protected-files guard is the sole exception: it runs with
`/usr/bin/python3 -I -B` (Python 3.9+) independently of project dependencies.

- `make check`: lock consistency, formatting, lint, strict typing and unit tests
    once in the active interpreter. The pre-push hook runs this gate.
- `make lint-fix`, `make format`, `make typecheck`: individual development checks/fixes.
- `make uv-add PKG=<spec> [GROUP=<group>]`, `make uv-remove PKG=<name> [GROUP=<group>]`,
    `make uv-upgrade [PKG=<name>]`: change dependencies and the lock.
- `make uv-lock`: relock and sync after a user-approved `pyproject.toml` edit.
- `make uv-reinstall`: delete `.venv` and rebuild it from `uv.lock`.
- `make test-one TEST=tests/test_file.py::test_name`: focused feedback; it does
    not replace the full `make check` gate or apply its suite-wide coverage threshold.
- `make test-permissions`: agent policy tests plus the real Codex execpolicy
    checks; set `CODEX_TEST_BINARY` to an installed CLI first.
- `make docs-build`: build MkDocs with strict link and anchor checks.
- `make test-integration`: real tool, release, documentation and, in the generator,
    Copier scenarios. CI runs these; local execution is only needed when developing
    an integration test or its harness. Select the relevant scenario with
    `make test-one`, rather than rerunning the entire generation matrix.

Classify tests by their scope and real component interactions, not their runtime.
A short integration test still belongs in the integration suite.

Git hooks own routine verification: pre-commit formats and lints changed file
types, checks Python changes with the active interpreter, and checks metadata
lock consistency. Commit-msg uses Commitizen. Pre-push runs `make check` and
`make docs-build`, excluding integration tests. CI tests Python 3.11–3.13.
Do not repeat a passing hook gate manually before a PR or after each edit.
Use focused checks when developing or diagnosing a change.

Do not replace these targets with ad hoc pytest/ruff/pyright commands, alternate
configs or flags that weaken checks. If a routine action lacks a Make target,
propose adding one. Tool invocations inside the recipes and test harnesses are
implementation details, not alternate agent workflows.

## Protected configuration and approvals

Neither agent runs in an OS sandbox. A guard hook (`scripts/hooks/guard_config.py`)
runs around every Claude Code and Codex tool call and protects the files that
define this project's rules and tooling: `pyproject.toml`, `uv.lock`, `Makefile`,
`scripts/development.mk`, `scripts/hooks/`, the agent settings (`.claude/settings.json`,
`.codex/`), `.pre-commit-config.yaml`, the scripts the Make targets run
(`scripts/format_files.py`, `release.py`, `install_taplo.py`, `docs_version.py`),
`mkdocs.yml`, `.taplo.toml`, `.mdformat.toml`, `.python-version`, `AGENTS.md`
and `CLAUDE.md`. It also forbids new files that override tool settings, such as
`ruff.toml`, `pytest.ini`, `pyrightconfig.json`, `.coveragerc`, `uv.toml`,
`GNUmakefile` or a nested `pyproject.toml`.

Reading protected files is always allowed. Routine source, test and docs edits
proceed within the user's task without repeated confirmation.

Agents do not write protected files directly. After each call the guard restores
any change no approved route made, deletes new override files and unstages
unapproved staged content, then says why. The approved routes are:

- the `uv-*` targets for dependencies, the `format*` and `lint-fix` targets,
    `install`, `setup` and `install-taplo`, run as the exact plain command
    `make <target> [VAR=value]` from the project root and in the foreground;
- a plain standalone `git commit`, so pre-commit's formatting is kept. The guard
    refuses a commit chained with other commands or built with `$(...)`; write
    the message to a file and use `git commit -F <file>`;
- plain `git switch`, `checkout`, `pull`, `merge`, `rebase`, `reset`, `restore`,
    `stash`, `cherry-pick` and `gh pr checkout`, which ask the user first;
- a Claude Code Edit or Write on a protected file, which asks the user first.
    Prepare the exact diff before asking; an approval covers that change. Codex
    cannot make approved edits: ask the user to make the change.

The guard refuses commands that switch checks off: `--no-verify`, `git commit -n`,
`SKIP=`, `core.hooksPath`, `GIT_CONFIG_*`, `PYTEST_ADDOPTS`, `COVERAGE_RCFILE`,
`MAKEFLAGS`, `MAKEFILES`, `make -f/-C/-e/--directory`, `pre-commit uninstall`,
running the guard or touching its baseline directly, and `guard-accept-changes`,
`bump-*` or `release-tag` combined with other goals or variables. When a merge,
rebase or cherry-pick leaves protected files conflicted, the guard leaves them
for the user to resolve. Calls that may change
protected files run alone; when the guard says another call is running, retry
after it finishes.

When the guard reports that protected state changed outside an agent call, stop
and ask the user whether they made the change. If they want to keep it, they
approve `make guard-accept-changes`; never run it to get past a refusal on your
own. When it reports a changed `.venv`, run `make uv-reinstall`.

Do not substitute a script, symlink, chained shell command or edited Make recipe
to get around the guard, and never weaken checks merely to make them pass. Hook,
permission and guard changes are policy changes that need user authorization.
The guard is a guardrail against mistakes, not a security boundary; see
[agent setup and limitations](docs/contributing/agents.md) for how it works and
what it does not cover.

## Quality and code navigation

Use focused regression tests for behavior changes. Fix root causes; try ruff's
safe fixes before manual changes. Use focused tests while developing; commit and
push hooks provide the routine gates. Full details, including the suppression protocol, live in
[Lint & typing](docs/contributing/lint-typing.md).

Prefer LSP for definitions, references, and renames when the current host exposes
it. Otherwise use `rg` and inspect the relevant files and call sites; the commit
hook runs `make typecheck` for Python changes.
Never claim an LSP check ran when the tool is unavailable. Before changing a
signature or renaming a symbol, find and inspect its usages. Host diagnostic
errors are blockers when provided; explicit checks work on both hosts.

## Git and releases

Every change ships through a feature branch and PR. Never commit or push directly
to `main`. Use `codex/short-description` for Codex branches; otherwise use
`type/short-description`. Commits follow Conventional Commits, with subjects at
most 72 characters. Do not bypass Git hooks with `--no-verify`.

Before a PR, follow the [checklist](docs/contributing/pr-checklist.md)
and ensure the pre-push gates passed; no duplicate manual run is required. Merge with a regular merge commit,
never squash. Version bumps also use a PR. `make bump-{patch,minor,major}` prepares
the release commit without a tag. After review and merge, update main and run
`make release-tag` to annotate the reviewed merge; then push that tag. The whole release
command is not an automatic dependency-management exemption. Follow the
[release process](docs/contributing/release-process.md).

Publishing is the user's decision each time. Ask before every `git push`, opening
a PR, merging, `make bump-*`, `make release-tag` and pushing a tag; an earlier
approval covers only the PR or release it named, and a skill that publishes does
not change this. Every PR and every push to an open PR runs the full CI and
spends the organization's Actions minutes. Commit on the feature branch, report
what is ready, and let the user choose between more development and publishing;
batch related changes into one PR. Claude Code enforces this through its
permission rules, and Codex through `.codex/rules/publishing.rules`, which
prompts before these commands and `make guard-accept-changes` in a trusted
project. Codex matches only the plain forms, so never publish with prefixes such
as `git -C <dir> push`.

## Optional skills and private working notes

Both Claude Code and Codex enable Compound Engineering from project settings;
the pyright LSP plugin is Claude-only. Core development works without either
plugin. Use installed skills when useful.

Compound Engineering writes its artifacts under
`.compound-engineering/artifacts/`. The
`.compound-engineering/artifacts/solutions/` tree holds documented solutions to
past problems (bugs, best practices, workflow patterns), organized by category
with YAML frontmatter (`module`, `tags`, `problem_type`); it is worth searching
when working in an area it covers.

After a solved, verified problem, offer once to invoke the `ce-compound` skill at the completion checkpoint only when the work produced durable project reasoning that is not readily recoverable from the final code, tests, types, comments, or existing documentation, and losing it would plausibly cause recurrence, material risk, or substantial rediscovery. Apply this counterfactual: if the learning document disappeared, would a future engineer reading the final implementation still be likely to repeat the mistake or redo substantial investigation? If not, do not offer. Completion, effort, and diff size alone are not enough. Offer at the checkpoint so a qualifying learning can ship in the PR that produced it, and only where the repository treats captured learnings as tracked, committed knowledge.

Write every report, summary, or handoff to the user through the `ce-noslop` skill. This applies when you are the top-level agent writing to the user, not when you are a subagent reporting to its caller. Do not apply it to code, config, verbatim quotes, or text the user asked to post as written.

Private working notes and benchmark output stay in the gitignored `.private/`
directory.

The tracked `docs/` tree is the public website, not a private planning area.
