# Claude Code and Codex

Both agents work on the same Python project and run the same checks. There is no
separate generated source tree for each agent.

## Files and responsibilities

| File                            | Responsibility                                                   |
| ------------------------------- | ---------------------------------------------------------------- |
| `AGENTS.md`                     | Shared environment, quality, navigation, Git, and approval rules |
| `CLAUDE.md`                     | Imports `AGENTS.md` for Claude                                   |
| `.claude/`                      | Claude permissions, hooks, and existing plugin preferences       |
| `.codex/config.toml`            | Codex filesystem permissions and human approval routing          |
| `.codex/hooks.json`             | Codex hook registration                                          |
| `scripts/hooks/guard_config.py` | Shared protected-file policy and host response adapters          |

GitHub's **Use this template** workflow keeps all these files. It still renames
the package, personalizes the repository, and removes its own setup machinery.
Tests for the agent policy remain in the generated project.

## First setup

Run `make setup` in a terminal before starting either agent. The development
workflow supports macOS, Linux, and WSL with uv, Git and Make. uv manages Python
3.11–3.13 (3.13 by default), development tools and MkDocs; no separate Node/npm
installation is required. Pyright manages its own runtime.
The hook uses system `/usr/bin/python3` 3.9+ in isolated mode so it works even if
project metadata or `.venv` is broken. Native Windows shell commands are not
configured by this template; use WSL.

For Claude Code, open the repository root and accept project trust. Check
`/memory` for the imported shared instructions and `/permissions` for
`acceptEdits` plus the protected-file ask rules. Existing Claude plugins are
optional enhancements; they are not required for the server or checks.

For Codex, use CLI **0.154 or newer**, or a desktop build supporting both
permission profiles and `PermissionRequest` hooks. Open and trust the repository,
then review and enable the project hooks in `/hooks`. Hook trust is tied to the
exact configuration: review again when it changes. The template does not edit
your global settings or install plugins. See the official
[project instructions](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
and [hook trust documentation](https://learn.chatgpt.com/docs/hooks).

Start a fresh session and verify its active profile is `justpen-dev`, approval
policy is `on-request`, and reviewer is `user`. A running session does not acquire
new permissions simply because these files were added.

In linked Git worktrees, Codex 0.154 takes hook registration from the main
checkout. Inspect the source shown in `/hooks`; changes that exist only on a
worktree branch may not be active yet. Update the main checkout after the
reviewed change merges, then start a new session and review the hooks there.

## Everyday permissions

| Operation                                    | Claude Code                   | Codex with the profile active                                          |
| -------------------------------------------- | ----------------------------- | ---------------------------------------------------------------------- |
| Read root `pyproject.toml` / `uv.lock`       | Allowed                       | Allowed                                                                |
| Edit ordinary source, tests, or docs         | Automatic                     | Automatic inside the workspace                                         |
| Run listed development checks                | Allowed by project rules      | Automatic inside the sandbox                                           |
| Standalone `uv add/remove/lock/sync/version` | Normal dependency workflow    | Escalations are automatic only with the explicit root form below       |
| Project formatter output, including metadata | Allowed through Make          | Automatic escalation for the exact root-pinned formatter targets below |
| Directly rewrite protected metadata          | Human approval                | Human approval for the shell escalation                                |
| Patch protected metadata with `apply_patch`  | Human approval                | Hook blocks; present the diff and request a shell escalation           |
| Change permission policy or hook files       | User-authorized policy change | User-authorized policy change                                          |

Use the documented Make targets for routine tests, linting, typing and formatting.
`make test-one TEST=tests/test_file.py::test_name` selects a single test without
exposing arbitrary pytest flags. Pre-push runs `make check` and `make docs-build`;
do not repeat those gates manually after edits or before a PR. CI runs
`make test-integration`, including real tool/docs/release scenarios and, in the
generator, Copier scenarios. Run only the relevant integration test locally
when developing that test or its harness.
Claude's hook checks the complete `make test-one TEST=…` command before allowing
it; no wildcard permission covers extra Make options or targets.

The uv exception applies to a complete command, not a prefix. Chains, shell
expansions, output redirections, `uv run`, alternative project/interpreter/cache
targets, and the complete release macro do not receive automatic escalation.
Split dependency changes from follow-up commands. Local build backends and
dependencies must be trusted just as they are when running uv yourself.

For Codex automatic escalation, place the directory option before the action:

```bash
uv --directory /absolute/path/to/repository add rich
uv --directory /absolute/path/to/repository sync --locked --group dev --group docs
```

Use the actual absolute path, quoted if it contains spaces. Codex 0.154's
approval payload omits the execution directory; a plain `uv sync` cannot prove
which project it will affect. The hook therefore requires this explicit form,
rejects a second target option, and leaves ordinary uv escalations to the user.

Trusted formatter output is allowed, including formatting `pyproject.toml`.
When formatting needs Codex escalation, use one of these exact commands:

```bash
make --directory /absolute/path/to/repository format
make --directory /absolute/path/to/repository format-md
make --directory /absolute/path/to/repository format-toml
make --directory /absolute/path/to/repository format-yaml
make --directory /absolute/path/to/repository format-json
```

The hook recognizes these formatter targets with the actual absolute repository root.
Extra targets, Makefile/variable overrides and shell chains receive no automatic
approval. This assumes the project's Makefile and formatter configuration are
trusted; never modify their behavior to disguise a direct metadata write.

For a direct metadata change, the agent prepares the exact diff and asks for
approval. Approve that specific operation once. Do not save a blanket shell,
Python, or uv exemption, and do not disable the sandbox to suppress prompts.
The native approval request is the write gate; changing `AGENTS.md` is not an
alternative to approval.

## Verify and troubleshoot

The isolated policy tests run in `make check`. To also exercise a real Codex sandbox
against disposable files, run this from a terminal outside an existing sandbox:

```bash
CODEX_TEST_BINARY="$(command -v codex)" make test-permissions
```

The extra test makes no model/API request and does not alter project metadata.
It checks reads, ordinary writes, and rejected protected writes, deletions, and
replacement renames. A runtime without the required sandbox support fails this
test; it does not silently downgrade the policy.

Codex's legacy `sandbox_mode` / `sandbox_workspace_write` settings and CLI
`--sandbox` switches can override permission profiles. Remove conflicting
personal/session overrides before selecting this profile. Managed policies may
also restrict the configuration; check the active session rather than assuming
the repository setting won. See [permission profiles](https://learn.chatgpt.com/docs/permissions).

The filesystem boundary covers root metadata in each active workspace root.
Nested independent projects need explicit read rules of their own. Claude's
command hook conservatively inspects visible commands; opaque scripts mentioning
protected paths may still ask. It is not an OS sandbox for arbitrary subprocesses.
Codex uses its filesystem policy to catch indirect local shell writes. Browser,
remote MCP, connector, and already-running shell interactions have their own
controls; never use them to bypass a required approval. See
[Claude permissions](https://code.claude.com/docs/en/permissions).
