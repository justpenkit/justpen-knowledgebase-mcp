# Pre-PR checklist

Run through this list before opening a pull request. The goal is to keep the
review cycle short: an hour spent on the checklist saves a day of back-and-forth.

## 1. Branch and commits

- Branch name follows `type/short-description` (e.g. `feat/foo-bar`) or `codex/short-description`.
- Each commit uses [Conventional Commits](https://www.conventionalcommits.org/):
    `type(scope): subject`. **Scope** is optional and freeform; **type** is
    enforced by Commitizen with the project schema to one of:
    `feat`, `fix`, `docs`, `chore`, `ci`, `refactor`, `test`, `style`,
    `build`, `perf`, `revert`. Subject ≤72 chars, no trailing period.
- History is the shape you want merged (we use real merge commits, never
    squash). Rebase or `--amend` locally before opening the PR.

## 2. Local verification

The pre-push hook must pass `make check` and `make docs-build`. Do not repeat
those commands manually for the same passing push. `make check` verifies lock
consistency, Python/Markdown/TOML/YAML/JSON/HTML/CSS formatting, lint, strict
typing and unit tests with 80% branch coverage in the active Python environment.
The strict docs build rejects broken internal links and anchors.

CI verifies typing/unit tests across Python 3.11–3.13 and runs integration tests
separately on Python 3.13. When developing an integration test or its harness,
run the relevant scenario locally with `make test-one TEST=...`; other changes
do not require the local generation matrix.

## 3. Tests

- New behaviour is covered by a test.
- Bugs come with a regression test that fails on the old code and passes
    on the new code.

## 4. Documentation

- Public-facing behaviour changes are reflected in the relevant doc under
    `docs/`.

## 5. Opening the PR

- PR title mirrors the Conventional Commit subject of the main change.
- Description covers: what changed, why, how it was tested, and any
    follow-ups being deliberately deferred.
- Linked issues / discussions are referenced.
- CI is green on the PR branch before requesting review.
