---
title: Template v0.6.2 Sync - Plan
type: chore
date: 2026-10-04
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-plan-bootstrap
execution: code
---

# Template v0.6.2 Sync - Plan

## Goal Capsule

- **Objective:** Knowledgebase contributors get the same agent guardrails, Make targets and CI shape as the other justpen MCP repositories, and future template releases reach this repository through a plain `copier update`.
- **Means:** Point the Copier answers at the template's v0.4.0 release, then update to v0.6.2 and resolve conflicts by the rules in KTD3–KTD9 (KTD1, KTD2).
- **Authority:** User instructions in the session, then this plan, then the template v0.6.2 sources. Knowledgebase product code is out of scope and wins over any template text that would change it.
- **Stop conditions:** Stop and ask when a conflict touches knowledgebase product behavior, when a template test cannot pass without dropping a knowledgebase-only job or target, or when a protected-file change needs the user (KTD6).
- **Execution profile:** U1–U4 and the U5 gates run from the template-repo Claude Code session against the knowledgebase checkout path, as for the browser and integration updates. Do not run them from a Claude Code session opened in the knowledgebase: its OS sandbox keeps root `pyproject.toml` and `uv.lock` read-only, and `uvx copier` is not an excluded command. A knowledgebase session is used only after U5's `make setup` and restart, for the guard smoke check. The user runs commits and the Claude settings change through `!` commands; push, PR, merge and branch-protection changes wait for the user's approval.

---

## Product Contract

### Summary

Relink `.copier-answers.yml` to `gh:justpenkit/justpen-mcp-dev-template` at v0.4.0 and update to v0.6.2. The Claude Code OS sandbox gives way to the protected-files guard, and the `make uv-*` targets arrive. CI takes the template's job shape, and knowledgebase-only CI jobs, Make targets, dependencies and documentation stay.

### Problem Frame

The knowledgebase was created through GitHub's "Use this template" button. Its template-cleanup step ran Copier against the repository itself, so the answers file records `_src_path: "."` and `_commit: "13f7e7e"`. `copier update` therefore cannot find the template, and the repository has drifted. It still uses the Claude OS sandbox that the template replaced in v0.6.0. It lacks the `make uv-*` targets, and its CI runs a three-version matrix on every push that the other repositories dropped to save Actions minutes. The initial commit `13f7e7e` has exactly the tree of template tag v0.4.0 (template commit `3a31cde`), so v0.4.0 is the true base. `copier.yml` asks the same questions in v0.4.0 and v0.6.2.

### Requirements

**Template link**

- R1. `.copier-answers.yml` names `gh:justpenkit/justpen-mcp-dev-template` as its source and records v0.6.2 after the update, so a later `copier update --vcs-ref=<tag>` runs without manual relinking.
- R2. Existing answers (author, copyright year, description, initial version, project name, repo owner) keep their values.

**Agent policy and tooling**

- R3. Claude Code and Codex in the knowledgebase run the template's protected-files guard; nothing configures the Claude OS sandbox.
- R4. The `make uv-add`, `uv-remove`, `uv-upgrade`, `uv-lock`, `uv-reinstall` and `guard-accept-changes` targets work in the knowledgebase.
- R5. Knowledgebase-only Make targets keep working: `docs-catalog`, `test-native`, `test-consumer` and `benchmark-kb`.

**CI**

- R6. CI has the template's `check` job (Python 3.13 always, 3.11 and 3.12 on pull requests) and a single `integration` job on 3.13.
- R7. The knowledgebase-only CI behavior stays: the strace prerequisite for integration tests, the `consumer-runtime` matrix and the `deploy-docs` publication job.

**Project content**

- R8. Knowledgebase dependencies, build settings and product documentation are unchanged except where the template's text replaces sandbox-era agent instructions.
- R9. Files the knowledgebase deliberately deleted (`docs/api.md`, `docs/guides/template-updates.md`, the demo tools and their tests) stay deleted.
- R10. The knowledgebase's Compound Engineering config (model routing, codex review peer, `compound-packs`) is unchanged.

### Scope Boundaries

