"""The jev-browser MCP tools as a verifiers Toolset.

One tool server process per env-server worker; its rollouts share one headless Chrome (each `browser_open`
is its own tab/session, so rollouts do not see each other's pages). The CDP port is chosen per process so
several workers on one machine do not collide. Tool names are bare (`browser_open`, ...) so the skill document
the policy reads matches what the harness exposes.

Guard rails live here rather than in the harness (which is also used interactively):
* only public http(s) URLs can be opened: no file://, loopback, link-local or private addresses, so the policy
  cannot read this box's files (.env, the judge prompt, judge records) or its services (vLLM, tool servers);
* first opens of one site are spaced out across every rollout on this box, so the sibling rollouts of a task do
  not hit a site from one IP at the same instant;
* every result ends with a `time:` line (elapsed, and remaining when JEV_BUDGET_S is set) so the policy can
  budget its wall-clock without a clock of its own.
"""

from __future__ import annotations

import asyncio
import fcntl
import ipaddress
import json
import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import verifiers.v1 as vf

IDLE_CLOSE_S = int(os.environ.get("JEV_SESSION_IDLE_S", "600"))  # close a tab nobody touched for this long
MAX_SESSIONS = int(os.environ.get("JEV_MAX_SESSIONS", "40"))  # above this, close least-recently-used tabs
BUDGET_S = float(os.environ.get("JEV_BUDGET_S") or 0)  # wall-clock budget the policy was told; 0 = unknown
HOST_STAGGER_S = float(os.environ.get("JEV_HOST_STAGGER_S", "6"))  # min seconds between first opens of one host
HOST_STAGGER_DIR = Path(os.environ.get("JEV_HOST_STAGGER_DIR", "/tmp/jev-host-stagger"))
LOCAL_NAMES = {"localhost", "metadata", "metadata.google.internal"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def url_error(url: str) -> str | None:
    """Why `url` may not be opened, or None. Only public http(s) sites are allowed."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return f"error: cannot parse the URL {url!r}."
    if parts.scheme not in {"http", "https"}:
        return f"error: only http and https URLs can be opened (got {parts.scheme or 'no'} scheme)."
    host = (parts.hostname or "").lower()
    if not host:
        return "error: the URL has no host."
    if host in LOCAL_NAMES or host.endswith((".localhost", ".local", ".internal")):
        return f"error: {host} is not a public website."
    addresses = []
    try:
        addresses.append(ipaddress.ip_address(host))
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return None  # unresolvable: let Chrome show its own error page
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                pass
    if any(not a.is_global for a in addresses):
        return f"error: {host} is not a public address."
    return None


def stagger_host(url: str) -> float:
    """Wait until HOST_STAGGER_S seconds have passed since the last first-open of this host by any rollout on
    this box (a per-host lock file keeps the last open time), then record this open. Returns seconds waited."""
    if HOST_STAGGER_S <= 0:
        return 0.0
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return 0.0
    waited = 0.0
    try:
        HOST_STAGGER_DIR.mkdir(parents=True, exist_ok=True)
        path = HOST_STAGGER_DIR / (re.sub(r"[^a-z0-9.-]", "_", host) + ".lock")
        with open(path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                last = float(fh.read().strip() or 0)
                delay = max(0.0, last + HOST_STAGGER_S - time.time())
                if delay:
                    time.sleep(delay)
                    waited = delay
                fh.seek(0)
                fh.truncate()
                fh.write(str(time.time()))
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass
    return waited


def _mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}m{seconds % 60:02d}s"


class JevBrowserToolset(vf.Toolset[vf.ToolsetConfig]):
    TOOL_PREFIX = None

    async def setup(self) -> None:
        # One Chrome, one browser-harness daemon and one profile per server process: mcp_server reads these at
        # import time, so decide them before importing it. A per-rollout server therefore never shares browser
        # state with another rollout.
        port = os.environ.setdefault("JEV_CDP_PORT", str(_free_port()))
        os.environ.setdefault("BU_NAME", f"jev-{port}")
        os.environ.pop("BU_CDP_URL", None)
        os.environ.pop("BU_CDP_WS", None)
        if "JEV_CHROME_BINARY" not in os.environ and os.path.exists("/usr/local/bin/chrome-headless"):
            os.environ["JEV_CHROME_BINARY"] = "/usr/local/bin/chrome-headless"
        from jev_ultrafast import mcp_server

        mcp_server.load_env()
        mcp_server.ensure_chrome()
        self._m = mcp_server
        # warm the browser-harness daemon once, before any client can hit it concurrently
        from jev_ultrafast import browser as _browser

        _browser.ensure_daemon()
        self._t0 = time.monotonic()  # the rollout's clock: this server lives exactly as long as its rollout
        self._last_used: dict[str, float] = {}
        self._pool = ThreadPoolExecutor(
            max_workers=int(os.environ.get("JEV_TOOL_WORKERS", "48")), thread_name_prefix="jev"
        )
        # Policies rarely call browser_close, and a shared server has no per-rollout teardown hook, so leaked
        # tabs pile up in Chrome (hundreds of renderer processes; the box's pid cgroup limit then kills things).
        asyncio.get_running_loop().create_task(self._reaper())

    def _touch(self, session: str) -> None:
        self._last_used[session] = time.monotonic()

    def _stamp(self, out: str) -> str:
        """Append the time line every result carries."""
        elapsed = time.monotonic() - self._t0
        line = f"time: {_mmss(elapsed)} elapsed"
        if BUDGET_S:
            line += f", about {_mmss(BUDGET_S - elapsed)} left of your {_mmss(BUDGET_S)} budget"
        return f"{out}\n{line}"

    def _heal_chrome(self) -> bool:
        """If the shared Chrome died (something on the box kills it), relaunch it and drop dead sessions."""
        m = self._m
        if m._cdp_reachable(os.environ.get("BU_CDP_URL", "")):
            return False
        os.environ.pop("BU_CDP_URL", None)
        os.environ.pop("BU_CDP_WS", None)
        m.SESSIONS.clear()
        self._last_used.clear()
        m.ensure_chrome()
        return True

    async def _guard(self, fn, *args):
        """Run a blocking harness call in the worker pool so one slow browser_run never stalls the event loop
        (and with it every other rollout's connection, initialize and tools/list)."""
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(self._pool, fn, *args)
        except Exception as e:
            if await loop.run_in_executor(self._pool, self._heal_chrome):
                return (
                    "error: the browser had crashed and was restarted; all sessions are gone. "
                    "Call browser_open(url) again to continue."
                )
            raise e

    def _close_quietly(self, session: str) -> None:
        """Close one session's tab and forget it; never raises (the reaper runs unattended)."""
        self._last_used.pop(session, None)
        s = self._m.SESSIONS.pop(session, None)
        if s is None:
            return
        try:
            s.close()
        except Exception:
            pass

    def _reap(self) -> None:
        now = time.monotonic()
        live = [sid for sid in list(self._m.SESSIONS) if sid]
        for sid in live:
            if now - self._last_used.get(sid, now) > IDLE_CLOSE_S:
                self._close_quietly(sid)
        live = [sid for sid in list(self._m.SESSIONS) if sid]
        if len(live) > MAX_SESSIONS:
            for sid in sorted(live, key=lambda x: self._last_used.get(x, 0))[: len(live) - MAX_SESSIONS]:
                self._close_quietly(sid)

    async def _reaper(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                self._reap()
            except Exception:
                pass

    # Blocking wrappers that apply the URL guard (and the per-host stagger) before touching the harness.
    def _open_checked(self, url: str) -> str:
        if why := url_error(url):
            return why
        stagger_host(url)
        return self._m.browser_open(url)

    def _goto_checked(self, session: str, url: str) -> str:
        if why := url_error(url):
            return why
        return self._m.browser_goto(session, url)

    @vf.tool
    async def browser_open(self, url: str) -> str:
        """Open a public http(s) URL in a fresh tab and start a session. Returns the session id, the element table
        and the first 8,000 characters of the page's text (whole document; a [truncated] line tells you when there
        is more, which browser_text reads). Every other tool needs the session id this returns."""
        try:
            self._reap()
        except Exception:
            pass
        out = await self._guard(self._open_checked, url)
        for sid in self._m.SESSIONS:
            self._last_used.setdefault(sid, time.monotonic())
        return self._stamp(out)

    @vf.tool
    async def browser_run(self, session: str, goal: str, max_steps: int = 25) -> str:
        """THE MAIN NAVIGATION TOOL. State one goal in plain English and Jev (a fast browser model) clicks,
        types, scrolls and waits toward it, then returns the page and a status: done, needs_input (type a value
        into the named element with browser_type, then call browser_run again with the SAME goal), blocked (use a
        smaller goal or browser_goto), budget (call again with the same goal to continue)."""
        self._touch(session)
        return self._stamp(await self._guard(self._m.browser_run, session, goal, max_steps))

    @vf.tool
    async def browser_type(self, session: str, element: str, text: str, submit: bool = False) -> str:
        """Type text into element [n]; submit=True presses Enter afterwards. This is how you answer needs_input."""
        self._touch(session)
        return self._stamp(await self._guard(self._m.browser_type, session, element, text, submit))

    @vf.tool
    async def browser_text(self, session: str, max_chars: int = 12000, links: bool = False) -> str:
        """Read the ENTIRE page's text (not just the viewport), up to max_chars (default 12,000; a [truncated] line
        tells you when to call again with a larger value). links=True also lists every visible link (text -> URL)
        and image (alt -> URL). Use this to extract lists, details, URLs."""
        self._touch(session)
        return self._stamp(await self._guard(self._m.browser_text, session, max_chars, links))

    @vf.tool
    async def browser_goto(self, session: str, url: str) -> str:
        """Navigate the session's tab to a public http(s) URL."""
        self._touch(session)
        return self._stamp(await self._guard(self._goto_checked, session, url))

    @vf.tool
    async def browser_back(self, session: str) -> str:
        """Go back one page."""
        self._touch(session)
        return self._stamp(await self._guard(self._m.browser_back, session))

    @vf.tool
    async def browser_observe(self, session: str, text_chars: int = 1500, find: str | None = None) -> str:
        """Re-read the current page: element table plus visible text. find= highlights elements matching a word."""
        self._touch(session)
        return self._stamp(await self._guard(self._m.browser_observe, session, text_chars, find))

    @vf.tool
    async def browser_click(self, session: str, element: str) -> str:
        """Click element [n] yourself. Prefer browser_run for anything that takes more than one click."""
        self._touch(session)
        return self._stamp(await self._guard(self._m.browser_click, session, element))

    @vf.tool
    async def browser_select(self, session: str, element: str, option: str) -> str:
        """Choose an option in a <select> element [n]."""
        self._touch(session)
        return self._stamp(await self._guard(self._m.browser_select, session, element, option))

    @vf.tool
    async def browser_scroll(self, session: str, direction: str = "down") -> str:
        """Scroll the page up or down by one viewport."""
        self._touch(session)
        return self._stamp(await self._guard(self._m.browser_scroll, session, direction))

    @vf.tool
    async def browser_wait(self, session: str, seconds: float = 1.0) -> str:
        """Wait for the page to settle, then re-read it."""
        self._touch(session)
        return self._stamp(await self._guard(self._m.browser_wait, session, seconds))

    @vf.tool
    async def browser_screenshot(self, session: str):
        """JPEG screenshot of the tab, for when the element table is ambiguous or to verify visually."""
        self._touch(session)
        return await self._guard(self._m.browser_screenshot, session)

    @vf.tool
    async def browser_history(self, session: str) -> str:
        """Every page reached in this session so far (the task record)."""
        self._touch(session)
        return self._stamp(await self._guard(self._m.browser_history, session))

    @vf.tool
    async def browser_close(self, session: str) -> str:
        """Close the session's tab."""
        self._last_used.pop(session, None)
        return self._stamp(await self._guard(self._m.browser_close, session))


def _apply_tool_surface() -> None:
    """Shape the served tool set from the environment (same knobs as scripts/run/serve_tools.py)."""
    if os.environ.get("JEV_DISABLE_SCREENSHOT", "1") == "1" and hasattr(JevBrowserToolset, "browser_screenshot"):
        del JevBrowserToolset.browser_screenshot
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


def _shutdown_browser() -> None:
    """Kill the Chrome and the browser-harness daemon this process started (both run detached)."""
    import signal

    try:
        from jev_ultrafast import mcp_server
    except Exception:
        return
    proc = getattr(mcp_server, "CHROME_PROC", None)
    if proc is not None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    name = os.environ.get("BU_NAME")
    if name and name != "default":
        # The daemon is a detached `python -m browser_harness.daemon` whose cmdline carries no name; find it by the
        # BU_NAME in its environment (reliable, independent of browser-harness internals).
        for pid in filter(str.isdigit, os.listdir("/proc")):
            if int(pid) == os.getpid():
                continue
            try:
                with open(f"/proc/{pid}/environ", "rb") as fh:
                    env = fh.read().split(b"\0")
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    cmd = fh.read()
            except OSError:
                continue
            if f"BU_NAME={name}".encode() in env and b"browser_harness" in cmd:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except OSError:
                    pass


if __name__ == "__main__":
    # Run as a per-rollout tool server: `python -m jev_browser.toolset` with VF_CONFIG / MCP_PORT_FILE from verifiers.
    import atexit
    import signal
    import traceback

    _apply_tool_surface()
    atexit.register(_shutdown_browser)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: (_shutdown_browser(), os._exit(0)))
    try:
        JevBrowserToolset.run()
    except BaseException:
        # The runtime directory (and its log) is deleted when the rollout ends; keep a durable copy of failures.
        log_dir = Path(os.environ.get("JEV_TOOL_LOG_DIR", "/workspace/vf-runs/tool-servers"))
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            keys = sorted(
                k for k in os.environ if k.startswith(("JEV_", "BU_", "MCP_", "VF_", "TMPDIR", "HOME", "PATH"))
            )
            (log_dir / f"{os.getpid()}-{time.strftime('%H%M%S')}.log").write_text(
                traceback.format_exc() + "\nenv: " + json.dumps({k: os.environ[k][:120] for k in keys}, indent=1)
            )
        except OSError:
            pass
        raise
