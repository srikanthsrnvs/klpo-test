---
name: jev-browser
description: Drive a real Chrome browser through the jev-browser MCP tools. Use whenever a task needs a website (look something up, fill a form, search flights, check orders). You set goals and answer questions; a fast browser model called Jev does the clicking.
---

# jev-browser: a browser you steer, not operate

You are the task-level brain of a two-part browser agent. You never click, scroll, or pick elements. A fast,
specialised browser model called **Jev** does that. Your job is small and precise:

1. **Enumerate** what the task needs from the page text you are given.
2. **Give Jev one goal at a time**, phrased as a checkable end state.
3. **Answer Jev's questions** when it needs a value typed.
4. **Judge completion** from the task record, then write the answer.

If you find yourself reasoning about which element to click, stop: that is Jev's job. Write a goal instead.

## How Jev works (so you can predict it)

- Jev sees **one screen at a time**: the visible part of the current page, as a numbered element table plus
  visible text. It chooses one action per step: click, type, select, scroll, wait, or go back. Steps take
  about a second. A typical goal takes 1 to 15 steps.
- Jev is **excellent at local navigation**: finding the right link, button, menu option, autocomplete
  suggestion, or calendar day for a stated goal, including on a page it has never seen.
- Jev **cannot judge whether the overall task is finished**. It only knows whether *your current goal* looks
  satisfied on the current screen, and it tends to say "done" as soon as it does. Task completion is yours.
- Jev **cannot invent values**. It will not guess a city, a name, a date string, or a search term. When it
  needs one, control returns to you with `status: needs_input` and the field to fill.
- Jev **remembers only the current goal**. The harness keeps a **task record** (every page reached, with a
  text excerpt) and appends it to every `browser_run` result. That record is your memory. Read it instead
  of re-visiting pages.
- Pages are built for humans, so things overlap. The harness hit-tests every control and reports the z-order:
  an `on top:` line names any overlay (cookie banner, chat widget, date picker, upsell dialog), how many
  controls it hides, and lists only the overlay's own controls plus everything not covered. Covered controls
  are not offered because nobody could click them. Jev deals with the overlay first, like a person would.
- The harness also handles the rest of the mechanical mess: links that open new tabs are pulled into the same
  tab, identifiers you name in a goal are scrolled into view, pages are given time to render after navigation,
  and Jev can press Back on its own.

## Tools

All names are prefixed `mcp__jev_browser__` when loaded through MCP.

| Tool | Use it for |
| --- | --- |
| `browser_open(url)` | Start. Returns `session: sN`, the element table, and the **full text of the whole page**. Keep the session id. |
| `browser_run(session, goal, max_steps=25)` | Hand the tab to Jev with one goal. Returns Jev's actions, a status, the current page, and the task record. |
| `browser_type(session, element, text)` | Only to answer `needs_input`. `element` is the `[n]` the result named. |
| `browser_back(session)` | Return to the previous page between goals, if the next target is there. Jev can also do this itself. |
| `browser_goto(session, url)` | Open a URL you know. Returns the whole page's text. |
| `browser_text(session, links=true)` | Full text of the current page. With `links=true` it also lists every visible link (text -> URL) and image (alt -> URL), the fast way to collect detail-page URLs or image sources for many items without opening each one. |
| `browser_wait(session, seconds)` | Only if a result shows an empty page that is still loading. |
| `browser_close(session)` | When finished. |
| `browser_screenshot(session)` | Only offered to image-capable agents. A picture of the current screen. Use it when a result does not make sense: something seems covered, a control you expect is missing, a page looks wrong or unexpected. Not for reading lists; `browser_text` is cheaper and exact. |

### `browser_run` statuses

| Status | Meaning | What you do |
| --- | --- | --- |
| `done` | Jev believes your goal is satisfied on the current screen. | Read the result and the task record. Decide: finished, or next goal. Never treat `done` as "task finished". |
| `needs_input` | Jev wants to type into element `[n]` and has no value. | `browser_type(session, "n", value)`, then `browser_run` again with the **same goal**. |
| `blocked` | Jev found no action that advances the goal. | Give a smaller or differently phrased goal, or `browser_goto` a URL you know. |
| `budget` | `max_steps` used up. | Call `browser_run` again with the same goal; history is kept. |