- Reducing the `consumer-runtime` matrix (nine jobs, including macOS, on every pull request) to fit the template's CI-minutes policy is not in this plan. It is a knowledgebase decision with its own trade-offs.
- Product source under `src/` and product tests are not touched.
- No release or version bump is part of this work.

#### Deferred to Follow-Up Work

- Branch protection for `main` currently requires `check (py3.11)`, `check (py3.12)`, `check (py3.13)` and `integration (py3.13)`. After the PR's CI shows the new job names, the required contexts change to `check` and `integration`, with the user's approval at that moment, as was done for justpen-browser-mcp.

---

## Planning Contract

### Key Technical Decisions

- KTD1. **Relink to v0.4.0 rather than regenerate or sync by hand.** The initial commit's tree equals template v0.4.0, so Copier's three-way update sees exactly the knowledgebase's own changes since then. Hand syncing would keep the answers file broken. (session-settled: user-approved — chosen over leaving the knowledgebase outside the template: the user asked for the template as its base)
- KTD2. **Run Copier inside the knowledgebase checkout.** Use `--vcs-ref=v0.6.2 --defaults` with inline conflict markers, as for the other repositories. A clone under the template tree is wrong, because the template session's guard restores protected-named files there.
- KTD3. **Agent policy files take the template version.** This covers `.codex/config.toml`, `.codex/hooks.json`, `.codex/rules/publishing.rules`, `scripts/hooks/guard_config.py`, `.claude/CLAUDE.md`, `tests/test_agent_permissions.py` and `tests/test_guard.py`. The knowledgebase versions encode the sandbox and the Codex-only hook that R3 removes. `AGENTS.md` takes the template text plus one knowledgebase line: benchmark output also lives in `.private/`.
- KTD4. **`.claude/settings.json` takes the template version verbatim.** The knowledgebase-only keys (`env`, `effortLevel`, `attribution`) already exist in the template file, and the template adds the guard hooks and the Compound Engineering marketplace.
- KTD5. **`scripts/development.mk` takes the template version plus the `docs-catalog` target** and its `.PHONY` and help entries (R5). The template already carries `install-taplo`.
- KTD6. **Protected files resolve through the user or plain Git.** The agent does not write `.claude/settings.json`; the user takes the template side with one Git command. `pyproject.toml` keeps the knowledgebase version, which already contains the template's v0.6.2 deltas and a newer `uv_build` floor (`>=0.12.18,<0.13`). The lock then refreshes through `make uv-lock`.
- KTD7. **CI merges the template shape with the knowledgebase-only jobs.** The `quality` and per-version `check` jobs become the template `check` job. `integration` drops its matrix and keeps the strace step. `consumer-runtime` stays as is, and `deploy-docs` then needs `check`, `integration` and `consumer-runtime`. The knowledgebase's `cache-suffix` lines stay, including in `audit.yml`.
- KTD8. **`tests/test_ci_workflow.py` lists the knowledgebase jobs.** The browser and integration repositories set the expected job set to their extra `deploy-docs` job. Here it becomes `{"check", "integration", "consumer-runtime", "deploy-docs"}`; every other template assertion stays.
- KTD9. **Documentation keeps the knowledgebase content and takes the template's agent text.** In `README.md`, `docs/index.md` and `docs/contributing/{agents,getting-started,pr-checklist}.md`, sandbox-era paragraphs give way to the template's guard text. Product pages and knowledgebase-specific sections stay. `.gitignore` and `.compound-engineering/config.example.yaml` take the template side. The knowledgebase moved the template's update guide to `docs/contributing/template-updates.md`, which Copier no longer renders or merges. That page takes the template v0.6.2 guide's changes by hand: the `gh:` source with `copier update --vcs-ref=<tag> --defaults`, `make uv-lock` instead of `uv lock`, the warning that the guard reverts an agent-run `copier update`, and the "CI job names" section.

### Assumptions

- The template's guard needs no knowledgebase-specific additions. Knowledgebase-only scripts (`scripts/catalog_reference.py`, the benchmark scripts) are not policy files.
- `make test-permissions` runs only where a Codex CLI is installed (`CODEX_TEST_BINARY`). Without one, the Codex execpolicy checks are reported as not run rather than skipped silently.
- The update needs no dependency change. A `make uv-lock` that changes `uv.lock` beyond the build backend is investigated before committing.

