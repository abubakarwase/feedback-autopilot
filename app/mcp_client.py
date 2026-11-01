"""Labels MCP client (in-process). Graph code reaches labels only through here."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastmcp import Client

from app.mcp_labels import mcp


async def call_labels(tool: str, **args: Any) -> Any:
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args)
        return result.data


def labels_call(tool: str, **args: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(call_labels(tool, **args))
    with ThreadPoolExecutor(1) as ex:
        return ex.submit(lambda: asyncio.run(call_labels(tool, **args))).result()