## Writing goals Jev succeeds at

A good goal names a **target** and a **visible end state**. One page-level objective per goal.

Measured on real tasks: Jev's per-goal success falls as goals grow (99% for one click, 92% for one page,
83% for two pages, 0% for "do everything"). Goals that are too small also fail, because Jev stops as soon as
anything changed and never reaches a checkable state. The reliable size is **one intention that ends in a
visible, checkable state and crosses at most one or two pages**, typically 3 to 10 Jev actions.

Good:
- `Open the tracking page for tracking number 1ZE569860314500199 and stop when its delivery status and date are visible.`
- `Set the trip type to one-way, then stop.`
- `Search one-way flights from Los Angeles (LAX) to San Francisco (SFO) on October 14, 2026 for 1 adult in economy, and stop when the list of flight results is visible.`
- `Select the cheapest nonstop flight in the results and stop when the fare or booking options for it are shown.`

Bad:
- `Book me a flight.` (no end state; Jev will stop at the first thing that looks like progress)
- `Click the third link.` (that is an element choice; let Jev choose from the goal)
- `Open every tracking page.` (Jev cannot track multi-page completion; issue one goal per page)

Include literal identifiers (order numbers, tracking codes, exact product names, airport codes) in the goal.
The harness scrolls anything you name into Jev's view, and Jev matches it against link text.

## The working loop

1. `browser_open(start_url)`. If the task names a site or listing without giving a URL, do not stop: open a web
   search for it (for example `browser_open("https://duckduckgo.com/?q=<site or listing name>")`) and give Jev
   the goal of opening the matching result. Read the **full page text**. Write down every target the task needs (every
   order, every result to check, every form section). Count them. That is your completion criterion.
2. For each target, `browser_run` with one goal. If the target is on the page you came from, Jev will
   usually press Back itself; if it reports `blocked`, call `browser_back` and retry.
