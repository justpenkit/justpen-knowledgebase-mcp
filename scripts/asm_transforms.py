"""Deterministic transforms from ASM and OSINT tool output fields to catalog property values.

The coverage fixtures under `tests/fixtures/asm/` map every output field of a tool to a catalog sink,
optionally through a chain of these transforms. They are the only way a mapped value may change on
its way into a property, so a reviewer reads one closed set instead of ad hoc code per fixture.
`scripts/catalog_reference.py` renders the glossary from `TRANSFORMS` and the docstrings.

This module is test and documentation tooling, not an ingestion adapter: nothing here writes to a
knowledge base. Every transform takes the field value and the whole record it came from, because a
few spellings need a sibling field (an SNI host, the zone of a domain).
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import cast

from justpen_knowledgebase_mcp.catalog import catalog_view, validate_record
from justpen_knowledgebase_mcp.errors import ExpectedValidationError
from justpen_knowledgebase_mcp.psl import classify_dns_name

Transform = Callable[[object, Mapping[str, object]], object]
TRANSFORMS: dict[str, Transform] = {}


def transform(function: Transform) -> Transform:
    """Register one transform under its function name."""
    TRANSFORMS[function.__name__] = function
    return function


def _items(value: object) -> list[object]:
    """A list value as its members, any other value as a one-member list."""
    return list(cast("list[object]", value)) if isinstance(value, list) else [value]


def apply_chain(value: object, chain: list[str], record: Mapping[str, object]) -> object:
    """Apply transforms left to right; an unknown name is a mapping error, not a silent pass.

    A transform returns None for a record that carries no value for the sink, and the chain stops there.
    """
    for name in chain:
        if value is None:
            return None
        value = TRANSFORMS[name](value, record)
    return value


@transform
def int_value(value: object, _record: Mapping[str, object]) -> int:
    """Parse a decimal string, such as nuclei's string `port`, into an integer."""
    if type(value) is int:
        return value
    return int(str(value), 10)


@transform
def float_value(value: object, _record: Mapping[str, object]) -> float:
    """Parse a numeric string, such as an EPSS `epss` field, into a number."""
    return float(str(value))


@transform
def lower(value: object, _record: Mapping[str, object]) -> str:
    """Lowercase a string."""
    return str(value).lower()


@transform
def upper(value: object, _record: Mapping[str, object]) -> str:
    """Uppercase a string, such as the `cve-2021-44228` nuclei lowercases, back to MITRE's spelling."""
    return str(value).upper()


@transform
def strip_trailing_dot(value: object, _record: Mapping[str, object]) -> str:
    """Drop the root dot of a fully qualified DNS name and lowercase it."""
    return str(value).rstrip(".").lower()


@transform
def url_without_query(value: object, _record: Mapping[str, object]) -> str:
    """Drop the query and fragment of a URL; the catalog keeps one endpoint per path."""
    return str(value).split("#", 1)[0].split("?", 1)[0]


@transform
def url_host(value: object, _record: Mapping[str, object]) -> str:
    """The lowercase host of an absolute URL, without port or brackets."""
    match = re.match(r"[a-z]+://(\[[^\]]+\]|[^/:?#]+)", str(value), re.IGNORECASE)
    if match is None:
        raise ValueError("not an absolute URL")
    return match.group(1).strip("[]").lower()


@transform
def url_with_path(value: object, _record: Mapping[str, object]) -> str:
    """Give a bare origin (`https://example.com`) the root path the catalog requires."""
    text = str(value)
    return text if re.match(r"[a-z]+://[^/]+/", text) else text + "/"


@transform
def sha256_hex(value: object, _record: Mapping[str, object]) -> str:
    """Lowercase hex SHA-256 of the UTF-8 bytes, untrimmed; CRLF becomes LF first, nothing else changes."""
    return hashlib.sha256(str(value).replace("\r\n", "\n").encode("utf-8")).hexdigest()


@transform
def hex_prefix16(value: object, _record: Mapping[str, object]) -> str:
    """The first 16 characters of a hex digest."""
    return str(value)[:16]


@transform
def digest16(value: object, _record: Mapping[str, object]) -> str:
    """First 16 lowercase hex characters of the SHA-256 of the exact UTF-8 bytes, a bounded discriminator."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


@transform
def openssh_b64_to_hex(value: object, _record: Mapping[str, object]) -> str:
    """Convert OpenSSH's `SHA256:<base64>` fingerprint into 64 lowercase hex characters."""
    text = str(value).removeprefix("SHA256:")
    return base64.b64decode(text + "=" * (-len(text) % 4)).hex()


@transform
def wappalyzer_name_slug(value: object, _record: Mapping[str, object]) -> str:
    """`Nginx:1.18.0` or `Microsoft ASP.NET` becomes a tech_token: lowercase, runs of other characters `-`."""
    name = str(value).split(":", 1)[0]
    return re.sub(r"[^a-z0-9._+]+", "-", name.lower()).strip("-.")


@transform
def wappalyzer_version(value: object, _record: Mapping[str, object]) -> str | None:
    """The version half of an httpx `Name:Version` technology entry; None when it reports no version."""
    name, separator, version = str(value).partition(":")
    return version if separator and name else None


@transform
def cpe22_uri_to_cpe23(value: object, _record: Mapping[str, object]) -> str:
    """Nmap prints `cpe:/a:vendor:product:version`; bind it as a CPE 2.3 formatted string."""
    parts = str(value).removeprefix("cpe:/").split(":")
    parts += ["*"] * (11 - len(parts))
    return "cpe:2.3:" + ":".join(part or "*" for part in parts)


@transform
def cpe_product_level(value: object, _record: Mapping[str, object]) -> str:
    """Set the version attribute of a CPE 2.3 string to `*`, for the shared technology node."""
    parts = str(value).split(":")
    parts[5] = "*"
    return ":".join(parts)


_RDAP_TO_EPP = {"active": "ok", "associated": "linked"}


