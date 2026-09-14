# Lint & Type Check

Ruff and pyright are both configured in `pyproject.toml`. Ruff runs with a broad rule set; pyright runs in `typeCheckingMode = "strict"` with `reportUnnecessaryTypeIgnoreComment = "error"` — dead suppressions are caught automatically.

Non-Python formatting is installed through uv alongside the development tools:

| Files    | Formatter                                    | Make target        |
| -------- | -------------------------------------------- | ------------------ |
| Python   | Ruff                                         | `make format`      |
| Markdown | mdformat with GFM/frontmatter/MkDocs support | `make format-md`   |
| TOML     | taplo                                        | `make format-toml` |
| YAML     | yamlfix                                      | `make format-yaml` |
| JSON     | pretty-format-json                           | `make format-json` |
| HTML     | djlint                                       | `make format-html` |
| CSS      | cssbeautifier                                | `make format-css`  |

`make format` runs all formatters; `make format-check` checks without rewriting
files. The formatters cover tracked and untracked non-ignored files, excluding
`uv.lock`, private work areas and the Jinja source tree. Generated projects run
the same checks on their rendered files. Make, Git hooks and CI use the same
tools and configuration. The editor follows the same formatting conventions;
for JSON it uses Python's `json.tool`, while Make uses `pretty-format-json`.
Both preserve key order and Unicode, with two-space indentation. JSON formatting
applies to standard JSON; JSON with comments is not supported.

The MkDocs Markdown plugin preserves admonitions and other MkDocs syntax during
formatting, so callout contents keep their structure in the rendered website.

HTML and CSS formatting also runs through the existing Make, pre-commit,
pre-push and CI gates. djlint formats HTML and inline CSS/JavaScript;
cssbeautifier formats standalone CSS. Both use two-space indentation and are
Python packages installed by `make install` or `make setup`, with no separate
Node/npm installation. Use `make format-html-check` or `make format-css-check`
to check files without rewriting them.

**Fix the root cause, do not silence warnings.**

## Auto-fix first

For ruff violations, run `make lint-fix` before editing by hand. The target uses Ruff's safe fixes. Review remaining suggestions and fix the underlying code; do not introduce ad hoc flags that weaken the checks.

## Suppression bans

Never use any of the following to bypass a lint or type error without a strong, documented reason:

- `# noqa` / `# noqa: <code>` (ruff)
- `# type: ignore` / `# type: ignore[...]` (generic)
- `# pyright: ignore[reportX]` (pyright — preferred form when a pyright suppression is truly unavoidable, because it is rule-specific and pyright will flag it if it becomes unnecessary)
- `# pragma: no cover` (coverage — same discipline: only for code that legitimately cannot be executed in tests, not to hide untested paths)

## What counts as a strong, documented reason

One of:

- Known ruff or pyright bug with an upstream issue link.
- Third-party API whose typing or runtime behavior cannot be worked around (explain which API and why).
- Architectural trade-off already discussed and approved by the user.

## Suppression format — when one is truly justified

- Always use the **specific rule code** (`# noqa: E501`, `# pyright: ignore[reportUnknownMemberType]`), never a blanket form.
- Add an inline comment explaining **why** on the same line or the line immediately above.
- Prefer refactoring the code over suppressing the warning; suppression is the last resort.

## Config changes require escalation

Never modify `pyproject.toml` ruff or pyright rules unilaterally to make warnings disappear. This includes:

- `[tool.ruff.lint] select`
- `[tool.ruff.lint] ignore`
- `[tool.ruff.lint.per-file-ignores]`
- Any `report*` severity under `[tool.pyright]`

Raise the concern with the user first and only edit after explicit approval. Do not sprinkle suppressions across the codebase as a substitute for fixing the underlying issue.

## Focused development and automatic gates

Use `make test-one TEST=tests/test_file.py::test_name` for feedback while
implementing a behavior. It accepts one test file/node under `tests/`, not
arbitrary pytest flags, and does not apply the suite-wide coverage threshold.
Host diagnostic errors remain blockers when provided.

Git hooks own routine verification; do not manually repeat a passing gate after
every edit or before a PR. Run an individual Make target when diagnosing a
failure or when earlier feedback is useful.

Classify tests by the behavior and component boundary they verify, never by
elapsed time. Unit tests check one component with its external collaborators
isolated; integration tests check real components working together. A short
integration test still belongs in CI.

For example, Make recipes with a stubbed uv and the publication script with a
stubbed GitHub CLI check isolated command routing and decisions. Reading workflow
permissions is a static configuration check. These do not validate the real
tool integrations. Running Python or a shell as the component's interpreter does
not by itself make a test an integration test.

Bootstrap tests that use real Git are integration tests even when Copier is
stubbed. Project configuration checked by real Commitizen, registered hook
launchers, formatters, release tooling, docs builds and generated projects also
exercise integrations. CI runs this suite through `make test-integration`;
the actual Codex sandbox probes additionally require an explicit
`CODEX_TEST_BINARY` and remain skipped in normal CI. Local execution is only needed while developing such a test or its harness: select
the relevant scenario with `make test-one` instead of the full generation matrix.

TOML formatting of Python metadata through Make and uv-managed lockfile changes
remain trusted tool operations. Direct AI metadata rewrites and unilateral
changes to lint/type/coverage policy still require approval.

## Git hooks

`make setup` installs dependencies through uv and all three Git hook stages:

- `pre-commit` — conflict/whitespace checks, `make lint-fix` and active-Python
    `make typecheck` for Python changes, `make format` for text changes, and
    `make lock-check` when `pyproject.toml` or `uv.lock` changes.
- `pre-push` — `make check` and `make docs-build`. The first runs all formatting
    checks, lint, active-Python typing and unit tests with 80% branch coverage.
    The second builds this project's docs once with strict link/anchor checks.
    Neither starts the integration suite.
- `commit-msg` — uv runs Commitizen's `cz check` with the project schema,
    including its allowed types, optional scope, 72-character subject limit and
    prohibition on a trailing period. The default changelog plugin remains
    `cz_conventional_commits`; only validation selects `cz_customize`.

Commitizen handles Git comment/scissor sections and normalizes surrounding
message whitespace before validation. The project schema does not require a
blank line before the body. Generated merge/revert/autosquash messages retain
the existing explicit prefix exemptions.
Unlike the previous script, a comment must start with `#` in column zero;
an indented `#` line is message content. Leading blank lines and trailing spaces
are normalized, so a period followed only by whitespace is still rejected.
Git's scissors marker ends the message; text below it cannot supply a subject.

CI's shared job runs formatting, lint and the docs build once. Each Python matrix
job runs strict typing and unit tests only for its selected interpreter. Real
integration scenarios run separately; generated projects keep their own
infrastructure tests and CI integration job.

Use `make setup` to install/reinstall hooks after configuration changes.
`make pre-commit` is available for deliberate all-file hook diagnostics, not an
additional routine gate. Diagnose hook failures and fix their cause; never
bypass hooks with `--no-verify`.
