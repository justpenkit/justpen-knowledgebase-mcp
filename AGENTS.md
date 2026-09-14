# Development rules

These rules apply to contributors, Claude Code, and Codex. Host-specific files
must reference this policy instead of maintaining a second copy.

## Environment and commands

Install uv first. Run `make setup` once to install locked development and docs
dependencies and all Git hook stages; `make install` installs dependencies only.
uv manages Python 3.11–3.13, with 3.13 as the local default. Use the Make targets below
for routine development checks; their recipes select the tools and arguments.
Use uv directly for dependency management and running the application. Do not
install project dependencies into system Python.
The stdlib-only permission hook is the sole exception: it runs with
`/usr/bin/python3 -I -B` (Python 3.9+) independently of project dependencies.

- `make check`: lock consistency, formatting, lint, strict typing and unit tests
    once in the active interpreter. The pre-push hook runs this gate.
- `make lint-fix`, `make format`, `make typecheck`: individual development checks/fixes.
- `make test-one TEST=tests/test_file.py::test_name`: focused feedback; it does
    not replace the full `make check` gate or apply its suite-wide coverage threshold.
- `make test-permissions`: policy tests plus the real Codex sandbox probe;
    set `CODEX_TEST_BINARY` to an installed CLI first.
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

Reading `pyproject.toml` and `uv.lock` is allowed. Routine source, test, and docs
edits may proceed within the user's task without repeated confirmation.

Use `uv add`, `uv remove`, `uv lock`, `uv sync`, and `uv version` for normal
dependency/version changes. Run them as separate commands from the project root.
Do not hand-edit the lockfile.

Trusted formatting through `make format`, `make format-md`, `make format-toml`,
`make format-yaml` or `make format-json` is allowed, including TOML formatting
of `pyproject.toml`. The gate protects against direct
AI rewrites; it does not prohibit normal uv-managed changes or formatter output.

Any direct AI write to root `pyproject.toml` or `uv.lock` needs explicit user approval
for the concrete change. Prepare the exact diff first. This includes description,
build metadata, lint/type settings, and edits made through scripts or patches.
An approval covers that change; do not request it again without a scope change.
Do not substitute a Python script, symlink, shell session, or edited Makefile to
evade the gate. Never weaken checks merely to make them pass.

Codex uses `justpen-dev` filesystem permissions and **user** approval review.
Protected `apply_patch` calls are blocked by the hook: show the diff and perform
the approved change through a shell command with native sandbox escalation.
Request no persistent broad command exemption. For automatic uv escalation, use
`uv --directory /absolute/path/to/repo <action> …`, with the actual absolute root
and the directory option **before** the action. Codex's approval hook receives
the session directory, which may differ from the command's working directory;
plain uv commands therefore retain normal approval handling when they escalate.
The exact `make --directory /absolute/path/to/repo <target>` command also receives
automatic escalation for trusted formatting, where the single target is `format`,
`format-md`, `format-toml`, `format-yaml` or `format-json`. No extra targets, variable
overrides or shell chains are covered. Other escalations remain user-reviewed.
Treat hook/config changes as policy changes requiring user authorization.
See [agent setup and limitations](docs/contributing/agents.md).

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

## Optional skills and private working notes

Core development works without host plugins. Use installed skills when useful;
do not assume Claude plugins or LSP capabilities exist in Codex.

Superpowers artifacts stay in the gitignored `.superpowers/` directory:

- Specs: `.superpowers/docs/specs/YYYY-MM-DD-topic-design.md`
- Plans: `.superpowers/docs/plans/YYYY-MM-DD-topic-plan.md`

The tracked `docs/` tree is the public website, not a private planning area.