@transform
def rdap_status_to_epp(value: object, _record: Mapping[str, object]) -> list[str]:
    """RFC 8056: RDAP `client transfer prohibited` becomes EPP `clientTransferProhibited`."""
    statuses: list[str] = []
    for status in _items(value):
        text = str(status)
        if text in _RDAP_TO_EPP:
            statuses.append(_RDAP_TO_EPP[text])
            continue
        first, *rest = text.split()
        statuses.append(first + "".join(word.capitalize() for word in rest))
    return statuses


@transform
def whois_status_to_epp(value: object, _record: Mapping[str, object]) -> list[str]:
    """Keep the EPP code, the first token, of each WHOIS `Domain Status:` line."""
    return [str(item).split()[0] for item in _items(value)]


@transform
def whois_dnssec_bool(value: object, _record: Mapping[str, object]) -> bool:
    """`unsigned` is false and `signedDelegation` true; any other spelling fails the mapping."""
    return {"unsigned": False, "signedDelegation": True}[str(value)]


@transform
def rfc3339_utc(value: object, _record: Mapping[str, object]) -> str:
    """Normalize a timestamp to the catalog's `YYYY-MM-DDTHH:MM:SS[.f]Z`; a naive one is taken as UTC."""
    # Go prints nanoseconds; Python parses at most microseconds, and the catalog keeps six digits.
    text = re.sub(r"(\.[0-9]{6})[0-9]+", r"\1", str(value).strip())
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    fraction = f".{parsed.microsecond:06d}".rstrip("0") if parsed.microsecond else ""
    return parsed.strftime("%Y-%m-%dT%H:%M:%S") + fraction.rstrip(".") + "Z"


@transform
def epoch_utc(value: object, record: Mapping[str, object]) -> str:
    """A float epoch, as BBOT 3 prints `timestamp`, in the catalog's UTC spelling."""
    return str(rfc3339_utc(datetime.fromtimestamp(float(str(value)), UTC).isoformat(), record))


@transform
def partial_date(value: object, _record: Mapping[str, object]) -> str:
    """Keep `YYYY`, `YYYY-MM` or `YYYY-MM-DD` exactly as precise as the source gives it."""
    text = str(value).strip()
    if re.fullmatch(r"[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?", text) is None:
        raise ValueError("not a partial date")
    return text


@transform
def colon_hex_serial_to_lower_hex(value: object, _record: Mapping[str, object]) -> str:
    """Tlsx may print `0A:BC:...`; the catalog stores lowercase hex without separators or leading zeros."""
    digits = str(value).replace(":", "").lower().lstrip("0")
    return digits or "0"


@transform
def media_type_essence(value: object, _record: Mapping[str, object]) -> str:
    """`text/html; charset=utf-8` becomes `text/html`."""
    return str(value).split(";", 1)[0].strip().lower()


@transform
def sorted_set(value: object, _record: Mapping[str, object]) -> list[str]:
    """Sort ascending and drop duplicates: the one spelling of a set-like array."""
    return sorted({str(item) for item in _items(value)})


@transform
def mta_sts_mx_lines(value: object, _record: Mapping[str, object]) -> list[str]:
    """Collect the repeated `mx:` values of a policy file into a list."""
    return [str(item).strip() for item in _items(value)]


@transform
def scheme_service(value: object, _record: Mapping[str, object]) -> str:
    """`http` and `https` both speak the registry's `http` service; TLS is the `secure` flag."""
    return {"http": "http", "https": "http"}[str(value)]


@transform
def scheme_secure(value: object, _record: Mapping[str, object]) -> bool:
    """Whether a URL scheme runs over TLS."""
    return {"http": False, "https": True}[str(value)]


@transform
def http_request_method(value: object, _record: Mapping[str, object]) -> str:
    """The method token of a raw HTTP request's first line."""
    return str(value).split(" ", 1)[0]


@transform
def nuclei_rule(value: object, _record: Mapping[str, object]) -> str:
    """A nuclei template id as a finding rule: `nuclei:<template-id>`."""
    return f"nuclei:{value}"


@transform
def nuclei_matcher(_value: object, record: Mapping[str, object]) -> str:
    """The nuclei discriminator: `matcher-name`, else `extractor-name`, else empty."""
    return str(record.get("matcher-name") or record.get("extractor-name") or "")


@transform
def sni_matcher(value: object, record: Mapping[str, object]) -> str:
    """For a TLS result on a named host, `<host>:<matcher>`, so two virtual hosts stay two findings.

    The host is lowercased and loses a trailing dot. An IP literal host sends no SNI, so the matcher
    stays bare.
    """
    host = str(record.get("host", "")).rstrip(".").lower()
    matcher = str(value or "")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return f"{host}:{matcher}"
    return matcher


_DAST_LOCATIONS = {"query": "query", "body": "body", "header": "header", "cookie": "cookie", "path": "path"}


@transform
def dast_location(value: object, _record: Mapping[str, object]) -> str:
    """A nuclei `fuzzing_position` as a parameter location."""
    return _DAST_LOCATIONS[str(value)]


@transform
def bbot_rule(value: object, _record: Mapping[str, object]) -> str:
    """A BBOT module name as a finding rule: `bbot:<module>`."""
    return f"bbot:{value}"


@transform
def bbot_nuclei_template(value: object, _record: Mapping[str, object]) -> str:
    """BBOT's nuclei module puts `template: [<id>]` in the description; lift it into `nuclei:<id>`."""
    match = re.search(r"template: \[([^\]]+)\]", str(value))
    if match is None:
        raise ValueError("no nuclei template in the BBOT description")
    return f"nuclei:{match.group(1)}"


@transform
def bbot_nuclei_matcher(value: object, _record: Mapping[str, object]) -> str:
    """The `name: [<matcher>]` part of a BBOT nuclei description."""
    match = re.search(r"name: \[([^\]]*)\]", str(value))
    return "" if match is None else match.group(1)


@transform
def bbot_trufflehog_detector(value: object, _record: Mapping[str, object]) -> str:
    """`TruffleHog - AWS` becomes the finding rule `trufflehog:aws`."""
    return "trufflehog:" + str(value).removeprefix("TruffleHog - ").strip().lower()


