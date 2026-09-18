"""Real 1/4/8 stdio processes share canonical graph identities and committed merges."""

import asyncio
from contextlib import AsyncExitStack

import pytest

from ..tools import envelope
from .mcp_client import client_for

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("count", [1, 4, 8])
async def test_disjoint_writes_same_key_merges_and_readers(tmp_path, count):
    async with AsyncExitStack() as stack:
        clients = [await stack.enter_async_context(client_for(tmp_path, "stdio")) for _ in range(count)]
        start = asyncio.Event()

        async def writer(index, client):
            await start.wait()
            shared = envelope(
                await client.call_tool(
                    "kb_write",
                    {
                        "nodes": [
                            {
                                "type": "subdomain",
                                "properties": {"value": "shared.example.test", f"writer{index}": index},
                            }
                        ]
                    },
                )
            )["data"]["nodes"][0]["id"]
            own = envelope(
                await client.call_tool(
                    "kb_write",
                    {"nodes": [{"type": "subdomain", "properties": {"value": f"client{index}.example.test"}}]},
                )
            )["data"]["nodes"][0]["id"]
            read = envelope(await client.call_tool("kb_get", {"kind": "nodes", "ids": [shared, own]}))["data"]
            assert len(read["records"]) == 2
            return shared, own

        tasks = [asyncio.create_task(writer(index, client)) for index, client in enumerate(clients)]
        start.set()
        results = await asyncio.gather(*tasks)
        assert len({shared for shared, _own in results}) == 1
        assert len({own for _shared, own in results}) == count
        record = envelope(await clients[0].call_tool("kb_get", {"kind": "nodes", "ids": [results[0][0]]}))["data"][
            "records"
        ][0]
        assert record["properties"] == {
            "value": "shared.example.test",
            **{f"writer{index}": index for index in range(count)},
        }
        # Closing all but one stdio lifespan cannot shut down the shared database.
        if count > 1:
            await clients[-1].close()
            assert envelope(await clients[0].call_tool("kb_get", {"kind": "nodes", "ids": [results[0][0]]}))["data"][
                "records"
            ]
