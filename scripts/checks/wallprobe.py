"""A/B probe: launch Chrome with a given command, open wall-prone pages, report which look like bot walls.
    python wallprobe.py LABEL "/usr/local/bin/chrome-headless"
"""

import json
import re
import socket
import subprocess
import sys
import time
import urllib.request

from websockets.sync.client import connect

label, chrome = sys.argv[1], sys.argv[2]
s = socket.socket()
s.bind(("127.0.0.1", 0))
port = s.getsockname()[1]
s.close()
prof = f"/root/.config/jev-browser/probe-{port}"
proc = subprocess.Popen(
    chrome.split() + [f"--remote-debugging-port={port}", f"--user-data-dir={prof}", "about:blank"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={"TMPDIR": "/tmp", "PATH": "/usr/bin:/bin", "HOME": "/root"},
)
for _ in range(60):
    try:
        ver = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1))
        break
    except Exception:
        time.sleep(0.5)
print(label, "UA:", ver["User-Agent"][:95])
pat = re.compile(
    r"access denied|just a moment|attention required|are you a human|verify you are human|not a robot|captcha|"
    r"unusual traffic|403|pardon our interruption|press & hold|checking your browser|security check|enable javascript",
    re.I,
)
URLS = [
    "https://www.google.com/search?q=peekaboo+lahore+deals",
    "https://www.walmart.com/search?q=ddr5",
    "https://www.apartments.com/arlington-tx/",
    "https://www.zillow.com/arlington-tx/apartments-for-rent/",
    "https://www.autotrader.co.uk/",
    "https://www.booking.com/searchresults.html?ss=Goa",
]
for url in URLS:
    req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{url}", method="PUT")
    tgt = json.load(urllib.request.urlopen(req))
    ws = connect(tgt["webSocketDebuggerUrl"], max_size=50_000_000)
    time.sleep(7)
    ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "document.title + ' || ' + document.body.innerText.slice(0,500)", "returnByValue": True}}))
    txt = ""
    for _ in range(30):
        m = json.loads(ws.recv())
        if m.get("id") == 1:
            txt = m.get("result", {}).get("result", {}).get("value", "") or ""
            break
    print("  ", f"{url.split('/')[2]:28}", "WALL" if pat.search(txt) else "ok  ", "|", txt[:80].replace("\n", " "))
    ws.close()
proc.kill()
