"""jev-pi: verifiers' pi harness, allowed to run in the subprocess runtime, with compaction and full context.

verifiers marks every harness NEEDS_CONTAINER because a coding agent can touch the host. Our policy's only
tools are the jev-browser MCP tools (pi's built-in tools are not exposed), and the Vast box has no Docker, so
the subprocess runtime is what we have. The upstream harness also writes pi's models.json without a context
window and never writes a settings.json, so pi runs with its default window and no compaction; this subclass
fixes both. Select with `env.agent.harness.id = "jev-pi"`.

Tool surface: pi gets an ALLOWLIST (`--tools`, JEV_PI_TOOLS, default = the eight browser tools) on top of the
config's `disabled_tools` denylist, so a pi upgrade that ships a new built-in never becomes a live host tool.
"""

import hashlib
import json
import shlex
import time
from pathlib import Path

from verifiers.v1.acp import ACPConfig
from verifiers.v1.harnesses.pi.harness import PiHarness

DEFAULT_TOOLS = "browser_open,browser_run,browser_type,browser_back,browser_goto,browser_text,browser_wait,browser_close"
_HASH_FIELDS = (
    "args",
    "auth",
    "bearerToken",
    "bearerTokenEnv",
    "command",
    "cwd",
    "env",
    "excludeTools",
    "exposeResources",
    "headers",
    "includeTools",
    "protocolVersion",
    "requestHeadersCommand",
    "socket",
    "url",
)


def adapter_config_hash(url: str) -> str:
    """pi-mcp-adapter's `computeServerHash` for a `{url}` server entry (lifecycle/directTools are not part of the
    identity): sha256 of the stableStringify'd identity object, sorted keys, absent fields serialized as the bare
    word `undefined`. Reproduces the hash the adapter wrote for http://127.0.0.1:18100/mcp exactly."""
    body = ",".join(
        json.dumps(k) + ":" + (json.dumps(url, separators=(",", ":")) if k == "url" else "undefined")
        for k in sorted(_HASH_FIELDS)
    )
    return hashlib.sha256(("{" + body + "}").encode()).hexdigest()


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
        # Pre-seed the adapter's tool-metadata cache so the model's first turn already has the tools registered
        # (without it the adapter connects asynchronously and the first turn can see "Tool browser_open not
        # found"). The adapter only honours an entry whose configHash matches the server definition, which
        # includes this rollout's URL, so the hash and timestamp are computed per rollout.
        cache = Path(__file__).with_name("mcp-cache.template.json")
        if cache.exists() and mcp_urls:
            template = json.loads(cache.read_text())
            entry = next(iter(template.get("servers", {}).values()), None)
            if entry:
                now_ms = int(time.time() * 1000)
                servers = {
                    name: {**entry, "configHash": adapter_config_hash(url), "cachedAt": now_ms}
                    for name, url in mcp_urls.items()
                }
                await runtime.write(
                    f"{agent_dir}/mcp-cache.json",
                    json.dumps({"version": template.get("version", 1), "servers": servers}).encode(),
                )
        # Allowlist on top of the denylist: pi only ever registers these tools, whatever it ships with.
        tools = self.config.env.get("JEV_PI_TOOLS", DEFAULT_TOOLS).strip()
        if tools:
            wrapper_path = f"{agent_dir}/pi"
            wrapper = (await runtime.read(wrapper_path)).decode()
            if "--tools" not in wrapper and ' "$@"' in wrapper:
                wrapper = wrapper.replace(' "$@"', f' --tools {shlex.quote(tools)} "$@"', 1)
                await runtime.write(wrapper_path, wrapper.encode())
        settings = {
            "compaction": {
                "enabled": True,
                "reserveTokens": self._setting("JEV_PI_RESERVE_TOKENS", self.COMPACTION_RESERVE_TOKENS),
                "keepRecentTokens": self._setting("JEV_PI_KEEP_RECENT_TOKENS", self.COMPACTION_KEEP_RECENT_TOKENS),
            }
        }
        await runtime.write(f"{agent_dir}/settings.json", json.dumps(settings).encode())
        return acp


__all__ = ["JevPiHarness", "adapter_config_hash"]
