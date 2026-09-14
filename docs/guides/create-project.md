# Create a project

## GitHub: Use this template

GitHub copies all the template files into your new repository. Setup renders
those files locally and uses GitHub's automatic repository token for the final
push. No additional token or Actions secret is required, even for a private
template source.

1. Select **Use this template → Create a new repository** on
    [justpen-mcp-dev-template](https://github.com/justpenkit/justpen-mcp-dev-template).
2. Choose a lowercase name ending in `-mcp`, such as `weather-mcp`, and set the
    repository description. Keep `main` as the default branch: CI push triggers
    and the release workflow use that name. Enable Actions if GitHub asks.
3. Wait for **Initial project setup** in the Actions tab. It creates the project,
    resolves its Python dependencies, and runs formatting, linting, strict type
    checking, tests, the documentation build and the package build before pushing
    the setup commit.
4. Clone the finished repository, run `make setup`, and follow its
    `docs/contributing/getting-started.md` guide to finish repository setup.

The repository owner becomes the default author. The repository description goes
into the project's Python metadata and documentation. An empty description uses
`MCP server for <project-name>.` You can later change answers through Copier.

Copier records `.` as the source and the copied repository's actual commit in
`.copier-answers.yml`. This keeps the original template available in your Git
history after setup removes the generator files. The project uses exactly the
template you copied, without downloading the upstream repository again. Follow
the [update guide](template-updates.md) to fetch later template changes.

If validation fails, the setup commit is not created and the original tracked
files stay in place. Fix the reported issue and rerun the failed workflow. Setup
is restricted to the default branch and becomes a no-op after its sentinel is
removed. A GitHub token push does not start another CI run; the setup workflow
already runs the generated project's gates. Normal CI runs on pushes to `main`
and on pull requests. Repository rules must permit the initial bot push.

The setup token cannot add or modify workflow files, so retained workflows stay
identical. A validation check rejects changes to those files before installation.

## Local: Copier

Install [uv](https://docs.astral.sh/uv/), then run:

For a private template, first authenticate Git over HTTPS, for example with
`gh auth login` followed by `gh auth setup-git --hostname github.com`.

```bash
uvx --from 'copier>=9.18.2,<10' copier copy --vcs-ref=main \
  https://github.com/justpenkit/justpen-mcp-dev-template.git weather-mcp
cd weather-mcp
git init -b main
make setup
git add .
git commit -m "chore: initialize MCP project"
```

Copier asks for the project name, GitHub owner, description, author and copyright
year. The Python import name is derived: `weather-mcp` becomes `weather_mcp`.
Both setup routes reject whitespace, including trailing newlines, in project
and owner names. The required `-mcp` suffix keeps generated package names such
as `logging_mcp` and `pytest_mcp` distinct from Python's `logging` module and the
`pytest` dependency; unsuffixed names such as `logging`, `pytest` and `scripts`
are rejected.

Copying only renders files: no Copier tasks, custom Jinja extensions or `--trust`
are required. `make setup` installs the tools and creates `uv.lock`. Commit that
lock and `.copier-answers.yml` with the project.

Setup also formats generated Markdown, TOML, YAML and JSON using uv-installed
tools. Formatting preserves metadata and recorded answer values. A new project
starts at `0.0.0`, supports Python 3.11–3.13 and defaults to Python 3.13 locally.

Use an explicit `--vcs-ref=main` until the first Copier-compatible release is
published. Earlier `v0.x` releases use the old generator. Afterwards, choose a
Copier-compatible release tag or an exact commit for reproducible generation.

Both routes produce the same MCP starter, shared development policy and agent
integrations. Continue with the [agent setup guide](../contributing/agents.md).
