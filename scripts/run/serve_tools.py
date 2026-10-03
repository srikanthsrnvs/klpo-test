"""Run the jev-browser Toolset as a standalone MCP HTTP server, outside verifiers' launcher.

verifiers normally packages the toolset and installs it into a fresh venv per runtime; our toolset depends on
this repo (jev-ultrafast, browser-harness, a Chrome binary), which that venv cannot resolve. Running the server
ourselves in the prime-rl venv and pointing the taskset at it (`taskset.tools.url`) skips the launcher.

    cd /workspace/prime-rl && MCP_PORT=18100 uv run --no-sync python /workspace/jev-browser-rl/scripts/run/serve_tools.py
"""

import os

import verifiers.v1 as vf
from jev_browser.toolset import JevBrowserToolset

if __name__ == "__main__":
    if os.environ.get("JEV_DISABLE_SCREENSHOT") == "1":
        # Text-only policy: prime-rl refuses multimodal samples unless vision training is configured.
        del JevBrowserToolset.browser_screenshot
    # Small models given click/scroll/select tools stop delegating to Jev and micro-step (see SKILL.md); serve
    # only the high-level tools unless JEV_TOOL_WHITELIST says otherwise.
    whitelist = os.environ.get(
        "JEV_TOOL_WHITELIST",
        "browser_open,browser_run,browser_type,browser_back,browser_goto,browser_text,browser_wait,browser_close",
    )
    keep = {t.strip() for t in whitelist.split(",") if t.strip()}
    for name in [n for n in dir(JevBrowserToolset) if n.startswith("browser_") and n not in keep]:
        try:
            delattr(JevBrowserToolset, name)
        except AttributeError:
            pass
    os.environ.setdefault("MCP_HOST", "127.0.0.1")
    os.environ.setdefault("MCP_PORT", "18100")
    toolset = JevBrowserToolset(vf.SharedToolsetConfig())
    # Launch Chrome before the server binds its port: verifiers binds first and runs setup() afterwards, so a
    # client connecting during the Chrome launch would hang and give up (pi then runs with no tools).
    import asyncio

    asyncio.run(toolset.setup())
    toolset._serve()
