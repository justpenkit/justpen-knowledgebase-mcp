# bbot

- **Command:** `bbot -t example.com example.org -p subdomain-enum spider -m portscan http nuclei trufflehog badsecrets -om json -y`
    (the `json` output module writes newline-delimited events to `output.json` in the scan folder; the
    fixture file is named `output.jsonl` so the repository's JSON formatter does not parse it as one document)
- **Version:** BBOT 3.0.2 (branch `stable`, commit `a6fb827`); its default `modules.trufflehog.only_verified: true`
- **Schema:**
    [`Event.json()`](https://github.com/blacklanternsecurity/bbot/blob/a6fb827bb144cdb85b52e142a4d6e14ed5f94b69/bbot/core/event/base.py#L919-L1006):
    string data under `data`, dict data under `data_json`, `timestamp` a float epoch, `tags` sorted, and
    `parent` / `parent_uuid` skipping omitted event types (`HTTP_RESPONSE`, `URL_UNVERIFIED`, per
    `omit_event_types` in `bbot/defaults.yml`), so a spidered URL's parent is the page URL;
    [event types and the `data`/`data_json` rule](https://github.com/blacklanternsecurity/bbot/blob/a6fb827bb144cdb85b52e142a4d6e14ed5f94b69/docs/scanning/events.md);
    [`FINDING`](https://github.com/blacklanternsecurity/bbot/blob/a6fb827bb144cdb85b52e142a4d6e14ed5f94b69/bbot/core/event/base.py#L1856-L1975)
    (`host`, `severity`, `name`, `description`, `confidence`, `url`, and the optional `full_url`, `path`,
    `cves`, `archive_url`), with the enums in
    [`validators.py`](https://github.com/blacklanternsecurity/bbot/blob/a6fb827bb144cdb85b52e142a4d6e14ed5f94b69/bbot/core/helpers/validators.py#L164-L183)
    (there is no `VULNERABILITY` type in 3.x);
    FINDING text per module:
    [trufflehog](https://github.com/blacklanternsecurity/bbot/blob/a6fb827bb144cdb85b52e142a4d6e14ed5f94b69/bbot/modules/trufflehog.py#L117-L206)
    (the raw secret is inside `description`, and the discovery context quotes it),
    [badsecrets](https://github.com/blacklanternsecurity/bbot/blob/a6fb827bb144cdb85b52e142a4d6e14ed5f94b69/bbot/modules/badsecrets.py#L70-L113)
    (the known secret is inside `description` and the discovery context),
    [nuclei](https://github.com/blacklanternsecurity/bbot/blob/a6fb827bb144cdb85b52e142a4d6e14ed5f94b69/bbot/modules/nuclei.py#L153-L292)
    (`template: [<id>], name: [<matcher>]`);
    DNS wildcard tags (`wildcard`, `wildcard-possible`, `<rdtype>-wildcard`) and the `_wildcard.<zone>` rewrite
    of a name whose every record is a wildcard answer, from `handle_wildcard_event` in
    [`dnsresolve.py`](https://github.com/blacklanternsecurity/bbot/blob/a6fb827bb144cdb85b52e142a4d6e14ed5f94b69/bbot/modules/internal/dnsresolve.py#L158-L197)
    and `is_wildcard` in
    [`helpers/dns/dns.py`](https://github.com/blacklanternsecurity/bbot/blob/a6fb827bb144cdb85b52e142a4d6e14ed5f94b69/bbot/core/helpers/dns/dns.py#L177-L260);
    URL `data_json` (`url`, `http_title`, `hash`, `redirect_location`) and the `status-<code>` tag from
    [`modules/http.py`](https://github.com/blacklanternsecurity/bbot/blob/a6fb827bb144cdb85b52e142a4d6e14ed5f94b69/bbot/modules/http.py#L230-L250)
    and [`response_event.py`](https://github.com/blacklanternsecurity/bbot/blob/a6fb827bb144cdb85b52e142a4d6e14ed5f94b69/bbot/core/helpers/web/response_event.py).

Derived from the documented schema, not recorded from a live target. Addresses are documentation ranges; the
secrets are the AWS documentation example key pair and the password `correct horse battery staple`.

The fixture holds the event types in scope (`DNS_NAME`, `OPEN_TCP_PORT`, `URL`, `FINDING`); the `SCAN`,
`IP_ADDRESS`, `TECHNOLOGY` and other events of the same scan are left out. `host_metadata` appears only when
cloudcheck matches a provider, which no documentation address does, so its rows are `documented_only`.
