# rdap_domain

- **Commands:**
    - `curl -s -H 'Accept: application/rdap+json' https://rdap.verisign.com/com/v1/domain/example.com` → `example.com.json`
    - `curl -s -H 'Accept: application/rdap+json' https://rdap.identitydigital.services/rdap/domain/example.io` → `example-nohandle.json`
    - `curl -s -H 'Accept: application/rdap+json' https://rdap.publicinterestregistry.org/rdap/domain/example.org` → `example-placeholder.json`
- **Version:** RDAP level 0 with the ICANN gTLD RDAP response profile (`icann_rdap_response_profile_1`); the body is the
    registry server's, so the HTTP client version does not change it.
- **Schema:** the domain object members per [RFC 9083 §5.3](https://www.rfc-editor.org/rfc/rfc9083#section-5.3)
    (`eventAction`, `status` and `roles` values per §10.2), RDAP status to EPP per
    [RFC 8056 §2](https://www.rfc-editor.org/rfc/rfc8056#section-2), and the `redacted` array per
    [RFC 9537 §4.2](https://www.rfc-editor.org/rfc/rfc9537#section-4.2). Shapes follow the registry responses at
    [Verisign](https://rdap.verisign.com/com/v1/domain/google.com) (registrar entity with the IANA Registrar ID
    publicId and a nested abuse entity) and [Identity Digital](https://rdap.identitydigital.services/rdap/domain/google.io)
    (no `handle`, `redacted` names `$.handle`).

Derived from the documented schema, not recorded from a live target.

- `example.com.json` is a gTLD response with a real registry handle and registrant, administrative and technical
    contact entities. It carries `port43`, which live Verisign responses omit, because `whois_server` holds the
    registry's WHOIS server and RDAP `port43` is its only source.
- `example-nohandle.json` is a ccTLD response with no `handle` whose `redacted` array names `$.handle`.
- `example-placeholder.json` carries the placeholder handle `REDACTED-REDACTED`, which the `roid` shape accepts and
    `registry_domain_id_assigned.1` refuses.

The last two write only the `domain` and its `has_nameserver` edges: rows that feed the registration, the registrar
and the registration contacts require `rdap_registration_accepted`, so their values stay in evidence.
