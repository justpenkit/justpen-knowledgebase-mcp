# Template updates

Copier records the template source, revision and your answers in
`.copier-answers.yml`. Commit this file and let Copier manage it. Editing its
source, revision or answers by hand can break the comparison used for updates.

## Update on a feature branch

Run these commands from the project root with a clean working tree and a
committed project. Fetch the upstream template into a separate remote-tracking
ref, then update to that exact commit:

```bash
git switch -c chore/update-template
git fetch --no-tags https://github.com/justpenkit/justpen-mcp-dev-template.git main:refs/remotes/template/main
template_commit=$(git rev-parse refs/remotes/template/main)
uvx --from 'copier>=9.18.2,<10' copier update --vcs-ref="$template_commit" --defaults
git diff
```

Both creation routes support this flow. GitHub-created projects record `.` as
their Copier source: the original template is in their initial Git history, and
the fetch makes the new template revision available in the same repository.
Direct Copier projects keep the source URL used at creation. No answers-file
rewrite is needed. `--no-tags` keeps template release tags out of your project's
release history; the template's branch is never merged into your project branch.

Use a full clone and preserve the initial Git history. Fetch before every update,
including after cloning onto another computer, so the previous and new template
revisions are available. For a private upstream, use your normal Git credentials
with access to that repository. This is needed only when downloading updates;
GitHub's initial setup needs no additional credentials.

`--defaults` reuses your recorded answers. The recorded initial-version answer
records the project's starting point; it is not the current release version.
Keep its suggested value when Copier asks about it during creation or updates.
New projects start at `0.0.0`; projects created before that answer existed retain
the legacy `0.1.0` baseline. Template updates preserve version bumps made in
your project and do not reset them to a template default.

Omit `--defaults` to answer the questions again, or supply a specific value with `--data description='New project description'`.
Use `--vcs-ref=:current:` when changing answers without upgrading the template
(fetch first on a fresh clone). Always specify a ref: bare `copier update` in a
GitHub-created project would inspect the project's own tags rather than the
upstream template.

Copier merges template changes with your project changes. Independent custom
files and edits are preserved. Changes to the same lines can create inline
conflicts and unmerged Git entries. Inspect `git status`, resolve every conflict,
then run:

```bash
uv lock
make setup
```

Review and commit the result through a PR. Do not use `copier recopy` as a
shortcut for an update: it can overwrite project changes.

The Git pre-commit hook checks the staged diff and rejects unresolved conflict
markers, including markers that were accidentally staged as resolved.

## When a coding agent performs the update

The project's metadata gate also applies to Copier updates. Claude Code and
Codex must first render the update in a disposable checkout, present any direct
`pyproject.toml` or `uv.lock` changes, and obtain your approval before applying
them to the working project. Use the same exact template commit and answers for
the preview and approved update. Ordinary dependency resolution uses `uv`.

The shared guard is not a universal interception layer for every subprocess.
Follow the policy even when an indirect tool can write the file. Activate Codex's
filesystem profile as described in the [agent guide](../contributing/agents.md).

## Projects created before Copier

A project without `.copier-answers.yml` is not enrolled in this update flow.
Do not invent an answers file or run the first-run bootstrap on an established
project. Generate a fresh reference project separately, compare its files to
your existing server, and migrate deliberately in a reviewed PR.

See [Copier's update documentation](https://copier.readthedocs.io/en/stable/updating/)
for the merge model and recovery options.