def _bracketed(label: str, text: str) -> str | None:
    """The `<label>: [<value>]` part of a BBOT description, up to the next ` <Label> ...: [` or the end."""
    match = re.search(rf"{label}: \[(.*?)\](?= [A-Z][A-Za-z0-9 ]*: \[|$)", text, re.DOTALL)
    return None if match is None else match.group(1)


@transform
def bbot_trufflehog_secret_part(value: object, _record: Mapping[str, object]) -> str:
    """The secret half of the credential a BBOT trufflehog description quotes, before redaction.

    BBOT copies trufflehog's `Raw` and `RawV2` into the description; the detector type picks the
    secret part exactly as `trufflehog_secret_part` does, so a two-part key is never identified by
    its public half.
    """
    text = str(value)
    raw = _bracketed("Raw result", text)
    if raw is None:
        raise ValueError("no raw result in the BBOT trufflehog description")
    result = {
        "DetectorName": _bracketed("Detector Type", text) or "",
        "Raw": raw,
        "RawV2": _bracketed("RawV2 result", text) or "",
    }
    return str(trufflehog_secret_part(raw, result))


@transform
def bbot_nuclei_template_id(value: object, _record: Mapping[str, object]) -> str:
    """The bare template id in a BBOT nuclei description, the one human-readable rule name BBOT keeps."""
    return str(bbot_nuclei_template(value, _record)).removeprefix("nuclei:")


@transform
def bbot_without_extracted_data(value: object, _record: Mapping[str, object]) -> str:
    """A BBOT nuclei description without the ` Extracted Data: [...]` suffix, which can quote a secret."""
    return re.sub(r" Extracted Data: \[.*\]$", "", str(value), flags=re.DOTALL)


# The secret half of a multi-part trufflehog credential, per detector family. `Raw` alone is the
# secret for every other family. The public half goes to `key_id` through `trufflehog_public_part`.
_TRUFFLEHOG_SECRET_PART: dict[str, Callable[[Mapping[str, object]], str]] = {
    "AWS": lambda record: str(record["RawV2"]).split(":", 2)[1],
    "AWSSessionKey": lambda record: str(record["RawV2"]).split(":", 2)[1],
    "Twilio": lambda record: str(record["RawV2"])[len(str(record["Raw"])) :],
}
# Families whose `Raw` is a public identifier, with the grammar that identifier must satisfy.
_TRUFFLEHOG_PUBLIC_PART: dict[str, str] = {
    "AWS": r"(?:AKIA|ABIA|ACCA|ASIA)[A-Z0-9]{16}",
    "AWSSessionKey": r"(?:AKIA|ABIA|ACCA|ASIA)[A-Z0-9]{16}",
    "Twilio": r"AC[0-9a-f]{32}",
}


@transform
def trufflehog_secret_part(value: object, record: Mapping[str, object]) -> str:
    """The secret material of a trufflehog result, never a public key id and never `RawV2` whole."""
    family = str(record.get("DetectorName", ""))
    if family in _TRUFFLEHOG_SECRET_PART:
        return _TRUFFLEHOG_SECRET_PART[family](record)
    return str(value)


@transform
def trufflehog_public_part(value: object, record: Mapping[str, object]) -> str:
    """The public identifier half of a two-part credential; an unknown family fails the mapping."""
    family = str(record.get("DetectorName", ""))
    grammar = _TRUFFLEHOG_PUBLIC_PART[family]
    if re.fullmatch(grammar, str(value)) is None:
        raise ValueError(f"{family} Raw is not a public identifier")
    return str(value)


@transform
def detector_token(value: object, _record: Mapping[str, object]) -> str:
    """A scanner rule or detector name as a tech_token: lowercase, runs of other characters `-`."""
    return re.sub(r"[^a-z0-9._+]+", "-", str(value).lower()).strip("-.")


def _breach_slug(value: object) -> str:
    """Lowercase, each run outside `[a-z0-9._/-]` one `-`, ends trimmed; lossy on purpose."""
    return re.sub(r"[^a-z0-9._/-]+", "-", str(value).lower()).strip("-")


@transform
def leakcheck_breach_token(value: object, _record: Mapping[str, object]) -> str:
    """A LeakCheck `source.name` as a breach token: `leakcheck:<slug>`. LeakCheck has no separate id."""
    return f"leakcheck:{_breach_slug(value)}"


@transform
def public_suffix_of(value: object, _record: Mapping[str, object]) -> str:
    """The zone a registrable domain is registered in: the name without its first label."""
    name = str(value).rstrip(".").lower()
    if classify_dns_name(name) != "domain":
        raise ValueError("not a registrable domain")
    return name.split(".", 1)[1]


@transform
def asn_number(value: object, _record: Mapping[str, object]) -> int:
    """`AS16509`, as asnmap and httpx print it, or a bare number, as the catalog's integer."""
    return int(str(value).upper().removeprefix("AS"))


@transform
def cloud_service(value: object, _record: Mapping[str, object]) -> str:
    """The cloud_resource service whose canonical default hostname this is, as the catalog decides it."""
    hostname = str(value).rstrip(".").lower()
    services = catalog_view()["nodes"]["cloud_resource"]["required"]["service"]
    for service in services:
        try:
            validate_record("nodes", "cloud_resource", {"service": service, "hostname": hostname})
        except ExpectedValidationError:
            continue
        return str(service)
    raise ValueError("not a canonical provider default hostname")


@transform
def ip_version(value: object, _record: Mapping[str, object]) -> int:
    """4 or 6, read off an address or network spelling."""
    return 6 if ":" in str(value) else 4


