# Template architecture

The root repository develops and tests the generator. `template/` contains the
MCP project it emits. Copier provides validation, rendering, recorded answers and
updates; `scripts/bootstrap.py` adapts it to GitHub's first-run workflow.

```text
copier.yml                  Questions, defaults and validators
template/                   Generated Python MCP project
scripts/bootstrap.py        Stage, validate and install GitHub's first-run output
scripts/development.mk      Shared Make recipes for both repositories
scripts/hooks/              Shared Claude/Codex metadata guard and Git hooks
tests/                      Rendering, bootstrap, update and policy contracts
docs/                       Public generator documentation
```

The root `pyproject.toml` depends on Copier and has no MCP package entrypoint.
The emitted `pyproject.toml` uses FastMCP, uv_build and a project-specific console
script. Ruff's entire lint and formatting policy is identical in both projects;
strict type checking and the 80% branch coverage threshold are preserved. Only
the source paths and generator-specific test configuration differ.

## Shared sources

Jinja includes reuse the root permission hooks, host settings, Git hook
configuration, Make recipes, formatting and release helpers, and common contributor
guides. Each Makefile declares its source and coverage targets, then includes
`scripts/development.mk`, including the shared `test-integration` target. The generated
getting-started guide reuses the root guide with the project's repository details.
A shared file is maintained once. Project-specific files, including Python metadata, README,
runtime code and MkDocs configuration, live under `template/`.

Included sources are parsed by Jinja. Avoid literal Jinja delimiters in shared
files or explicitly escape them. The generator does not scan and replace strings
throughout an existing repository or rewrite TOML after rendering.

## Verification

`make check` runs lock, formatting, lint, active-Python typing and unit tests.
The local pre-push hook runs this gate plus one strict docs build. Unit tests
cover isolated bootstrap decisions/recovery, permission decisions and Make
command routing. They retain the 80% branch coverage requirement.

CI runs shared format/lint/docs checks once and typing/unit tests once per
supported Python. `make test-integration` separately covers real Git/bootstrap,
Commitizen configuration, formatter/hooks,
release operations, documentation regressions and Copier render/update scenarios.
These infrastructure tests remain in emitted projects, whose CI owns their
integration run. The generator's Template CI owns the root integration suite.
The shared CI workflow uses the presence of `copier.yml` to select that owner;
it stays byte-identical across GitHub's initial setup, so its token does not
need permission to rewrite workflows.

Generation integration exercises local Copier and GitHub bootstrap on Python
3.11, 3.12 and 3.13. Each project checks its selected Python once, builds its
package, and builds docs on the applicable 3.13 path. The suite then installs
the wheel in a fresh environment using only runtime dependencies exported from
its lockfile. Outside the checkout, it verifies the installed package and
`py.typed`, starts the console script, lists tools and calls `echo` over MCP stdio.
One 3.13 GitHub-generated project additionally runs its shared integration suite;
that suite is not repeated inside all six generation cases.
The consumer check belongs to Template CI, does not add a consumer gate to
emitted projects and does not test minimum dependency versions.

Template source files retain their Jinja syntax and are excluded from root
formatting; generated output runs the real checks. Local integration execution
is only needed when developing a test or its harness: use `make test-one` for
the relevant case. The full generation matrix runs in CI.

Bootstrap requires a clean target with `.github/.template-pending`, refuses the
original template repository, preserves `.git` and ignored local files, and
rejects file collisions and symlinks. It installs only the rendered manifest plus
the uv-generated lock, leaving temporary environments and build products behind.
I/O failures during installation restore the tracked files from a temporary
backup. If restoration itself fails, it retains the backup and reports its path
for manual recovery. Index flags that can hide local changes are also rejected.
This is a first-run adapter, not an updater for established projects.

GitHub setup renders from the copied repository at its current commit without
contacting the upstream template. Copier records the relative source `.` and
that genuine local revision. The initial template remains in project history;
later updates fetch upstream commits into `refs/remotes/template/main` and use
Copier's normal three-way merge. This avoids an extra setup secret, duplicate
source snapshots, or manually rewritten answers. Regression tests cover unrelated
GitHub initial history, fresh clones, consecutive updates and merge conflicts.

Publish the first Copier-compatible release only after this migration is merged
and verified. Earlier tags lack `copier.yml`; documentation therefore specifies
an explicit source ref instead of relying on Copier's latest-tag default.

## Documentation and releases

Both projects use uv-installed MkDocs Material, Markdown pages under `docs/`,
and strict link validation. Generated projects additionally use mkdocstrings to
render their configuration, error, response and tool APIs from Python docstrings.
The root generator documents creation, updates and architecture instead.

Both MkDocs configurations load the shared `scripts/docs_version.py` hook. At
build time it reads `project.version` and `project.urls.Repository` from the
`pyproject.toml` beside the active configuration. Versioned `git+` installation
URLs for that exact repository render with the current version on documentation pages,
including prerelease pins and optional `.git` URL suffixes. Other repositories
and nonversion refs remain unchanged. Changelog pages retain their historical
pins, and the hook does not edit Markdown sources.
These rendered pins describe the checkout version; the hook does not establish
that its release tag has been published.

New projects start at `0.0.0`, independently of the generator's own version.
Copier records the initial version so later template updates preserve released
application versions. Existing Copier projects without this answer keep the
legacy `0.1.0` baseline when updating.

Commitizen generates changelogs from Conventional Commits. The release helper
uses uv for version changes and prepares a release commit for PR review. After
the PR merges, `make release-tag` annotates the reviewed merge on updated `main`.
Generated applications exclude inherited template
history from their changelogs. See the [release process](../contributing/release-process.md)
for the PR, merge, tag creation and automatic GitHub Release sequence.
