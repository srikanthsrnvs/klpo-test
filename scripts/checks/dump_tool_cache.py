"""Write env/jev_pi/mcp-cache.template.json from a live per-rollout tool server, so the pre-seeded cache pi gets
matches the tools the server actually serves. Start a server (python -m jev_browser.toolset, or
scripts/checks/test_rollout_server.sh prints its port) and run:

    uv run --no-sync python scripts/checks/dump_tool_cache.py http://127.0.0.1:<port>/mcp env/jev_pi/mcp-cache.template.json
"""

import asyncio
import json
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(url, out):
    async with streamable_http_client(url) as (r, w, *_):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = (await s.list_tools()).tools
    entries = []
    for t in tools:  # the SDK's attribute spelling varies by version; the adapter's cache keys are camelCase
        in_schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
        out_schema = getattr(t, "output_schema", None) or getattr(t, "outputSchema", None)
        e = {"name": t.name, "description": t.description or "", "inputSchema": in_schema}
        if out_schema:
            e["outputSchema"] = out_schema
        entries.append(e)
    cache = {
        "version": 1,
        "servers": {
            "": {"configHash": "set-per-rollout", "tools": entries, "resources": [], "prompts": [],
                 "cachedAt": int(time.time() * 1000)}
        },
    }
    with open(out, "w") as fh:
        json.dump(cache, fh)
    print(f"wrote {out}: {len(entries)} tools: {', '.join(t['name'] for t in entries)}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
