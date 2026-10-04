"""Warm browser pool: Chrome + browser-harness daemon pairs launched ahead of time, so a per-rollout tool server
adopts one in milliseconds instead of launching its own (about 5 s per rollout) and tears it down off the
critical path.

Keeper (one per box):   python -m jev_browser.warm_pool --size 104
    Keeps `size` ready slots under JEV_POOL_DIR (default /tmp/jev-pool). A slot is one headless Chrome on its own
    CDP port and profile plus one browser-harness daemon (BU_NAME=jev-<port>) attached to it; `<port>.ready` is
    written once CDP answers. Claimed slots that were released, or whose owner died, are replaced.
Claim (tool server start):   claim() -> port | None
    Atomically renames `<port>.ready` to `<port>.claimed-<pid>` and exports JEV_CDP_PORT / BU_NAME / BU_CDP_URL /
    JEV_POOL_SLOT so jev_ultrafast.mcp_server attaches to the slot instead of launching Chrome.
Release (tool server exit):  release(port)
    Kills that Chrome and daemon and removes the claim marker; the keeper launches a fresh replacement.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

POOL_DIR = Path(os.environ.get("JEV_POOL_DIR", "/tmp/jev-pool"))
PORT_BASE = int(os.environ.get("JEV_POOL_PORT_BASE", "21000"))
PROFILE_ROOT = Path(os.environ.get("JEV_CHROME_PROFILE", Path.home() / ".config" / "jev-browser" / "profile"))


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} pool: {msg}", file=sys.stderr, flush=True)


def _cdp_reachable(port: int, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            return bool(json.loads(r.read())["webSocketDebuggerUrl"])
    except Exception:
        return False


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _chrome_binary() -> str:
    if env := os.environ.get("JEV_CHROME_BINARY"):
        return env
    if os.path.exists("/usr/local/bin/chrome-headless"):
        return "/usr/local/bin/chrome-headless"
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        if path := shutil.which(name):
            return path
    raise RuntimeError("No Chrome found; set JEV_CHROME_BINARY")


def profile_dir(port: int) -> Path:
    # Same scheme as jev_ultrafast.mcp_server, so a server that adopts the slot agrees on the profile path.
    return PROFILE_ROOT.with_name(f"{PROFILE_ROOT.name}-{port}")


def _pids_matching(needle_cmd: bytes, needle_env: bytes | None = None) -> list[int]:
    out = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        if int(pid) == os.getpid():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read()
            if needle_cmd not in cmd:
                continue
            if needle_env is not None:
                with open(f"/proc/{pid}/environ", "rb") as fh:
                    if needle_env not in fh.read().split(b"\0"):
                        continue
        except OSError:
            continue
        out.append(int(pid))
    return out


def kill_slot(port: int) -> None:
    """Kill the Chrome (by its debugging port) and the daemon (by BU_NAME in its environment) of one slot."""
    for pid in _pids_matching(f"--remote-debugging-port={port}".encode()):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    for pid in _pids_matching(b"browser_harness", f"BU_NAME=jev-{port}".encode()):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _markers(port: int) -> list[Path]:
    return [p for p in POOL_DIR.glob(f"{port}.*")]


# ---- claim / release (tool server side) -----------------------------------------------------------------


def claim() -> int | None:
    """Take a ready slot and point the harness at it. None when the pool is absent or empty (the server then
    launches its own Chrome as before)."""
    if not POOL_DIR.is_dir():
        return None
    for ready in sorted(POOL_DIR.glob("*.ready")):
        try:
            port = int(ready.stem)
        except ValueError:
            continue
        claimed = ready.with_name(f"{port}.claimed-{os.getpid()}")
        try:
            ready.rename(claimed)  # atomic: exactly one process wins the slot
        except OSError:
            continue
        if not _cdp_reachable(port):
            kill_slot(port)
            claimed.unlink(missing_ok=True)
            continue
        os.environ.update(
            JEV_CDP_PORT=str(port),
            BU_NAME=f"jev-{port}",
            BU_CDP_URL=f"http://127.0.0.1:{port}",
            JEV_POOL_SLOT=str(port),
        )
        return port
    return None


def release(port: int) -> None:
    kill_slot(port)
    for m in _markers(port):
        m.unlink(missing_ok=True)


# ---- keeper -------------------------------------------------------------------------------------------------


class Keeper:
    def __init__(self, size: int, parallel: int) -> None:
        self.size, self.parallel = size, parallel
        self.launching: dict[int, tuple[subprocess.Popen, float]] = {}
        POOL_DIR.mkdir(parents=True, exist_ok=True)
        self.binary = _chrome_binary()

    def _next_port(self) -> int:
        used = {int(p.stem) for p in POOL_DIR.glob("*.*") if p.stem.isdigit()} | set(self.launching)
        port = PORT_BASE
        while port in used or not _port_free(port):
            port += 1
        return port

    def _launch(self, port: int) -> None:
        prof = profile_dir(port)
        shutil.rmtree(prof, ignore_errors=True)
        prof.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [
                self.binary,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={prof}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "--window-size=1200,900",
                "about:blank",
            ],
            stdout=open(prof / "chrome.log", "ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "TMPDIR": os.environ.get("JEV_CHROME_TMPDIR", "/tmp")},
        )
        self.launching[port] = (proc, time.monotonic())

    def _finish(self, port: int) -> bool:
        """Chrome answers: start its daemon and mark the slot ready."""
        env = {**os.environ, "BU_NAME": f"jev-{port}", "BU_CDP_URL": f"http://127.0.0.1:{port}"}
        env.pop("BU_CDP_WS", None)
        r = subprocess.run(
            [sys.executable, "-c", "from browser_harness.admin import ensure_daemon; ensure_daemon()"],
            env=env,
            capture_output=True,
            timeout=60,
        )
        if r.returncode != 0:
            _log(f"daemon for slot {port} failed: {r.stderr.decode()[-300:]}")
            return False
        (POOL_DIR / f"{port}.ready").write_text(
            json.dumps({"port": port, "name": f"jev-{port}", "profile": str(profile_dir(port)), "t": time.time()})
        )
        return True

    def _sweep(self) -> None:
        # Claimed slots whose owning server died without releasing: tear down so they get replaced.
        for m in POOL_DIR.glob("*.claimed-*"):
            try:
                port, pid = int(m.stem.split(".")[0]), int(m.name.rsplit("-", 1)[1])
            except ValueError:
                continue
            if not Path(f"/proc/{pid}").exists():
                _log(f"owner {pid} of slot {port} is gone; recycling")
                release(port)
        # Ready slots whose Chrome died.
        for m in POOL_DIR.glob("*.ready"):
            port = int(m.stem)
            if not _cdp_reachable(port):
                _log(f"ready slot {port} lost its Chrome; recycling")
                release(port)

    def run(self) -> None:
        _log(f"keeping {self.size} warm slots in {POOL_DIR} (ports from {PORT_BASE}, chrome={self.binary})")
        last_sweep = 0.0
        while True:
            # finish launches whose Chrome is up
            for port, (proc, t0) in list(self.launching.items()):
                if _cdp_reachable(port):
                    del self.launching[port]
                    if not self._finish(port):
                        kill_slot(port)
                elif proc.poll() is not None or time.monotonic() - t0 > 60:
                    _log(f"slot {port} failed to start (exit={proc.poll()}); retrying")
                    del self.launching[port]
                    kill_slot(port)
            ready = len(list(POOL_DIR.glob("*.ready")))
            claimed = len(list(POOL_DIR.glob("*.claimed-*")))
            while ready + len(self.launching) < self.size and len(self.launching) < self.parallel:
                self._launch(self._next_port())
                ready += 0
            if time.monotonic() - last_sweep > 15:
                self._sweep()
                last_sweep = time.monotonic()
                _log(f"ready={ready} claimed={claimed} launching={len(self.launching)}")
            time.sleep(0.5)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=int(os.environ.get("JEV_POOL_SIZE", "104")))
    ap.add_argument("--parallel", type=int, default=8)
    a = ap.parse_args()
    Keeper(a.size, a.parallel).run()


if __name__ == "__main__":
    main()
