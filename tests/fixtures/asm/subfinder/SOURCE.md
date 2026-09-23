# subfinder

- **Commands:**
    - `output.jsonl`: `subfinder -d example.com -json -oI -nW -o output.jsonl`
    - `sources.jsonl`: `subfinder -d example.com -json -cs -o sources.jsonl`
- **Version:** subfinder, `dev` branch at commit `20d140a`
- **Schema:** [`jsonSourceIPResult` (`-oI`) and `jsonSourcesResult` (`-cs`)](https://github.com/projectdiscovery/subfinder/blob/20d140af05d479a612626f8b1c0ca1e4e482f532/pkg/runner/outputter.go#L21-L41);
    `wildcard_certificate` is set when a source reported `*.<host>`
    ([enumerate.go](https://github.com/projectdiscovery/subfinder/blob/20d140af05d479a612626f8b1c0ca1e4e482f532/pkg/runner/enumerate.go)).

Derived from the documented schema, not recorded from a live target. Addresses are documentation ranges.

subfinder prints no timestamp, so the writes carry no `observed_at` and the server stamps the write time.
