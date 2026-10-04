# Claude Code integration

Shared development rules are loaded by root `CLAUDE.md` through `@AGENTS.md`.
Do not duplicate them here. See `docs/contributing/agents.md`
for setup and the protected-files guard. The guard reverts agent writes to
protected configuration files: use the `make` targets it documents (for example
`make uv-add`) or ask the user, and never retry a reverted write.

Use pyright LSP when the installed plugin exposes it. Treat diagnostic error
reminders as blockers. If a plugin is unavailable, use the shared command-line
checks and report the missing capability without blocking routine work.

The pyright LSP and Compound Engineering plugin preferences remain in
`.claude/settings.json`; their availability does not change the shared policy.
