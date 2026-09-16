"""Streaming breadth-first traversal with bounded visited sets and adjacency."""

from __future__ import annotations

import heapq
import time
from collections import deque
from contextlib import ExitStack, closing
from typing import TYPE_CHECKING, Any

from ..errors import NotFoundError
from ..models import NeighborsResult
from ..mutations import canonical_json
from .graph import require_ready, row_by_id
from .graph_sql import ADJACENCY, READY

if TYPE_CHECKING:
    from collections.abc import Generator

    import apsw

    from ..models import NeighborsRequest
    from .worker import OperationToken


def _edges(
    connection: apsw.Connection, owner: int, request: NeighborsRequest
) -> Generator[tuple[Any, ...], None, None]:
    """Merge at most 200 selective index streams, retaining one row per stream."""
    directions = (
        ("source_id", "target_id")
        if request.direction == "both"
        else ("source_id",)
        if request.direction == "out"
        else ("target_id",)
    )
    with ExitStack() as stack:
        streams: list[apsw.Cursor] = []
        for column in directions:
            for type_name in dict.fromkeys(request.relation_types) if request.relation_types is not None else [None]:
                clause = " AND o.type=?" if type_name is not None else ""
                parameters = (owner, 0, type_name) if type_name is not None else (owner, 0)
                streams.append(
                    stack.enter_context(
                        closing(
                            connection.execute(
                                ADJACENCY[column].format(type_clause=clause, ready=READY["relations"]), parameters
                            )
                        )
                    )
                )
        yield from heapq.merge(*streams, key=lambda item: item[0])


class _Traversal:
    def __init__(self, connection: apsw.Connection, token: OperationToken, request: NeighborsRequest) -> None:
        self.connection, self.token, self.request = connection, token, request
        self.output: dict[str, Any] = {"nodes": [], "edges": [], "truncated": False, "reason": None, "frontier": []}
        self.visited: dict[int, str] = {}
        self.edges: set[int] = set()
        self.queue: deque[tuple[int, int]] = deque()

    def seeds(self) -> None:
        for identifier in self.request.seed_ids:
            self.token.check()
            row = row_by_id(self.connection, "nodes", identifier)
            if row is None:
                raise NotFoundError("seed missing")
            require_ready(self.connection, "nodes", row)
            self.visited[row["id"]] = row["uuid"]
            self.output["nodes"].append({"id": row["uuid"], "type": row["type"]})
            self.queue.append((row["id"], 0))

    def append_edge(self, owner: int, depth: int, edge: tuple[Any, ...]) -> str | None:
        edge_id, uuid, type_name, source, target = edge
        if len(self.edges) == self.request.max_edges:
            return "max_edges"
        other = target if source == owner else source
        new_node = other not in self.visited
        if new_node and len(self.visited) == self.request.max_nodes:
            return "max_nodes"
        row = row_by_id(self.connection, "nodes", other) if new_node else None
        if new_node and row is None:
            raise NotFoundError("endpoint missing")
        other_uuid = row["uuid"] if row else self.visited[other]
        item = {
            "id": uuid,
            "type": type_name,
            "source_id": self.visited[owner] if source == owner else other_uuid,
            "target_id": other_uuid if source == owner else self.visited[owner],
        }
        node = {"id": other_uuid, "type": row["type"]} if row else None
        self.output["edges"].append(item)
        if node is not None:
            self.output["nodes"].append(node)
        if len(canonical_json(self.output).encode("utf-8")) > 200000:
            self.output["edges"].pop()
            if node is not None:
                self.output["nodes"].pop()
            return "response_bytes"
        self.edges.add(edge_id)
        if new_node:
            self.visited[other] = other_uuid
            self.queue.append((other, depth + 1))
        return None

    def expand(self, owner: int, depth: int) -> str | None:
        if time.monotonic() >= self.token.deadline - 0.01:
            return "deadline"
        with closing(_edges(self.connection, owner, self.request)) as edges:
            for edge in edges:
                self.token.check()
                # Best-effort completion room, never an extension of the worker deadline.
                if time.monotonic() >= self.token.deadline - 0.01:
                    return "deadline"
                if edge[0] in self.edges:
                    continue
                reason = self.append_edge(owner, depth, edge)
                if reason is not None:
                    return reason
        return None

    def run(self) -> dict[str, Any]:
        self.seeds()
        while self.queue:
            owner, depth = self.queue.popleft()
            if depth == self.request.depth:
                continue
            reason = self.expand(owner, depth)
            if reason is not None:
                self.output.update(
                    truncated=True,
                    reason=reason,
                    frontier=[
                        self.visited[owner],
                        *[self.visited[item] for item, level in self.queue if level < self.request.depth],
                    ],
                )
                break
        return self.output


def neighbors(connection: apsw.Connection, token: OperationToken, request: NeighborsRequest) -> dict[str, Any]:
    """Materialize only output-bounded nodes/edges, retaining a bounded frontier."""
    return NeighborsResult.model_validate(_Traversal(connection, token, request).run()).model_dump()
