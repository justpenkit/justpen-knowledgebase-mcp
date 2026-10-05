# Claude Code and Codex

Both agents work on the same Python project and run the same checks. There is no
separate generated source tree for each agent.

## Files and responsibilities

| File                            | Responsibility                                                      |
| ------------------------------- | ------------------------------------------------------------------- |
| `AGENTS.md`                     | Shared environment, quality, navigation, Git, and approval rules    |
| `CLAUDE.md`                     | Imports `AGENTS.md` for Claude                                      |
| `.claude/`                      | Claude permissions, guard hooks, and plugin preferences             |
| `.codex/config.toml`            | Codex approval routing and plugin preferences                       |
| `.codex/hooks.json`             | Codex hook registration                                             |
| `.codex/rules/`                 | Codex prompt rules for publishing commands                          |
| `scripts/hooks/guard_config.py` | Protected-files guard for both hosts, one stdlib-only Python script |
| `.compound-engineering/`        | Compound Engineering settings and committed artifacts               |
| `compound-packs/`               | Project rules that Compound Engineering cites                       |

GitHub's **Use this template** workflow keeps all these files. It still renames
the package, personalizes the repository, and removes its own setup machinery.
Tests for the agent policy remain in the generated project.

## First setup

Run `make setup` in a terminal before starting either agent. The development
workflow supports macOS, Linux, and WSL with uv, Git and Make. uv manages Python
3.11–3.13 (3.13 by default), development tools and MkDocs; no separate Node/npm
installation is required. Pyright uses `node` from `PATH` when there is one;
otherwise it downloads Node from nodejs.org, so run `make typecheck` once from
a terminal on a machine without Node.
The guard hook uses system `/usr/bin/python3` 3.9+ in isolated mode (`-I -B`) so
it works even if project metadata or `.venv` is broken. Native Windows shell
commands are not configured by this template; use WSL.

