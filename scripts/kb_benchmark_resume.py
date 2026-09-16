"""Exclusive benchmark ownership and exhaustive bounded continuation validation."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from justpen_knowledgebase_mcp.identity import identity_key

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from benchmark_knowledgebase import Measurements

    from justpen_knowledgebase_mcp.service import KnowledgeBase

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def benchmark_owner(output: Path) -> Generator[None]:
    """Hold one nonblocking owner lock for every new run and continuation."""
    fd = os.open(output / ".benchmark-owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError("benchmark owner lock must be a single-link regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another benchmark still owns this output") from error
        yield
    finally:
        os.close(fd)


def core_hashes() -> dict[str, str]:
    """Compare every product Python file, never infer compatibility from HEAD alone."""
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "src").rglob("*.py"))
    }


def previous_attempt(output: Path, scale: str, seed: int) -> dict[str, Any]:
    """Validate provenance before opening a mutable runtime or archiving anything."""
    source = output / "report.json"
    previous = json.loads(source.read_text())
    if previous.get("scale") != scale or previous.get("seed") != seed or previous.get("status") != "partial":
        raise RuntimeError("resume requires this scale/seed's terminal partial attempt")
    provenance = previous.get("provenance_at_start")
    if provenance is None:
        raise RuntimeError("missing start provenance")
    recorded = {name: digest for name, digest in provenance["python_source_sha256"].items() if name.startswith("src/")}
    if recorded != core_hashes():
        raise RuntimeError("product source changed; resume compatibility not established")
    previous["archived_report_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    return previous


def archive_attempt(output: Path) -> str:
    """Preserve exact previous bytes in an exclusive, read-only attempt artifact."""
    source = output / "report.json"
    number = len(list(output.glob("report-attempt-*.json"))) + 1
    target = output / f"report-attempt-{number:03}.json"
    with target.open("xb") as destination:
        destination.write(source.read_bytes())
    target.chmod(0o444)
    return target.name


async def settle_jobs(measure: Measurements, kb: KnowledgeBase) -> None:
    """Keep source bytes intact until existing accepted work settles or retry succeeds."""
    rows = await kb.workers.read(
        lambda c, _t: c.execute(
            "select uuid,state,error_code from jobs where state!='completed' order by id"
        ).fetchall()
    )
    settled: list[dict[str, Any]] = []
    measure.report["resume_job_reconciliation"] = settled
    for identifier, state, error in rows:
        measure.check()
        if not isinstance(identifier, str):
            raise TypeError("invalid durable job identifier")
        if state == "failed":
            if error not in {"BUSY", "LIMIT"}:
                raise RuntimeError(f"resume refuses nonretryable prior job {identifier}: {error}")
            response = await kb.jobs({"action": "retry", "job_id": identifier})
            settled.append({"job_id": identifier, "prior_state": state, "retry_response": response})
        elif state not in {"queued", "running"}:
            raise RuntimeError(f"resume refuses prior job state {state}")
        result = await kb.job_runner.wait(identifier, min(measure.deadline, time.monotonic() + 120))
        if result["state"] != "completed":
            raise RuntimeError(f"prior durable work did not settle: {result}")
        settled.append({"job_id": identifier, "final": result})


async def validate_nodes(
    measure: Measurements, kb: KnowledgeBase, expected_node: Callable[[int], dict[str, Any]]
) -> int:
    """Validate a complete prefix of generated node batches,1000rows at a time."""
    ordinal = after = 0
    while True:
        measure.check()
        rows = await kb.workers.read(
            lambda c, _t, after=after: c.execute(
                "select id,uuid,type,properties,metadata,lifecycle,key from nodes where id>? order by id limit 1000",
                (after,),
            ).fetchall()
        )
        if not rows:
            break
        for identifier, uuid, kind, raw, metadata, lifecycle, key in rows:
            if (
                not isinstance(identifier, int)
                or not isinstance(uuid, str)
                or not isinstance(raw, str)
                or not isinstance(metadata, str)
            ):
                raise TypeError("invalid canonical node storage types")
            if (
                ordinal >= measure.nodes
                or kind != "endpoint"
                or json.loads(raw) != expected_node(ordinal)
                or key != identity_key("nodes", "endpoint", json.loads(raw))
                or json.loads(metadata).get("label") is not None
                or json.loads(metadata).get("source") is not None
                or lifecycle != "ready"
            ):
                raise RuntimeError(f"foreign/mismatched canonical node at ordinal{ordinal}")
            if ordinal == 0:
                measure.hub = uuid
            ordinal += 1
            after = identifier
    if ordinal % 100 or ordinal > measure.nodes:
        raise RuntimeError("canonical node count is not a completed batch prefix")
    return ordinal


async def validate_relations(measure: Measurements, kb: KnowledgeBase, nodes: int) -> int:
    """Validate every relation batch against its original canonical target page."""
    ordinal = after = 0
    while True:
        measure.check()
        rows = await kb.workers.read(
            lambda c, _t, after=after: c.execute(
                "select r.id,r.type,r.properties,s.uuid,t.id,r.lifecycle,r.key from relations r join nodes s on s.id=r.source_id join nodes t on t.id=r.target_id where r.id>? order by r.id limit 1000",
                (after,),
            ).fetchall()
        )
        if not rows:
            break
        for first in range(0, len(rows), 100):
            offset = (ordinal % (measure.nodes - 1)) + 1
            targets = await kb.workers.read(
                lambda c, _t, offset=offset: c.execute(
                    "select id from nodes where id>? order by id limit 100", (offset,)
                ).fetchall()
            )
            if not targets:
                raise RuntimeError("resume relation page has no canonical target prefix")
            for index, (identifier, kind, raw, source, target, lifecycle, key) in enumerate(rows[first : first + 100]):
                if not isinstance(identifier, int) or not isinstance(raw, str):
                    raise TypeError("invalid canonical relation storage types")
                if (
                    ordinal >= measure.edges
                    or kind != "redirects_to"
                    or key != identity_key("relations", "redirects_to", json.loads(raw))
                    or source != measure.hub
                    or target != targets[index % len(targets)][0]
                    or json.loads(raw) != {"context": f"seed{measure.report['seed']}-edge{ordinal}"}
                    or lifecycle != "ready"
                ):
                    raise RuntimeError(f"foreign/mismatched canonical relation at ordinal{ordinal}")
                ordinal += 1
                after = identifier
    edges = ordinal
    if edges % 100 or (edges and nodes != measure.nodes):
        raise RuntimeError("canonical relation count is not a completed batch prefix")
    return edges


async def require_blob(kb: KnowledgeBase, digest: str, size: int, check: Callable[[], None]) -> None:
    """Verify actual owned bytes for both inline and path-backed evidence."""
    if await asyncio.to_thread(kb.job_runner.store.verify_blob, digest, size, check) is None:
        raise RuntimeError("canonical evidence blob missing")


async def validate_evidence(
    measure: Measurements, kb: KnowledgeBase, expected_text: Callable[[int, int], bytes]
) -> int:
    """Verify every expected source digest and actual blob before source reuse."""
    raw_bytes = ordinal = after = 0
    inline_hash = hashlib.sha256(b"small inline proof").hexdigest()
    while True:
        measure.check()
        rows = await kb.workers.read(
            lambda c, _t, after=after: c.execute(
                "select e.id,e.uuid,e.sha256,e.byte_size,e.index_state,e.lifecycle,"
                "(select count(*) from node_evidence ne where ne.evidence_id=e.id),"
                "(select count(*) from node_evidence ne join nodes n on n.id=ne.node_id where ne.evidence_id=e.id and n.uuid=?),"
                "(select count(*) from relation_evidence re where re.evidence_id=e.id) "
                "from evidence e where e.id>? order by e.id limit 32",
                (measure.hub, after),
            ).fetchall()
        )
        if not rows:
            break
        for identifier, uuid, digest, size, state, lifecycle, node_links, hub_links, relation_links in rows:
            if (
                not isinstance(identifier, int)
                or not isinstance(uuid, str)
                or not isinstance(digest, str)
                or not isinstance(size, int)
            ):
                raise TypeError("invalid canonical evidence storage types")
            if uuid != "e_" + digest or state != "ready" or lifecycle != "ready":
                raise RuntimeError("canonical evidence is not ready after reconciliation")
            is_inline = digest == inline_hash and size == 18
            expected_links = 0 if is_inline else 1
            if (node_links, hub_links, relation_links) != (expected_links, expected_links, 0):
                raise RuntimeError("foreign/missing per-blob canonical evidence links")
            if is_inline:
                await require_blob(kb, digest, size, measure.check)
                measure.inline_present = True
                after = identifier
                continue
            expected_size = min(1048576, measure.raw_bytes - raw_bytes)
            if (
                expected_size <= 0
                or size != expected_size
                or digest != hashlib.sha256(expected_text(ordinal, size)).hexdigest()
                or uuid != "e_" + digest
                or state != "ready"
                or lifecycle != "ready"
            ):
                raise RuntimeError(f"foreign/mismatched evidence at ordinal{ordinal}")
            await require_blob(kb, digest, size, measure.check)
            raw_bytes += size
            ordinal += 1
            after = identifier
    return raw_bytes


async def reconcile(
    measure: Measurements,
    kb: KnowledgeBase,
    expected_node: Callable[[int], dict[str, Any]],
    expected_text: Callable[[int, int], bytes],
) -> None:
    """Validate every canonical record/blob with bounded pages before continuing writes."""
    started = time.perf_counter()
    nodes = await validate_nodes(measure, kb, expected_node)
    edges = await validate_relations(measure, kb, nodes)
    await settle_jobs(measure, kb)
    raw_bytes = await validate_evidence(measure, kb, expected_text)
    links = await kb.workers.read(
        lambda c, _t: {
            "nodes": c.execute("select count(*) from node_evidence").get,
            "foreign": c.execute(
                "select count(*) from node_evidence ne join nodes n on n.id=ne.node_id where n.uuid!=?", (measure.hub,)
            ).get,
            "relations": c.execute("select count(*) from relation_evidence").get,
        }
    )
    expected_links = (raw_bytes + 1048575) // 1048576
    if links != {"nodes": expected_links, "foreign": 0, "relations": 0}:
        raise RuntimeError("foreign/missing canonical evidence links")
    measure.report["resume_evidence_links"] = links
    measure.report["completed"] = {"nodes": nodes, "relations": edges, "raw_text_bytes": raw_bytes}
    measure.report["resumed_confirmed"] = dict(measure.report["completed"])
    measure.report["resume_validation_seconds"] = time.perf_counter() - started
    measure.report["resume_validation_scope"] = (
        "every canonical node/relation identity/properties, every expected evidence SHA+actual blob hash; bounded pages; no inferred uninterrupted throughput"
    )
    measure.save()
