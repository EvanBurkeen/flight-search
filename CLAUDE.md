# Working on this repo

You are maintaining **Flight Assistant** (production: flights.evanburkeen.com), a
conversational flight concierge. This file loads automatically; the README does
not. **Read [README.md](README.md) before your first change** — it is the living
record: architecture, the Changelog, and the traps listed under "Lessons the hard
way". Do not rediscover what is already written there.

## Hard rules

1. **Every push that touches `api/`, `public/`, `scripts/` or `vercel.json` ships
   a Changelog entry in README.md, in the same commit.** A pre-push hook blocks
   you otherwise. Say what changed and the bug or request behind it.
2. **`.venv/bin/python scripts/check.py` must pass before you push.** The hook
   runs it. Every check encodes a bug a human already found in production — if
   one fails, you have reintroduced it. Read the check's docstring and the
   Changelog entry it names before touching the test.
3. **One-time setup in a fresh clone:** `git config core.hooksPath .githooks`
   (check.py fails until you do; hooks are not cloned).
4. **Always `git -C <repo path>`** — a stray repo at `$HOME` bit this project once.
5. **Verify in production after pushing** (~60-90s to deploy), do not assume:
   ```
   curl -s -X POST https://flights.evanburkeen.com/api/search \
     -H 'Content-Type: application/json' \
     -d '{"query":"JFK to ORD one way sept 18 cheapest"}'
   ```
   Assert non-empty `sections[].results`. `"debug_timings": true` adds per-phase
   latency.
6. **Add a check to `scripts/check.py` whenever you fix a real bug.** That is how
   this suite grew, and it is the only thing that makes a fix permanent.

## Ground rules for the product

- Owner: **Evan Burkeen**. Goal: *"the flight tool I use instead of Google
  Flights/Kayak."* Assistant voice: concierge, **no em or en dashes**.
- **Never claim more than the data supports.** Past bugs were all of this shape:
  telling the user a route had no other nonstops (we had truncated them),
  promising "one ticket" when Google sells separate tickets, quoting a price for
  an itinerary we were not displaying. When results are a sample, say so.
- **Rank by value, not by fare.** Flying is tiring; a long layover to save a few
  dollars is rarely the favour it looks like. Weights live at the top of
  `api/index.py` and are a product judgement — tune them there, deliberately.
- **Never hide a genuinely better option.** The client-side filters only filter
  what the server shipped, so the shipped set must represent the option space.

## Verifying UI work

The dev server is `.venv/bin/python scripts/dev_server.py` (port 8123); with no
`ANTHROPIC_API_KEY` the Claude loop is stubbed but searches hit live Google.

Two traps that have produced false confidence in this repo:
- **The browser can serve a stale `index.html`.** Load `?v=something` and assert
  a string from your edit is present before believing a screenshot.
- **An occluded preview pane reports `viewportH: 0`** and returns junk from
  `getComputedStyle`, and throttles timers. Cross-check with a screenshot,
  `curl` of the deployed CSS, or the CSSOM before concluding anything.
- **Alpine:** mutate the reactive proxy (`this.messages[this.messages.length-1]`),
  never the object you pushed, or the DOM silently stops updating.
