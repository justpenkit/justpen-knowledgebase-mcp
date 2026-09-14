<!--
Thanks for opening a PR! Walk through the contributor checklist before
requesting review: docs/contributing/pr-checklist.md
-->

## Summary

<!-- What changes and why. One or two sentences is plenty. -->

## Testing

<!--
How did you verify this works? E.g.:
- Pre-push `make check` and `make docs-build` passed
- New/updated unit tests cover the change
- Relevant focused integration test passed when its test/harness changed
- CI runs the full integration suite and Python matrix
-->

## Checklist

- [ ] Branch follows `type/short-description` or `codex/short-description`.
- [ ] Commits use Conventional Commits (`type(scope): subject`).
- [ ] Pre-push `make check` and `make docs-build` passed.
- [ ] CI covers integration tests; relevant local test/harness checks are reported.
- [ ] Relevant docs updated (`docs/`, `README.md`, or `AGENTS.md` as appropriate).
- [ ] PR will be merged with a regular merge commit (not squash).

## Notes for the reviewer

<!-- Anything non-obvious: architectural tradeoffs, deliberately deferred work, follow-up issues. -->
