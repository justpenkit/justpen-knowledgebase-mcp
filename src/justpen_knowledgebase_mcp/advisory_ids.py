"""Advisory id grammars: CVE, GHSA and the OSV databases that publish one record per vulnerability."""

import re


def advisory_prefix(value: str) -> str:
    """The database an advisory id belongs to: the part before its first `-`."""
    return value.split("-", 1)[0]


def _padded(width: int) -> str:
    """A sequence number zero-padded to `width` digits, written unpadded once it outgrows them."""
    return f"(?:[0-9]{{{width}}}|[1-9][0-9]{{{width},6}})"


_UNPADDED = "[1-9][0-9]{0,6}"
# One grammar per accepted advisory id prefix, for the whole id: CVE, GHSA, and the OSV databases
# that publish one record per vulnerability, each in the number shape that database issues. Vendor
# and distribution bulletins, which bundle many CVEs, and prefixes with no settled id shape are left
# out. No id is case-folded: GHSA keeps its lowercase body, and every other id is uppercase.
_ADVISORY_ID_GRAMMARS: dict[str, str] = {
    "CVE": r"CVE-[0-9]{4}-[0-9]{4,19}",
    "GHSA": r"GHSA(?:-[23456789cfghjmpqrvwx]{4}){3}",
    "PYSEC": rf"PYSEC-[0-9]{{4}}-{_UNPADDED}",
    "RUSTSEC": rf"RUSTSEC-[0-9]{{4}}-{_padded(4)}",
    "GO": rf"GO-[0-9]{{4}}-{_padded(4)}",
    "OSV": rf"OSV-[0-9]{{4}}-{_UNPADDED}",
    "HSEC": rf"HSEC-[0-9]{{4}}-{_padded(4)}",
    "JLSEC": rf"JLSEC-[0-9]{{4}}-{_UNPADDED}",
    "OSEC": rf"OSEC-[0-9]{{4}}-{_padded(2)}",
    "RSEC": rf"RSEC-[0-9]{{4}}-{_UNPADDED}",
    "EEF": r"EEF-CVE-[0-9]{4}-[0-9]{4,19}",
    "DRUPAL": rf"DRUPAL-(?:CONTRIB|CORE)-[0-9]{{4}}-{_padded(3)}",
    "MAL": rf"MAL-[0-9]{{4}}-{_UNPADDED}",
}


def valid_advisory_id(text: str) -> bool:
    """Return whether text is a whole advisory id in its database's grammar."""
    grammar = _ADVISORY_ID_GRAMMARS.get(advisory_prefix(text))
    return grammar is not None and re.fullmatch(grammar, text) is not None
