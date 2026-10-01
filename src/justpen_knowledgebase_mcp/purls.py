"""Versionless package purl grammars, with the registry repository of an oci image."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# One OCI distribution-spec repository path component: lowercase, separators only between runs.
_OCI_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
# The canonical `namespace/name` part of a versionless purl, per accepted purl type. Only `oci`
# keeps a qualifier, so the type alone decides whether `?repository_url=` may follow. `docker` is
# not listed: it has no settled place for the registry, so an image is written as `oci`.
_PURL_COORDINATES: dict[str, str] = {
    # The scope's `@` is always `%40`, and npm scopes are lowercase. The name is case-sensitive and
    # written as published: npm lowercases only new names, so legacy ones such as `JSONStream` remain.
    "npm": r"(?:%40[a-z0-9~-][a-z0-9._~-]*/)?[A-Za-z0-9~-][A-Za-z0-9._~-]*",
    # The PyPA normalized name: lowercase, and every run of `-`, `_` and `.` one `-`.
    "pypi": r"[a-z0-9]+(?:-[a-z0-9]+)*",
    "maven": r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*",
    # NuGet ids are case-insensitive; the v3 flat container's lowercase id is the canonical one.
    "nuget": r"[a-z0-9_]+(?:[.-][a-z0-9_]+)*",
    "gem": r"[A-Za-z0-9_][A-Za-z0-9._-]*",
    # crates.io treats names differing in case or in `-` versus `_` as one crate; its canonical
    # name is lowercase with every `-` an `_`.
    "cargo": r"[a-z][a-z0-9_]{0,63}",
    # The purl spec lowercases a golang namespace and name, so a Go module path is written lowercased.
    "golang": r"[a-z0-9_~-]+(?:\.[a-z0-9_~-]+)*(?:/[a-z0-9_~-]+(?:\.[a-z0-9_~-]+)*)+",
    "composer": r"[a-z0-9](?:[_.-]?[a-z0-9]+)*/[a-z0-9](?:(?:[_.]|-{1,2})?[a-z0-9]+)*",
    "oci": _OCI_COMPONENT,
}
# Docker Hub is spelled `docker.io`; these hosts name the same registry under another spelling.
_DOCKER_HUB_ALIASES = frozenset(
    {"index.docker.io", "registry-1.docker.io", "registry.hub.docker.com", "hub.docker.com"}
)
# An ECR private registry is spelled `<account>.dkr.ecr.<region>.amazonaws.com`, or `.com.cn` in the
# China partition, which is a separate registry. Its FIPS and dual-stack hosts name the same
# registry under another spelling.
_ECR_ALIAS_HOST = (
    r"[0-9]{12}\.(?:dkr\.ecr-fips\.[^.]+\.amazonaws\.com(?:\.cn)?"
    r"|dkr-ecr(?:-fips)?\.[^.]+\.(?:on\.aws|on\.amazonwebservices\.com\.cn))"
)


def purl_parts(text: str) -> tuple[str, str, str | None]:
    """The type, the `namespace/name` coordinates and the qualifier string (None when absent) of a purl."""
    path, separator, query = text.removeprefix("pkg:").partition("?")
    purl_type, _slash, coordinates = path.partition("/")
    return purl_type, coordinates, query if separator else None


def valid_package_purl(text: str, valid_host: Callable[[str], bool]) -> bool:
    """A purl with no version, subpath or qualifier, except the registry repository of an image.

    `valid_host` is the caller's DNS host name check, applied to the registry host.
    """
    if len(text) > 512 or not text.startswith("pkg:") or "@" in text or "#" in text:
        return False
    purl_type, coordinates, query = purl_parts(text)
    grammar = _PURL_COORDINATES.get(purl_type)
    if grammar is None or re.fullmatch(grammar, coordinates) is None:
        return False
    if purl_type != "oci":
        return query is None
    key, _equals, repository = (query or "").partition("=")
    return key == "repository_url" and _valid_oci_repository(repository, coordinates, valid_host)


def _valid_oci_repository(repository: str, name: str, valid_host: Callable[[str], bool]) -> bool:
    """`<registry host>[:<port>]/<repository path>`, whose last component is the purl name.

    Docker Hub nests nothing, so its path is the namespace and the name, `library` for an
    official image. An alias spelling of Docker Hub or of an ECR registry is refused, so one image
    is one package. A port is 1 to 65535 without leading zeros, and never the default 443, which
    would spell the registry a second way; Docker Hub takes no port at all.
    """
    host, _slash, path = repository.partition("/")
    hostname, colon, port = host.partition(":")
    if colon and (re.fullmatch(r"[1-9][0-9]{0,4}", port) is None or int(port) > 65535 or int(port) == 443):
        return False
    if (
        (colon and hostname == "docker.io")
        or hostname in _DOCKER_HUB_ALIASES
        or re.fullmatch(_ECR_ALIAS_HOST, hostname) is not None
        or not valid_host(hostname)
    ):
        return False
    components = path.split("/")
    if any(re.fullmatch(_OCI_COMPONENT, component) is None for component in components):
        return False
    if host == "docker.io" and len(components) != 2:
        return False
    return components[-1] == name
