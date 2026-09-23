# rdap_ip

- **Command:** `curl -s -H 'Accept: application/rdap+json' https://rdap.arin.net/registry/ip/198.51.100.0` → `ip-198.51.100.0.json`
- **Version:** RDAP level 0 with the NRO RDAP profile (`nro_rdap_profile_0`) and the `cidr0` extension; the body is
    the registry server's.
- **Schema:** the IP network object per [RFC 9083 §5.4](https://www.rfc-editor.org/rfc/rfc9083#section-5.4),
    entities per [§5.1](https://www.rfc-editor.org/rfc/rfc9083#section-5.1) with jCard `vcardArray` per
    [RFC 7095](https://www.rfc-editor.org/rfc/rfc7095). The ARIN shape, a holder org (`registrant`, vCard `kind`
    `org`) that nests its points of contact, follows ARIN's response for
    [192.0.2.0](https://rdap.arin.net/registry/ip/192.0.2.0).

Derived from the documented schema, not recorded from a live target. `country` is an RFC 9083 member that ARIN omits;
it is included to exercise `ip_cidr.country`. The RIR comes from `port43`, the registry's own WHOIS server.
