## v0.4.0 (2026-09-13)

### Feat

- add Python HTML and CSS formatting

### Fix

- complete shared development tooling parity
- create release tags after reviewed merges

### Refactor

- align hooks, CI and commit validation

## v0.3.1 (2026-09-11)

### Fix

- preserve Copier answer strings during formatting
- backport template validation and setup improvements

## v0.3.0 (2026-09-10)

### Feat

- simplify MCP tooling with uv and MkDocs
- generate MCP projects with Copier
- support Claude and Codex with protected config approvals

### Fix

- propagate MCP server failures to the CLI
- run commit validation with uv and clarify setup docs
- align agent workflows with Make targets
- bootstrap from the copied template without extra secrets
- support private template sources in GitHub setup

### Refactor

- remove unused artifacts and share template setup

## v0.2.0 (2026-04-26)

### Feat

- **docs**: trim landing CardGrid to PR checklist + Lint & typing
- **docs**: trim Starlight sidebar to PR checklist + Lint & typing
- **docs**: convert landing index to MDX with CardGrid contributor links
- **docs**: add custom CSS palette + GitHub expressive-code themes
- **docs**: add placeholder logo + favicon (downstream replaces)
- **docs**: add lastUpdated timestamps + engines.node>=24
- **docs**: drop starlight editLink — no per-page edit button
- **template-init**: pin downstream pyproject version to 0.1.0
- **docs**: migrate contributing docs to starlight content collection
- **docs**: migrate landing page to starlight content collection
- **docs**: scaffold astro starlight project under docs/

### Fix

- pin package.json name to stop lockfile drift across worktrees
- **ci**: pass --root-dir to lychee so absolute paths resolve
- drop --group docs from make setup, update CLAUDE.md docs reference
- **docs**: pin zod to v3 and sitemap to 3.7.0 via npm overrides

## v0.1.0 (2026-04-25)

### Feat

- initial template setup
