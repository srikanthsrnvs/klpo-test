"""Exit 0 once the jev-browser tool server answers an MCP initialize + tools/list (real readiness, unlike a GET)."""

import asyncio
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18100/mcp"
DEADLINE = time.time() + float(sys.argv[2] if len(sys.argv) > 2 else 90)


async def probe():
    async with streamable_http_client(URL) as (r, w, *_):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            return sorted(t.name for t in tools.tools)


while True:
    try:
        names = asyncio.run(asyncio.wait_for(probe(), 15))
        print("mcp ready:", len(names), "tools")
        sys.exit(0)
    except Exception as e:  # not up yet
        if time.time() > DEADLINE:
            print("mcp NOT ready:", type(e).__name__, e)
            sys.exit(1)
        time.sleep(3)
