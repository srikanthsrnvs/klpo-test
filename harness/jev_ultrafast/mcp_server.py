"""MCP server: a browser for agents.

Two layers over the same Chrome tab:

* Manual tools (observe / click / type / select / scroll / goto) — no model calls, deterministic.
* ``browser_run`` — hands a natural-language goal to Jev (TypeSafe), which drives the tab itself and
  returns when it is done, blocked, or needs a value typed that only the caller knows.

Element indices in every result are ``[n]`` numbers from the most recent observation of that session.
Model output never becomes selectors or code; every action resolves to an observed DOM node.
"""

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = Path(os.environ.get("JEV_CHROME_PROFILE", Path.home() / ".config" / "jev-browser" / "profile"))
CDP_PORT = int(os.environ.get("JEV_CDP_PORT", "9333"))
CHROME_PROC = None  # the Chrome this process launched (None when attaching to an existing one)
if CDP_PORT != 9333:  # one profile per Chrome instance; a shared profile makes Chrome defer to the running one
    PROFILE_DIR = PROFILE_DIR.with_name(f"{PROFILE_DIR.name}-{CDP_PORT}")


def load_env():
    for env in (ROOT / ".env", ROOT.parent / ".env"):  # harness root, then the training repo root
        if not env.exists():
            continue
        for line in env.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def _chrome_binary():
    if env := os.environ.get("JEV_CHROME_BINARY"):
        return env
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        if path := shutil.which(name):
            candidates.append(path)
    return next((c for c in candidates if Path(c).exists()), None)


def _cdp_reachable(url):
    try:
        with urllib.request.urlopen(f"{url}/json/version", timeout=1) as r:
            return bool(json.loads(r.read())["webSocketDebuggerUrl"])
    except Exception:
        return False


