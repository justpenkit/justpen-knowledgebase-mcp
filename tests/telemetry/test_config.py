from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING, cast

import pytest

from justpen_knowledgebase_mcp.telemetry.config import read_config

if TYPE_CHECKING:
    from collections.abc import MutableMapping

PREFIX = "JUSTPEN_KNOWLEDGEBASE_OTEL_"


def test_only_knowledgebase_master_switch_enables_export():
    assert not read_config({}).enabled
    assert not read_config({"OTEL_TRACES_EXPORTER": "otlp", PREFIX + "ENDPOINT": "http://localhost:4318"}).enabled
    config = read_config({PREFIX + "ENABLED": "TrUe", "OTEL_SDK_DISABLED": "true"})
    assert config.enabled
    assert config.traces.exporter == config.logs.exporter == config.metrics.exporter == "otlp"


def test_browser_and_sibling_prefixes_cannot_configure_knowledgebase():
    config = read_config(
        {
            "JUSTPEN_BROWSER_OTEL_ENABLED": "true",
            "JUSTPEN_INTEGRATION_OTEL_ENABLED": "true",
            "JUSTPEN_BROWSER_OTEL_ENDPOINT": "https://wrong.example",
        }
    )
    assert not config.enabled
    assert config.service_name == "justpen-knowledgebase-mcp"
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in config.sdk_environment


def test_signal_override_is_independent_of_other_signals():
    config = read_config({PREFIX + "ENABLED": "true", PREFIX + "LOGS_ENABLED": "false"})
    assert config.traces.exporter == "otlp"
    assert config.logs.exporter == "none"
    assert config.metrics.exporter == "otlp"
    assert not read_config({PREFIX + "TRACES_ENABLED": "true"}).enabled


def test_protocol_and_sdk_settings_use_only_prefixed_values():
    config = read_config(
        {
            PREFIX + "ENABLED": "true",
            PREFIX + "PROTOCOL": "grpc",
            PREFIX + "TRACES_PROTOCOL": "http/protobuf",
            PREFIX + "ENDPOINT": "https://collector.example/base/",
            PREFIX + "LOGS_ENDPOINT": "https://logs.example/custom",
            PREFIX + "HEADERS": "authorization=private-value",
            PREFIX + "TIMEOUT": "2.5",
            PREFIX + "BSP_MAX_QUEUE_SIZE": "8",
            PREFIX + "TRACES_SAMPLER": "parentbased_traceidratio",
            PREFIX + "TRACES_SAMPLER_ARG": "0.5",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://wrong.example",
            PREFIX + "UNRECOGNIZED": "discard-me",
        }
    )
    assert config.traces.protocol == "http/protobuf"
    assert config.logs.protocol == config.metrics.protocol == "grpc"
    assert config.sdk_environment["OTEL_EXPORTER_OTLP_ENDPOINT"] == "https://collector.example/base/"
    assert config.sdk_environment["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"] == "https://logs.example/custom"
    assert config.sdk_environment["OTEL_EXPORTER_OTLP_TIMEOUT"] == "2.5"
    assert config.sdk_environment["OTEL_BSP_MAX_QUEUE_SIZE"] == "8"
    assert config.sdk_environment["OTEL_TRACES_SAMPLER_ARG"] == "0.5"
    assert "OTEL_UNRECOGNIZED" not in config.sdk_environment
    assert "private-value" not in repr(config)


def test_invalid_protocol_disables_only_affected_signal_without_logging_value(caplog):
    config = read_config({PREFIX + "ENABLED": "true", PREFIX + "TRACES_PROTOCOL": "private-invalid-value"})
    assert config.traces.exporter == "none"
    assert config.logs.exporter == "otlp"
    assert "private-invalid-value" not in caplog.text
    assert "TRACES_PROTOCOL" in caplog.text


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "nope"])
def test_invalid_timeouts_use_bounded_defaults(value):
    config = read_config({PREFIX + "SHUTDOWN_TIMEOUT_MS": value, PREFIX + "TIMEOUT": value})
    assert config.shutdown_timeout_ms == 5000
    assert "OTEL_EXPORTER_OTLP_TIMEOUT" not in config.sdk_environment


def test_configuration_is_an_immutable_snapshot():
    env = {PREFIX + "ENDPOINT": "http://first.example"}
    config = read_config(env)
    env[PREFIX + "ENDPOINT"] = "http://second.example"
    assert config.sdk_environment["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://first.example"
    with pytest.raises(TypeError):
        cast("MutableMapping[str, str]", config.sdk_environment)["OTEL_EXPORTER_OTLP_ENDPOINT"] = "changed"
    with pytest.raises(FrozenInstanceError):
        object.__getattribute__(config, "__setattr__")("service_name", "changed")


def test_invalid_master_boolean_does_not_enable_export(caplog):
    assert not read_config({PREFIX + "ENABLED": "yes"}).enabled
    assert "ENABLED" in caplog.text


def test_oversized_numbers_and_unsupported_compression_use_defaults(caplog):
    config = read_config({PREFIX + "SHUTDOWN_TIMEOUT_MS": "9" * 400, PREFIX + "COMPRESSION": "sentinel-secret"})
    assert config.shutdown_timeout_ms == 5000
    assert "OTEL_EXPORTER_OTLP_COMPRESSION" not in config.sdk_environment
    assert "sentinel-secret" not in caplog.text


def test_small_batch_queue_also_bounds_default_batch_size():
    config = read_config({PREFIX + "BSP_MAX_QUEUE_SIZE": "8", PREFIX + "BLRP_MAX_QUEUE_SIZE": "4"})
    assert config.sdk_environment["OTEL_BSP_MAX_EXPORT_BATCH_SIZE"] == "8"
    assert config.sdk_environment["OTEL_BLRP_MAX_EXPORT_BATCH_SIZE"] == "4"


@pytest.mark.parametrize("sampler", ["parentbased_traceidratio", "sentinel-secret"])
@pytest.mark.parametrize("ratio", ["0", "1", "-1", "1.1", "nan", "sentinel-secret"])
def test_sampler_ratio_bounds_and_invalid_values_are_private(sampler, ratio, caplog):
    config = read_config({PREFIX + "TRACES_SAMPLER": sampler, PREFIX + "TRACES_SAMPLER_ARG": ratio})
    assert config.sdk_environment["OTEL_TRACES_SAMPLER"] == (
        sampler if sampler != "sentinel-secret" else "parentbased_always_on"
    )
    if ratio in {"0", "1"}:
        assert config.sdk_environment["OTEL_TRACES_SAMPLER_ARG"] == str(float(ratio))
    else:
        assert "OTEL_TRACES_SAMPLER_ARG" not in config.sdk_environment
    assert "sentinel-secret" not in caplog.text


def test_removed_optional_session_switch_has_no_configuration_surface():
    config = read_config({PREFIX + "REQUIRE_SESSION": "false"})
    assert not hasattr(config, "require_session")
    assert "OTEL_REQUIRE_SESSION" not in config.sdk_environment
