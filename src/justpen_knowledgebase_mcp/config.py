"""Immutable, explicitly namespaced startup settings."""

from __future__ import annotations

import json
import re
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from collections.abc import Mapping

PREFIX = "JUSTPEN_KNOWLEDGEBASE_"


class ServerConfig(BaseModel):
    """Validated settings; workspace existence is checked by WorkspacePaths."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    workspace_dir: Path
    data_dir: Path = Path(".justpen/knowledgebase")
    db_path: Path | None = None
    evidence_dir: Path | None = None
    tmp_dir: Path | None = None
    lock_dir: Path | None = None
    transport: Literal["stdio", "http"] = "stdio"
    host: str = "127.0.0.1"
    allowed_hosts: tuple[str, ...] = ()
    port: int = Field(default=8934, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    allow_non_loopback: bool = False
    db_busy_timeout_ms: int = Field(default=5000, ge=1)
    query_timeout_ms: int = Field(default=10000, ge=1)
    db_reader_threads: int = Field(default=2, ge=1, le=8)

    @field_validator("workspace_dir")
    @classmethod
    def absolute_workspace(cls, value: Path) -> Path:
        """Require an explicit absolute root, never CWD resolution."""
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("CONFIGURATION: absolute workspace required")
        return value

    @field_validator("allow_non_loopback", mode="before")
    @classmethod
    def explicit_boolean(cls, value: object) -> object:
        """Only accept boolean values and explicit true/false env spellings."""
        if isinstance(value, str):
            if value.lower() not in {"true", "false"}:
                raise ValueError("CONFIGURATION: expected true or false")
            return value.lower() == "true"
        return value

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def concrete_allowed_hosts(cls, value: object) -> tuple[str, ...]:
        """Accept a small JSON array of concrete host names, without URL or wildcard syntax."""
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("CONFIGURATION: ALLOWED_HOSTS must be a JSON array") from exc
        if not isinstance(value, (list, tuple)) or len(cast("list[object] | tuple[object, ...]", value)) > 16:
            raise ValueError("CONFIGURATION: ALLOWED_HOSTS must be a JSON array of at most 16 hosts")
        entries = cast("list[object] | tuple[object, ...]", value)
        hosts: list[str] = []
        for host in entries:
            if (
                not isinstance(host, str)
                or not host
                or len(host) > 253
                or not host.isascii()
                or any(char in host for char in "*?[]")
            ):
                raise ValueError("CONFIGURATION: ALLOWED_HOSTS entries must be concrete hosts")
            try:
                valid_ip = not ip_address(host).is_unspecified
            except ValueError:
                labels = host.split(".")
                valid_ip = not all(label.isdecimal() for label in labels) and all(
                    re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
                )
            if not valid_ip or host in hosts:
                raise ValueError("CONFIGURATION: ALLOWED_HOSTS entries must be unique concrete hosts")
            hosts.append(host)
        return tuple(hosts)

    @model_validator(mode="after")
    def validate_bind(self) -> Self:
        """Validate the effective host after all CLI overrides are merged."""
        if self.host == "localhost":
            object.__setattr__(self, "host", "127.0.0.1")
        if self.transport == "http" and not self.allow_non_loopback and not self.is_loopback:
            raise ValueError("CONFIGURATION: non-loopback HTTP requires ALLOW_NON_LOOPBACK=true")
        return self

    @property
    def is_loopback(self) -> bool:
        """Treat DNS names as non-loopback regardless of ambient DNS answers."""
        try:
            return ip_address(self.host).is_loopback
        except ValueError:
            return False

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, overrides: Mapping[str, object] | None = None) -> Self:
        """Read only KB fields, then validate merged command-line overrides."""
        values: dict[str, object] = {
            name: env[PREFIX + name.upper()] for name in cls.model_fields if PREFIX + name.upper() in env
        }
        if "log_level" in values:
            values["log_level"] = str(values["log_level"]).strip().upper()
        if overrides:
            values.update({key: value for key, value in overrides.items() if value is not None})
        return cls.model_validate(values)


class WorkspacePolicy(BaseModel):
    """Versioned workspace settings, persisted once; never process overrides."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    format_version: Literal[1] = 1
    wal_low_bytes: int = Field(default=64 * 1024**2, gt=0)
    wal_high_bytes: int = Field(default=256 * 1024**2, gt=0)
    wal_autocheckpoint: int = Field(default=1000, ge=0)
    journal_size_limit: int = Field(default=64 * 1024**2, ge=0)
    completed_retention_seconds: int = Field(default=7 * 86400, gt=0)
    completed_retention_count: int = Field(default=100000, gt=0)
    failed_cancelled_retention_seconds: int = Field(default=30 * 86400, gt=0)
    failed_cancelled_retention_count: int = Field(default=10000, gt=0)
    disk_reserve_bytes: int = Field(default=2 * 256 * 1024**2 + 1024**3, gt=0)
    disk_check_interval_bytes: int = Field(default=8 * 1024**2, gt=0)

    @model_validator(mode="after")
    def validate_thresholds(self) -> Self:
        """Reject incompatible policy before opening product admission."""
        if self.wal_low_bytes >= self.wal_high_bytes:
            raise ValueError("low must be less than high")
        if self.disk_reserve_bytes != 2 * self.wal_high_bytes + 1024**3:
            raise ValueError("disk reserve must equal twice WAL high plus 1GiB")
        return self