### Risks

- A Claude Code session opened in the knowledgebase keeps the OS sandbox until it restarts with the new settings, so the Copier run, the relock and `make setup` fail there. Mitigation: the work runs from the template-repo session (Execution profile), and the knowledgebase session restarts only after the commit, for the guard smoke check (U5).
- Later template updates will conflict on the `set(jobs)` line in `tests/test_ci_workflow.py` and on the extra CI jobs. The browser and integration repositories accept the same cost.

---

## Implementation Units

### U1. Relink the Copier answers

- **Goal:** `copier update` can find the template's v0.4.0 base.
- **Requirements:** R1, R2; KTD1.
- **Dependencies:** none.
- **Files:** `.copier-answers.yml`, `.compound-engineering/artifacts/plans/2026-10-04-2256-chore-template-v062-sync-plan.md`.
- **Approach:**
  1. On branch `chore/template-v0.6.2`, change `_src_path` to `gh:justpenkit/justpen-mcp-dev-template` and `_commit` to `v0.4.0`. Leave the other answers unchanged.
  2. The user commits the change together with this plan file through a `!` command. Copier refuses a dirty tree, and its check counts untracked files; earlier plans in this directory are already tracked.
- **Test expectation:** none -- metadata only; U2's Copier run proves it.
- **Verification:** The answers diff shows only the two changed lines, and `git status --porcelain` is empty after the commit.

### U2. Update to v0.6.2 and resolve agent policy and tooling

- **Goal:** The knowledgebase carries the template's guard, settings, Codex rules and Make targets (R3, R4, R5).
- **Requirements:** R3, R4, R5, R10; KTD2–KTD6.
- **Dependencies:** U1.
- **Files:** `.claude/settings.json`, `.claude/CLAUDE.md`, `.codex/config.toml`, `.codex/hooks.json`, `.codex/rules/publishing.rules`, `AGENTS.md`, `scripts/development.mk`, `scripts/hooks/guard_config.py`, `pyproject.toml`, `uv.lock`, `.pre-commit-config.yaml`, `tests/test_agent_permissions.py`, `tests/test_guard.py`, `tests/test_development_workflow.py`.
- **Approach:**
  1. Run the Copier update (KTD2) and record the conflicted-file list.
  2. Resolve the policy files by KTD3, `development.mk` by KTD5 and `pyproject.toml` by KTD6. Hand the user the Git command that takes the template side of `.claude/settings.json` (KTD4).
  3. Run `make uv-lock`.
