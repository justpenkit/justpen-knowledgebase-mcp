# leak_corpus

- **Command:** `curl -s -H "X-API-Key: $LEAKCHECK_KEY" "https://leakcheck.io/api/v2/query/alice@example.com?type=email"`
- **Version:** LeakCheck Pro API v2
- **Schema:** [Pro lookup](https://docs.leakcheck.io/pro-api/lookup) and the OpenAPI
    [`ProResultRow` and `Source` schemas](https://docs.leakcheck.io/api-reference/pro-api-v2/pro-lookup):
    top-level `success`, `found`, `quota`, `result[]`; `source` holds `name`, `breach_date` (`YYYY-MM`,
    "when known"), `unverified`, `passwordless`, `compilation` (integers 0 or 1); a result row is open
    (`additionalProperties: true`) with representative keys `email`, `username`, `password`,
    `first_name`, `last_name`, `name`, `dob`, `address`, `zip`, `phone` and `fields[]`, plus `collected`
    on info-stealer rows.

Derived from the documented schema, not recorded from a live target. The password is the fixture
password `correct horse battery staple`; the hash is its unsalted SHA-1.

## Notes

- The first two rows are one credential pair (`alice`, one password) published in two breaches, so the
    password's `secret` node has two `authenticates` edges to the address, keyed on (`username`, `breach`).
- `breach` is `leakcheck:` plus the slug of `source.name`; LeakCheck publishes no separate breach id.
    `breach_title` keeps the name verbatim and `breach_date` keeps the month precision LeakCheck gives.
- `hashed_password` is not among the documented representative keys; it is the key the harness's
    `SECRET_FIELDS` names for a published password hash, and it is written with `kind: password_hash`,
    digested exactly as published.
- Names, date of birth, postal address and phone are personal PII and never become properties.
