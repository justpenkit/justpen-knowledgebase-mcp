# Template updates

Copier records the template source (`gh:justpenkit/justpen-mcp-dev-template`),
revision, and answers in `.copier-answers.yml`. Let Copier manage this file:
editing its source, revision or answers by hand can break the comparison used for
updates.

## Update on a feature branch

Apply updates on a feature branch from a clean, committed worktree. Name the
template release tag you are updating to:

```bash
git switch -c chore/update-template
uvx --from 'copier>=9.18.2,<10' copier update --vcs-ref=vX.Y.Z --defaults
git diff
```

Always pass `--vcs-ref`. `--defaults` reuses the recorded answers.

Copier merges template changes with project changes. Changes to the same lines
create inline conflicts and unmerged Git entries. Resolve every conflict, then
run:

```bash
make uv-lock
make setup
```

Review and ship the result through a PR. Do not use `copier recopy` as an update
shortcut: it can overwrite project changes. Files this project moved or deleted,
such as this guide (the template keeps it at `docs/guides/template-updates.md`),
receive no template changes; port those by hand.

## When a coding agent performs the update

The protected-files guard also applies to Copier updates. Claude Code and Codex
must first render the update in a disposable checkout, present any direct
`pyproject.toml` or `uv.lock` changes, and obtain your approval before applying
them to the working project. Use the same exact template revision and answers for
the preview and the approved update. Ordinary dependency resolution uses the
`make uv-*` targets.

The guard reverts a `copier update` run by an agent that changes protected files,
because `copier` is not an approved route. Run `copier update` from a terminal
after approving the preview, or approve the result with
`make guard-accept-changes`. See the
[agent guide](agents.md#protected-files-guard) for the guard and its limits.
Follow the policy even when an indirect tool can write the file.

## CI job names

Template updates can rename CI jobs. One `check` job replaced the `quality` job
and the `check (py3.x)` matrix legs. If the repository requires status checks by
name, the update's own pull request waits for checks that no longer run. Once
`check` and `integration` pass on that pull request, change the required checks
to those names, then merge.

Projects without `.copier-answers.yml` should generate a fresh reference project
and migrate selected changes instead of inventing an answers file.
