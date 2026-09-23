# dnsx

- **Command:** `dnsx -l hosts.txt -json -a -aaaa -cname -ns -txt -mx -soa -caa -cdn -asn -o output.jsonl`
- **Version:** dnsx, `dev` branch at commit `d261a79`, with retryabledns v1.0.116
- **Schema:** [`ResponseData`](https://github.com/projectdiscovery/dnsx/blob/d261a79266e86040b7e2b371e2d802084b21ec95/libs/dnsx/dnsx.go#L37-L51)
    embedding [`retryabledns.DNSData`](https://github.com/projectdiscovery/retryabledns/blob/v1.0.116/client.go#L734-L768);
    `all` holds each answer RR in miekg/dns presentation form (`ParseFromRR`, same file), and `raw` is blanked
    without `-raw` ([runner.go](https://github.com/projectdiscovery/dnsx/blob/d261a79266e86040b7e2b371e2d802084b21ec95/internal/runner/runner.go)).

Derived from the documented schema, not recorded from a live target. Addresses are documentation ranges.

- `mx` holds exchanges only and `caa` holds values only (no flags, no tag), so the MX preference and every
    CAA fact are read from the tagged records in `all`.
- dnsx has no wildcard field: `-wd` and `-auto-wildcard` remove lines instead, so no row feeds `wildcard`.
- The `_dmarc.example.com` answer is attached to `example.com` through `has_dmarc`; no `_dmarc` subdomain
    node is written.
- `app.example.com` CNAMEs to the NLB default hostname `my-lb-1234567890abcdef.elb.us-east-2.amazonaws.com`
    ([ELB DNS names](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/network-load-balancers.html#dns-name)),
    written as a `cloud_resource` with the `region` its hostname encodes and reached through `hosted_on`.
