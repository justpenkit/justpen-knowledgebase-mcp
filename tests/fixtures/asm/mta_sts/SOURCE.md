# mta_sts

- **Commands:**
    - `curl -s https://mta-sts.example.com/.well-known/mta-sts.txt` → `mta-sts.txt`
    - `dig +short TXT _mta-sts.example.com` → `"v=STSv1; id=20260901T000000;"` (the announcing TXT record; not a
        fixture file, see the `derived` entries)
- **Version:** MTA-STS policy version `STSv1`; the body is the policy host's.
- **Schema:** the policy file grammar per [RFC 8461 §3.2](https://www.rfc-editor.org/rfc/rfc8461#section-3.2):
    `version`, `mode`, `max_age` and repeated `mx` keys, LF or CRLF terminated.

Derived from the documented schema, not recorded from a live target.

The `mta_sts_policy` node is keyed on the TXT value at `_mta-sts.example.com`, and its parent `domain` is the policy
host without its `mta-sts` label. The policy body names neither, so both are declared `derived` with that reason.