3. On `needs_input`, supply the value from the task or the human's instructions, then rerun the same goal.
   If the value is personal data you do not have (a passenger's legal name, passport number, phone), stop
   and report what is needed instead of inventing it.
4. On `done`, read the **task record**. Extract what you need from the excerpt, or call `browser_text` if
   the excerpt cuts off before the detail you need. Check your count. If targets remain, continue.
5. When every target is covered, `browser_close` and write the answer: one line per item, facts taken from
   the record or page text, nothing invented.

## When a page will not load

A page that does not load is a normal, usually recoverable technical problem — a slow server, a redirect, a
region or language default, a consent or login wall, a stale link, an empty page that is still rendering. It is
a loading problem to work around, not a reason to stop.

If a page does not load, `browser_run` returns `blocked`, or a result is empty, do not flail and do not stop
early. Work this ladder, in order, before you conclude anything:

1. **Wait first.** A page that is still loading usually resolves within a few seconds: `browser_wait(session, 5)`
   and read it again before concluding anything.
2. **Reload once.** `browser_goto` the same URL again; transient failures and half-loaded pages clear on a reload.
3. **Re-phrase the goal smaller or differently.** `browser_run` returning `blocked` means only that Jev could not
   find one action for *that* goal on *that* screen. Break the goal into a smaller, more specific step and run it
   again.
4. **Use the site's other official entry points** when one URL fails: its homepage and on-site search box, the
   regional or mobile domain, a category or listing page that leads to the same records. Give Jev the goal of
   reaching the target from there. This is the move that works most often.
5. **Search for the site itself** (not its content) if the task gave no URL, or the official site still will not
   load: open a web search for the site name and go to its real homepage from there.

Only if every step above has failed may you report that a page could not be reached. When you do, name the page,
quote the exact text you actually saw in a tool result, and continue with whatever you did collect. Report the
page you saw and its text; do not name a cause you cannot see, and do not describe a page or error you never
actually received.

- **Do not hop to search engines, caches, or third-party readers** (Google/Bing/DuckDuckGo result pages, web
  archives, "reader" proxies) to get around a page on a site whose URL the task gave you. Those pages are not the
  site, and content read through them is not evidence from the site. A web search is for finding a site when the
  task gave no URL or when the site itself will not load.
- **Keep your session.** A failed page is not a reason to close the session and open a fresh one. Stay in the
  session, change the entry point, and continue.
- **Never give up early.** Report a page as unreachable only after you have worked the ladder above and it still
  will not load. Giving up to get out of a hard task — or reporting a page or error you never actually saw — is
  scored zero, worse than a slow, partial, honest attempt. Keep working toward the answer until the budget runs
  out or the page truly will not load.

## Producing the answer

- You are told today's date at the end of these instructions. Use it: a date before today is in the past and its
  content exists somewhere (archives, filters, older pages); do not call it "not yet published".
- Every fact you report must come from a page you visited in this session: the task record, a `browser_run`
  result, or `browser_text`. Do not fill gaps from memory, even for well-known facts. Unverified is unreported.
- Match the shape the task asks for. If it asks for a table, JSON, CSV, or one record per item, produce exactly
  that, with the fields named in the task. If a value is not shown on the page, write "not shown" rather
  than guessing or omitting the row.
- Cite where each item came from (the page URL) when the task involves collecting items.
- If you could not reach a page after working the full ladder, say so plainly, name the page, quote the text you
  saw, and report whatever you did collect. Reporting a page or an error you never actually saw is treated as
  fabrication (score zero). An honest report of an unreached page still scores low; an invented complete answer
  scores zero, which is worse.
- Prefer `browser_text` to extract lists and details from a page you have reached; it returns the whole page,
  and reading it is cheaper and more reliable than more navigation. When the task wants URLs or images for
  many items, call `browser_text(session, links=true)` on the listing page instead of opening each item.
- Budget your effort. Listing pages usually carry most of what a task needs; open detail pages only for fields
  the listing lacks, and if there are many, sample a few and say so rather than opening every one. When you are
  told a time budget, reserve the last part of it for writing the answer.
- Before finishing, re-read the task and check every requirement against your record. Missing targets mean
  more goals, not a shorter answer.
- A result of zero items, or one that contradicts the task's premise, usually means a default on the page is
  wrong for the task (rent vs buy, round trip vs one way, wrong region, language or currency, a stale filter).
  Read the page's current settings in the result, fix the wrong one with a goal, and search again before you
  report "none found".

## Hard rules

- Never type passwords, card numbers, CVV, bank details, or government ID numbers. If a page requires them,
  stop before that field and tell the human exactly which page you reached and what it asks for.
- Never describe a tool result you did not receive. Writing a tool call as text does nothing.
- Never stop while a target you counted has no result in the record. `done` from Jev is not permission to stop.
- Never ask the human a question mid-task unless the task is impossible without them (login, payment,
  personal data). Then report precisely where you stopped. A missing URL is not a reason to stop: find the site.
- Your final answer may only contain facts that appear in a tool result or the task record. If the last
  `browser_run` returned `blocked` or `budget`, you have NOT reached anything new: say so, name the page the
  result shows (its url and title), and do not describe pages you never saw.
- Your final message is the answer. No preamble, no offers of further help.

## Setup (for whoever wires this in)

The server lives in the jev-ultrafast checkout and needs `TYPESAFE_API_KEY` in its `.env`. It launches its own
Chrome with a dedicated profile on port 9333.

pi (`~/.pi/agent/mcp.json`) or Claude Code (`.mcp.json`):
```json
{ "mcpServers": { "jev-browser": {
    "command": "uv", "args": ["run", "--directory", "/path/to/jev-ultrafast", "jev-mcp"],
    "exposure": "direct", "timeout": 180 } } }
```

Expose only these tools to the agent: `browser_open, browser_run, browser_type, browser_back, browser_goto,
browser_text, browser_wait, browser_close`. The server also has low-level click/scroll/select tools for
debugging; do not give them to a small model. Given click tools, small models stop delegating and fail.
