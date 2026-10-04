"""Run the jev-browser Toolset as a standalone MCP HTTP server, outside verifiers' launcher.

verifiers normally packages the toolset and installs it into a fresh venv per runtime; our toolset depends on
this repo (jev-ultrafast, browser-harness, a Chrome binary), which that venv cannot resolve. Running the server
ourselves in the prime-rl venv and pointing the taskset at it (`taskset.tools.url`) skips the launcher.

    cd /workspace/prime-rl && MCP_PORT=18100 uv run --no-sync python /workspace/jev-browser-rl/scripts/run/serve_tools.py
"""

import os

import verifiers.v1 as vf
from jev_browser.toolset import JevBrowserSharedToolset as JevBrowserToolset, _apply_tool_surface

if __name__ == "__main__":
    # Small models given click/scroll/select tools stop delegating to Jev and micro-step (see SKILL.md); serve
    # only the high-level tools unless JEV_TOOL_WHITELIST says otherwise.
    _apply_tool_surface(JevBrowserToolset)
    os.environ.setdefault("JEV_TOOL_WORKERS", "48")  # one shared server serves every rollout
    os.environ.setdefault("MCP_HOST", "127.0.0.1")
    os.environ.setdefault("MCP_PORT", "18100")
    toolset = JevBrowserToolset(vf.SharedToolsetConfig())
    # Launch Chrome before the server binds its port: verifiers binds first and runs setup() afterwards, so a
    # client connecting during the Chrome launch would hang and give up (pi then runs with no tools).
    import asyncio

    asyncio.run(toolset.setup())
    toolset._serve()
