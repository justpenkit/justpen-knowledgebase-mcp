# tlsx

- **Command:** `tlsx -l hosts.txt -p 443,8443 -sm ztls -json -san -cn -so -tv -cipher -hash sha256 -jarm -ja3 -ja3s -tps -se -ve -ce -re -o output.jsonl`
- **Version:** tlsx v1.4.0 (commit `ffe1cfe`)
- **Schema:** [`clients.Response` and `CertificateResponse`](https://github.com/projectdiscovery/tlsx/blob/ffe1cfef11fc71fd7b73e41c603c18bebfb28258/pkg/tlsx/clients/clients.go#L195-L353);
    `serial` per [`FormatToSerialNumber`](https://github.com/projectdiscovery/tlsx/blob/ffe1cfef11fc71fd7b73e41c603c18bebfb28258/pkg/tlsx/clients/utils.go#L124-L139);
    the ztls client, `sni` and `ja3s_hash` per [ztls.go](https://github.com/projectdiscovery/tlsx/blob/ffe1cfef11fc71fd7b73e41c603c18bebfb28258/pkg/tlsx/ztls/ztls.go).

Derived from the documented schema, not recorded from a live target. Addresses are documentation ranges.

- JSON output always carries the whole response; the flags choose what is computed (`jarm_hash`,
    `ja3s_hash`, `version_enum`, `cipher_enum`). Certificate booleans are `omitempty`, so a false value is absent.
- ztls sets no `key_exchange`, which only ctls fills, and the `-re` revocation result appears only when true.
- The ztls client config sets no NextProtos, so `presents_certificate.alpn_offered` is `[]`. An IP target
    sends no SNI, so `sni` is absent and `server_name` is `""`.
- tlsx identifies no application protocol, so the service is the registry's `unknown` with `secure: true`.
