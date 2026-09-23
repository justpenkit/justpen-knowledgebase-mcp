# katana

- **Command:** `katana -u https://www.example.com -jsonl -o output.jsonl -d 3 -jc -kf robotstxt -fx -td -kb -do`
- **Version:** katana v1.7.0 (`internal/runner/banner.go` at commit `c1de26d`)
- **Schema:**
    [`output.Result`](https://github.com/projectdiscovery/katana/blob/c1de26dbe69764a92dfa9a142eb64035e5d05a9d/pkg/output/result.go)
    (`timestamp`, `request`, `response`, `error`);
    [`navigation.Request`](https://github.com/projectdiscovery/katana/blob/c1de26dbe69764a92dfa9a142eb64035e5d05a9d/pkg/navigation/request.go#L13-L24)
    (`method`, `endpoint`, `body`, `headers`, `tag`, `attribute`, `source`, `raw`);
    [`navigation.Response`](https://github.com/projectdiscovery/katana/blob/c1de26dbe69764a92dfa9a142eb64035e5d05a9d/pkg/navigation/response.go#L14-L44)
    (`status_code`, `headers` lower-cased by `Headers.MarshalJSON`, `body`, `content_length`, `technologies`, `raw`,
    `forms[]`, `knowledgebase`);
    `(tag, attribute)` pairs from
    [`parser.go`](https://github.com/projectdiscovery/katana/blob/c1de26dbe69764a92dfa9a142eb64035e5d05a9d/pkg/engine/parser/parser.go)
    (`a`/`href`, `script`/`src`, `js`/`regex`, `form`/`action`) and
    [`files/robotstxt.go`](https://github.com/projectdiscovery/katana/blob/c1de26dbe69764a92dfa9a142eb64035e5d05a9d/pkg/engine/parser/files/robotstxt.go)
    (`file`/`robotstxt`); `source` is the URL of the page the link was found on, as the crawler requested it
    (so the seed page is `https://www.example.com` without a path);
    `forms[]` from [`utils/formfields.go`](https://github.com/projectdiscovery/katana/blob/c1de26dbe69764a92dfa9a142eb64035e5d05a9d/pkg/utils/formfields.go);
    `knowledgebase.PageType` from `BuildKnowledgeBase` in
    [`types/crawler_options.go`](https://github.com/projectdiscovery/katana/blob/c1de26dbe69764a92dfa9a142eb64035e5d05a9d/pkg/types/crawler_options.go);
    the `-do` out-of-scope result with `error: "out of scope"` and no response from `Shared.Output` in
    [`engine/common/base.go`](https://github.com/projectdiscovery/katana/blob/c1de26dbe69764a92dfa9a142eb64035e5d05a9d/pkg/engine/common/base.go)
    and [`engine/common/error.go`](https://github.com/projectdiscovery/katana/blob/c1de26dbe69764a92dfa9a142eb64035e5d05a9d/pkg/engine/common/error.go).

Derived from the documented schema, not recorded from a live target. Addresses are documentation names.

The POST body `username=katana&password=katanaP%40assw0rd1` is katana's built-in form-fill default
(`pkg/utils/formfill.go`), not a credential of the target. Fields this command does not emit are not mapped:
`request.custom_fields` needs `-flc`, `response.xhr_requests` needs `-headless -xhr`, and
`response.stored_response_path` needs `-sr`.
