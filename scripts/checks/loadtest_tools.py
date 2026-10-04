"""Load test for the jev-browser tool server: N concurrent MCP clients each do initialize + tools/list, then
browser_open(url) -> browser_text -> browser_close, while measuring how long a cold tools/list takes DURING the load.
Fails (exit 1) if any client errors or if tools/list under load exceeds the latency budget.

    uv run --no-sync python scripts/loadtest_tools.py --clients 16 --url http://127.0.0.1:18100/mcp
"""

import argparse
import asyncio
import statistics
import sys
import time

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _unwrap(e):
    """Flatten ExceptionGroups (anyio task groups) to the leaf messages."""
    subs = getattr(e, "exceptions", None)
    if subs:
        return " || ".join(_unwrap(x) for x in subs)
    return f"{type(e).__name__}: {e}"


PAGES = ["https://example.com", "https://en.wikipedia.org/wiki/Web_browser", "https://httpbin.org/html"]


async def client(i, url, results):
    t0 = time.time()
    try:
        async with streamable_http_client(url) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                names = sorted(t.name for t in tools.tools)
                t_list = time.time() - t0
                page = PAGES[i % len(PAGES)]
                res = await s.call_tool("browser_open", {"url": page})
                text = res.content[0].text if res.content else ""
                if "url:" not in text:
                    raise RuntimeError(f"unexpected browser_open result: {text[:200]!r}")
                # per-rollout servers are sessionless; shared servers (serve_tools.py) take a session id
                sid = next((tok.split()[1] for tok in text.splitlines() if tok.startswith("session:")), None)
                args = {"session": sid} if sid else {}
                res2 = await s.call_tool("browser_text", {**args, "max_chars": 2000})
                body = res2.content[0].text if res2.content else ""
                await s.call_tool("browser_close", args)
                results.append(
                    {
                        "i": i,
                        "ok": True,
                        "t_list": t_list,
                        "t_total": time.time() - t0,
                        "tools": names,
                        "text_len": len(body),
                    }
                )
    except BaseException as e:  # noqa: BLE001
        results.append({"i": i, "ok": False, "err": _unwrap(e)[:400], "t_total": time.time() - t0})


async def cold_list(url, results, delay):
    """A fresh client arriving mid-load: this is what a new pi rollout experiences."""
    await asyncio.sleep(delay)
    t0 = time.time()
    try:
        async with streamable_http_client(url) as (r, w, *_):
            async with ClientSession(r, w) as s:
                await s.initialize()
                await s.list_tools()
        results.append({"cold_list": time.time() - t0})
    except Exception as e:
        results.append({"cold_list_err": f"{type(e).__name__}: {e}"[:200]})


async def main(a):
    results, probes = [], []
    tasks = [client(i, a.url, results) for i in range(a.clients)]
    tasks += [cold_list(a.url, probes, d) for d in (3, 8, 15)]
    await asyncio.gather(*tasks)
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    print(f"clients={a.clients} ok={len(ok)} failed={len(bad)}")
    for r in bad[:5]:
        print("  FAIL", r)
    if ok:
        print(
            f"tools/list at start: median {statistics.median(r['t_list'] for r in ok):.2f}s max {max(r['t_list'] for r in ok):.2f}s"
        )
        print(
            f"open+text+close total: median {statistics.median(r['t_total'] for r in ok):.1f}s max {max(r['t_total'] for r in ok):.1f}s"
        )
        print("tools:", ok[0]["tools"])
    print("cold tools/list during load:", probes)
    worst = max([p.get("cold_list", 999) for p in probes] + [0])
    if bad or worst > a.max_cold_list:
        print(f"RESULT: FAIL (failed={len(bad)}, worst cold list {worst:.1f}s > {a.max_cold_list}s)")
        sys.exit(1)
    print("RESULT: PASS")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:18100/mcp")
    ap.add_argument("--clients", type=int, default=16)
    ap.add_argument("--max-cold-list", type=float, default=3.0)
    asyncio.run(main(ap.parse_args()))
