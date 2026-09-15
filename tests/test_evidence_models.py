"""Pure evidence request contracts, before filesystem admission."""

import base64
from uuid import uuid4

import pytest
from pydantic import ValidationError

from justpen_knowledgebase_mcp import evidence, models
from justpen_knowledgebase_mcp.cursors import CursorBinding
from justpen_knowledgebase_mcp.models import DeleteRequest, GetRequest, WriteRequest


@pytest.mark.parametrize("body", [{}, {"text": "a", "path": "a"}, {"text": None}, {"base64": "%%%"}, {"path": ""}])
def test_exactly_one_valid_source(body):
    with pytest.raises(ValidationError):
        evidence.IngestRequest.model_validate(body)


def test_inline_limit_is_decoded_bytes():
    assert evidence.IngestRequest(text="é" * 131072).inline_size == 262144
    assert evidence.IngestRequest(base64=base64.b64encode(b"x" * 262144).decode()).inline_size == 262144
    for body in ({"text": "é" * 131073}, {"base64": base64.b64encode(b"x" * 262145).decode()}):
        with pytest.raises(ValidationError):
            evidence.IngestRequest.model_validate(body)


@pytest.mark.parametrize(
    "media",
    [
        "text/plain",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
        "message/http",
        "application/problem+json",
        "application/example+xml",
    ],
)
def test_explicit_text_candidate_preserves_media(media):
    request = evidence.IngestRequest(path="capture.bin", media_type=media)
    assert request.effective_media_type == media
    assert request.text_candidate
    assert request.warnings == []


def test_defaults_do_not_sniff_filename_or_force_binary_decoder():
    request = evidence.IngestRequest(path="nmap.xml")
    assert request.effective_media_type == "application/octet-stream"
    assert request.warnings == ["MEDIA_TYPE_DEFAULTED_TEXT_INDEX_SKIPPED"]
    assert not request.text_candidate
    assert evidence.IngestRequest(text="x").effective_media_type == "text/plain"
    explicit = evidence.IngestRequest(base64="AA==", media_type="application/octet-stream")
    assert explicit.warnings == []
    assert not explicit.text_candidate


@pytest.mark.parametrize(
    "body",
    [
        {"media_type": "Text/plain"},
        {"media_type": "text/plain;charset=utf-8"},
        {"media_type": "image/png", "encoding": "utf-8"},
        {"targets": [{"kind": "evidence", "id": str(uuid4())}]},
        {"targets": [{"kind": "nodes", "id": "x" * 36}]},
    ],
)
def test_invalid_media_encoding_and_targets(body):
    with pytest.raises(ValidationError):
        evidence.IngestRequest.model_validate({"text": "hello", **body})


@pytest.mark.parametrize(
    "body", [{"offset": -1}, {"length": 65537}, {"length": -1}, {"format": "raw"}, {"offset": True}]
)
def test_read_range_bounds(body):
    with pytest.raises(ValidationError):
        evidence.ReadEvidenceRequest.model_validate({"evidence_id": "e_" + "a" * 64, **body})


def test_evidence_ids_are_kind_sensitive_across_get_delete_links():

    identifier = "e_" + "a" * 64
    assert GetRequest(kind="evidence", ids=[identifier]).ids == [identifier]
    assert DeleteRequest(kind="evidence", ids=[identifier]).ids == [identifier]
    assert WriteRequest.model_validate({"nodes": [{"id": str(uuid4()), "evidence_add": [identifier]}]})
    for body in (
        {"kind": "evidence", "ids": [str(uuid4())]},
        {"kind": "nodes", "ids": [identifier]},
        {"kind": "evidence", "ids": [identifier.upper()]},
    ):
        with pytest.raises(ValidationError):
            GetRequest.model_validate(body)


def test_evidence_cursor_owner_is_hash_and_workspace_is_uuid():

    binding = CursorBinding(str(uuid4()), 0, "evidence", "e_" + "a" * 64, "sources", {})
    assert binding.decode(binding.encode(7)) == 7


def test_job_result_is_closed_and_hides_internal_stage_state():
    result = models.JobResult.model_validate(
        {
            "job_id": str(uuid4()),
            "kind": "ingest",
            "state": "queued",
            "lane": "short",
            "attempts": 1,
            "progress": {"bytes": 4, "chunks": 0},
            "index_state": "pending",
        }
    )
    assert result.progress.bytes == 4
    with pytest.raises(ValidationError):
        models.JobResult.model_validate({**result.model_dump(), "input_stage": "private"})
    with pytest.raises(ValidationError):
        models.JobResult.model_validate({**result.model_dump(), "progress": {"stage_token": "private"}})


def test_read_result_rejects_unbounded_range_or_private_locator():
    body = {
        "evidence_id": "e_" + "a" * 64,
        "sha256": "a" * 64,
        "total_size": 100000,
        "returned_range": {"offset": 0, "length": 65537},
        "format": "base64",
        "content": "",
    }
    with pytest.raises(ValidationError):
        evidence.EvidenceReadResult.model_validate(body)
    body["returned_range"] = {"offset": 0, "length": 0}
    body["blob_path"] = "private"
    with pytest.raises(ValidationError):
        evidence.EvidenceReadResult.model_validate(body)


@pytest.mark.parametrize(
    ("kind", "owner"),
    [("evidence", "00000000-0000-0000-0000-000000000001"), ("evidence", "e_" + "A" * 64), ("nodes", "e_" + "a" * 64)],
)
def test_cursor_owner_rejects_wrong_kind_or_noncanonical_hash(kind, owner):
    binding = CursorBinding(str(uuid4()), 0, kind, owner, "sources", {})
    with pytest.raises(ValueError):
        binding.decode(binding.encode(1))


def test_media_type_shared_input_and_public_metadata_bound():
    valid = "image/" + "x" * 249
    assert evidence.IngestRequest(text="x", media_type=valid).media_type == valid
    job = {
        "job_id": str(uuid4()),
        "kind": "ingest",
        "state": "queued",
        "lane": "short",
        "attempts": 0,
        "index_state": "not_applicable",
        "effective_media_type": valid,
    }
    assert models.JobResult.model_validate(job).effective_media_type == valid
    for invalid in (valid + "x", "image/" + "x" * 300000, "image/é", "image/x\n"):
        with pytest.raises(ValidationError):
            evidence.IngestRequest(text="x", media_type=invalid)
        with pytest.raises(ValidationError):
            models.JobResult.model_validate({**job, "effective_media_type": invalid})
