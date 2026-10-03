"""jev-pi: verifiers' pi harness, allowed to run in the subprocess runtime, with compaction and full context.

verifiers marks every harness NEEDS_CONTAINER because a coding agent can touch the host. Our policy's only
tools are the jev-browser MCP tools (pi's built-in tools are not exposed), and the Vast box has no Docker, so
the subprocess runtime is what we have. The upstream harness also writes pi's models.json without a context
window and never writes a settings.json, so pi runs with its default window and no compaction; this subclass
fixes both. Select with `env.agent.harness.id = "jev-pi"`.
"""

import json
from pathlib import Path

from verifiers.v1.acp import ACPConfig
from verifiers.v1.harnesses.pi.harness import PiHarness


class JevPiHarness(PiHarness):
    NEEDS_CONTAINER = False
    # verifiers resolves the harness config type from PiHarness's generic base, so these cannot live on a config
    # subclass; override through `env.agent.harness.env = { JEV_PI_CONTEXT_WINDOW = "..." }` if needed.
    CONTEXT_WINDOW = 262144  # tokens pi assumes the model accepts; compaction triggers near this minus the reserve
    MAX_TOKENS = 16384  # max output tokens pi requests per turn
    COMPACTION_RESERVE_TOKENS = 24576  # headroom kept below the window before pi compacts
    COMPACTION_KEEP_RECENT_TOKENS = 48000  # most recent conversation kept verbatim; older turns are summarized

    def _setting(self, key: str, default: int) -> int:
        return int(self.config.env.get(key, default))

    async def prepare_acp(self, ctx, trace, runtime, endpoint, secret, mcp_urls, data) -> ACPConfig:
        acp = await super().prepare_acp(ctx, trace, runtime, endpoint, secret, mcp_urls, data)
        agent_dir = acp.env["PI_CODING_AGENT_DIR"]
        models = json.loads((await runtime.read(f"{agent_dir}/models.json")).decode())
        for provider in models["providers"].values():
            for model in provider["models"]:
                model["contextWindow"] = self._setting("JEV_PI_CONTEXT_WINDOW", self.CONTEXT_WINDOW)
                model["maxTokens"] = self._setting("JEV_PI_MAX_TOKENS", self.MAX_TOKENS)
        await runtime.write(f"{agent_dir}/models.json", json.dumps(models).encode())
        # Hide the adapter's generic `mcp` gateway via its own setting. Excluding a tool literally named "mcp" with
        # --exclude-tools made pi (under the ACP adapter) expose NO tools at all; `mcpScript` is excluded by name.
        mcp_js = (await runtime.read(f"{agent_dir}/mcp.js")).decode()
        mcp_js = mcp_js.replace(
            '\\"mcpServers\\":', '\\"settings\\": {\\"disableProxyTool\\": true}, \\"mcpServers\\":', 1
        )
        await runtime.write(f"{agent_dir}/mcp.js", mcp_js.encode())
        # Pre-seed the adapter's tool-metadata cache. Without it the adapter connects asynchronously after
        # startup and the model's first turn can run before any tool is registered ("Tool browser_open not found").
        cache = Path(__file__).with_name("mcp-cache.template.json")
        if cache.exists():
            await runtime.write(f"{agent_dir}/mcp-cache.json", cache.read_bytes())
        settings = {
            "compaction": {
                "enabled": True,
                "reserveTokens": self._setting("JEV_PI_RESERVE_TOKENS", self.COMPACTION_RESERVE_TOKENS),
                "keepRecentTokens": self._setting("JEV_PI_KEEP_RECENT_TOKENS", self.COMPACTION_KEEP_RECENT_TOKENS),
            }
        }
        await runtime.write(f"{agent_dir}/settings.json", json.dumps(settings).encode())
        return acp


__all__ = ["JevPiHarness"]
