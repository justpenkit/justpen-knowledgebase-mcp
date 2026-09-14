# Release process

This project releases from `main`. Create a version bump on a feature branch,
merge its PR, then annotate the reviewed merge and push that tag to create the
GitHub Release.
The sequence is identical for patch, minor and major bumps.

## Versioning policy

We follow [Semantic Versioning 2.0](https://semver.org/). New MCP projects start
at `0.0.0`. During `0.x`, minor bumps may include breaking changes, as SemVer
permits for initial development. The template generator has its own version;
updating a project through Copier does not reset the application's version.

## Tooling

Use `make changelog` to generate `CHANGELOG.md` from Conventional Commits with
Commitizen. By default, generated applications begin their changelog history at
the commit that introduced `.copier-answers.yml`, so inherited template commits
are not presented as application features. Preserve that initial history for
Copier updates.

An explicit `changelog_start_rev` under `[tool.commitizen]` takes precedence over
that default, including after later Copier enrollment. Set a reviewed commit to
choose a project's history boundary, or an empty string to include its full
history. Changing this metadata setting follows the normal approval policy.

Each `make bump-{patch,minor,major}` target:

1. Requires a clean working tree on a feature branch, not `main`, `master` or a
    detached checkout.
2. Calls `uv version --bump <segment>` to update `pyproject.toml` and `uv.lock`.
3. Generates the changelog with Commitizen and formats `CHANGELOG.md` with
    mdformat.
4. Updates this project's installation references in tracked `README.md` and
    Markdown files beneath `docs/`, matching `git+<Repository>[.git]@v<version>`.
    The full URL comes from `[project.urls].Repository`; other repositories,
    branch/SHA refs, changelog pages, symlinks and untracked files stay unchanged.
    Older installation pins also move to the new version; no manual per-file
    version update is needed.
5. Commits the metadata, changelog and updated installation examples with the
    normal Git hooks enabled, so the pin changes are included in PR review.

The bump prepares the release for review without creating a tag. After the PR
merges, `make release-tag` creates `v<new-version>` at the reviewed merge commit
on an updated, clean `main`. It rejects feature branches, stale local main,
non-merge commits and existing tags.

Every failed step stops the command. Inspect the reported error and working
tree before retrying; formatting and hook changes are not silently discarded.
Neither command pushes tags or publishes to PyPI.

For coding agents, ordinary uv-managed version changes remain allowed. The
complete release operation also commits and tags, so it requires release
authorization and is not an automatic dependency-management exemption.

## Step-by-step flow

Start with a clean working tree and an up-to-date `main`.

### 1. Create the release branch

```bash
git switch main
git pull --ff-only
git switch -c chore/bump-v<new-version>
```

Choose `<new-version>` from the current version (`make version`) and intended
segment. Codex uses `codex/bump-v<new-version>` for its feature branch.

### 2. Bump

```bash
make bump-patch     # or bump-minor, or bump-major
```

Review the resulting metadata, lockfile and changelog. The command creates the
bump commit locally. If you chose the wrong segment, inspect the unpushed commit
and agree on a correction before changing history.

### 3. Push the branch only

```bash
git push -u origin chore/bump-v<new-version>
```

Use the branch name you created. Finish PR review before creating the tag so
the release includes corrections made after the bump commit.

### 4. Open and merge the PR

- Title: `chore: bump version to v<new-version>`.
- Complete the [PR checklist](pr-checklist.md); the pre-push hook runs
    `make check` and `make docs-build`, so no duplicate manual run is required.
- Review the changelog as the exact notes that will accompany this release.
- Merge with a regular merge commit, never squash. The finalization command
    requires a merge commit on `main`.

### 5. Create and push the reviewed tag

Once the PR is merged, update `main` and review that its latest merge is the
release you intend to publish:

```bash
git switch main
git pull --ff-only
make release-tag
git push origin v<new-version>
```

The tag points at the merged contents, including changes made during review.
Never move or overwrite an existing published tag. If a historical branch tag
omits changes included when its PR merged, release validation rejects it; an old
branch tag with identical merged contents remains valid.

### 6. Automatic GitHub Release

The tag-triggered workflow runs validation code from `main` with read-only
permissions. It verifies that the tag is annotated, matches the tagged
`pyproject.toml`, belongs to `origin/main`, and includes its reviewed merge
contents. It extracts the corresponding section from the tagged `CHANGELOG.md`.
Only the separate publication job has write permission; it receives those notes
as data and creates the GitHub Release. A rerun keeps an already-created release
instead of duplicating it.

Inspect the workflow result in Actions and the release notes in GitHub Releases.
This automation creates the GitHub Release only; package-index publishing is
not configured.