@transform
def openid_tenant_id(value: object, _record: Mapping[str, object]) -> str:
    """The tenant UUID inside an Entra OpenID `issuer` or `token_endpoint` URL."""
    match = re.search(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", str(value).lower())
    if match is None:
        raise ValueError("no tenant id in the OpenID URL")
    return match.group(0)


@transform
def arn_account(value: object, _record: Mapping[str, object]) -> str:
    """The 12-digit account of an AWS ARN."""
    return str(value).split(":")[4]


@transform
def cvss_one_decimal(value: object, _record: Mapping[str, object]) -> float | int:
    """A CVSS score as published, rounded to its one decimal; an integral score stays an integer."""
    score = round(float(str(value)), 1)
    return int(score) if score.is_integer() else score


# ---- registration transforms ----


def _rdap_event_date(value: object, record: Mapping[str, object], action: str) -> str | None:
    """The eventDate in UTC when a top-level RDAP event with this action carries it; None otherwise."""
    for event in _items(record.get("events", [])):
        if isinstance(event, Mapping):
            item = cast("Mapping[str, object]", event)
            if item.get("eventAction") == action and item.get("eventDate") == value:
                return str(rfc3339_utc(value, record))
    return None


@transform
def rdap_event_registration(value: object, record: Mapping[str, object]) -> str | None:
    """The eventDate of the RDAP `registration` event in UTC; None for another event's date."""
    return _rdap_event_date(value, record, "registration")


@transform
def rdap_event_last_changed(value: object, record: Mapping[str, object]) -> str | None:
    """The eventDate of the RDAP `last changed` event in UTC; None for another event's date."""
    return _rdap_event_date(value, record, "last changed")


@transform
def rdap_event_expiration(value: object, record: Mapping[str, object]) -> str | None:
    """The eventDate of the RDAP `expiration` event in UTC; None for another event's date."""
    return _rdap_event_date(value, record, "expiration")


@transform
def rdap_event_database_update(value: object, record: Mapping[str, object]) -> str | None:
    """The eventDate of the ICANN-profile `last update of RDAP database` event in UTC; None otherwise."""
    return _rdap_event_date(value, record, "last update of RDAP database")


def _rdap_entities(record: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Every entity of an RDAP response, nested entities included."""
    found: list[Mapping[str, object]] = []
    pending = _items(record.get("entities", []))
    while pending:
        entity = pending.pop()
        if isinstance(entity, Mapping):
            item = cast("Mapping[str, object]", entity)
            found.append(item)
            pending.extend(_items(item.get("entities", [])))
    return found


def _vcard_values(vcard: object, name: str) -> list[str]:
    """The values of one property of a jCard `["vcard", [[name, params, type, value], ...]]`."""
    parts = _items(vcard)
    if len(parts) != 2:
        return []
    values: list[str] = []
    for prop in _items(parts[1]):
        fields = _items(prop)
        if len(fields) >= 4 and fields[0] == name:
            values.append(str(fields[3]))
    return values


def _rdap_roles(key: str, value: object, record: Mapping[str, object]) -> set[str]:
    """The roles of every entity whose `key` member equals the value, united."""
    return {
        str(role)
        for entity in _rdap_entities(record)
        if entity.get(key) == value
        for role in _items(entity.get("roles", []))
    }


def _rdap_is_holder(key: str, value: object, record: Mapping[str, object]) -> bool:
    """Whether an entity with this member value holds the resource: role `registrant` and vCard kind `org`."""
    return any(
        entity.get(key) == value
        and "registrant" in {str(role) for role in _items(entity.get("roles", []))}
        and "org" in _vcard_values(entity.get("vcardArray"), "kind")
        for entity in _rdap_entities(record)
    )


def _first(values: list[str]) -> str | None:
    """The first non-empty value, or None."""
    return next((value for value in values if value), None)


def _e164(text: str) -> str | None:
    """`+` and the digits of an international phone spelling (`tel:` URI, dotted or spaced); None without `+`."""
    number = text.removeprefix("tel:").split(";", 1)[0].strip()
    digits = re.sub(r"[^0-9]", "", number)
    return "+" + digits if number.startswith("+") and digits else None


_RDAP_REGISTRATION_ROLES = {
    "registrant": "registrant",
    "administrative": "admin",
    "technical": "tech",
    "billing": "billing",
}


@transform
def rdap_registration_role(value: object, _record: Mapping[str, object]) -> str | None:
    """An RDAP registration contact role as the `has_contact` role (`technical` is `tech`); None for others."""
    return _RDAP_REGISTRATION_ROLES.get(str(value))


@transform
def rdap_abuse_role(value: object, _record: Mapping[str, object]) -> str | None:
    """The RDAP role `abuse` as the `has_contact` role; None for any other role."""
    return "abuse" if value == "abuse" else None


@transform
def rdap_registrar_name(value: object, record: Mapping[str, object]) -> str | None:
    """The vCard `fn` of the entity holding this vcardArray when it has the `registrar` role; None otherwise."""
    if "registrar" not in _rdap_roles("vcardArray", value, record):
        return None
    return _first(_vcard_values(value, "fn"))


@transform
def rdap_holder_name(value: object, record: Mapping[str, object]) -> str | None:
    """The vCard `fn` of the number resource holder (role `registrant`, kind `org`); None for other entities."""
    return _first(_vcard_values(value, "fn")) if _rdap_is_holder("vcardArray", value, record) else None


@transform
def rdap_holder_handle(value: object, record: Mapping[str, object]) -> str | None:
    """The handle of the number resource holder (role `registrant`, kind `org`); None for other entities."""
    return str(value) if _rdap_is_holder("handle", value, record) else None


@transform
def rdap_registration_contact_email(value: object, record: Mapping[str, object]) -> str | None:
    """The vCard `email` of a registrant, administrative, technical or billing entity; None for others."""
    if not _rdap_roles("vcardArray", value, record) & set(_RDAP_REGISTRATION_ROLES):
        return None
    return _first(_vcard_values(value, "email"))


@transform
def rdap_abuse_email(value: object, record: Mapping[str, object]) -> str | None:
    """The vCard `email` of an entity with the `abuse` role; None for other entities or an empty email."""
    if "abuse" not in _rdap_roles("vcardArray", value, record):
        return None
    return _first(_vcard_values(value, "email"))


@transform
def rdap_abuse_phone(value: object, record: Mapping[str, object]) -> str | None:
    """The first international vCard `tel` of an `abuse` entity as E.164; None for other entities."""
    if "abuse" not in _rdap_roles("vcardArray", value, record):
        return None
    return _first([_e164(tel) or "" for tel in _vcard_values(value, "tel")])


@transform
def whois_phone_e164(value: object, _record: Mapping[str, object]) -> str:
    """ICANN's port-43 `+1.7035550100` phone spelling as E.164 `+17035550100`; a national number fails."""
    number = _e164(str(value))
    if number is None:
        raise ValueError("not an international phone number")
    return number


@transform
def rdap_range_cidr(value: object, record: Mapping[str, object]) -> str | None:
    """The one network an RDAP `startAddress` and its `endAddress` span; None when the range is not one prefix."""
    start = ipaddress.ip_address(str(value))
    end = ipaddress.ip_address(str(record["endAddress"]))
    size = int(end) - int(start) + 1
    if start.version != end.version or size < 1 or size & (size - 1) or int(start) % size:
        return None
    return str(ipaddress.ip_network(f"{start}/{start.max_prefixlen - (size.bit_length() - 1)}"))


@transform
def rdap_single_autnum(value: object, record: Mapping[str, object]) -> int | None:
    """An RDAP `startAutnum` that equals its `endAutnum`, one AS; None for a block of several."""
    return value if type(value) is int and record.get("endAutnum") == value else None


_RIR_WHOIS_SERVERS = {
    "whois.arin.net": "arin",
    "whois.ripe.net": "ripe",
    "whois.apnic.net": "apnic",
    "whois.lacnic.net": "lacnic",
    "whois.afrinic.net": "afrinic",
}


@transform
def rir_from_whois_server(value: object, _record: Mapping[str, object]) -> str:
    """The RIR whose port-43 server an RDAP number object names (`whois.arin.net` is `arin`); others fail."""
    return _RIR_WHOIS_SERVERS[str(value).rstrip(".").lower()]


# ---- secrets transforms ----


def _url_path_segments(value: object) -> list[str]:
    """The non-empty path segments of an absolute URL, without query or fragment."""
    match = re.match(r"[a-z]+://[^/]+(/[^?#]*)?", str(value), re.IGNORECASE)
    if match is None:
        raise ValueError("not an absolute URL")
    return [segment for segment in (match.group(1) or "").split("/") if segment]


@transform
def repo_url_owner(value: object, _record: Mapping[str, object]) -> str:
    """The first path segment of a GitHub repository, clone or file URL, lowercased: the owner."""
    return _url_path_segments(value)[0].lower()


@transform
def repo_url_name(value: object, _record: Mapping[str, object]) -> str:
    """The second path segment of a GitHub repository, clone or file URL, without `.git`, lowercased: the name."""
    return _url_path_segments(value)[1].removesuffix(".git").lower()


@transform
def trufflehog_git_location(value: object, record: Mapping[str, object]) -> str:
    """A trufflehog git result's `file` joined with its `line` as `<file>:<line>`."""
    metadata = cast("Mapping[str, Mapping[str, Mapping[str, object]]]", record["SourceMetadata"])
    return f"{value}:{metadata['Data']['Git']['line']}"


@transform
def gitleaks_location(value: object, record: Mapping[str, object]) -> str:
    """A gitleaks finding's `File` joined with its `StartLine` as `<file>:<line>`."""
    return f"{value}:{record['StartLine']}"


# The credential kind each trufflehog detector family reports; an unlisted family fails the mapping.
_TRUFFLEHOG_KIND: dict[str, str] = {
    "AWS": "api_key",
    "AWSSessionKey": "session_token",
    "PrivateKey": "private_key",
    "Twilio": "api_key",
}


@transform
def trufflehog_secret_kind(value: object, _record: Mapping[str, object]) -> str:
    """The secret `kind` of a trufflehog `DetectorName`, from a closed per-family table."""
    return _TRUFFLEHOG_KIND[str(value)]


# The credential kind each gitleaks default rule reports; an unlisted rule fails the mapping.
_GITLEAKS_KIND: dict[str, str] = {
    "aws-access-token": "api_key",
    "generic-api-key": "api_key",
    "private-key": "private_key",
}


@transform
def gitleaks_secret_kind(value: object, _record: Mapping[str, object]) -> str:
    """The secret `kind` of a gitleaks `RuleID`, from a closed per-rule table."""
    return _GITLEAKS_KIND[str(value)]


_NVD_CVSS_FAMILIES = ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30")


@transform
def nvd_preferred_cvss(value: object, record: Mapping[str, object]) -> object:
    """Keep a (rounded) base score or vector only if the preferred metric carries it, else None.

    The preferred metric of a single-CVE NVD response is the first `Primary` entry across
    cvssMetricV40, cvssMetricV31 and cvssMetricV30, else the first entry, so a CNA or ADP `Secondary`
    score that disagrees with the NVD's own is not written.
    """
    vulnerabilities = cast("list[Mapping[str, Mapping[str, Mapping[str, object]]]]", record["vulnerabilities"])
    if len(vulnerabilities) != 1:
        raise ValueError("nvd_preferred_cvss needs a single-CVE response")
    metrics = vulnerabilities[0]["cve"]["metrics"]
    entries = [
        cast("Mapping[str, object]", entry)
        for family in _NVD_CVSS_FAMILIES
        for entry in _items(metrics.get(family, []))
    ]
    preferred = next((entry for entry in entries if entry.get("type") == "Primary"), entries[0] if entries else None)
    if preferred is None:
        return None
    data = cast("Mapping[str, object]", preferred["cvssData"])
    return value if value in (cvss_one_decimal(data["baseScore"], record), data["vectorString"]) else None


# ---- network transforms ----
# A transform here that may return None also accepts None, because `apply_chain` hands the None of
# one step to the next.


def _accepted(type_name: str, properties: dict[str, object]) -> bool:
    """Whether the catalog accepts these node properties, so the catalog, not a copy, decides."""
    try:
        validate_record("nodes", type_name, dict(properties))
    except ExpectedValidationError:
        return False
    return True


def _dns_name_of_kind(value: object, type_name: str) -> str | None:
    if value is None:
        return None
    name = str(value).rstrip(".").lower()
    return name if _accepted(type_name, {"value": name}) else None


@transform
def if_domain(value: object, _record: Mapping[str, object]) -> str | None:
    """A name the catalog accepts as a registrable `domain`, lowercased without the root dot; None otherwise."""
    return _dns_name_of_kind(value, "domain")


@transform
def if_subdomain(value: object, _record: Mapping[str, object]) -> str | None:
    """A name the catalog accepts as a `subdomain`, lowercased without the root dot; None otherwise."""
    return _dns_name_of_kind(value, "subdomain")


def _input_is_ip(record: Mapping[str, object]) -> bool:
    text = str(record.get("input", ""))
    return _accepted("ip_address", {"value": text, "version": ip_version(text, record)})


@transform
def if_ip_input(value: object, record: Mapping[str, object]) -> object:
    """The value when the record's `input` is an IP literal, so the tool classified that address; None otherwise."""
    return value if _input_is_ip(record) else None


@transform
def if_name_input(value: object, record: Mapping[str, object]) -> object:
    """The value when the record's `input` is a DNS name, so the tool classified the name; None otherwise."""
    return None if _input_is_ip(record) else value


@transform
def dmarc_owner_name(value: object, _record: Mapping[str, object]) -> str:
    """Drop the leading `_dmarc` label of a query name: RFC 7489 publishes it for the name below."""
    name = str(value).rstrip(".").lower()
    return name.removeprefix("_dmarc.")


@transform
def san_base_name(value: object, _record: Mapping[str, object]) -> str:
    """A certificate SAN without its `*.` wildcard label, lowercased: the name `covers_name` targets."""
    return str(value).lower().removeprefix("*.")


@transform
def san_coverage(value: object, _record: Mapping[str, object]) -> str:
    """`wildcard` for a `*.` SAN and `exact` for a literal one: the `covers_name` coverage."""
    return "wildcard" if str(value).startswith("*.") else "exact"


def _txt_value_of_type(value: object, type_name: str) -> str | None:
    if value is None:
        return None
    return str(value) if _accepted(type_name, {"value": str(value)}) else None


@transform
def if_txt_record(value: object, _record: Mapping[str, object]) -> str | None:
    """A TXT value `txt_record` accepts; None when a dedicated type such as `spf_record` claims it."""
    return _txt_value_of_type(value, "txt_record")


@transform
def if_spf_record(value: object, _record: Mapping[str, object]) -> str | None:
    """A TXT value `spf_record` accepts; None for any other TXT value."""
    return _txt_value_of_type(value, "spf_record")


@transform
def if_dmarc_record(value: object, _record: Mapping[str, object]) -> str | None:
    """A TXT value `dmarc_record` accepts; None for any other TXT value."""
    return _txt_value_of_type(value, "dmarc_record")


def _rr_rdata(value: object, rr_type: str) -> str | None:
    """The RDATA of one resource record in DNS presentation form, when it has this type."""
    fields = str(value).split(None, 4)
    if len(fields) < 5 or fields[2] != "IN" or fields[3] != rr_type:
        return None
    return fields[4]


@transform
def rr_mx_preference(value: object, _record: Mapping[str, object]) -> int | None:
    """The preference of an MX record printed `<owner> <ttl> IN MX 10 <exchange>`; None for another record."""
    rdata = _rr_rdata(value, "MX")
    return None if rdata is None else int(rdata.split()[0], 10)


def _caa(value: object, tag: str) -> tuple[int, str] | None:
    """The flags and the unquoted value of a CAA record printed `... IN CAA 0 <tag> "<value>"` with this tag."""
    rdata = _rr_rdata(value, "CAA")
    if rdata is None:
        return None
    flags, record_tag, quoted = rdata.split(" ", 2)
    if record_tag.lower() != tag:
        return None
    return int(flags, 10), quoted.removeprefix('"').removesuffix('"')


def _caa_issue_parts(value: object, tags: tuple[str, ...]) -> tuple[int, str, list[dict[str, str]]] | None:
    """RFC 8659 `issue`/`issuewild`: the flags, the issuer domain and the `name=value` parameters."""
    parsed = next((item for item in (_caa(value, tag) for tag in tags) if item is not None), None)
    if parsed is None:
        return None
    flags, text = parsed
    issuer, *parameters = (part.strip() for part in text.split(";"))
    pairs = [part.partition("=") for part in parameters if part]
    named = [{"name": name.strip().lower(), "value": content.strip()} for name, _, content in pairs]
    return flags, issuer.lower(), named


@transform
def rr_caa_issue_flags(value: object, _record: Mapping[str, object]) -> int | None:
    """The flags of a CAA `issue` record in presentation form; None for another record."""
    parts = _caa_issue_parts(value, ("issue",))
    return None if parts is None else parts[0]


@transform
def rr_caa_issue_parameters(value: object, _record: Mapping[str, object]) -> list[dict[str, str]] | None:
    """The parameters of a CAA `issue` record as `{name, value}` pairs, names lowercased; None otherwise."""
    parts = _caa_issue_parts(value, ("issue",))
    return None if parts is None else parts[2]


@transform
def rr_caa_issuewild_flags(value: object, _record: Mapping[str, object]) -> int | None:
    """The flags of a CAA `issuewild` record in presentation form; None for another record."""
    parts = _caa_issue_parts(value, ("issuewild",))
    return None if parts is None else parts[0]


@transform
def rr_caa_issuewild_parameters(value: object, _record: Mapping[str, object]) -> list[dict[str, str]] | None:
    """The parameters of a CAA `issuewild` record as `{name, value}` pairs, names lowercased; None otherwise."""
    parts = _caa_issue_parts(value, ("issuewild",))
    return None if parts is None else parts[2]


@transform
def rr_caa_issuer(value: object, _record: Mapping[str, object]) -> str | None:
    """The issuer domain of a CAA `issue` or `issuewild` record; None for another record or an empty issuer (`;`)."""
    parts = _caa_issue_parts(value, ("issue", "issuewild"))
    return None if parts is None or not parts[1] else parts[1]


@transform
def rr_caa_iodef_email(value: object, _record: Mapping[str, object]) -> str | None:
    """The lowercased address of a CAA `iodef` `mailto:` URL; None for another record or URL scheme."""
    parsed = _caa(value, "iodef")
    if parsed is None or not parsed[1].lower().startswith("mailto:"):
        return None
    return parsed[1][len("mailto:") :].lower()


@transform
def rr_caa_iodef_url(value: object, _record: Mapping[str, object]) -> str | None:
    """The `https:` URL of a CAA `iodef` record, exactly as published; None for another record or scheme."""
    parsed = _caa(value, "iodef")
    return parsed[1] if parsed is not None and parsed[1].lower().startswith("https://") else None


@transform
def if_cloud_hostname(value: object, record: Mapping[str, object]) -> str | None:
    """A canonical provider default hostname, lowercased without the root dot; None for any other name."""
    return None if cloud_service_or_none(value, record) is None else str(value).rstrip(".").lower()


@transform
def cloud_service_or_none(value: object, record: Mapping[str, object]) -> str | None:
    """`cloud_service` for a canonical provider default hostname; None for any other name."""
    if value is None:
        return None
    try:
        return str(cloud_service(value, record))
    except ValueError:
        return None


@transform
def cloud_region(value: object, record: Mapping[str, object]) -> str | None:
    """The region a canonical provider default hostname encodes, as the catalog reads it; None when it has none."""
    # Each label, and each label without a `-NN` suffix, is offered to the catalog as the `region`; the
    # catalog accepts only the region its hostname pattern captures, so an ELB name label never passes.
    service = cloud_service_or_none(value, record)
    if service is None:
        return None
    hostname = str(value).rstrip(".").lower()
    for label in hostname.split("."):
        for candidate in (label, label.rpartition("-")[0]):
            properties: dict[str, object] = {"service": service, "hostname": hostname, "region": candidate}
            if candidate and _accepted("cloud_resource", properties):
                return candidate
    return None


@transform
def cdncheck_protection_kind(value: object, _record: Mapping[str, object]) -> str | None:
    """A cdncheck item type as a `protected_by` kind: `cdn` and `waf` keep their name, `cloud` (hosting) is None."""
    return {"cdn": "cdn", "waf": "waf", "cloud": None}[str(value)]


@transform
def go_time_string_utc(value: object, record: Mapping[str, object]) -> str:
    """Go's default `time.Time.String()` (`2006-01-02 15:04:05.999999999 -0700 MST`), as asnmap prints it, in UTC."""
    match = re.fullmatch(r"(\S+) (\S+) ([+-][0-9]{2})([0-9]{2})(?: \S+)?(?: m=\S+)?", str(value).strip())
    if match is None:
        raise ValueError("not a Go time.Time string")
    day, clock, hours, minutes = match.groups()
    return str(rfc3339_utc(f"{day}T{clock}{hours}:{minutes}", record))


@transform
def nmap_tunnel_secure(value: object, _record: Mapping[str, object]) -> bool:
    """Nmap's service `tunnel="ssl"`: the service runs over TLS. Nmap prints no other tunnel value."""
    return {"ssl": True}[str(value)]


@transform
def ssh_host_key_algorithm(value: object, _record: Mapping[str, object]) -> str | None:
    """A key type the `host_key` algorithm enum names, as ssh-hostkey's `type` element prints it; None otherwise."""
    algorithms = catalog_view()["nodes"]["host_key"]["required"]["algorithm"]
    return str(value) if value in algorithms else None


@transform
def nmap_ssh_hostkey_sha256(value: object, _record: Mapping[str, object]) -> str | None:
    """The one `SHA256:<base64>` fingerprint in an ssh-hostkey `output` (`ssh_hostkey=sha256`); None unless exactly one."""
    fingerprints = re.findall(r"SHA256:[A-Za-z0-9+/]+", str(value))
    return fingerprints[0] if len(fingerprints) == 1 else None


# ---- web transforms ----


def _is_bbot_wildcard_placeholder(record: Mapping[str, object]) -> bool:
    """Whether a BBOT event's `data` is the `_wildcard.<zone>` name BBOT substitutes for a wildcard hit."""
    return str(record.get("data", "")).lower().startswith("_wildcard.")


@transform
def bbot_dns_name(value: object, _record: Mapping[str, object]) -> str:
    """The name a BBOT DNS_NAME stands for, lowercased without a root dot; `_wildcard.<zone>` becomes `<zone>`."""
    return str(value).rstrip(".").lower().removeprefix("_wildcard.")


@transform
def bbot_wildcard_zone(value: object, record: Mapping[str, object]) -> bool | None:
    """True for a `_wildcard.<zone>` DNS_NAME tagged `wildcard`, whose zone answers random labels; else None."""
    return True if _is_bbot_wildcard_placeholder(record) and "wildcard" in _items(value) else None


@transform
def bbot_wildcard_answer(value: object, record: Mapping[str, object]) -> bool | None:
    """A real DNS_NAME's tags: `wildcard` is true, `wildcard-possible` false, anything else None."""
    tags = _items(value)
    if _is_bbot_wildcard_placeholder(record):
        return None
    if "wildcard" in tags:
        return True
    return False if "wildcard-possible" in tags else None


@transform
def bbot_status_tag(value: object, _record: Mapping[str, object]) -> int | None:
    """The HTTP status in a BBOT URL event's `status-<code>` tag; None when the tags carry none."""
    for tag in _items(value):
        match = re.fullmatch(r"status-([1-5][0-9]{2})", str(tag))
        if match is not None:
            return int(match.group(1))
    return None


# Record-level conditions a mapping row may require; an unmet condition routes the value to evidence.
Condition = Callable[[Mapping[str, object]], bool]
CONDITIONS: dict[str, Condition] = {}


def condition(function: Condition) -> Condition:
    """Register one condition under its function name."""
    CONDITIONS[function.__name__] = function
    return function


def _registration_id_accepted(identifier: object, zone_source: object) -> bool:
    """The full acceptance a `whois_registration` needs: a ROID the catalog takes, for a real zone."""
    if type(identifier) is not str or type(zone_source) is not str:
        return False
    try:
        properties = {"registry": public_suffix_of(zone_source, {}), "registry_domain_id": identifier}
        validate_record("nodes", "whois_registration", properties)
    except (ExpectedValidationError, ValueError):
        return False
    return True


@condition
def rdap_registration_accepted(record: Mapping[str, object]) -> bool:
    """An RDAP domain object carries a real handle: present, accepted, and not listed in `redacted`."""
    names_handle = any(
        isinstance(item, Mapping) and cast("Mapping[str, object]", item).get("prePath") == "$.handle"
        for item in _items(record.get("redacted", []))
    )
    return not names_handle and _registration_id_accepted(record.get("handle"), record.get("ldhName"))


@condition
def whois_registration_accepted(record: Mapping[str, object]) -> bool:
    """A port-43 response carries a real `Registry Domain ID`."""
    return _registration_id_accepted(record.get("Registry Domain ID"), record.get("Domain Name"))


# ---- web conditions ----


def _dns_name_kind(name: object) -> str | None:
    """`domain` or `subdomain` by the PSL; None for an IP literal, a public suffix or a non-name."""
    text = str(name or "").rstrip(".").lower()
    if ":" in text or re.fullmatch(r"[0-9.]+", text):
        return None
    try:
        return classify_dns_name(text)
    except ExpectedValidationError:
        return None


@condition
def host_is_domain(record: Mapping[str, object]) -> bool:
    """The record's `host` is a registrable domain."""
    return _dns_name_kind(record.get("host")) == "domain"


@condition
def host_is_subdomain(record: Mapping[str, object]) -> bool:
    """The record's `host` is a name below a registrable domain, not an IP literal."""
    return _dns_name_kind(record.get("host")) == "subdomain"


@condition
def bbot_names_domain(record: Mapping[str, object]) -> bool:
    """A BBOT DNS_NAME stands for a registrable domain, reading `_wildcard.<zone>` as its zone."""
    return _dns_name_kind(bbot_dns_name(record.get("data", ""), record)) == "domain"


@condition
def bbot_names_subdomain(record: Mapping[str, object]) -> bool:
    """A BBOT DNS_NAME stands for a subdomain, reading `_wildcard.<zone>` as its zone."""
    return _dns_name_kind(bbot_dns_name(record.get("data", ""), record)) == "subdomain"


@condition
def bbot_not_wildcard_placeholder(record: Mapping[str, object]) -> bool:
    """A BBOT event names a real host, not the `_wildcard.<zone>` stand-in whose answers belong to the zone."""
    return not _is_bbot_wildcard_placeholder(record)


@condition
def nuclei_http_result(record: Mapping[str, object]) -> bool:
    """A nuclei result of an `http` or `headless` template, whose parent is the matched endpoint."""
    return record.get("type") in ("http", "headless")


@condition
def nuclei_ssl_result(record: Mapping[str, object]) -> bool:
    """A nuclei result of an `ssl` template, whose matcher carries the SNI host."""
    return record.get("type") == "ssl"


@condition
def nuclei_not_ssl_result(record: Mapping[str, object]) -> bool:
    """A nuclei result of any template type but `ssl`."""
    return record.get("type") != "ssl"


# A scanner-reported secret field, per source. The harness refuses a mapping that leaves one of these
# unredacted, so the raw value never reaches evidence.
SECRET_FIELDS: dict[str, frozenset[str]] = {
    "trufflehog": frozenset({"Raw", "RawV2", "Redacted", "SecretParts.*"}),
    "gitleaks": frozenset({"Secret", "Match", "Line"}),
    "leak_corpus": frozenset({"result[].password", "result[].hashed_password"}),
    "nuclei": frozenset({"extracted-results", "request", "response", "curl-command", "matched-at"}),
    "bbot": frozenset(
        {
            "{type=FINDING,module=trufflehog}.data_json.description",
            "{type=FINDING,module=badsecrets}.data_json.description",
            "{type=FINDING,module=nuclei}.data_json.description",
        }
    ),
}

# The only derivations that may carry a value out of a redacted field: each strips the secret. A
# `redact: true` row sinks to `non_storable` or matches one of these exactly.
REDACTED_DERIVATIONS: frozenset[tuple[str, str, tuple[str, ...], str]] = frozenset(
    {
        ("nuclei", "matched-at", ("url_without_query",), "nodes.endpoint.url"),
        ("bbot", "{type=FINDING,module=nuclei}.data_json.description", ("bbot_nuclei_template",), "nodes.finding.rule"),
        (
            "bbot",
            "{type=FINDING,module=nuclei}.data_json.description",
            ("bbot_nuclei_matcher",),
            "nodes.finding.matcher",
        ),
        (
            "bbot",
            "{type=FINDING,module=nuclei}.data_json.description",
            ("bbot_nuclei_template_id",),
            "nodes.finding.title",
        ),
        (
            "bbot",
            "{type=FINDING,module=nuclei}.data_json.description",
            ("bbot_without_extracted_data",),
            "nodes.finding.description",
        ),
        ("nuclei", "request", ("http_request_method",), "nodes.endpoint.method"),
        ("trufflehog", "Raw", ("trufflehog_secret_part", "sha256_hex"), "nodes.secret.value_sha256"),
        ("trufflehog", "Raw", ("trufflehog_public_part",), "nodes.secret.key_id"),
        ("gitleaks", "Secret", ("sha256_hex",), "nodes.secret.value_sha256"),
        ("leak_corpus", "result[].password", ("sha256_hex",), "nodes.secret.value_sha256"),
        ("leak_corpus", "result[].hashed_password", ("sha256_hex",), "nodes.secret.value_sha256"),
        (
            "bbot",
            "{type=FINDING,module=trufflehog}.data_json.description",
            ("bbot_trufflehog_secret_part", "sha256_hex", "hex_prefix16"),
            "nodes.finding.matcher",
        ),
    }
)
