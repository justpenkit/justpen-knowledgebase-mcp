# nuclei

- **Command:** `nuclei -l targets.txt -t http/,headless/,ssl/,network/,javascript/,dns/ -t /home/analyst/custom-templates/whois-registrar-expiry.yaml -headless -jsonl -o scan.jsonl`, then `nuclei -l targets.txt -dast -jsonl -o dast.jsonl` (with `-dast` nuclei loads only fuzzing templates, so the DAST result needs its own run), concatenated into `output.jsonl`
    (`targets.txt` lists `https://www.example.com/`, `https://confluence.example.com/`,
    `https://www.example.com/search?q=test`, `a.example.com:443`, `b.example.com:443`, `192.0.2.10:443`,
    `192.0.2.20:22` and `example.com`)
- **Version:** nuclei v3.11.1 (fields checked at `dev` commit `a5b59a9`). Request/response pairs are
    included by default in v3; `-irr` is deprecated and defaults to true, and only `-omit-raw` removes them
    ([main.go](https://github.com/projectdiscovery/nuclei/blob/v3.11.1/cmd/nuclei/main.go#L335-L336),
    [format_json.go](https://github.com/projectdiscovery/nuclei/blob/v3.11.1/pkg/output/format_json.go)).
- **Schema:**
    [`output.ResultEvent`](https://github.com/projectdiscovery/nuclei/blob/a5b59a9a4e8bba485fab3a75bea18d3b642e927a/pkg/output/output.go);
    `info` per [`model.Info` and `model.Classification`](https://github.com/projectdiscovery/nuclei/blob/a5b59a9a4e8bba485fab3a75bea18d3b642e927a/pkg/model/model.go)
    (`tags`, `author`, `cve-id` and `cwe-id` are lower-cased JSON arrays and an unset one prints `null`, per
    [`stringslice.go`](https://github.com/projectdiscovery/nuclei/blob/a5b59a9a4e8bba485fab3a75bea18d3b642e927a/pkg/model/types/stringslice/stringslice.go#L63-L142));
    `severity` per [`severity.go`](https://github.com/projectdiscovery/nuclei/blob/a5b59a9a4e8bba485fab3a75bea18d3b642e927a/pkg/model/types/severity/severity.go#L32-L38);
    `type` values per [`types.go`](https://github.com/projectdiscovery/nuclei/blob/a5b59a9a4e8bba485fab3a75bea18d3b642e927a/pkg/templates/types/types.go#L50-L61)
    (the network protocol reports `tcp`);
    per-protocol fields from each `MakeResultEventItem`
    ([http](https://github.com/projectdiscovery/nuclei/blob/a5b59a9a4e8bba485fab3a75bea18d3b642e927a/pkg/protocols/http/operators.go),
    [headless](https://github.com/projectdiscovery/nuclei/blob/a5b59a9a4e8bba485fab3a75bea18d3b642e927a/pkg/protocols/headless/operators.go),
    [ssl](https://github.com/projectdiscovery/nuclei/blob/a5b59a9a4e8bba485fab3a75bea18d3b642e927a/pkg/protocols/ssl/ssl.go),
    [network](https://github.com/projectdiscovery/nuclei/blob/a5b59a9a4e8bba485fab3a75bea18d3b642e927a/pkg/protocols/network/operators.go),
    [javascript](https://github.com/projectdiscovery/nuclei/blob/a5b59a9a4e8bba485fab3a75bea18d3b642e927a/pkg/protocols/javascript/js.go),
    [dns](https://github.com/projectdiscovery/nuclei/blob/a5b59a9a4e8bba485fab3a75bea18d3b642e927a/pkg/protocols/dns/operators.go),
    [whois](https://github.com/projectdiscovery/nuclei/blob/a5b59a9a4e8bba485fab3a75bea18d3b642e927a/pkg/protocols/whois/whois.go)):
    ssl results carry no request or response, headless results no request, and whois results no request,
    `matched-at` or `ip`; `matcher-name` / `extractor-name` per
    [`protocols.go`](https://github.com/projectdiscovery/nuclei/blob/a5b59a9a4e8bba485fab3a75bea18d3b642e927a/pkg/protocols/protocols.go#L425-L450);
    `template` and `template-url` only for signed ProjectDiscovery templates and `template-encoded` only for
    other templates ([compile.go](https://github.com/projectdiscovery/nuclei/blob/a5b59a9a4e8bba485fab3a75bea18d3b642e927a/pkg/templates/compile.go#L700-L745));
    fuzzing fields per [`request_fuzz.go`](https://github.com/projectdiscovery/nuclei/blob/a5b59a9a4e8bba485fab3a75bea18d3b642e927a/pkg/protocols/http/request_fuzz.go#L213-L215).
- **Templates** (info blocks copied from `projectdiscovery/nuclei-templates` `main`):
    `http-missing-security-headers`, `CVE-2021-26084`, `prototype-pollution-check`, `reflected-xss` (DAST),
    `weak-cipher-suites` (two virtual hosts on `192.0.2.10:443`), `deprecated-tls` (the bare address),
    `openssh-detect` (`tcp`), `ssh-password-auth` (`javascript`), `dmarc-detect` (`dns`), and the custom
    `whois-registrar-expiry` (`whois`, severity `unknown`), whose YAML is the decoded `template-encoded`.

Derived from the documented schema, not recorded from a live target. Addresses are documentation ranges.

Fields the recorded flags do not produce here are mapped `documented_only`: `interaction` (interactsh
templates), `global-matchers` and `analyzer_details`. `matched-line` (file templates), `error` (only with
`-ms`), `issue_trackers` (only with a reporting config), `req_url_pattern` (a debug export) and `llm` (LLM
matchers) are outside the recorded flags and not mapped.
