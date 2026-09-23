# rdap_autnum

- **Command:** `curl -s -H 'Accept: application/rdap+json' https://rdap.db.ripe.net/autnum/64496` → `as64496.json`
- **Version:** RDAP level 0 with the NRO RDAP profile (`nro_rdap_profile_0`, `nro_rdap_profile_asn_flat_0`) and RFC
    9537 `redacted`; the body is the registry server's.
- **Schema:** the autnum object per [RFC 9083 §5.5](https://www.rfc-editor.org/rfc/rfc9083#section-5.5), entities per
    [§5.1](https://www.rfc-editor.org/rfc/rfc9083#section-5.1) with jCard `vcardArray` per
    [RFC 7095](https://www.rfc-editor.org/rfc/rfc7095), and `redacted` per
    [RFC 9537 §4.2](https://www.rfc-editor.org/rfc/rfc9537#section-4.2). The RIPE shape, with the holder org,
    maintainers (also `registrant`, vCard `kind` `individual`), admin/tech and abuse roles as top-level entities,
    follows RIPE's response for [AS3333](https://rdap.db.ripe.net/autnum/3333).

Derived from the documented schema, not recorded from a live target. AS64496 is an RFC 5398 documentation number.
`country` is an RFC 9083 member that RIPE omits; it is included to exercise `asn.country`. The RIR comes from `port43`.
