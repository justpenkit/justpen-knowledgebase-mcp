# Template updates

Copier records the template source, revision, and answers in
`.copier-answers.yml`. Apply updates on a feature branch from a clean, committed
worktree:

```bash
git switch -c chore/update-template
git fetch --no-tags https://github.com/justpenkit/justpen-mcp-dev-template.git main:refs/remotes/template/main
template_commit=$(git rev-parse refs/remotes/template/main)
uvx --from 'copier>=9.18.2,<10' copier update --vcs-ref="$template_commit" --defaults
git diff
```

Resolve every conflict, then run `uv lock` and `make setup`. Review and ship the
result through a PR. Do not edit `.copier-answers.yml` by hand or use `copier recopy` as an update shortcut. Coding agents must follow the protected metadata
approval rules in the [agent guide](agents.md) for direct `pyproject.toml` or
`uv.lock` changes.

Projects without `.copier-answers.yml` should generate a fresh reference project
and migrate selected changes instead of inventing an answers file.
