# entra_realm

- **Commands:**
    - `curl -s https://login.microsoftonline.com/example.com/v2.0/.well-known/openid-configuration` → `openid-configuration.json`
    - `curl -s 'https://login.microsoftonline.com/getuserrealm.srf?login=user@example.com&json=1'` → `getuserrealm.json`
- **Version:** Microsoft identity platform v2.0 OpenID discovery document; `getuserrealm.srf` JSON (`json=1`).
- **Schema:** the OpenID configuration per
    [Microsoft identity platform OIDC](https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc):
    `issuer` and `token_endpoint` carry the tenant GUID. `getuserrealm.srf` is undocumented; its field list
    (`State`, `UserState`, `Login`, `NameSpaceType`, `DomainName`, `FederationBrandName`, `CloudInstanceName`,
    `CloudInstanceIssuerUri`, and for a federated domain `FederationGlobalVersion`, `AuthURL`, `AuthNForwardType`)
    comes from the live observations recorded in the catalog v3 upstream research (§16).

Derived from the documented schema, not recorded from a live target. The tenant id is a placeholder UUID.

The identity tenant is written from the OpenID document first; the realm batch then points `federates_with` at it by
reference, because `namespace_type` comes from the realm file. The federated `AuthURL` host becomes a `subdomain` of
the realm domain.
