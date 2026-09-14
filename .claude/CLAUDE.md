# Claude Code integration

Shared development rules are loaded by root `CLAUDE.md` through `@AGENTS.md`.
Do not duplicate them here. See `docs/contributing/agents.md`
for setup and permission behavior.

Use pyright LSP when the installed plugin exposes it. Treat diagnostic error
reminders as blockers. If a plugin is unavailable, use the shared command-line
checks and report the missing capability without blocking routine work.

The existing Superpowers, pyright LSP, and Caveman plugin preferences remain in
`.claude/settings.json`; their availability does not change the shared policy.
