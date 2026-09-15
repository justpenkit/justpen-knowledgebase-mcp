import uuid

import pytest

from justpen_knowledgebase_mcp.telemetry.config import read_config
from justpen_knowledgebase_mcp.telemetry.resource import build_resource

PREFIX = "JUSTPEN_KNOWLEDGEBASE_OTEL_"


@pytest.mark.parametrize(
    "extra",
    [
        "justpen.session.id=other",
        "justpen.session.id=real",
        "justpen.session.id=first,justpen.session.id=last",
        "%6Austpen.session.id=encoded",
    ],
)
@pytest.mark.parametrize("session", [None, "", "invalid id", "real"])
def test_session_identity_has_exactly_one_source(extra, session, caplog):
    env = {
        PREFIX + "RESOURCE_ATTRIBUTES": extra + ",deployment.environment.name=test,justpen.run.id=run-1",
        "OTEL_RESOURCE_ATTRIBUTES": "justpen.session.id=ambient",
    }
    if session is not None:
        env["JUSTPEN_SESSION_ID"] = session
    resource = build_resource(read_config(env), service_version="0.7.0")
    assert resource.attributes.get("justpen.session.id") == ("real" if session == "real" else None)
    assert resource.attributes["justpen.run.id"] == "run-1"
    assert resource.attributes["deployment.environment.name"] == "test"
    assert "ambient" not in caplog.text
    assert "invalid id" not in caplog.text


def test_mandatory_session_cannot_be_satisfied_by_resource_attribute():
    config = read_config(
        {
            PREFIX + "ENABLED": "true",
            PREFIX + "RESOURCE_ATTRIBUTES": "justpen.session.id=forbidden",
        }
    )
    with pytest.raises(ValueError, match="JUSTPEN_SESSION_ID"):
        build_resource(config, service_version="0.7.0")
    disabled = read_config({})
    assert "justpen.session.id" not in build_resource(disabled, service_version="0.7.0").attributes


def test_resource_has_explicit_service_identity_without_ambient_detection(monkeypatch):
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "unwanted=ambient,justpen.session.id=ambient")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "wrong-service")
    config = read_config({PREFIX + "RESOURCE_ATTRIBUTES": "service.version=fake,service.name=custom"})
    resource = build_resource(config, service_version="0.7.0")
    assert resource.attributes["service.name"] == "custom"
    assert resource.attributes["service.version"] == "0.7.0"
    assert "unwanted" not in resource.attributes
    uuid.UUID(str(resource.attributes["service.instance.id"]))


def test_default_resource_identifies_knowledgebase_service():
    resource = build_resource(read_config({"JUSTPEN_SESSION_ID": "pentest-a"}), service_version="0.1.1")
    assert resource.attributes["service.name"] == "justpen-knowledgebase-mcp"
    assert resource.attributes["justpen.session.id"] == "pentest-a"


def test_service_override_and_encoded_extra_values_are_preserved():
    config = read_config(
        {
            "JUSTPEN_SESSION_ID": "pentest-123",
            PREFIX + "SERVICE_NAME": "utility-worker",
            PREFIX + "RESOURCE_ATTRIBUTES": "service.name=other,service.instance.id=worker-1,note=a%2Cb%3Dc,broken",
        }
    )
    resource = build_resource(config, service_version="0.7.0")
    assert resource.attributes["service.name"] == "utility-worker"
    assert resource.attributes["service.instance.id"] == "worker-1"
    assert resource.attributes["note"] == "a,b=c"


def test_invalid_run_id_does_not_change_session():
    config = read_config({"JUSTPEN_SESSION_ID": "real", PREFIX + "RESOURCE_ATTRIBUTES": "justpen.run.id=bad%20id"})
    resource = build_resource(config, service_version="0.7.0")
    assert resource.attributes["justpen.session.id"] == "real"
    assert "justpen.run.id" not in resource.attributes


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {PREFIX + "REQUIRE_SESSION": "false"},
        {PREFIX + "TRACES_ENABLED": "false", PREFIX + "LOGS_ENABLED": "false", PREFIX + "METRICS_ENABLED": "false"},
    ],
)
def test_master_enable_always_requires_canonical_session(extra):
    with pytest.raises(ValueError, match="JUSTPEN_SESSION_ID"):
        build_resource(read_config({PREFIX + "ENABLED": "true", **extra}), service_version="test")
