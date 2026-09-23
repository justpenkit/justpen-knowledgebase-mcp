# whois_gtld

- **Command:** `whois -h whois.verisign-grs.com example.com` → `example.com.txt`
- **Version:** the registry's port-43 output in the ICANN RDDS labeling-policy key names; the client version does not
    change it.
- **Schema:** key names per the [RDDS labeling policy](https://www.icann.org/resources/pages/rdds-labeling-policy-2024-02-21-en)
    and the [2013 RAA specifications](https://www.icann.org/resources/pages/approved-with-specs-2013-09-17-en);
    `Domain Status` and `DNSSEC` spellings per the
    [2024 advisory §2.5 and §2.8](https://www.icann.org/en/contracted-parties/advisories/documents/advisory-clarifications-to-the-registry-and-registrar-requirements-for-whois-data-directory-services-21-02-2024-en).

Derived from the documented schema, not recorded from a live target.

The queried server, `whois.verisign-grs.com`, is recorded here and is not mapped: the text format has no
registry-server line, and `whois_server` is fed by the RDAP `port43` of `rdap_domain` only. `Registrar WHOIS Server`
deliberately differs from it (`whois.registrar.example`) and stays in evidence. `Registry Domain ID` is the handle of
`rdap_domain/example.com.json`, so both views key the same `whois_registration`. The `>>> Last update of WHOIS database`
footer and the `URL of the ICANN Whois Inaccuracy Complaint Form` line are notices the parser skips.
