# httpx

- **Command:** `httpx -json -irh -title -status-code -content-length -content-type -web-server -tech-detect -cdn -favicon -hash sha256 -jarm -ip -cname -asn -location -l hosts.txt`
- **Version:** httpx v1.12.0
- **Schema:** [`runner.Result`](https://github.com/projectdiscovery/httpx/blob/d3b9d3d3443cfbd32513999a6cef1e5ce8ba256d/runner/types.go#L24-L125);
    `host` and `host_ip` per [runner.go](https://github.com/projectdiscovery/httpx/blob/v1.12.0/runner/runner.go#L2699-L2700);
    `tech` entries per [wappalyzergo `FormatAppVersion`](https://github.com/projectdiscovery/wappalyzergo/blob/main/fingerprints.go#L510-L515).

Derived from the documented schema, not recorded from a live target. Addresses are documentation ranges.