Neither agent runs in an OS sandbox. Claude Code's sandbox block is gone from
`.claude/settings.json`, and Codex runs with `sandbox_mode = "danger-full-access"`,
`approval_policy = "on-request"` and `approvals_reviewer = "user"`. `bubblewrap`
and `socat` are not prerequisites. What protects the project's configuration is
the [protected-files guard](#protected-files-guard) below.

For Claude Code, open the repository root and accept project trust. Check
`/memory` for the imported shared instructions and `/permissions` for
`acceptEdits` plus the protected-file ask rules.
`make setup` installs the plugins `.claude/settings.json` declares (pyright LSP and
Compound Engineering) in project scope; the CLI rewrites that file, so setup
restores its tracked bytes afterwards. The plugins are optional enhancements;
they are not required for the server or checks.

For Codex, use a CLI release that supports project hooks. Open and trust the
repository, then review and enable the project hooks in `/hooks`. Hook trust is
tied to the exact configuration: **re-trust the hooks whenever `.codex/hooks.json`
changes**. The project enables Compound Engineering in `.codex/config.toml`, so it
is active only in this project. `make setup` downloads its marketplace with a
one-command trust override, writing nothing to your global Codex configuration.
Codex itself reads the declaration once you trust the project; restart Codex
after setup.
The template does not edit your global settings. See the official
[project instructions](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
and [hook trust documentation](https://learn.chatgpt.com/docs/hooks).

Start a fresh session and verify that the approval policy is `on-request` and the
reviewer is `user`. A running session does not acquire new permissions or hooks
simply because these files were added.

In linked Git worktrees, Codex takes hook registration from the main
checkout. Inspect the source shown in `/hooks`; changes that exist only on a
worktree branch may not be active yet. Update the main checkout after the
reviewed change merges, then start a new session and review the hooks there.

## Everyday permissions

| Operation                                | Claude Code                                          | Codex                                         |
| ---------------------------------------- | ---------------------------------------------------- | --------------------------------------------- |
| Read `pyproject.toml` / `uv.lock`        | Allowed                                              | Allowed                                       |
| Edit ordinary source, tests, or docs     | Automatic                                            | Automatic                                     |
| Run listed development checks            | Allowed by project rules                             | Automatic                                     |
| Dependency changes                       | `make uv-add`, `uv-remove`, `uv-upgrade`, `uv-lock`  | Same targets; the guard accepts the result    |
| Formatter output, including metadata     | `make format*` targets; the guard accepts the result | Same targets                                  |
| Edit a protected file directly           | Edit/Write asks; the approved edit is accepted       | `apply_patch` is refused; you make the change |
| Keep a change the guard reverted         | `make guard-accept-changes`, which always asks you   | The same target; Codex prompts you            |
| Publish (push, PR create/merge, release) | Always asks you                                      | Prompt rules ask you                          |

Use the documented Make targets for routine tests, linting, typing and formatting.
`make test-one TEST=tests/test_file.py::test_name` selects a single test without
exposing arbitrary pytest flags. Pre-push runs `make check` and `make docs-build`;
do not repeat those gates manually after edits or before a PR. CI runs
`make test-integration`, including real tool/docs/release scenarios and, in the
generator, Copier scenarios. Run only the relevant integration test locally
when developing that test or its harness.

Dependency changes go through Make, never through `uv add`, `uv remove`,
`uv lock`, `uv sync` or `uv version` run by an agent:

| Target                                      | Use                                                                          |
| ------------------------------------------- | ---------------------------------------------------------------------------- |
| `make uv-add PKG=<spec> [GROUP=<group>]`    | Add a dependency; quote a spec that carries an environment marker            |
| `make uv-remove PKG=<name> [GROUP=<group>]` | Remove a dependency                                                          |
| `make uv-upgrade [PKG=<name>]`              | Upgrade one package or all of them                                           |
| `make uv-lock`                              | Relock after a `pyproject.toml` edit you approved                            |
| `make uv-reinstall`                         | Delete `.venv`, rebuild it from `uv.lock` and restore taplo on linux aarch64 |
| `make guard-accept-changes`                 | Keep protected changes made outside an agent call                            |

The `uv-*` targets run without approval in Claude Code. Run each as a standalone
foreground command from the repository root. After a target changes
`pyproject.toml`, run `make format` before committing if the formatter would
rewrite it.

Publishing asks in both hosts. Claude Code's permission rules ask before `git push`,
`gh pr create`, `gh pr merge`, `make bump-*` and `make release-tag`. In a trusted
project, `.codex/rules/publishing.rules` holds the matching Codex prompt rules, and
`approvals_reviewer = "user"` sends each prompt to you. The rules match only those
plain command prefixes: a form such as `git -C <dir> push` bypasses them (the
guard refuses `make -C/--directory` and these goals combined with other goals
or variables), an untrusted project does not
load them, and a user-level `approval_policy = "never"` or `--ignore-rules`
disables them. `CODEX_TEST_BINARY=... make test-permissions` checks each rule
against the installed CLI.

For a direct metadata change, the agent prepares the exact diff and asks for
approval. Approve that specific operation once. Do not save a blanket shell,
Python, or uv exemption. The approval is the write gate; changing `AGENTS.md` is
not an alternative to it.

## Protected-files guard

`scripts/hooks/guard_config.py` is a stdlib-only script, run as
`/usr/bin/python3 -I -B`, that both hosts call around every agent tool call. It
keeps the configuration that defines the project's checks and permissions from
changing without you.

### What it does

For each worktree it keeps a last-approved baseline in
`<git-dir>/protected-baseline/`. After every agent tool call it compares the
protected files with that baseline and then:

- restores protected files that no approved route changed,
- deletes newly created override configuration files,
- unstages protected content nobody approved, and
- tells the agent that writing these files is forbidden: use the Make targets or
    ask you.

Hook registration:

| Host        | Events                                                                                                                                                          |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Claude Code | `PreToolUse`, `PostToolUse`, `PostToolUseFailure` on `Bash\|Monitor\|PowerShell\|Edit\|Write\|MultiEdit\|NotebookEdit\|mcp__.*`, plus `ConfigChange` and `Stop` |
| Codex       | `PreToolUse` and `PostToolUse` on `Bash\|apply_patch`, plus `Stop`                                                                                              |

At the end of a turn, `Stop` reports any drift once.

### Protected files

- `pyproject.toml`, `uv.lock`, `Makefile`, `scripts/development.mk`
- `scripts/hooks/*`, `scripts/format_files.py`, `scripts/release.py`,
    `scripts/install_taplo.py`, `scripts/docs_version.py`
- `.claude/settings.json`, `.codex/config.toml`, `.codex/hooks.json`, `.codex/rules/*`
- `.pre-commit-config.yaml`, `mkdocs.yml`, `.taplo.toml`, `.mdformat.toml`,
    `.python-version`
- `AGENTS.md`, `CLAUDE.md`

The guard also deletes these files when they appear, because they would override
the project's tool settings:

- At any depth: `ruff.toml`, `.ruff.toml`, `pytest.ini`, `.pytest.ini`,
    `pytest.toml`, `.pytest.toml`, `tox.ini`, `setup.cfg`, and any `pyproject.toml`
    or `.mdformat.toml` below the root.
- At the root: `pyrightconfig.json`, `.coveragerc`, `.coveragerc.toml`,
    `GNUmakefile`, `makefile`, the Commitizen files (`.cz.toml`, `cz.toml`,
    `.cz.json`, `cz.json`, `.cz.yaml`, `cz.yaml`), `uv.toml`, `.gitattributes`,
    `taplo.toml`, `yamlfix.toml` and `.yamlfix.toml`.

### Approved routes

A change to a protected file stays only if it arrives through one of these:

- An exact, plain `make <target> [VAR=value]` from the repository root, in the
    foreground, for one of: `uv-add`, `uv-remove`, `uv-upgrade`, `uv-lock`,
    `uv-reinstall`, `format`, `format-toml`, `format-json`, `format-md`,
    `format-yaml`, `format-project-text`, `format-ruff`, `lint-fix`, `install`,
    `setup`, `install-taplo`, `bump-patch`, `bump-minor`, `bump-major` and
    `guard-accept-changes`.
- A plain standalone `git commit`. Pre-commit's formatting is accepted. A commit
    chained with other commands or built with `$(...)` is refused; write the
    message to a file and run `git commit -F <file>`.
- Plain `git switch`, `checkout`, `pull`, `merge`, `rebase`, `reset`, `restore`,
    `stash` or `cherry-pick`, and `gh pr checkout`, when the resulting files match
    the new `HEAD` or content you approved earlier.
- A Claude Code Edit/Write on a protected file that you approved through the ask
    rules. In Codex, `apply_patch` on a protected file is refused: you make the
    change.
- `make guard-accept-changes`, which always asks you (a Claude Code ask rule, a
    Codex execpolicy prompt rule).

The publishing commands keep their own approval, described under
[Everyday permissions](#everyday-permissions).

### Refused before running

The guard refuses these commands before they run:

- `git commit -n` / `--no-verify`, `--no-verify` on any git command, and `SKIP=`
- anything that touches `core.hooksPath`, and `GIT_CONFIG_*` variables
- assignments to `PYTEST_ADDOPTS`, `COVERAGE_RCFILE`, `MAKEFLAGS` and `MAKEFILES`
- `make -f`, `-C`, `-e` or `--directory`
- `pre-commit uninstall`
- running the guard script directly, or touching the baseline directory
- an allowlisted Make target in the background
- a `git commit` that is not a standalone command
- `guard-accept-changes`, `bump-*` or `release-tag` combined with other goals or
    variables, which would skip your approval

Patterns are matched against what the shell runs: quoted text and heredoc bodies
are ignored, except code passed to `sh -c` or `eval`.

### Changes made outside an agent call

If you edit `pyproject.toml` in an editor, or a write lands after its call ended,
the next agent call is refused with "ask the user whether they made this
change". Approve `make guard-accept-changes` to keep the change. The guard does
not undo a change it did not see being made: to drop it, restore the file
yourself, and agent calls resume once it matches the baseline again. On the very first run the protected files must match
`HEAD`; if they do not, the same flow applies.

### Commits

Because a commit must be a standalone call, the protected files it records are
the ones earlier calls left behind, and the guard has already checked those. A
new first pre-commit hook, `guard-staged`, also fails an agent's commit when it
stages protected content that nobody approved; an allowlisted target such as
`make bump-patch` may commit its own output. A commit you make outside an agent
call passes. A commit made with Git plumbing that carries unapproved protected
content, including a new file in a protected directory, blocks further agent
calls until you resolve it.

When a merge, rebase or cherry-pick leaves protected files conflicted or
unfinished, the guard does not restore them. It leaves them as they are and
blocks further agent calls until you resolve the conflict, finish the operation
and approve `make guard-accept-changes`.

### Environment and Git hooks

The guard also watches the environment that runs the checks:

- **`.venv`.** It records the name, size and mtime of `.venv/bin` entries,
    `site-packages` `*.pth` files, `*.dist-info` names and any
    `sitecustomize.py` / `usercustomize.py`. Changes from declared Make targets and
    Git commands are fine. Any other change refuses further calls until you run
    `make uv-reinstall`, which deletes `.venv` and rebuilds it from `uv.lock`.
- **`.git/hooks`.** The hooks are fingerprinted. A change outside
    `make setup` / `make install` is restored.

### Agent and user settings

- **Claude Code `ConfigChange`.** The guard blocks settings that set
    `disableAllHooks`, and removes that key from a local or user settings file on
    disk. It also blocks project settings that drop the guard hooks.
- **User-level Codex settings.** An agent shell change that sets `hooks = false`
    or `codex_hooks = false`, untrusts the project in `~/.codex/config.toml`, or
    breaks `~/.codex/hooks.json` is restored. Your own edits between calls are
    kept.

### Parallel calls

A call that can change protected state (an allowlisted Make target, a plain
`git commit` or other history command, or a protected edit) runs alone. Other
calls started meanwhile are refused with "retry after it finishes". The post-call
check runs when the last running call ends.

### Limits

The guard is a guardrail against mistakes, not a security boundary. Know what it
does not do:

- It cannot stop an agent that deliberately rewrites the guard script. Such a
    change shows in `git diff`; review it.
- Hooks fail open on a timeout. They also skip the guard when the checked-out
    commit has no guard, or an older one that does not declare
    `GUARD_PROTOCOL = 2`, so switching to such a commit does not lock the agent
    out; the guard resumes on the next call after switching back.
- `conftest.py` and `.github/workflows` are not protected; review catches
    changes there.
- Codex cannot make approved edits of protected files: you edit them.
- Without an OS sandbox, agents have unrestricted network access and can write
    outside the repository. The guard protects only the files listed above, the
    Git hooks and the user-level hook settings.
- An edit inside the files of an already-installed package in `.venv` is not
    fingerprinted.
- A write to another worktree surfaces only on that worktree's next call.
- Your `!` commands and cancelled calls fire no tool events. Their effects surface
    at the next agent call as changes made outside a call.

## Compound Engineering

Compound Engineering's shipped `.compound-engineering/config.yaml` sends plans and
brainstorm approaches to Opus, implementation units to a Sonnet worker with a
Codex fallback, and cross-model reviews to Codex. Without a sandbox, its detached
jobs (cross-model reviews and external `ce-work` workers) keep running after the
call that started them. Review diffs, and on fallback implementation code, go to
OpenAI whenever the `codex` CLI is installed. To keep a project's code away from
it, set `cross_model_review_mode: off` and `work_engine_mode: off` in the
untracked `.compound-engineering/config.local.yaml`.

## Verify and troubleshoot

The isolated policy tests run in `make check`. To also check each Codex publishing
rule against an installed CLI, run this from a terminal:

```bash
CODEX_TEST_BINARY="$(command -v codex)" make test-permissions
```

The extra tests make no model/API request and do not alter project metadata.

Codex's legacy `sandbox_mode` settings and CLI `--sandbox` switches, and managed
policies, can override the repository's configuration. Check the active session
rather than assuming the repository setting won.

When the guard reverts a change you wanted, or refuses a call because of a change
it did not see, run `make guard-accept-changes` and approve it. When it refuses
calls because of `.venv` drift, run `make uv-reinstall`.

Some work still needs a terminal: the Copier integration matrix (uv downloads
missing Python versions), and commits or GitHub CLI calls that need a signing
agent or a desktop keyring.

Browser, remote MCP, connector, and already-running shell interactions have their
own controls; never use them to bypass a required approval. See
[Claude permissions](https://code.claude.com/docs/en/permissions).
