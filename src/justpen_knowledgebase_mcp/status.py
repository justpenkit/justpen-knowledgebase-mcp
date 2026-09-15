"""Bounded SQL-free public status and one lifespan-owned sampling loop."""

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from .config import WorkspacePolicy
from .errors import McpError
from .models import ClosedModel, IndexCoverage, RetentionStatus
from .storage.status import sample_status

if TYPE_CHECKING:
    from .storage.worker import DatabaseWorkers

Count = Annotated[int, Field(ge=0)]


class JobCounts(ClosedModel):
    """Current durable job states sampled in a single read transaction."""

    queued: Count
    running: Count
    completed: Count
    failed: Count
    cancelled: Count


class PropertyFallback(ClosedModel):
    """Ready records whose projection cannot prove every property predicate."""

    nodes: Count
    relations: Count


class DatabaseSample(ClosedModel):
    """Measured schema, shared policy and bounded aggregate counts."""

    schema_version: Count
    catalog_version: Count
    index_format_version: Count
    policy: WorkspacePolicy
    jobs: JobCounts
    index_coverage: IndexCoverage
    property_index_fallback: PropertyFallback


class DatabaseStatus(ClosedModel):
    """A prior sample is retained on errors; unknown is never a healthy zero."""

    available: bool = False
    stale: bool = True
    cached_at: float | None = None
    cache_age: float | None = None
    last_error: Annotated[str, Field(max_length=32)] | None = None
    sample: DatabaseSample | None = None


class WalStatus(ClosedModel):
    """Preserve maintenance cache diagnostics during SQL admission reset."""

    phase: Literal["normal", "pressure", "assessment_pending", "unknown", "reset"] = "unknown"
    sample_seq: int = Field(default=0, ge=0)
    sample_at: float | None = None
    allocated_bytes: int | None = Field(default=None, ge=0)
    log_frames: int | None = Field(default=None, ge=0)
    checkpointed_frames: int | None = Field(default=None, ge=0)
    page_size: int | None = Field(default=None, gt=0)
    unbackfilled_bytes: int | None = Field(default=None, ge=0)
    pressure_started_at: float | None = None
    attempt_id: str | None = None
    attempt_started_at: float | None = None
    attempt_finished_at: float | None = None
    next_attempt_not_before: float | None = None
    checkpoint_mode: str | None = None
    last_attempt: str | None = None
    sample_age: float | None
    cache_age: float | None
    stale_normal: bool
    unavailable: bool
    evaluation_requested: bool
    pressure_elapsed: float | None
    retry_after_ms: Annotated[int, Field(ge=1000, le=30000)]
    estimated_completion_ms: None = None
    reason: Literal["WAL_PRESSURE", "RESET_PENDING"] | None = None


class DatabaseQueues(ClosedModel):
    """Process-local admission state, without record or job identifiers."""

    read_queued: Count
    write_queued: Count
    control_queued: Count
    reader_running: Count
    writer_running: Count
    stopping: bool


class IOLane(ClosedModel):
    """Admitted queued plus running native callbacks in one serial I/O lane."""

    pending: Count
    capacity: Literal[32] = 32


class IOQueues(ClosedModel):
    """Independent short/bulk lanes; neither grows beyond its admission bound."""

    short: IOLane
    bulk: IOLane
    stopping: bool


class Capabilities(ClosedModel):
    """Fixed limits and effective process capacities, never throughput promises."""

    query_timeout_ms: int
    db_busy_timeout_ms: int
    db_reader_threads: int
    writer_queue: Literal[128] = 128
    reader_queue: Literal[128] = 128
    control_queue: Literal[16] = 16
    io_lane_capacity: Literal[32] = 32
    io_short_threads: Literal[1] = 1
    io_bulk_threads: Literal[1] = 1
    checkpoint_connections: Literal[1] = 1
    response_bytes: Literal[262144] = 262144
    properties_bytes: Literal[65536] = 65536
    property_index_paths: Literal[512] = 512
    property_index_path_bytes: Literal[1024] = 1024
    property_index_value_bytes: Literal[1024] = 1024
    json_depth: Literal[16] = 16
    write_records: Literal[100] = 100
    read_ids: Literal[100] = 100
    link_mutations: Literal[100] = 100
    query_bytes: Literal[2048] = 2048
    query_words_distinct_tokens: Literal[32] = 32
    query_literal_total_tokens: Literal[32] = 32
    page_default: Literal[20] = 20
    page_max: Literal[100] = 100
    evidence_inline_bytes: Literal[262144] = 262144
    evidence_read_bytes: Literal[65536] = 65536
    neighbors_depth: Literal[3] = 3
    neighbors_nodes: Literal[1000] = 1000
    neighbors_edges: Literal[3000] = 3000
    job_lease_ms: Literal[30000] = 30000
    job_heartbeat_ms: Literal[5000] = 5000
    shutdown_grace_ms: Literal[30000] = 30000
    status_sample_interval_ms: Literal[30000] = 30000


class StatusResult(ClosedModel):
    """One configured workspace and engagement; no request-level isolation key."""

    deployment_scope: Literal["single_workspace"] = "single_workspace"
    engagement_scope: Literal["single_engagement"] = "single_engagement"
    request_workspace_selection: Literal[False] = False
    canonical_session_source: Literal["JUSTPEN_SESSION_ID"] = "JUSTPEN_SESSION_ID"
    bind_scope: Literal["stdio", "loopback", "non_loopback"]
    authentication: Literal["none"] = "none"
    database: DatabaseStatus
    wal: WalStatus
    retention: RetentionStatus
    database_queues: DatabaseQueues
    io_queues: IOQueues
    capabilities: Capabilities


class StatusSampler:
    """One initial and then nonoverlapping 30-second ordinary reader sample."""

    def __init__(self, workers: "DatabaseWorkers") -> None:
        """Allocate cache only; the service explicitly owns start and close."""
        self.workers = workers
        self._cache = DatabaseStatus()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Collect an initial bounded sample before scheduling periodic work."""
        await self.refresh()
        self._task = asyncio.create_task(self._run(), name="kb-status-sampler")

    async def refresh(self) -> None:
        """Ordinary admission and its absolute deadline apply to all aggregates."""
        async with self._lock:
            try:
                sample = DatabaseSample.model_validate(await self.workers.read(sample_status))
            except (McpError, OSError, ValueError) as exc:
                self._cache = self._cache.model_copy(
                    update={
                        "stale": True,
                        "last_error": exc.error_type
                        if isinstance(exc, McpError)
                        else "IO_ERROR"
                        if isinstance(exc, OSError)
                        else "INTERNAL",
                    }
                )
            else:
                self._cache = DatabaseStatus(available=True, stale=False, cached_at=time.time(), sample=sample)

    def snapshot(self) -> dict[str, Any]:
        """Materialize cached data without database/file work or waiting."""
        result = self._cache.model_dump(mode="json")
        stamp = self._cache.cached_at
        age = None if stamp is None else max(0, time.time() - stamp)
        result.update(cache_age=age, stale=self._cache.stale or age is None or age >= 60)
        return result

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(30)
            await self.refresh()

    async def close(self) -> None:
        """Cancel and join the sampler before database owners are drained."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