- **Patterns to follow:** the same update in justpen-integration-mcp (`chore: update the development template to v0.6.0`, merged as #20) and justpen-browser-mcp (#71).
- **Test scenarios:**
  - `tests/test_agent_permissions.py` and `tests/test_guard.py` pass against the knowledgebase's files.
  - The merged `tests/test_development_workflow.py` keeps the template tests and the knowledgebase CI tests that U3 updates.
  - A search of tracked configuration finds no `sandbox` settings block in `.claude/settings.json`.
- **Verification:** No conflict markers remain in these files, and `make uv-add` and `make docs-catalog` both appear in `make help`.

### U3. Merge the CI workflows

- **Goal:** CI runs the template job shape plus the knowledgebase-only jobs (R6, R7).
- **Requirements:** R6, R7; KTD7, KTD8.
- **Dependencies:** U2.
- **Files:** `.github/workflows/ci.yml`, `.github/workflows/audit.yml`, `tests/test_ci_workflow.py`, `tests/test_development_workflow.py`.
- **Approach:**
  1. Build `ci.yml` from the template `check` and `integration` jobs.
  2. Re-add the strace step, `consumer-runtime` and `deploy-docs` with the updated `needs` (KTD7).
  3. Set the expected job set in `tests/test_ci_workflow.py` (KTD8).
  4. Update the knowledgebase's own CI tests in `tests/test_development_workflow.py`. Copier merges them without a conflict marker, but they still assert the old `quality` job. `test_full_integration_suite_runs_on_python_313` asserts the merged single-version `integration` job: Python 3.13, `needs: check`, its `if` on `needs.check.outputs.generated`, the strace step and `make test-integration`. `test_documentation_deploy_waits_for_every_validation_job` expects `deploy-docs` to need `check`, `integration` and `consumer-runtime`.
- **Test scenarios:**
  - `tests/test_ci_workflow.py` passes with the four-job set.
  - The two updated tests in `tests/test_development_workflow.py` pass against the merged `ci.yml` and fail against the old `quality` shape.
  - `deploy-docs` runs only on a push to main after `check`, `integration` and `consumer-runtime` succeed.
  - A pull request runs the 3.11 and 3.12 steps; a push to main runs only 3.13.
- **Verification:** The PR's CI shows checks named `check` and `integration`, plus the unchanged `consumer/runtime (...)` jobs.

### U4. Reconcile documentation and remaining template files

- **Goal:** Contributor documentation describes the guard and the `make uv-*` workflow and keeps the knowledgebase's own content (R8, R9).
- **Requirements:** R8, R9, R10; KTD9.
- **Files:** `README.md`, `docs/index.md`, `docs/contributing/agents.md`, `docs/contributing/getting-started.md`, `docs/contributing/pr-checklist.md`, `docs/contributing/lint-typing.md`, `docs/contributing/release-process.md`, `docs/contributing/template-updates.md`, `.github/PULL_REQUEST_TEMPLATE.md`, `.gitignore`, `.compound-engineering/config.example.yaml`, `compound-packs/README.md`.
- **Dependencies:** U2.
- **Approach:**
  1. Resolve each conflict block by KTD9.
  2. Port the template v0.6.2 update-guide changes into `docs/contributing/template-updates.md` (KTD9), with the guard link pointing at the knowledgebase's `agents.md`.
  3. Search the docs for leftover sandbox instructions (`bubblewrap`, `excludedCommands`, "OS sandbox") and replace them with the template's guard text.
  4. Keep `docs/api.md` and `docs/guides/template-updates.md` deleted if Copier reintroduces them (R9).
- **Test scenarios:**
  - `tests/test_documentation.py` passes.
  - The strict MkDocs build finds no broken links or anchors.
- **Verification:** No conflict markers remain in the repository, and no doc page tells contributors to install bubblewrap or socat.

### U5. Verify locally and switch the local environment

- **Goal:** The branch passes the project gates and the guard works in a real session.
- **Requirements:** R3, R4; all units.
- **Dependencies:** U2, U3, U4.
- **Files:** none.
- **Approach:**
  1. Run the local gates in the Verification Contract: check, documentation and agent policy.
  2. The user commits the update through a `!` command.
  3. On the clean committed tree, run the template-link gate.
  4. The user runs `make setup` and restarts Claude Code in the knowledgebase.
  5. Smoke-check that a direct write to `pyproject.toml` is restored with the guard's message and that `make uv-lock` is allowed.
  6. The PR's CI gate runs only after the user approves opening the PR.
- **Test expectation:** none -- covered by the gates and the smoke check.
- **Verification:** Every gate passes, and the smoke check shows the guard restoring a protected write.

---

## Verification Contract

| Gate | Command | Proves |
|---|---|---|
| Lock, format, lint, typing, unit tests | `make check` | U2–U4 leave the project green on the active Python |
| Documentation | `make docs-build` | U4 links and anchors are valid |
| Agent policy | `make test-permissions` (needs `CODEX_TEST_BINARY`) | the guard and Codex rules hold; report as not run without a Codex CLI |
| Template link | `uvx --from 'copier>=9.18.2,<10' copier update --vcs-ref=v0.6.2 --defaults` on the committed, clean branch | R1: an update to the same tag reports no changes |
| CI | the pull request's checks | R6, R7 |

---

## Definition of Done

- No conflict markers remain, and `git status` shows only the intended changes.
- Every gate in the Verification Contract passes or is reported as not run with its reason.
- `.copier-answers.yml` records `_commit: "v0.6.2"` and the template source.
- The PR is open only after the user approves it, and its CI is green.
- No leftover trial files, `.rej` files or abandoned edits remain in the diff.
