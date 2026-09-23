# github_repository

- **Command:** `curl -s -H "Accept: application/vnd.github+json" -H "X-GitHub-Api-Version: 2022-11-28" https://api.github.com/repos/Example-Org/Web-App`
- **Version:** GitHub REST API 2022-11-28, unauthenticated
- **Schema:** [Get a repository](https://docs.github.com/en/rest/repos/repos#get-a-repository), the
    `full-repository` object. The member list and order follow the unauthenticated response for an
    organization-owned public repository (`owner.type` `Organization`, so `organization` repeats the
    owner, and `custom_properties` holds the organization's property values). Authenticated-only members
    (`permissions`, `security_and_analysis`) are absent; `temp_clone_token` is `null` without a token.

Derived from the documented schema, not recorded from a live target. Owner, name, ids, counts and times
are fixture values.

## Notes

- The repository identity is the lowercase host, owner and name: `github.com`, `example-org`, `web-app`,
    although GitHub echoes the owner's and the name's display case.
- `full_name` repeats owner and name, and `private` is implied by `visibility`, so neither is stored.
- Every URL template, count, flag and timestamp stays in the evidence.
- The `visibility` enum (`public`, `private`, `internal`) is taken from the catalog; the research did not
    fetch it from the docs page.
