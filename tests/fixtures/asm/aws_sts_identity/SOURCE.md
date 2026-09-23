# aws_sts_identity

- **Command:** `aws sts get-caller-identity --output json`
- **Version:** AWS CLI v2, STS API version 2011-06-15
- **Schema:** [GetCallerIdentity response elements](https://docs.aws.amazon.com/STS/latest/APIReference/API_GetCallerIdentity.html):
    `UserId`, `Account` and `Arn`, which the CLI prints as JSON with the same PascalCase keys.

Derived from the documented schema, not recorded from a live target. `UserId` and the account are the
documentation example values; the user name in `Arn` is a fixture name.

## Notes

- `Account` is the 12-digit account id, kept as a string because leading zeros are significant. The
    account field of `Arn` is the same account and maps to the same property.
- `UserId` identifies the calling principal, not the account, and stays in the evidence.
