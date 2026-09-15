"""Closed graph requests; nested model field sets preserve metadata presence."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .identity import EvidenceID, parse_timestamp, validate_evidence_id, validate_record_id
from .mutations import validate_properties
from .query import validate_filter
from .responses import BlockerDetails

Kind = Literal["nodes", "relations", "evidence"]
GraphKind = Literal["nodes", "relations"]
RecordID = Annotated[str, Field(min_length=36, max_length=36)]


class ClosedModel(BaseModel):
    """Reject coercion and unknown public fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


class NodeRef(ClosedModel):
    """Exactly one existing UUID or zero-based node batch index."""

    id: RecordID | None = None
    node_index: Annotated[int, Field(ge=0, le=99)] | None = None

    @model_validator(mode="after")
    def exclusive(self) -> Self:
        """Require a single non-null reference field."""
        if self.model_fields_set not in ({"id"}, {"node_index"}) or (self.id is None and self.node_index is None):
            raise ValueError("exactly one id or node_index required")
        if self.id is not None:
            UUID(self.id)
        return self


class TargetRef(ClosedModel):
    """An evidence association's graph target."""

    kind: GraphKind
    id: RecordID


class Mutation(ClosedModel):
    """Shared node/relation patch fields and bounded evidence mutations."""

    id: RecordID | None = None
    type: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    source: str | None = None
    observed_at: str | None = None
    remove_properties: list[str] = Field(default_factory=list, max_length=100)
    evidence_add: list[EvidenceID] = Field(default_factory=list, max_length=100)
    evidence_remove: list[EvidenceID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_mutation(self) -> Self:
        """Validate metadata without losing explicit null or omission."""
        if self.id is None and (self.type is None or "properties" not in self.model_fields_set):
            raise ValueError("creation requires type and properties")
        if self.id is not None:
            UUID(self.id)
        if "observed_at" in self.model_fields_set:
            if self.observed_at is None:
                raise ValueError("observed_at cannot be null")
            parse_timestamp(self.observed_at)
        if self.source is not None and len(self.source.encode("utf-8")) > 256:
            raise ValueError("source exceeds 256 bytes")
        validate_properties(self.properties)
        for identifiers in (self.evidence_add, self.evidence_remove):
            for identifier in identifiers:
                validate_evidence_id(identifier)
            if len(identifiers) != len(set(identifiers)):
                raise ValueError("duplicate evidence mutation")
        if set(self.evidence_add) & set(self.evidence_remove):
            raise ValueError("conflicting evidence mutation")
        return self


class NodeWrite(Mutation):
    """Node upsert or ID patch with optional label presence."""

    label: str | None = None

    @field_validator("label")
    @classmethod
    def label_limit(cls, value: str | None) -> str | None:
        """Bound label in UTF-8 bytes."""
        if value is not None and len(value.encode("utf-8")) > 512:
            raise ValueError("label exceeds 512 bytes")
        return value


class RelationWrite(Mutation):
    """Relation upsert or ID patch; existing endpoints are immutable."""

    source_ref: NodeRef | None = None
    target_ref: NodeRef | None = None

    @model_validator(mode="after")
    def endpoints(self) -> Self:
        """Require both graph endpoints for creation."""
        if self.id is None and (self.source_ref is None or self.target_ref is None):
            raise ValueError("creation requires both endpoints")
        return self


class WriteRequest(ClosedModel):
    """One atomic batch with independent record and link budgets."""

    nodes: list[NodeWrite] = Field(default_factory=list[NodeWrite], max_length=100)
    relations: list[RelationWrite] = Field(default_factory=list[RelationWrite], max_length=100)

    @model_validator(mode="after")
    def budgets(self) -> Self:
        """Enforce aggregate budgets across both record kinds."""
        records: list[Mutation] = [*self.nodes, *self.relations]
        if not 1 <= len(records) <= 100:
            raise ValueError("write requires 1 to 100 records")
        if sum(len(record.evidence_add) + len(record.evidence_remove) for record in records) > 100:
            raise ValueError("write exceeds 100 link mutations")
        return self


class GetRequest(ClosedModel):
    """Bounded record or single-owner association retrieval."""

    kind: Kind
    ids: list[RecordID | EvidenceID] = Field(min_length=1, max_length=100)
    view: Literal["record", "links", "sources"] = "record"
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    cursor: str | None = None

    @model_validator(mode="after")
    def view_rules(self) -> Self:
        """Bind association views to exactly one valid owner."""
        for identifier in self.ids:
            validate_record_id(self.kind, identifier)
        if self.view != "record" and len(self.ids) != 1:
            raise ValueError("association view requires one id")
        if self.view == "sources" and self.kind != "evidence":
            raise ValueError("sources is evidence-only")
        if self.view == "record" and self.cursor is not None:
            raise ValueError("record view uses remaining_ids")
        return self


class DeleteRequest(ClosedModel):
    """Atomic admission of a bounded unique target set."""

    kind: Kind
    ids: list[RecordID | EvidenceID] = Field(min_length=1, max_length=100)
    cascade: bool = False

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        """Reject duplicate targets before database state checks."""
        for identifier in self.ids:
            validate_record_id(self.kind, identifier)
        if len(set(self.ids)) != len(self.ids):
            raise ValueError("duplicate delete ids")
        return self


class TypesRequest(ClosedModel):
    """Controlled type discovery with optional detail selection."""

    kind: GraphKind
    type: str | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    cursor: str | None = None


class PropertyIndexCoverage(ClosedModel):
    """Independent path, non-array path, and materialized value coverage."""

    complete: bool
    paths_complete: bool
    non_array_complete: bool
    indexed_paths: Annotated[int, Field(ge=0, le=512)]
    total_paths: Annotated[int, Field(ge=0)]
    omitted_values: Annotated[int, Field(ge=0, le=512)]


class MutationResult(ClosedModel):
    """Compact mutation acknowledgment, never a duplicate full property payload."""

    id: RecordID
    created: bool
    updated: bool
    links_added: Annotated[int, Field(ge=0, le=100)]
    links_removed: Annotated[int, Field(ge=0, le=100)]
    property_index: PropertyIndexCoverage


class WriteResult(ClosedModel):
    """Bounded per-kind acknowledgments for an atomic graph write."""

    nodes: list[MutationResult] = Field(max_length=100)
    relations: list[MutationResult] = Field(max_length=100)


class RecordViewResult(ClosedModel):
    """Full records and explicit missing or response-budget remainder IDs."""

    records: list[dict[str, Any]] = Field(max_length=100)
    missing_ids: list[RecordID | EvidenceID] = Field(max_length=100)
    remaining_ids: list[RecordID | EvidenceID] = Field(max_length=100)


class LinksViewResult(ClosedModel):
    """Stable association page for one graph or evidence owner."""

    links: list[EvidenceID | TargetRef] = Field(max_length=100)
    next_cursor: str | None


class EvidenceSource(ClosedModel):
    """Provenance label and observation bounds, without a managed path."""

    source: str
    first_seen_at: str
    last_seen_at: str


class SourcesViewResult(ClosedModel):
    """One bounded evidence provenance page."""

    sources: list[EvidenceSource] = Field(max_length=100)
    next_cursor: str | None


GetResult = RecordViewResult | LinksViewResult | SourcesViewResult


class SearchRequest(ClosedModel):
    """Exact graph selection in stable ID order; text arrives separately."""

    kind: GraphKind
    type: str | None = None
    key: str | None = None
    source: str | None = None
    source_id: RecordID | None = None
    target_id: RecordID | None = None
    observed_at_min: str | None = None
    observed_at_max: str | None = None
    properties: dict[str, Any] | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    cursor: str | None = None

    @model_validator(mode="after")
    def selection_rules(self) -> Self:
        """Validate predicate complexity and kind-specific explicit fields."""
        if self.kind == "nodes" and self.model_fields_set & {"source_id", "target_id"}:
            raise ValueError("endpoint filters require relations")
        for identifier in (self.source_id, self.target_id):
            if identifier is not None:
                UUID(identifier)
        for timestamp in (self.observed_at_min, self.observed_at_max):
            if timestamp is not None:
                parse_timestamp(timestamp)
        if self.properties is not None:
            validate_filter(self.properties)
        return self


class NeighborsRequest(ClosedModel):
    """Bounded breadth-first seeds, depth and output budgets."""

    seed_ids: list[RecordID] = Field(min_length=1, max_length=1000)
    direction: Literal["in", "out", "both"] = "both"
    relation_types: list[str] | None = Field(default=None, max_length=100)
    depth: Annotated[int, Field(ge=0, le=3)] = 1
    max_nodes: Annotated[int, Field(ge=1, le=1000)] = 100
    max_edges: Annotated[int, Field(ge=0, le=3000)] = 300

    @model_validator(mode="after")
    def seed_budget(self) -> Self:
        """Reject duplicate/oversized seeds before any database work."""
        if len(self.seed_ids) > self.max_nodes or len(set(self.seed_ids)) != len(self.seed_ids):
            raise ValueError("seed budget exceeded or duplicate seed")
        for identifier in self.seed_ids:
            UUID(identifier)
        return self


class SearchSummary(ClosedModel):
    """A bounded graph record summary, without canonical properties."""

    id: RecordID
    type: str
    key: str
    source: str | None = None
    label: str | None = None
    property_index: PropertyIndexCoverage | None = None


class SearchResult(ClosedModel):
    """Exact graph search completion and canonical evaluation accounting."""

    results: list[SearchSummary] = Field(max_length=100)
    next_cursor: str | None
    has_more: bool
    property_filter_mode: Literal["index_only", "canonical_fallback"]
    canonical_scan_count: Annotated[int, Field(ge=0)]
    incomplete: bool


class NeighborNode(ClosedModel):
    """Bounded graph node identity for traversal."""

    id: RecordID
    type: str


class NeighborEdge(NeighborNode):
    """A directed stored relation, without inferred facts."""

    source_id: RecordID
    target_id: RecordID


class NeighborsResult(ClosedModel):
    """Bounded traversal and unexpanded frontier."""

    nodes: list[NeighborNode] = Field(max_length=1000)
    edges: list[NeighborEdge] = Field(max_length=3000)
    truncated: bool
    reason: Literal["max_nodes", "max_edges", "deadline", "response_bytes"] | None
    frontier: list[RecordID] = Field(max_length=1000)


class JobProgress(ClosedModel):
    """Only bounded operational counts leave the durable checkpoint store."""

    bytes: Annotated[int, Field(ge=0)] = 0
    chunks: Annotated[int, Field(ge=0)] = 0
    rows_deleted: Annotated[int, Field(ge=0)] = 0


class JobResult(ClosedModel):
    """One public job state, independent of private path and token bookkeeping."""

    job_id: RecordID
    kind: Literal["ingest", "delete", "reindex"]
    state: Literal["queued", "running", "completed", "failed", "cancelled"]
    lane: Literal["short", "bulk"]
    attempts: Annotated[int, Field(ge=0)]
    progress: JobProgress = Field(default_factory=JobProgress)
    effective_media_type: str | None = None
    index_state: Literal["pending", "ready", "not_applicable", "index_failed"]
    incomplete: bool = False
    evidence_id: EvidenceID | None = None
    warnings: list[Annotated[str, Field(max_length=256)]] = Field(default_factory=list[str], max_length=101)
    needs_attention: bool = False
    purge_pending: bool = False
    error: Annotated[str, Field(max_length=32)] | None = None
    reason: Annotated[str, Field(max_length=256)] | None = None
    deleted_ids: list[RecordID | EvidenceID] = Field(default_factory=list[str], max_length=100)
    status: Literal["accepted", "completed", "failed", "cancelled"] | None = None

    details: BlockerDetails | None = None

    @model_validator(mode="after")
    def blocker_presence(self) -> Self:
        """Pending failures keep the same typed bounded blocker as direct operations."""
        if self.reason == "RECORD_DELETING" and not isinstance(self.details, BlockerDetails):
            raise ValueError("pending failure requires blocker details")
        return self
