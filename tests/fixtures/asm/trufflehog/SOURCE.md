# trufflehog

- **Commands:**
    - `git.jsonl`: `trufflehog git https://github.com/Example-Org/Web-App.git --json --no-update`
    - `s3.jsonl`: `trufflehog s3 --role-arn=arn:aws:iam::123456789012:role/trufflehog-audit --json --no-update`
- **Version:** trufflehog v3.97.6
- **Schema:**
    [`JSONPrinter` output struct](https://github.com/trufflesecurity/trufflehog/blob/v3.97.6/pkg/output/json.go);
    `SourceMetadata` per [`source_metadata.proto`](https://github.com/trufflesecurity/trufflehog/blob/v3.97.6/proto/source_metadata.proto)
    (`Git`: commit, file, email, repository, timestamp, line, repository_local_path; `S3`: bucket, file, link, email,
    timestamp; protobuf `omitempty` drops empty members);
    git `timestamp` spelled `2006-01-02 15:04:05 -0700` per
    [`git.go`](https://github.com/trufflesecurity/trufflehog/blob/v3.97.6/pkg/sources/git/git.go), S3 `timestamp`
    (`LastModified.String()`), `email` (object owner display name) and `link` per
    [`s3.go`](https://github.com/trufflesecurity/trufflehog/blob/v3.97.6/pkg/sources/s3/s3.go);
    AWS `Raw`, `RawV2`, `Redacted`, `ExtraData` and `SecretParts` per
    [`accesskey.go`](https://github.com/trufflesecurity/trufflehog/blob/v3.97.6/pkg/detectors/aws/access_keys/accesskey.go#L149-L161)
    and [`aws/utils.go`](https://github.com/trufflesecurity/trufflehog/blob/v3.97.6/pkg/detectors/aws/utils.go#L17-L31);
    numeric `DetectorType` (AWS = 2) and `SourceType` (S3 = 13, GIT = 16) from `detector_type.pb.go` and `sources.pb.go`
    at the same tag.

Derived from the documented schema, not recorded from a live target. The credential is the AWS
documentation example pair.

## Notes

- `AKIAIOSFODNN7EXAMPLE` has `I` as its fifth character, so trufflehog cannot decode an account from the
    key id and sets `ExtraData.account` only after verification (`GetCallerIdentity`, which also adds
    `user_id`, `arn` and `rotation_guide`). Both results are therefore `Verified: true`; the git result's
    second occurrence and the S3 result reuse the cached verification (`VerificationFromCache`).
- The digest is the SHA-256 of the secret access key alone (`trufflehog_secret_part` takes the part of
    `RawV2` after the colon); `key_id` is `Raw`, the public key id. `RawV2`, `Redacted` and every
    `SecretParts` member are redacted before the output is ingested as evidence.
- One key at two paths of the repository is one `secret` node with two `exposes_secret` edges, keyed on
    `<file>:<line>`. The S3 hit is a third edge, from the bucket, keyed on the object key.
- **`in_account` attribution.** The S3 scan ran with `--role-arn` of account 123456789012 and no
    `--bucket`, so trufflehog enumerated the buckets that account's `ListBuckets` returns, which are the
    buckets it owns. The key found in the object decodes, through verification, to the same account
    (`ExtraData.account`, and the account field of `ExtraData.arn`). The writer adds
    `storage_bucket -> in_account -> cloud_account` only when those two agree; a bucket reached with
    `--bucket` alone, or whose key names another account, keeps the key's account on the `authenticates`
    edge only.
- A PrivateKey result (`Raw` a PEM ending in a newline) is not included: its `RawV2` is the empty string,
    and the integration test's evidence check treats every redacted string, the empty one included, as a
    forbidden substring of the stored evidence, which the empty string always is.