def ensure_chrome():
    """Point Browser Harness at a dedicated automation Chrome with its own profile.

    Using our own Chrome instance avoids Chrome 144+'s per-connection "Allow remote debugging" dialog
    and keeps agent browsing out of the user's real profile. Set BU_CDP_URL / BU_CDP_WS to use another
    browser instead."""
    if os.environ.get("BU_CDP_WS") or os.environ.get("BU_CDP_URL"):
        return
    url = f"http://127.0.0.1:{CDP_PORT}"
    if not _cdp_reachable(url):
        binary = _chrome_binary()
        if not binary:
            raise RuntimeError("No Chrome/Chromium found; set JEV_CHROME_BINARY")
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        global CHROME_PROC
        CHROME_PROC = subprocess.Popen(
            [
                binary,
                f"--remote-debugging-port={CDP_PORT}",
                f"--user-data-dir={PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "--window-size=1200,900",
                "about:blank",
            ],
            stdout=open(PROFILE_DIR / "chrome.log", "ab"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            # Chrome's singleton socket lives under TMPDIR and Unix socket paths max out at 108 bytes; a long
            # TMPDIR (verifiers points it at the rollout's runtime dir) makes Chrome abort with "Socket path too long".
            env={**os.environ, "TMPDIR": os.environ.get("JEV_CHROME_TMPDIR", "/tmp")},
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not _cdp_reachable(url):
            if CHROME_PROC.poll() is not None:
                break
            time.sleep(0.2)
        if not _cdp_reachable(url):
            tail = ""
            try:
                tail = (PROFILE_DIR / "chrome.log").read_text()[-800:]
            except OSError:
                pass
            raise RuntimeError(
                f"Chrome did not expose DevTools on {url} (binary={binary}, exit={CHROME_PROC.poll()}); "
                f"log tail: {tail}"
            )
    os.environ["BU_CDP_URL"] = url


load_env()
ensure_chrome()

from browser_harness import admin as _bh_admin  # noqa: E402
from browser_harness import helpers as _bh_helpers  # noqa: E402
from browser_harness.helpers import cdp  # noqa: E402
from mcp.server.mcpserver import Image, MCPServer  # noqa: E402

from .browser import Browser, StalePage  # noqa: E402
from .model import action_space, choose, field_context, field_text  # noqa: E402

# A distinct daemon name so we never fight a "default" daemon attached to the user's own Chrome.
# browser_harness reads BU_NAME at import time (and jev_ultrafast/__init__ imports it early), so set both.
DAEMON_NAME = os.environ.get("BU_NAME", "jev")
os.environ["BU_NAME"] = DAEMON_NAME
_bh_admin.NAME = _bh_helpers.NAME = DAEMON_NAME

server = MCPServer(
    "jev-browser",
    instructions=(
        "A Chrome tab you drive with tool calls. Start with browser_open(url). Every result ends with an "
        "element table; use its [n] indices for browser_click / browser_type / browser_select. "
        "For multi-step goals, prefer browser_run(goal): Jev picks the clicks itself and only comes back "
        "when finished, stuck, or when a field needs a value you must supply (status needs_input). "
        "Then browser_type the value and call browser_run again with the same goal to continue. "
        "Never type passwords, card numbers, or government IDs; ask the human to do those."
    ),
)

SAME_TAB_JS = """(() => {
  if (window.__jevSameTab) return; window.__jevSameTab = true;
  const open = window.open;
  window.open = (url, ...rest) => {
    if (url) { location.href = String(url); return window; }
    return open.call(window, url, ...rest);
  };
  addEventListener('click', e => {
    const a = e.target && e.target.closest && e.target.closest('a[target]');
    if (a && /^_blank$/i.test(a.target)) a.target = '_self';
  }, true);
})()"""

GOAL_STOPWORDS = {
    "open",
    "click",
    "stop",
    "when",
    "then",
    "page",
    "with",
    "from",
    "that",
    "this",
    "until",
    "visible",
    "shown",
    "select",
    "choose",
    "search",
    "scroll",
    "navigate",
    "find",
    "show",
    "list",
    "the",
    "and",
    "for",
    "its",
    "into",
}
SESSIONS: dict[str, "Session"] = {}
_SESSIONS_LOCK = threading.Lock()  # browser_open may run on several threads (shared tool server)
TOOL_CALLS = [0]  # incremented per tool invocation; evidence manifest records it
_counter = 0


class Session:
    def __init__(self, url):
        self.browser = Browser(url)
        # Pop-up tabs are outside the harness. Keep navigation in this tab: retarget target=_blank links
        # at click time and turn window.open into a same-tab navigation.
        self.browser.call("Page.addScriptToEvaluateOnNewDocument", source=SAME_TAB_JS)
        try:
            self.browser.evaluate(SAME_TAB_JS)
        except StalePage:
            pass
        self.page = None
        self.elements = []  # from last observation
        self.node_by_index = {}
        self.history = []  # shared with Jev so it sees manual actions too
        self.goal = None
        self.record = []  # pages reached in this session: (via, url, title, excerpt)
        self.observe()
        self.note_page("open")

    # ---- observation -------------------------------------------------------------------------
    LAYERS_JS = """(nodes => {
      // Hit-test every observed control at its centre. Whatever is on top at that point is what a human sees.
      const covered = {}; const layers = new Map();
      const describe = el => {
        // Walk up to the overlay's root: a fixed/absolute/dialog container, then label it like a person would.
        let root = el, cur = el;
        for (let i = 0; cur && cur !== document.body && i < 25; i++, cur = cur.parentElement) {
          const cs = getComputedStyle(cur);
          if (cur.matches('[role="dialog"],[role="alertdialog"],dialog,[aria-modal="true"]') ||
              cs.position === 'fixed' || cs.position === 'absolute' || cs.position === 'sticky') root = cur;
        }
        const h = root.querySelector('h1,h2,h3,h4,[role="heading"]');
        const label = root.getAttribute('aria-label') || (h && h.innerText) || root.innerText || root.tagName;
        return {root, label: label.trim().replace(/\\s+/g, ' ').slice(0, 70)};
      };
      for (const n of nodes) {
        const e = window.__jevFast?.nodes.get(n); if (!e) continue;
        const r = e.getBoundingClientRect(); const x = r.x + r.width / 2, y = r.y + r.height / 2;
        if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
        const top = document.elementFromPoint(x, y);
        if (!top || e.contains(top) || top.contains(e)) continue;
        const d = describe(top);
        if (d.root.contains(e)) continue;  // overlap inside the same component, not an overlay over it
        if (!layers.has(d.root)) {
          const rr = d.root.getBoundingClientRect();
          const w = Math.max(0, Math.min(rr.right, innerWidth) - Math.max(rr.left, 0));
          const h = Math.max(0, Math.min(rr.bottom, innerHeight) - Math.max(rr.top, 0));
          layers.set(d.root, {label: d.label, covers: 0, ownIds: [], area: (w * h) / (innerWidth * innerHeight)});
        }
        layers.get(d.root).covers++; covered[n] = d.label;
      }
      // Which observed controls belong to each covering layer?
      for (const [root, info] of layers) for (const n of nodes) {
        const e = window.__jevFast?.nodes.get(n); if (e && root.contains(e)) info.ownIds.push(n);
      }
      const out = [...layers.values()].sort((a, b) => b.covers - a.covers);
      return {covered, layers: out};
    })(%s)"""

    def observe(self, screenshot=False):
        self.page = self.browser.observe(screenshot=screenshot)
        # Z-order awareness: controls hidden under an overlay are not offered (a person could not click them
        # either); the overlay itself is described so Jev and the agent know what is on top and what it covers.
        nodes = [a["node"] for a in self.page["actions"] if a.get("node") is not None]
        self.layers, self.dialog = [], None
        try:
            info = self.browser.evaluate(self.LAYERS_JS % json.dumps(nodes)) if nodes else None
        except (StalePage, RuntimeError):
            info = None
        if info and info["covered"]:
            covered = {int(k) for k in info["covered"]}
            kept = [a for a in self.page["actions"] if a.get("node") not in covered]
            self.page = {**self.page, "actions": kept, "covered_count": len(covered)}
            self.layers = info["layers"]
            # A modal-like overlay is one a person has to deal with before using the page: it occupies a real
            # share of the viewport and hides several controls. Sticky headers and small widgets do not qualify;
            # their covered controls are simply not offered.
            modal = [
                layer for layer in self.layers if layer["area"] >= 0.25 and layer["covers"] >= 3 and layer["ownIds"]
            ]
            if modal:
                self.dialog = modal[0]["label"]
        self.elements, _, _ = action_space(self.page["actions"])
        # action_space numbers nodes in first-seen order; mirror that here.
        order = []
        for a in self.page["actions"]:
            if a["kind"] in {"click", "fill", "select"} and a["node"] not in order:
                order.append(a["node"])
        self.node_by_index = {str(i + 1): node for i, node in enumerate(order)}
        self.index_by_node = {node: index for index, node in self.node_by_index.items()}
        return self.page

    def _index_of(self, action):
        return self.index_by_node.get(action["node"], "?")

    def render(self, *, status=None, note=None, text_chars=1500, query=None, full_text=False):
        page = self.page
        lines = []
        if note:
            lines.append(f"note: {note}")
        if status:
            lines.append(f"status: {status}")
        lines.append(f"url: {page['url']}")
        lines.append(f"title: {page['title']}")
        scroll = page["scroll"]
        pos = f"{scroll['y']}/{max(scroll['height'] - page['h'], 0)}"
        can = [a["id"] for a in page["actions"] if a["kind"] == "scroll"]
        lines.append(f"scroll: {pos}" + (f" ({', '.join(can)})" if can else ""))
        shown = self.elements
        if query:
            q = query.lower()
            shown = [
                e
                for e in shown
                if q in e["label"].lower()
                or q in str(e.get("value", "")).lower()
                or any(q in o["label"].lower() for o in e.get("options", []))
            ]
        for layer in getattr(self, "layers", []) or []:
            kind = "modal overlay" if layer["label"] == self.dialog else "overlapping element"
            lines.append(
                f"on top ({kind}, {layer['area']:.0%} of viewport): {layer['label']!r} covers {layer['covers']} "
                f"control(s) underneath; {len(layer['ownIds'])} of its own controls are listed. "
                "Covered controls are not listed."
            )
        lines.append(f"elements ({len(shown)}{' matching' if query else ''}):")
        for e in shown:
            ops = []
            if "CLICK" in e["operations"]:
                ops.append("click")
            if "TYPE_TEXT" in e["operations"]:
                ops.append("type")
            if "SELECT" in e["operations"]:
                ops.append("select")
            extra = ""
            if e.get("value"):
                extra += f'  = "{str(e["value"])[:80]}"'
            for key in ("checked", "selected", "expanded"):
                if key in e:
                    extra += f"  {key}={e[key]}"
            if e.get("options"):
                opts = ", ".join(f"{o['index'].split(':')[1]}:{o['label'][:30]}" for o in e["options"][:12])
                more = f" …+{len(e['options']) - 12}" if len(e["options"]) > 12 else ""
                extra += f"  options[{opts}{more}]"
            label = " ".join(e["label"].split())[:90]
            lines.append(f"[{e['index']}] {e['role']:<10} {label}{extra}  ({'/'.join(ops)})")
        if page.get("omitted_actions"):
            lines.append(f"(+{page['omitted_actions']} more controls not shown)")
        if text_chars:
            if full_text:
                text = self.main_text()
                heading = f"full page text ({len(text)} chars, whole page, not just the visible part):"
            else:
                text = " ".join(page["text"].split())
                heading = "visible text:"
            if len(text) > text_chars:
                text = text[:text_chars] + " …"
            lines.append(heading)
            lines.append(text)
        return "\n".join(lines)

    # ---- manual actions ----------------------------------------------------------------------
    def _action(self, index, kind, option=None):
        index = str(index).strip().lstrip("[").rstrip("]")
        node = self.node_by_index.get(index)
        if node is None:
            raise ValueError(
                f"No element [{index}] in the latest observation. Indices change after every action; "
                "use only the [n] numbers from the table below."
            )
        # Re-observe so freshness guards pass; the node id survives while the DOM element does.
        self.observe()
        candidates = [a for a in self.page["actions"] if a.get("node") == node and a["kind"] == kind]
        if kind == "click":
            candidates.sort(key=lambda a: a["label"].startswith("Open "))
        if kind == "select" and option is not None:
            opt = str(option).strip().lower()
            picked = [
                a
                for i, a in enumerate(candidates, 1)
                if str(i) == opt or a["value"].lower() == opt or a["label"].split(" → ")[-1].lower() == opt
            ]
            if not picked:
                picked = [a for a in candidates if opt in a["label"].split(" → ")[-1].lower()]
            candidates = picked
        if not candidates:
            raise ValueError(
                f"Element [{index}] can no longer be used for {kind}"
                + (f" with option {option!r}" if option else "")
                + ". The page changed; see the fresh observation."
            )
        return candidates[0]

    def arm_same_tab(self):
        # bfcache restores drop injected scripts; re-arm same-tab navigation right before every click.
        try:
            self.browser.evaluate(SAME_TAB_JS)
        except StalePage:
            pass

    def adopt_popups(self):
        """If the last action opened a new tab anyway, pull its URL into this tab and close the popup."""
        targets = cdp("Target.getTargets")["targetInfos"]
        popups = [t for t in targets if t["type"] == "page" and t.get("openerId") == self.browser.target]
        if not popups:
            return False
        url = popups[-1]["url"]
        for t in popups:
            try:
                cdp("Target.closeTarget", targetId=t["targetId"])
            except Exception:
                pass
        if url and url != "about:blank":
            self.goto(url)
        return True

    def close_dialog(self):
        """Dismiss the topmost overlay via its own Done/Close/OK button, else Escape. True if it went away."""
        before_label = self.layers[0]["label"] if self.layers else None
        own = self.layers[0]["ownIds"] if self.layers else []
        clicked = False
        try:
            clicked = bool(
                self.browser.evaluate(
                    """((ids) => {
                  // The overlay root = the common ancestor of its own controls (no role attributes needed).
                  const els = ids.map(i => window.__jevFast?.nodes.get(i)).filter(Boolean);
                  let top = els[0] || null;
                  for (const e of els.slice(1)) while (top && !top.contains(e)) top = top.parentElement;
                  if (!top) return false;
                  const re = /^(done|close|ok|apply|save|continue|got it|×|✕)$|close|dismiss/i;
                  const label = b => (b.getAttribute('aria-label') || b.title || b.innerText || '').trim();
                  const btns = [...top.querySelectorAll('button,[role="button"]')]
                    .filter(b => b.checkVisibility() && re.test(label(b)));
                  if (!btns.length) return false;
                  btns.sort((a, b) => label(a).length - label(b).length); btns[0].click(); return true; })(%s)"""
                    % json.dumps(own)
                )
            )
        except (StalePage, RuntimeError):
            pass
        if not clicked:
            for kind in ("keyDown", "keyUp"):
                self.browser.call(
                    "Input.dispatchKeyEvent", type=kind, key="Escape", code="Escape", windowsVirtualKeyCode=27
                )
        time.sleep(0.4)
        self.observe()
        return not self.layers or self.layers[0]["label"] != before_label

    def uncover(self, node):
        """If an overlay (chat widget, cookie banner, modal) covers the node, dismiss it via its own close button.
        Returns True if something was dismissed."""
        return bool(
            self.browser.evaluate(
                """(n => {
              const e = window.__jevFast?.nodes.get(n); if (!e) return false;
              const r = e.getBoundingClientRect();
              const top = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
              if (!top || e.contains(top) || top.contains(e)) return false;
              const re =
                /close|dismiss|minimi|hide|^x$|×|✕|no thanks|got it|accept|reject|decline|^done$|^apply$|^ok$/i;
              const vis = b => { const q = b.getBoundingClientRect(); return q.width > 0 && q.height > 0 &&
                b.checkVisibility({checkOpacity:true, checkVisibilityCSS:true}); };
              const label = b => (b.getAttribute('aria-label') || b.title || b.innerText || '').trim();
              let cur = top;
              for (let i = 0; i < 12 && cur && cur !== document.body; i++, cur = cur.parentElement) {
                const buttons = [...cur.querySelectorAll('button,[role="button"],a')]
                  .filter(b => vis(b) && re.test(label(b)));
                if (buttons.length) {
                  buttons.sort((a, b) => label(a).length - label(b).length); buttons[0].click(); return true;
                }
              }
              return false; })(%d)"""
                % node
            )
        )

    def _previous_entry(self):
        """The history entry Back would land on, or None if there is no real page behind us."""
        history = self.browser.call("Page.getNavigationHistory")
        index = history["currentIndex"]
        if index <= 0:
            return None
        entry = history["entries"][index - 1]
        if entry["url"] in {"about:blank", ""}:
            return None
        return entry

    def press_enter(self):
        before = self.page
        for kind in ("keyDown", "keyUp"):
            self.browser.call(
                "Input.dispatchKeyEvent", type=kind, key="Enter", code="Enter", windowsVirtualKeyCode=13, text="\r"
            )
        time.sleep(0.3)
        self.observe()
        if self.page["url"] != before["url"]:
            self.settle()
            self.note_page("enter")
        self.capture_evidence("key: Enter")

    def back(self):
        entry = self._previous_entry()
        if entry is None:
            return False
        self.browser.call("Page.navigateToHistoryEntry", entryId=entry["id"])
        self.goto_wait()
        return True

    def can_go_back(self):
        try:
            return self._previous_entry() is not None
        except Exception:
            return False

    BACK_ACTION = {
        "id": "back",
        "kind": "back",
        "label": "Go back to the previous page (browser Back). Use when the goal's target was on the previous page.",
    }

    def act(self, action, page, text=None):
        if action["kind"] == "back":
            self.back()
            self.observe()
            return
        if action["kind"] == "click":
            self.arm_same_tab()
        if action["kind"] in {"click", "fill", "select"}:
            try:
                self.browser.evaluate(
                    "(n => { const e = window.__jevFast?.nodes.get(n); "
                    "if (e) e.scrollIntoView({block: 'center', inline: 'nearest'}); })(%d)" % action["node"]
                )
            except StalePage:
                pass
            if not self.browser.fresh(page):
                page = self.observe()
                action = next(
                    (
                        a
                        for a in page["actions"]
                        if a.get("node") == action["node"]
                        and a["kind"] == action["kind"]
                        and a.get("value") == action.get("value")
                    ),
                    action,
                )
        try:
            self.browser.act(action, page, text=text)
        except StalePage as e:
            if "covered" not in str(e) or action["kind"] == "wait" or not self.uncover(action["node"]):
                raise
            # Overlays animate out; wait until the target is actually hit-testable again.
            for _ in range(12):
                time.sleep(0.15)
                clear = self.browser.evaluate(
                    "(n => { const e = window.__jevFast?.nodes.get(n); if (!e) return false; "
                    "const r = e.getBoundingClientRect(); "
                    "return e.contains(document.elementFromPoint(r.x + r.width/2, r.y + r.height/2)); })(%d)"
                    % action["node"]
                )
                if clear:
                    break
            page = self.observe()
            action = next(
                a
                for a in page["actions"]
                if a.get("node") == action["node"]
                and a["kind"] == action["kind"]
                and a.get("value") == action.get("value")
            )
            self.browser.act(action, page, text=text)
        self.observe_after_action()
        if action["kind"] == "click" and self.adopt_popups():
            self.observe_after_action()
        if self.page["url"] != page["url"]:
            # A navigation happened. Give the new document time to render before anyone judges it.
            self.settle()
            self.note_page(action["label"])
        self.capture_evidence(f"{action['kind']}: {action['label'][:80]}" + (f" = {text!r}" if text else ""))

    def capture_evidence(self, what):
        """Independent evidence trail for judges: a screenshot after every browser action, with a manifest.
        Enabled by JEV_EVIDENCE_DIR. The agent neither chooses nor sees these images."""
        root = os.environ.get("JEV_EVIDENCE_DIR")
        if not root:
            return
        try:
            Path(root).mkdir(parents=True, exist_ok=True)
            n = len(list(Path(root).glob("*.jpg"))) + 1
            data = self.browser.call("Page.captureScreenshot", format="jpeg", quality=60)["data"]
            (Path(root) / f"{n:04d}.jpg").write_bytes(base64.b64decode(data))
            with open(Path(root) / "manifest.jsonl", "a") as f:
                f.write(
                    json.dumps(
                        {
                            "n": n,
                            "time": time.time(),
                            "url": self.page["url"][:300],
                            "what": what,
                            "tool_call": TOOL_CALLS[0],
                        }
                    )
                    + "\n"
                )
        except Exception:
            pass

    def note_page(self, via):
        """Append the current page to the session's task record if it is a new URL."""
        url = _short_url(self.page["url"])
        self.prev_text = getattr(self, "cur_text", "")
        self.cur_text = self.main_text()
        if any(r["url"] == url for r in self.record):
            return
        excerpt = self.main_text()[:400]
        self.record.append({"via": via[:60], "url": url, "title": self.page["title"][:80], "excerpt": excerpt})

    def main_text(self):
        """Rendered text of the page's main content (whole document, not just the viewport), skipping chrome."""
        try:
            text = self.browser.evaluate(
                """(() => {
              const root = document.querySelector('main, [role="main"], #main, article') || document.body;
              const skip = 'header,nav,footer,aside,script,style,noscript,template,[role="navigation"],'
                + '[role="banner"],[role="contentinfo"],[aria-hidden="true"],[hidden]';
              const out = []; let n = 0;
              const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
              let node;
              while ((node = w.nextNode()) && n < 20000) {
                const t = node.textContent.trim(); const p = node.parentElement;
                if (!t || !p || p.closest(skip)) continue;
                if (!p.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
                const r = p.getBoundingClientRect(); if (!r.width || !r.height) continue;
                out.push(t); n += t.length;
              }
              return out.join(' '); })()"""
            )
        except (StalePage, RuntimeError):
            text = self.page["text"]
        return " ".join(str(text or self.page["text"]).split())

    LINKS_JS = """(() => {
      const vis = e => { const r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && e.checkVisibility(); };
      const norm = t => (t || '').replace(/\\s+/g, ' ').trim();
      const links = [], seen = new Set();
      for (const a of document.querySelectorAll('a[href]')) {
        if (!vis(a)) continue;
        const href = a.href; if (!href || href.startsWith('javascript:') || seen.has(href)) continue;
        seen.add(href);
        const label = norm(a.innerText) || norm(a.getAttribute('aria-label')) || norm(a.title) ||
          norm(a.querySelector('img')?.alt) || '';
        links.push([label.slice(0, 80), href]);
      }
      const images = [], seenImg = new Set();
      for (const img of document.querySelectorAll('img')) {
        if (!vis(img)) continue;
        const src = img.currentSrc || img.src || '';
        if (!/^https?:/.test(src) || seenImg.has(src)) continue;
        const r = img.getBoundingClientRect(); if (r.width < 40 || r.height < 40) continue;
        seenImg.add(src);
        images.push([norm(img.alt).slice(0, 80), src]);
      }
      return {links, images}; })()"""

    def render_links(self, max_items=200):
        try:
            data = self.browser.evaluate(self.LINKS_JS) or {}
        except (StalePage, RuntimeError):
            return "links: (page changing, try again)"
        links, images = data.get("links", []), data.get("images", [])
        lines = [f"links ({len(links)} total, showing {min(len(links), max_items)}):"]
        lines += [f"  {label or '(no text)'} -> {href}" for label, href in links[:max_items]]
        lines.append(f"images ({len(images)} total, showing {min(len(images), max_items)}):")
        lines += [f"  {alt or '(no alt)'} -> {src}" for alt, src in images[:max_items]]
        return "\n".join(lines)

    def render_record(self):
        if not self.record:
            return ""
        lines = [f"task record ({len(self.record)} pages reached in this session):"]
        for i, r in enumerate(self.record, 1):
            lines.append(f"  {i}. via {r['via']!r} -> {r['url']}")
            lines.append(f"     {r['excerpt'][:300]}")
        return "\n".join(lines)

    def do(self, action, text=None):
        started = time.perf_counter()
        before = self.page
        self.act(action, before, text=text)
        self.history.append(
            {
                "step": len(self.history) + 1,
                "action": action["label"],
                "kind": action["kind"],
                "text": text,
                "page_changed": self.page["fingerprint"] != before["fingerprint"],
                "url": self.page["url"],
                "source": "manual",
                "ms": round((time.perf_counter() - started) * 1000),
            }
        )
        return self.history[-1]

    def observe_after_action(self, timeout=8.0):
        """Observe once the document exists again. The action already happened; a navigation in progress is not
        an error, so keep trying until the new page can be read."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.observe()
            except (StalePage, RuntimeError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.25)

    def settle(self, timeout=6.0):
        """Wait until the current document is loaded and shows some content (SPAs often paint late)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                ready = self.browser.evaluate("document.readyState") == "complete"
            except (StalePage, RuntimeError):
                ready = False
            if ready and (self.page["text"].strip() or len(self.page["actions"]) > 3):
                return
            time.sleep(0.25)
            try:
                self.observe()
            except (StalePage, RuntimeError):
                pass

    def goto(self, url):
        self.browser.call("Page.navigate", url=url)
        self.goto_wait()

    def goto_wait(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if self.browser.evaluate("document.readyState") == "complete":
                    break
            except (StalePage, RuntimeError):
                pass
            time.sleep(0.05)
        time.sleep(0.15)
        self.browser.after_input = None
        self.observe()

    # ---- Jev autopilot -----------------------------------------------------------------------
    def run(self, goal, max_steps):
        if goal != self.goal:
            self.goal, self.history = goal, []
        steps = []
        started = time.perf_counter()
        verifying_from = None
        revealed = set()
        stale_streak = 0
        last_stale = ""
        iterations = 0
        soft_done_waits = 0
        dismissed_dialogs = set()
        opened_boxes = set()
        while len(steps) < max_steps and iterations < max_steps * 3:
            iterations += 1
            if stale_streak:
                # The page is still changing (animation, loading overlay, typeahead). Back off before retrying
                # instead of burning the step budget on instant failures.
                time.sleep(min(0.3 * stale_streak, 2.0))
                if stale_streak >= 8:
                    return "blocked", steps, {"reason": f"page kept changing: {last_stale}"}
            self.observe()
            if self.reveal_goal_tokens(goal, revealed):
                self.observe()
            page = self.page
            jev_goal = goal
            if self.dialog:
                top = next(layer for layer in self.layers if layer["label"] == self.dialog)
                jev_goal += (
                    f"\nAn overlay {top['label']!r} is on top of the page and hides {top['covers']} control(s). "
                    "Only controls that are not covered are offered."
                )
            offer_back = not self.dialog and self.can_go_back() and (page["text"].strip() or len(page["actions"]) > 3)
            if offer_back and not steps:
                # As a goal's FIRST move, Back is only sensible if the goal names something that was on the previous
                # page and is absent here; otherwise the agent just positioned this page on purpose.
                offer_back = self.goal_targets_previous_page(goal)
            if offer_back:
                page = {**page, "actions": [*page["actions"], dict(self.BACK_ACTION)]}
                jev_goal += (
                    "\nIf the element this goal needs is not on the current page, choose BACK to return to the "
                    "previous page instead of typing into unrelated fields such as site search."
                )
            scroll_down = next((a for a in page["actions"] if a["id"] == "scroll_down"), None)
            extra = None
            if scroll_down is not None:
                extra = {
                    "more_below": {
                        "type": "choice",
                        "criteria": {
                            "YES": "Content further down this page (not yet seen) could still matter for the goal.",
                            "NO": "Nothing below the visible part of this page matters for the goal.",
                        },
                        "instructions": {"goal": jev_goal, "rules": "Judge the current page only."},
                    }
                }
            try:
                decision = choose(page, jev_goal, self.history, extra)
            except ValueError:
                # Occasional malformed answers from the model API; one retry is cheap and usually enough.
                time.sleep(0.5)
                decision = choose(page, jev_goal, self.history, extra)
            choice = decision["choice"]
            if choice == "BLOCKED":
                # Menus and dialogs often finish closing right after the observation Jev judged.
                # Give the page a moment; if it changed, decide again instead of giving up.
                time.sleep(0.4)
                self.observe()
                if self.page["fingerprint"] != page["fingerprint"]:
                    continue
                # Stuck inside a dialog whose contents cannot serve the goal (e.g. an auto-opened date picker):
                # dismiss it once and let Jev continue on the page underneath.
                top_label = self.layers[0]["label"] if self.layers else None
                if top_label and top_label not in dismissed_dialogs:
                    dismissed_dialogs.add(top_label)
                    if self.close_dialog():
                        self.observe()
                        continue
                return "blocked", steps, None
            if choice == "DONE":
                # A hesitant DONE while the page is still settling (WAIT nearly tied) is usually premature.
                done_p = decision["operation_probabilities"].get("DONE", 1.0)
                wait_p = decision["operation_probabilities"].get("WAIT", 0.0)
                if done_p < 0.6 and wait_p > 0.2 and soft_done_waits < 3:
                    soft_done_waits += 1
                    time.sleep(0.8)
                    continue
                # Jev judges from the viewport only. If it also says unseen content below could matter, scroll and
                # let it decide again; otherwise accept DONE and restore the scroll position where it first said so.
                more_below = decision["raw_answers"].get("more_below", {}).get("choice") == "YES"
                if scroll_down is not None and more_below:
                    if verifying_from is None:
                        verifying_from = self.page["scroll"]["y"]
                    try:
                        self.act(scroll_down, page)
                    except StalePage:
                        pass
                    continue
                if verifying_from is not None:
                    try:
                        self.browser.evaluate(f"window.scrollTo(0, {verifying_from})")
                    except StalePage:
                        pass
                    self.observe()
                return "done", steps, None
            verifying_from = None
            action = next(a for a in page["actions"] if a["id"] == choice)
            text = None
            if action["kind"] == "fill" and action.get("role") == "combobox" and action["node"] not in opened_boxes:
                # Many "comboboxes" are dropdowns with a text-shaped face. Open it first; if options appear, Jev can
                # pick one instead of the agent guessing a string the control would reject.
                opened_boxes.add(action["node"])
                opener = next(
                    (a for a in page["actions"] if a.get("node") == action["node"] and a["kind"] == "click"), None
                )
                if opener is not None:
                    before_options = self.count_options()
                    try:
                        self.act(opener, page)
                    except StalePage:
                        continue
                    if self.count_options() > before_options:
                        continue
            if action["kind"] == "fill":
                value = None
                if os.environ.get("TEXT_MODEL_API_KEY"):
                    try:
                        value, _ = field_text(field_context(goal, action, page, self.history))
                    except ValueError:
                        value = None
                if value is None:
                    index = self._index_of(action)
                    return (
                        "needs_input",
                        steps,
                        {
                            "element": index,
                            "label": action["label"],
                            "current_value": action.get("value", ""),
                            "confidence": decision["confidence"],
                        },
                    )
                text = value
            try:
                self.act(action, page, text=text)
            except StalePage as e:
                stale_streak += 1
                last_stale = str(e)
                continue
            stale_streak = 0
            entry = {
                "step": len(self.history) + 1,
                "action": action["label"],
                "kind": action["kind"],
                "text": text,
                "page_changed": self.page["fingerprint"] != page["fingerprint"],
                "url": self.page["url"],
                "source": "jev",
                "confidence": round(decision["confidence"], 2),
                "ms": round((time.perf_counter() - started) * 1000),
            }
            self.history.append(entry)
            steps.append(entry)
            recent = self.history[-3:]
            if len(recent) == 3 and all(h["page_changed"] is False and h["kind"] != "wait" for h in recent):
                return "blocked", steps, None
        return "budget", steps, None

    def goal_tokens(self, goal):
        tokens = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{7,}", goal) if not t.isalpha()]
        tokens += [" ".join(q.split()) for q in re.findall(r"[\"'“]([^\"'”]{3,60})[\"'”]", goal)]
        tokens += [w for w in re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", goal) if w.lower() not in GOAL_STOPWORDS]
        return tokens

    def goal_targets_previous_page(self, goal):
        """True if a goal-named token appears in the previous page's text but not in the current page's."""
        prev = (getattr(self, "prev_text", "") or "").lower()
        if not prev:
            return False
        here = self.main_text().lower()
        return any(t.lower() in prev and t.lower() not in here for t in self.goal_tokens(goal))

    def reveal_goal_tokens(self, goal, revealed):
        """If the goal names an identifier (order number, tracking code, exact phrase) that is on the page but
        outside the viewport, scroll it into view so Jev can act on it. Each token is revealed at most once."""
        tokens = self.goal_tokens(goal)
        viewport = " ".join(self.page["text"].split()).lower()
        key_base = _short_url(self.page["url"])
        for token in tokens:
            key = (key_base, token)
            if key in revealed or token.lower() in viewport:
                continue
            try:
                found = self.browser.evaluate(
                    """(needle => {
                  const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
                  const want = norm(needle).toLowerCase();
                  // Prefer the smallest visible element whose own text contains the phrase (handles text split
                  // across inline nodes), skipping anything already inside the viewport.
                  const inView = r => r.bottom > 0 && r.top < innerHeight && r.width > 0 && r.height > 0;
                  let best = null;
                  for (const e of document.body.querySelectorAll('*')) {
                    if (!norm(e.textContent).toLowerCase().includes(want)) continue;
                    if (e.children.length > 12) continue;
                    if (!e.checkVisibility()) continue;
                    const r = e.getBoundingClientRect(); if (inView(r)) continue;
                    if (!best || e.textContent.length < best.textContent.length) best = e;
                  }
                  if (!best) return false;
                  best.scrollIntoView({block: 'center'}); return true; })(%s)"""
                    % json.dumps(token)
                )
            except (StalePage, RuntimeError):
                found = False
            if found:
                revealed.add(key)
                time.sleep(0.1)
                return True
        return False

    def count_options(self):
        """Visible choice-like items (listbox options, menu items) currently on the page."""
        return sum(1 for e in self.elements if e.get("role") in {"option", "menuitem", "menuitemradio"})

    def visited_pages(self):
        """Distinct pages reached by clicks in this task, as short labels Jev can match against link text."""
        seen, out = set(), []
        for h in self.history:
            if h["kind"] != "click" or not h.get("page_changed"):
                continue
            label = f"'{h['action'][:50]}' -> {_short_url(h['url'])}"
            if label not in seen:
                seen.add(label)
                out.append(label)
        return out

    def close(self):
        self.browser.close()


def _short_url(url):
    from urllib.parse import parse_qsl, urlencode, urlsplit

    parts = urlsplit(url)
    query = [
        (k, v[:30]) for k, v in parse_qsl(parts.query) if not k.startswith("_") and k.lower() not in {"utm_source"}
    ]
    return parts.netloc + parts.path + ("?" + urlencode(query) if query else "")


def _session(session_id):
    TOOL_CALLS[0] += 1
    session = SESSIONS.get(session_id)
    if not session:
        raise ValueError(f"Unknown session {session_id!r}. Open one with browser_open.")
    return session


def _fmt_steps(steps):
    if not steps:
        return "actions: none"
    lines = ["actions:"]
    for s in steps:
        text = f' "{s["text"]}"' if s.get("text") else ""
        changed = "" if s["page_changed"] else "  (no page change)"
        lines.append(f"  {s['step']}. {s['kind']} {s['action'][:70]}{text}{changed}")
    return "\n".join(lines)


# ---- tools ---------------------------------------------------------------------------------------


@server.tool()
def browser_open(url: str) -> str:
    """Open a new Chrome tab at url. Returns a session id, the element table, and the FULL text of the page (whole
    document, so you can list every item without scrolling). Keep the session id; every other tool needs it."""
    global _counter
    with _SESSIONS_LOCK:
        _counter += 1
        TOOL_CALLS[0] += 1
        session_id = f"s{_counter}"
    session = Session(url)
    SESSIONS[session_id] = session
    session.capture_evidence(f"open: {url[:120]}")
    hint = f'\nNext: browser_run("{session_id}", "<goal in plain English>") navigates for you.'
    return f"session: {session_id}\n" + session.render(text_chars=8000, full_text=True) + hint


@server.tool()
def browser_observe(session: str, text_chars: int = 1500, find: str | None = None) -> str:
    """Re-read the current page: url, title, scroll position, numbered element table, visible text.
    find filters the element table by substring (label, value, or option). text_chars=0 hides page text."""
    s = _session(session)
    s.observe()
    return s.render(text_chars=text_chars, query=find)


@server.tool()
def browser_goto(session: str, url: str) -> str:
    """Navigate the session's tab to url and return the new observation."""
    s = _session(session)
    s.goto(url)
    s.note_page("goto")
    s.capture_evidence(f"goto: {url[:120]}")
    return s.render(text_chars=8000, full_text=True)


@server.tool()
def browser_back(session: str) -> str:
    """Go back one page in the tab's history (like the browser Back button) and return the observation."""
    s = _session(session)
    if not s.back():
        return s.render(note="cannot go back: no earlier page in this tab")
    s.observe()
    return s.render(note="went back")


@server.tool()
def browser_click(session: str, element: str) -> str:
    """Click element [n] from the last observation. Returns the observation after the click."""
    s = _session(session)
    try:
        entry = s.do(s._action(element, "click"))
    except (StalePage, ValueError) as e:
        return s.render(note=f"click failed: {e}")
    note = f"clicked {entry['action']!r}"
    if not entry["page_changed"]:
        note += ". Page did not change: do not click it again; try another element, browser_wait, or browser_goto(url)."
    return s.render(note=note)


@server.tool()
def browser_type(session: str, element: str, text: str, submit: bool = False) -> str:
    """Replace the contents of editable element [n] with text (select-all then insert). submit=true also presses
    Enter afterwards, which submits search boxes and forms that have no visible button.
    For autocomplete fields, the observation afterwards shows the suggestions; let Jev pick one."""
    s = _session(session)
    try:
        entry = s.do(s._action(element, "fill"), text=text)
    except (StalePage, ValueError) as e:
        return s.render(note=f"type failed: {e}")
    if submit:
        s.press_enter()
    return s.render(note=f"typed into {entry['action']!r}" + (" and pressed Enter" if submit else ""))


@server.tool()
def browser_select(session: str, element: str, option: str) -> str:
    """Choose an option in native <select> element [n]. option is the option number, its label, or its value
    as shown in the element table."""
    s = _session(session)
    try:
        entry = s.do(s._action(element, "select", option))
    except (StalePage, ValueError, RuntimeError) as e:
        return s.render(note=f"select failed: {e}")
    return s.render(note=f"selected {entry['action']!r}")


@server.tool()
def browser_scroll(session: str, direction: str = "down") -> str:
    """Scroll the page 'down' or 'up' by about one screen."""
    s = _session(session)
    s.observe()
    wanted = "scroll_down" if direction.lower().startswith("d") else "scroll_up"
    action = next((a for a in s.page["actions"] if a["id"] == wanted), None)
    if not action:
        return s.render(note=f"cannot scroll {direction}: already at the edge")
    s.do(action)
    return s.render(note=f"scrolled {direction}")


@server.tool()
def browser_wait(session: str, seconds: float = 1.0) -> str:
    """Wait up to 10 seconds for the page to load or update, then observe."""
    s = _session(session)
    time.sleep(min(max(seconds, 0), 10))
    s.observe()
    return s.render()


@server.tool()
def browser_run(session: str, goal: str, max_steps: int = 25) -> str:
    """THE MAIN NAVIGATION TOOL. Give it a goal in plain English and Jev (a fast browser model) does the clicking,
    scrolling and waiting for you, then returns the resulting page. Use it instead of chaining browser_click calls.

    Example goals:
      "Scroll through the whole page so every order and tracking number has been shown. Stop at the bottom."
      "Open the tracking page for tracking number 1ZE569860314500199 and stop when the delivery date is visible."
      "Search one-way flights ZRH to LHR on 2026-10-14 for 1 adult and stop when results are listed."

    Returns the page plus status:
      done        - goal visibly satisfied (confirm in the page text)
      needs_input - Jev must type into element [n] but needs the value from you: call browser_type(session, n, value)
                    then browser_run again with the SAME goal
      blocked     - stuck; call again with a smaller, more specific goal
      budget      - max_steps used; call again with the same goal to continue
    Calls with the same goal share history, so continuing is cheap."""
    s = _session(session)
    try:
        status, steps, pending = s.run(goal, max_steps)
    except (RuntimeError, ValueError) as e:
        s.observe()
        return s.render(status="error", note=str(e))
    note = None
    if pending and "reason" in pending:
        note = f"blocked: {pending['reason']}. Try browser_wait then the same goal, or a smaller goal."
    elif pending:
        note = (
            f"needs_input: type a value into [{pending['element']}] {pending['label']!r}"
            f" (currently {pending['current_value']!r}) with browser_type, then browser_run again."
        )
    return _fmt_steps(steps) + "\n" + s.render(status=status, note=note) + "\n\n" + s.render_record()


@server.tool()
def browser_screenshot(session: str) -> Image:
    """JPEG screenshot of the tab. Use when the element table is ambiguous or to verify visually."""
    s = _session(session)
    s.observe(screenshot=True)
    return Image(data=base64.b64decode(s.page["screenshot"]), format="jpeg")


@server.tool()
def browser_text(session: str, max_chars: int = 12000, links: bool = False) -> str:
    """Read the ENTIRE page's text (not just the visible part), up to max_chars. Use this first on long pages such as
    order lists or search results so you see every item at once instead of scrolling.
    links=true also returns the page's links (text -> URL) and images (alt -> URL), which is the fast way to collect
    detail-page URLs or image sources for many items without opening each one."""
    s = _session(session)
    s.observe()
    try:
        text = s.browser.evaluate(
            "(() => { const t = document.body ? document.body.innerText : ''; return t.replace(/\\s+\\n/g, '\\n'); })()"
        )
    except (StalePage, RuntimeError):
        text = s.page["text"]
    text = "\n".join(line.strip() for line in str(text).splitlines() if line.strip())
    if len(text) > max_chars:
        text = text[:max_chars] + " …"
    out = f"url: {s.page['url']}\nfull page text ({len(text)} chars):\n{text}"
    if links:
        out += "\n\n" + s.render_links(max_items=max(40, max_chars // 60))
    return out


@server.tool()
def browser_history(session: str) -> str:
    """Every action taken in this session so far, manual and Jev-driven."""
    s = _session(session)
    if not s.history:
        return "no actions yet"
    return "\n".join(
        f"{h['step']}. [{h['source']}] {h['kind']} {h['action'][:70]}"
        + (f' "{h["text"]}"' if h.get("text") else "")
        + ("" if h["page_changed"] else "  (no page change)")
        + f"  -> {h['url'][:80]}"
        for h in s.history
    )


@server.tool()
def browser_sessions() -> str:
    """List open sessions and their current URLs."""
    if not SESSIONS:
        return "no open sessions"
    return "\n".join(f"{sid}: {s.page['url']}" for sid, s in SESSIONS.items())


@server.tool()
def browser_close(session: str) -> str:
    """Close the tab and forget the session."""
    s = SESSIONS.pop(session, None)
    if not s:
        return f"no session {session}"
    s.close()
    return f"closed {session}"


def main():
    logging.getLogger("httpx").setLevel(logging.WARNING)
    print(f"jev-browser MCP: Chrome at {os.environ.get('BU_CDP_URL') or os.environ.get('BU_CDP_WS')}", file=sys.stderr)
    server.run("stdio")


if __name__ == "__main__":
    main()
