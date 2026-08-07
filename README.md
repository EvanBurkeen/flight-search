# Flight Assistant

Conversational flight concierge at [flights.evanburkeen.com](https://flights.evanburkeen.com) —
talk to Claude, which pulls live Google Flights data to search, compare, and recommend.

> This README is the living record of the project: how it works, how to run it,
> what has shipped, and the operational lessons learned. Update the Changelog
> with every push.

## For AI assistants picking this up

You (an AI coding assistant) are expected to maintain this project from this
document alone. Start with [CLAUDE.md](CLAUDE.md) (it loads automatically), then
this file, then **"Lessons the hard way"** below before debugging anything.

- **NON-NEGOTIABLE RULE: every deployment gets a Changelog entry in this README,
  in the same commit.** One or two lines: what changed and why, newest day first.
  Now **enforced by `.githooks/pre-push`** rather than trusted: the rule was
  broken on July 24, 2026 while written in bold right here, which is the whole
  argument for mechanism over instruction.
- **`.venv/bin/python scripts/check.py` before every push** (the hook runs it):
  offline checks, each encoding a bug already found in production. A failure
  means you reintroduced one — read its docstring, do not route around it.
  **When you fix a real bug, add a check.** A weak check is worse than none: an
  earlier version passed while `HOUR_VALUE` was zeroed, and that reached prod.
- **Fresh clone, one-time:** `git config core.hooksPath .githooks` (hooks are not
  cloned, and `check.py` fails until you set it).
- **Fix the CLASS, not the instance.** Every durable improvement here came
  from asking "what is the general shape of this bug?" — one missing
  airport became "no cut may erase an airport Google served", one wrong
  date became "state the day offset so the model never computes it", one
  false "no service" became "prove absence with a dedicated search".
  A fix that only handles the reported example will be re-reported.
- **Never `git reset --hard` with uncommitted work** and never `git add -A` for a
  throwaway commit; both cost real work here on July 24, 2026. Commit or stash
  the good work first, and stage probe files explicitly.

- **Owner:** Evan Burkeen. Product goal: *"the flight tool I use instead of
  Google Flights/Kayak."* Voice of the in-app assistant: concierge, no em dashes.
- **Repo:** this directory (`~/Downloads/flight-search-web`), GitHub
  `EvanBurkeen/flight-search`, production `flights.evanburkeen.com`. Pushing
  `main` deploys automatically (~60-90s). Always run git with an explicit
  `git -C <path>` (there was once a stray repo at `$HOME`; neutralized to
  `~/.git-old-flight-projects-backup`, but stay explicit).
- **Workflow that has worked:** reproduce/diagnose locally first
  (`scripts/dev_server.py`, stub grammar below) → fix → verify in local preview →
  commit with a descriptive message → push → wait ~80s → verify in production
  with `curl -X POST https://flights.evanburkeen.com/api/search -H 'Content-Type:
  application/json' -d '{"query": "JFK to ORD one way sept 18 cheapest"}'` and
  assert non-empty `sections[].results`. Update the Changelog in the same push.
- **Debugging empty results:** run the identical search locally vs production.
  Local works + prod empty = IP throttling (see Operations). Both empty = real
  data gap (small airports, or genuinely no service).
- **Env (Vercel only, never in git):** `ANTHROPIC_API_KEY`, `FLI_PROXY`
  (IPRoyal rotating US-residential proxy; the fix for Google throttling).
- **Costs Evan cares about:** Anthropic per-turn (~1-2 cents on Sonnet 5;
  `debug_timings` reports the real figure per turn as `est_cost_usd`, computed
  from each call's usage block at list prices — Sonnet's intro pricing
  through Aug 31, 2026 bills ~1/3 lower). The July 30 outage was this
  account running out of credit mid-conversation: top up at
  console.anthropic.com, and consider auto-reload with a monthly cap so it
  degrades predictably. IPRoyal bandwidth (~$6/GB, ~2GB purchased July 2026,
  months of runway at current usage).
- **Streaming is LIVE on main since July 25, 2026** (Evan's spec: the
  recommendation types out first, THEN the cards land). The frontend consumes
  `/api/search/stream` with a hard auto-fallback to `/api/search` on any
  transport problem, so streaming can only ever add. The old
  `streaming-experiment` branch (progressive cards, different reveal order) is
  history only — do not restore its handler. Gotchas already paid for: mutate
  Alpine's reactive proxy, never the pushed object; and the typewriter MUST
  NOT depend on requestAnimationFrame alone (browsers starve rAF in hidden
  tabs — a reply would never finish typing or finalize; the watchdog timer in
  `startTyping` is load-bearing).
- **Roadmap shelf (discussed, not built):** price watches (cron + email),
  login + saved trips, real booking via Duffel. (Search-result caching shipped
  July 24; the calendar heatmap and streaming replies shipped July 25; the
  cross-turn results ledger — the useful slice of "trip memory" without login
  — and price context shipped July 28.)
- **KNOWN OPEN BUGS (as of Aug 6, 2026) — pick these up before new features:**
  1. **Multi-city quotes a price it should not.** Production returned 8
     combos with "$5,409 one ticket" as best value while the per-leg
     searches in the SAME turn found $789 + $701 = $1,490 separate.
     `leg_price_index` only matches by exact flight signature, so
     `all_matched` is usually false and the separate-vs-combined comparison
     silently never fires. The per-leg futures (`leg_futs`) already run in
     parallel — use them as the separate-ticket floor, and never present a
     combined price above a purchasable separate one. This is the
     "quote what you can actually buy" invariant, currently violated.
  2. **Passenger count never reaches the Book link.** Cards say "per
     person" and the SEARCH honors `adults`/`children`, but `itinerary_url`
     defaults `adults=1` at all four call sites. A family-of-four search
     books one seat. Also settle whether `result.price` is per-person or
     party-total and label it.
  3. **Google withholds some fares entirely.** The $111 Breeze HVN-FLL fare
     never appears, even from a dedicated single-airport search. Disclosed
     by the manifest layer rather than hidden; probably not fixable
     client-side, but worth re-testing occasionally.
- **Known trade-offs accepted by Evan:** timeline layover dots use naive local
  times (schematic, not exact); Claude sees only top-6 summaries per search
  (with truncation warning baked in); the value-ranking weights
  (`HOUR_VALUE` etc. at the top of `api/index.py`) are a product judgement, not
  a tuned model — change them there; multi-city is the slow path (leg-2 fan-out
  is 4–6 candidates), so it is the trip type most likely to exhaust the 58s
  search budget; the guaranteed wrap-up call still narrates whatever landed.

## Lessons the hard way

Non-obvious things this project already paid for. **Read the relevant part before
debugging in its area** — each cost a real investigation, and several are
counterintuitive enough that a fresh assistant will otherwise repeat the bug.
`scripts/check.py` guards the testable ones.

**Debugging discipline (read this first — it is what actually costs time here)**
- **ALWAYS RUN A CONTROL.** Aug 2: multi-city searches were refused from
  this laptop, and the conclusion was "Google gated the multi-city
  endpoint" — published, committed, and wrong. The machine's IP had been
  soft-blocked, and a blocked address refuses EVERY trip type with the same
  ~95-byte body. A bisection with no control cannot tell "this endpoint is
  broken" from "this address is blocked". Before concluding anything from a
  failed request, run a request you KNOW should work (a plain one-way) in
  the same process, minutes apart. If the control fails, stop: you are
  debugging your own network, not the product.
- **Production is the only clean testbed.** It has the residential proxy;
  this laptop does not. When local evidence is ambiguous, instrument the
  code to report what it did (`combined_probe`, `price_context_status`,
  `est_cost_usd` all exist for exactly this reason), push, and read the
  answer from prod instead of inferring it locally.
- **The nearest plausible story is usually not the true one.** Aug 6: HVN
  flights were missing and the first diagnosis was "the value cut crowded
  them out" — impossible, since the HVN fare was the CHEAPEST in the set
  and the cut explicitly protects the cheapest and the only nonstop. The
  real cause was a thin session returning zero HVN rows. When Evan pushes
  back on a diagnosis, he is usually right; re-derive it from the data
  rather than defending it.
- **A wrong fix is worse than no fix.** That same wrong diagnosis cut the
  multi-city retry ladder to a single attempt, disabling the
  identity-rotation that beats the most common failure mode here. Check
  what a "fix" REMOVES, not just what it adds.

**Google's data layer**
- **Results arrive as PROGRESSIVE chunks inside one HTTP response.** `GetShoppingResults`
  returns several `wrb.fr` chunks (this is why the real UI "fills in").
  `fli` parses only the first; we patch it to take the UNION of every
  chunk's rows, deduped by itinerary (Aug 2: picking the single richest
  chunk was only right when chunks are cumulative re-renders — per-slice or
  delta chunks could still lose whole carriers). Symptom if this regresses:
  whole carriers missing on thin routes.
- **Google withholds some carriers' FARES from automated sessions while
  serving their schedules.** Beijing->Chengdu: our session got China
  Eastern's 25 flights with price null while Evan's browser showed them at
  $314 — and every layer counted only priced rows, so the reply called the
  $421 priced floor "the going rate." Unpriced inventory is inventory: rank
  it last, ship it, disclose it (carrier_note + unpriced_carriers), and the
  Book deep link prices it in the user's own browser. POS/language changes
  (gl=CN, zh-CN) do NOT unlock the fares; a fresh identity does not either.
- **The date grid can be degraded in the same way as the list** (one row
  where 43 belong), so the completeness sentinel (list cheapest vs the
  grid's price for the searched date, retry as a new identity, disclose if
  still short) catches session flakiness and parse loss, but NOT a
  uniformly thin session — the unpriced-carrier disclosure covers that.
- **Refusals are SESSION-STICKY soft blocks, not random flakiness.** HTTP 200 with a
  ~94-byte body (gRPC code 13). A flagged session failed 64/64 consecutive requests
  while brand-new sessions in the same seconds passed 20/32. Retrying on the same
  cookie jar cannot escape it: retire the identity.
- **Retrying harder makes it worse.** Sustained volume escalates a cheap session
  flag into an IP-level block. That is what the circuit breaker is for; do not
  "fix" refusals by adding attempts.
- **Multi-leg prices are cumulative, not per-leg.** In an expansion the first leg
  carries the best achievable total ("from") and the later leg carries that
  pairing's total. And Google's *joint* fare can sit far above buying the legs
  separately ($2,207 vs $1,026 for identical flights) — separate tickets are what
  its own booking page sells when the carriers do not interline.
- `SortBy.BEST` intermittently returns None (ladder falls back to CHEAPEST), and
  transient tiny-error bodies arrive in bursts (~5-10%).
- **Request timeouts are TOTAL time, not idle time**, and heavy queries STREAM
  their chunks slowly — a tight timeout kills a nearly-complete download and
  the retry re-pays the whole cost. Fail fast once (new identity), then be
  patient. Also: fli's client wraps every POST in tenacity retries ON THE SAME
  SESSION; the app bypasses them (`__wrapped__`) because the identity-rotating
  ladder is the correct retry layer.

**Booking deep links (`tfs`)**
- The `tfs` param is base64url protobuf naming the exact itinerary. **`f19` is the
  trip type (1 round, 2 one-way, 3 multi-city); `f2` is a constant 2.** Putting the
  trip type in `f2` makes Google reject the URL and fall back to its home page.
- Repeated `f3` = journey segments; a segment's repeated `f4` = the connecting
  flights within it. Endpoints take `{1:1, 2:"IATA"}` or `{1:3, 2:"/m/..."}` for a
  city entity; we always emit airports. The `tfu` token is session-scoped and
  **not required**.

**Model behavior**
- **Never make the model do calendar math.** Asked for "the latest departure
  landing Jan 3 morning", it named a flight whose arrival timestamp plainly
  read Jan 4 — it inferred "next day" from the departure instead of reading
  the date. Every flight summary the model sees (live and ledger) now states
  `lands_plus_days`, mirroring the +N badge on the card, and the prompt says
  to read arrival dates, not derive them. The same principle produced the
  timezone rules in the arrival-day guidance: state facts the model would
  otherwise have to compute.

**Ranking and truthfulness**
- **A multi-airport search is one combined pool, and the hub neighbor wins
  every seat.** "NYC to Gainesville" searched [GNV, MCO, JAX]; Google
  returned 15 GNV itineraries (American via MIA, from $397) and the
  pipeline erased every one — the expansion picker chose only MCO
  (cheapest/nonstop/fastest all live at the hub), the value-ordered cut
  dropped the pricier GNV one-stops, and the model told the user GNV "came
  back with nothing through-ticketed." JAX vanished the same way in the
  same search. Ranking by value is right for ORDER; it must never decide
  which airports exist. Every cut now guarantees each served airport a
  seat, and the message carries a per-airport breakdown over the full
  pre-cut set.
- **Rank BEFORE truncating.** Google returns results cheapest-first, so cutting
  first silently discards options that were never scored (12 nonstops existed; 11
  priced above the 50th-cheapest fare and vanished).
- **Client-side filters only filter what the server shipped**, so the shipped set
  must represent the option space or "Nonstop only" lies.
- **The model only sees `compact_for_model`'s top 6**, so a ranking bug is an
  *advice* bug: Claude confidently described nonstops it had never been shown.
- **A displayed price must belong to the itinerary displayed beside it.** Both the
  round-trip picker and multi-city broke this in different ways.

**Frontend**
- **Alpine:** mutate the reactive proxy (`this.messages[this.messages.length-1]`),
  never the raw object you pushed, or the DOM freezes after the first render while
  the data updates invisibly.
- **The browser serves a stale `index.html`** more often than you expect. Assert a
  string from your edit is in the loaded page before trusting a screenshot.
- **An occluded preview pane reports `viewportH: 0`**, returns junk from
  `getComputedStyle`, throttles `setTimeout` to ~1/s and pauses smooth scrolling.
  Several "bugs" were only that. Cross-check with a screenshot or `curl`.
- **Browsers starve `requestAnimationFrame` in hidden/occluded tabs.** Any
  animation loop that gates COMPLETION (the streaming typewriter) needs a
  timer watchdog or it never finishes for a user who switched tabs. The
  watchdog in `startTyping` is load-bearing.

## Invariants (`scripts/check.py` enforces these)

- One-way `tfs` links stay byte-identical to a real Google-issued URL.
- Time is genuinely priced: a cheaper flight that is hours longer must lose.
- An explicit "cheapest"/"fastest" request is obeyed verbatim, and "Best value"
  only appears when we actually ranked by value.
- The outright cheapest stays inside the preview; the shipped cut always keeps the
  cheapest, the fastest, and up to 8 nonstops, in value order.
- Cabin/dates/stops split the search-cache key; only cosmetic fields do not.
- Multi-city quotes the cheaper of one-ticket and separate-ticket, and says which.
- `itinerary_url()` returns None rather than a malformed link; callers fall back.
- A round-trip date grid prices a real stay (7 nights assumed and disclosed when
  the model gives none), never Google's same-day-return default.
- Round-trip expansion picks a REPRESENTATIVE outbound set (nonstops, fastest,
  cheapest anchors, departure spread), and the model is told about outbounds
  whose returns were not priced — it must never infer "no nonstops" from the
  expansion sample.
- A round-trip search that got the outbound list but no pairings ships every
  outbound from-priced with `spec_echo` (tap-to-price), never "No flights
  found" — and the model is told not to claim unavailability.
- The cross-turn ledger records what was SHIPPED, never what exists: an empty
  search leaves no record, a round trip carries its unpriced-outbound counts
  forward, and the carried block forbids inferring anything absent from it.
- Price context characterizes a fare that is actually on screen (the cheapest
  one shipped), never claims to be price history, refuses to speak from a
  sample under 8 dates, and is dropped rather than waited on.
- Joining the model's soft wraps never leaves a space before punctuation or a
  double space (the July 28 "on Nov 28 , with" report).
- No cut may erase an airport Google served: the expansion picker, the
  round-trip more_outbounds cap, the degraded-mode cap, and the one-way ship
  cut each keep at least one option per destination airport, and search
  messages state per-airport counts and cheapest fares over the full
  pre-cut set whenever more than one destination airport is in play.

## Stack

| Layer | What |
|---|---|
| Hosting | Vercel serverless (Python), auto-deploys on push to `main` (`EvanBurkeen/flight-search`) |
| Backend | [api/index.py](api/index.py) — FastAPI. `/api/search/stream` (SSE) is what the frontend uses (prose types first, cards land on `done`), with `/api/search` (JSON) as its automatic fallback and the prod-verification probe; `/api/returns` prices every return Google pairs with ONE outbound (no model in the loop — the board's tap-to-price, ~1-4s) |
| LLM | `claude-sonnet-5` agent loop (near-Opus on tool-driven work, 40% less per token; `ASSISTANT_MODEL` env var overrides it — set `claude-opus-4-8` in Vercel for an A/B, no code push), `max_retries=4`, effort `medium`, adaptive thinking on by default (hence `max_tokens` 8000: it caps thinking + text together); prompt caching: system+tools at 1h TTL (the only prefix that survives across turns), plus a 5m breakpoint on the conversation tail so a turn's later calls read its earlier messages at 0.1x — WITHIN-turn only, since the client replays history as bare prose and the wrap-up's `tool_choice: none` invalidates the messages tier (so that call sends no tail marker); plainly-route-shaped queries emit their first tool call via `claude-haiku-4-5` (no `output_config` — Haiku rejects effort), with an automatic loop-model redo if Haiku answers in prose instead of calling the tool |
| Flight data | [`fli`](https://github.com/punitarani/fli) (PyPI `flights`) — reverse-engineered Google Flights |
| Web context | Anthropic server-side `web_search` tool (max 3/turn) for event dates, venues, etc. |
| Coordinates | `airportsdata` (IATA → lat/lon) for route maps |
| Frontend | Single static [public/index.html](public/index.html), Alpine.js, no build step |
| Map data | [public/world.js](public/world.js): Natural Earth 110m land + lakes as SVG paths |

`ANTHROPIC_API_KEY` lives only in Vercel env. Optional `FLI_PROXY` env routes all
Google traffic through a proxy (see Operations).

## How a turn works

1. Frontend POSTs `{query, history, ledgers}` (history = last 12 user/assistant
   text turns; `ledgers` = the **results ledger** from the last 2 turns that
   shipped cards, echoed straight back). History is prose only, so without the
   ledger every structured result vanished at the end of a turn and a follow-up
   ("the second one", "book the JetBlue") forced a re-search or an invented
   fare. `ledger_entry` records what was SHIPPED (numbered as the user saw it:
   fare, route, times, stops, flight numbers, plus the unpriced-outbound counts
   for round trips) and `ledger_context` carries it into the next turn as a
   bracketed block whose rule is that anything absent from the record has not
   been seen and must be searched for. Capped at 3 searches x 6 options x 2
   turns, and re-clamped server-side.
2. `run_assistant` runs an agent loop. Latency doctrine (Aug 2): everything
   that can overlap, does — an unmistakable "JFK to ORD sept 18" query
   starts its search BEFORE the router runs (speculation; the single-flight
   cache collapses the router's identical spec onto it; `SPECULATE=0`
   kills it), turn start opens TLS to Google with ~1KB `generate_204`
   requests on the exact threads that will carry the real POSTs
   (`WARM_CONNECTIONS=0` kills it; this is NOT the deleted 1.8MB page-load
   warmup — that was about cookies, this is only the tunnel), each search
   dispatches the moment its tool_use block finishes STREAMING rather than
   at message end (on comparisons, search 1 runs while calls 2-4 are still
   being written), the Haiku router reads a request-rules-only prompt
   (~58% of the full one), and searchy follow-ups ("how about jfk to bos
   friday") reach the fast router instead of full effort (≤3 search rounds, ≤8 API calls, **58s
   budget for NEW search rounds** — when the budget dies with data on screen,
   a guaranteed tool-less wrap-up call still writes a real reply from a
   per-search status log, stating exactly what is or is not missing):
   Claude converses and calls `search_flights` (up to 5/turn, executed **concurrently**)
   and/or `web_search`. `pause_turn` (server-side search) is resumed automatically.
3. `execute_spec` per search: roll past dates forward (with a visible note) →
   resolve airports (multi-airport cities supported) → build fli filters
   (cabin, stops, airlines, alliances, price cap, times, currency pinned USD) →
   `run_search` (4-attempt ladder: sort, sort, CHEAPEST, CHEAPEST; adaptive
   per-attempt timeouts — 12s fail-fast first, 28s on retries so Google's
   slow progressive streams can finish, 12s/22s for expansions;
   **whole ladder bounded by `budget_s`**, 35s default / 45s
   multi-city, because a retry re-runs the entire search including any
   fan-out; each failed attempt RETIRES the identity — cookies + exit IP +
   fingerprint — because refusals are session-sticky; a process-wide circuit
   breaker imposes quiet after repeated refusals, capped at 1.5s on a user's
   first attempt. No warmup page-load: the study found warmed sessions did
   not outperform fresh ones) →
   post-process:
   - `via_airports` filter over the FULL result set (the only trustworthy way to
     assert a routing exists/doesn't)
   - arrival-day + arrival-time enforcement app-side (`arrival_ok`) — Google's own
     arrival filter is clock-hour based and unusable
   - **rank by value, then cut** (`order_by_value` / `retain_representative`):
     fare plus a dollar cost for duration over the fastest, stops, and existing
     warnings. Cutting first would discard options never scored — see Changelog
     July 24. The cut always keeps up to 8 nonstops plus the outright cheapest
     and fastest, because the client-side filters run over what we ship.
   - serialize up to 50 one-ways / ~10 round-trip OUTBOUND groups (a
     REPRESENTATIVE set — cheapest anchors, nonstops, fastest, departure-time
     spread via `representative_outbounds`, because fli expands `flights[:top_n]`
     in sort order and "the 10 cheapest" once hid every nonstop; each group
     carries its own `return_options`, every option priced as the real total
     for that pairing, PLUS up to 24 `more_outbounds` — every outbound Google
     listed but we did not expand, shipped with its honest "from" total) /
     8 multi-city itineraries (priced BOTH as one ticket and as
     separate tickets, quoting the cheaper — `price_basis` says which), each
     with a `tfs` deep link to that exact itinerary, alliance tag, aircraft,
     warnings (tight <45m connections, overnight, self-transfer, airport
     change, mixed-carrier separate-ticket risk), CO2 delta, and
     `route_points` (with per-stop `layover_min`) for the map.
4. Claude sees a **compact top-6 summary per search** (with route endpoints and an
   explicit truncation warning); the browser gets everything. Fixed-date searches
   also carry **price context**: a `SearchDates` grid over 21 days either side of
   the departure, fetched BESIDE the search (never in front of it, 3s wait cap,
   dropped if late), cached 6 hours, giving the shipped fare's place among nearby
   dates plus the cheapest nearby date. It is a comparison across DATES, not
   price history, and both the model's note and the on-card line say so.
   It stands down entirely while the circuit breaker is open. It is started and
   attached in `cached_execute_spec`, deliberately OUTSIDE the payload cache: a
   grid that misses its window keeps running, warms its own cache, and annotates
   the next serve, where attaching inside `execute_spec` froze "no context" into
   the cached payload for its whole TTL (see Changelog July 28). Every payload
   carries a one-word `price_context_status` (`landed`/`cached`/`late`/`thin`/
   `empty`/`off`) so the next diagnosis costs no probe cycle.
5. Reply text ends with a `SUGGESTIONS: [...]` line → stripped and rendered as
   tappable follow-up chips.
6. Trip types: one-way, round-trip (choose an outbound, then a return, with the
   total moving as you pick — Google's own flow), multi-city (2–5 legs; Google
   prices them together but mixed carriers are often SEPARATE tickets, so both
   prices are computed and the cheaper is quoted), flexible-date grids
   (`SearchDates`, round-trip duration supported).

### Assistant behavior rules (prompt-enforced)

- Concierge voice; no em/en dashes; `**bold**` only.
- Ground every fare/time claim in tool results; quote airports exactly from the
  `route` field (EWR is not JFK).
- Ask ONE question only when origin is missing or destination is a whole region;
  otherwise assume and state assumptions.
- Arrival-day intent → `arrival_date` (enforced on actual arrival timestamps) +
  timezone heuristics; for wide-open routings it searches both same-day and
  day-before departures so Middle-East/late-night itineraries aren't missed.
- Small regional airports with no service → widen to nearby majors, say the drive.
- Never claim a route doesn't exist without a `via_airports`-filtered search.

## Frontend features

Chat with stop/supersede (send during a search cancels and re-asks) · Detailed/Compact
views (global toggle + per-section override) · top-picks preview with "Show all N"
and instant client-side filters (sort, departure window, duration, airline, alliance,
stops — options derived from the data; they filter only what the server
shipped, which is why the cut keeps nonstops/cheapest/fastest) · **price
context line** above a fixed-date section (where its cheapest fare sits among
nearby dates, the cheapest nearby date, and an explicit "not price history"
footnote) · round-trip
**mix & match board** (the DEFAULT round-trip view since Aug 2: every
outbound on the left — tapping a from-priced row prices its returns IN
PLACE via `/api/returns`, ~1-4s, no model turn, with an ask-the-concierge
fallback — the selected outbound's returns on the right, each priced as the
real pairing total and filterable/sortable, "Load every return" for the
full Google list, sticky chosen-pairing bar with Book; "Show top picks
only" collapses to the pre-paired cards) · Best value badge ·
Book deep-links straight to the chosen itinerary on Google · flexible-date grids show best-value dates
first (within 15% of cheapest) and expand to month calendars heatmapped by price
(tiers relative to the window's cheapest; cheapest days outlined; round-trip
pairings in tooltips) · per-flight atlas maps (land+lakes, graticule,
sequential longitude unwrapping so every leg takes the short way; outbound solid,
return dashed; layover dots with durations) · timeline layover rings · suggestion
chips · search ladder (jump-to index of every results section: fixed rail on
wide screens, floating 'Searches' button + overlay elsewhere) · **streaming
replies** (the reply types out once the answer is settled — a preamble that
turns out to precede a search is never shown at all — at an eased, bounded
cadence, then the cards rise in, then the chips; automatic fallback to the
JSON turn on any transport problem) · **"Send this
flight"** (a quiet Share beside every Book renders a boarding-pass PNG on
canvas — trip type, route with flight-arc motif, segment rows with durations,
date-range + price + barcode stub, flights.evanburkeen.com footer — with copy
image / copy text incl. booking link / save / native share) · degraded round
trips render a notice + tap-to-price board instead of a blank section ·
concierge styling (Fraunces serif, brass fittings, boarding-pass dividers,
greeting; August 2 broadsheet + professional passes: heavy masthead rule,
per-section hairlines, tabular numerals on all figures, crisper shadows,
serif reply voice, monochrome airline marks, squared control language).

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt uvicorn
.venv/bin/python scripts/dev_server.py   # API + frontend on :8123
.venv/bin/python scripts/check.py       # invariant checks (the hook runs these)
```

No `ANTHROPIC_API_KEY` locally → the Claude loop is stubbed by a pattern parser
(`scripts/dev_server.py`); searches still hit live Google. Stub grammar: airport
codes, "round", "flex/weekend", "compare", "multi A B C" (common English words
that are also IATA codes — THE, FOR, AND — are ignored). The stub emits the
same SSE events as the real loop, so streaming is fully exercisable locally.

## Operations

- **Google throttles datacenter IPs in waves.** Symptom: empty results/429s in prod
  while the identical search works from a residential IP (diagnose exactly that way).
  Mitigations built in: retry ladders, adaptive 12s/28s request timeouts, 58s
  search budget with a guaranteed wrap-up reply, honest
  "Google is slow" messaging. Stopgap: rotate `regions` in [vercel.json](vercel.json)
  — but pools burn in ~1–3 days of active use (July 2026 burn order:
  iad1 → sfo1 → cle1 → pdx1 → fra1). **Durable fix (ACTIVE since July 15, 2026):** `FLI_PROXY` in
  Vercel env holds an IPRoyal rotating US-residential proxy URL (account:
  evanburkeen@gmail.com; ~2GB non-expiring credit bought July 2026; top up in
  their dashboard when low; searches use ~100-300KB each). If switching
  providers keep the URL format `http://user:pass@host:port`; the sticky
  session suffix is IPRoyal-gated in code.
- **Identity pool (supersedes the old single sticky session).** Each identity
  bundles its own cookie jar, proxy exit (`_session-<random>_lifetime-30m`) and
  browser fingerprint. `checkout_identity()` prefers the most recently
  SUCCESSFUL one so its TLS connection through the proxy is reused — that alone
  took searches from ~6s to 0.4–2.3s, since a cold residential handshake costs
  seconds. An identity holds one curl handle PER THREAD (same exit, same
  fingerprint, cookies cloned from its primary jar): a single shared handle
  silently serialized every parallel code path in fli — see Changelog July 25,
  round trips v2 — so never "simplify" back to one session per identity. Any refusal RETIRES that identity, so the retry is a genuinely new
  visitor. Do not "fix" a refusal by retrying on the same session: refusals are
  session-sticky (a flagged session failed 64/64 while fresh ones passed 20/32
  in the same window — see Changelog July 24).
- Currency is pinned to USD in every search, so non-US regions are safe.
- **Anthropic API errors say what they are** (`assistant_error_reply`):
  429/529 keep the polite congestion copy; a credit-exhaustion 400 is named
  outright (only Evan can fix it — top up in the Anthropic console); other
  4xx admit that retrying won't help and show the HTTP code; 5xx stay
  try-again with the code visible. Full detail (status, error type,
  `request-id` for Anthropic support) is printed to the Vercel function log,
  and the JSON reply carries `error_detail`. Born July 30: an outage was
  answered with "temporary ... try once more" four retries in a row, and the
  diagnosis cost a probe cycle because the status was visible nowhere.
- **Google backend transience:** ~5-10% of search POSTs return HTTP 200 with a
  tiny `travel.frontend.flights.ErrorResponse` body (parses to empty), in
  bursts. Handled by the 4-attempt jittered ladder in `run_search` — do not
  mistake these for IP throttling (throttling = persistent, this = flicker).
- fli quirks: `SortBy.BEST` intermittently returns None (fallback to CHEAPEST);
  multi-city is slow (fan-out capped at `top_n=4`, `maxDuration` 300s).
- `world.js` is generated by `scripts/build_world.py` (Natural Earth 110m land +
  lakes; run it, then bump the `?v=N` cache-buster on the script tag in index.html).

## Changelog

**August 6, 2026 (the README learns what the sessions learned)**
- Evan: "why don't you add that to the readme — the lessons learned and the
  important stuff a new agent should know?" Three additions, each pinned by
  a drift check so they cannot quietly rot:
  - **Debugging discipline**, now the FIRST subsection of Lessons: always
    run a control (the Aug 2 "Google gated multi-city" conclusion was a
    soft-blocked laptop IP, and a blocked address refuses every trip type
    identically); production is the only clean testbed, so instrument and
    read rather than infer locally; the nearest plausible story is usually
    not the true one (Aug 6 "the cut crowded out HVN" — impossible, the HVN
    fare was the cheapest and the cut protects the cheapest); and check
    what a fix REMOVES, since that same wrong diagnosis deleted the
    multi-city retry ladder.
  - **KNOWN OPEN BUGS**, so unfinished work is visible before a new session
    starts something else: the multi-city separate-vs-combined comparison
    that silently never fires, the passenger count that never reaches the
    Book link, and the fares Google withholds outright.
  - **"Fix the CLASS, not the instance"** in the opening section — the
    instruction that turned each reported example into a general invariant.

**August 6, 2026 ("HVN has no service to FLL" — it does)**
- Asked for HVN/BDL into FLL, the reply asserted HVN had no service and
  recommended a $204 BDL flight. HVN has an Avelo NONSTOP at $180, from
  the airport the user named first. My first read was crowd-out; Evan
  pushed back correctly — the HVN fare is the CHEAPEST in the set, and
  both the cheapest and the only-nonstop are explicitly protected by the
  cut, so a dropped row cannot explain it. Re-running the same search
  minutes later DID return the HVN row. The session had simply been given
  a thin slice, and **a thin slice is indistinguishable from absence
  unless you go and look**.
- `probe_missing_airports`: any airport the USER NAMED that returns zero
  rows in a multi-airport search is now searched ALONE (bounded: 2 probes,
  12s) before results ship. Rows found are merged; an airport that is
  still empty ships `VERIFIED NO SERVICE`, and the prompt now permits "no
  service" ONLY on that line — otherwise the model must say it did not get
  options and offer to look again. The server proves absence instead of
  asking the model to remember to check.
- Airport representation is symmetric at last: `rescue_airports` and the
  breakdown ran destination-only, so a single-destination search emitted
  no breakdown at all and the model had zero counter-evidence. Both sides
  now get a guaranteed seat and a reported line ("By origin airport: BDL
  (Windsor Locks, US) 104 from $141, HVN (New Haven, US) 1 from $180").
- Known and disclosed, not fixed: Google's API withholds the $111 Breeze
  HVN-FLL fare even from a dedicated HVN search. The manifest layer names
  such carriers rather than pretending the cheapest priced fare is the
  route's floor.

**August 2, 2026 (multi-city: a wrong diagnosis, corrected)**
- The user's ATL-PEK + HKG-JFK multi-city priced at $1,288 on Google's own
  page while the app returned nothing. FIRST DIAGNOSIS WAS WRONG: header
  capture in a browser suggested the combined endpoint was BotGuard-gated,
  and the probe was cut to a single attempt on that basis. The controlled
  re-test exposed the error — **the local one-way CONTROL was failing too**,
  i.e. that home IP was soft-blocked and returns the same ~95-byte refusal
  for everything. A bisection without a control cannot tell "this endpoint
  is gated" from "this address is blocked". (The BGR token is also
  single-use, so the one "success" was a replayed capture, and the browser
  pane shares the same flagged IP.)
- **Corrected:** the combined probe keeps its full identity-rotating ladder
  (refusals here are session-sticky like everywhere else, so a fresh
  identity is exactly the right retry), and only MEASURED consecutive
  refusals — three — buy a 15-minute quiet period, which any success
  clears. A real gate then costs ~20s three times instead of forever; an
  un-gating recovers on its own with no code change.
- Every multi-city payload now reports `combined_probe` (what the probe
  actually did), so the next diagnosis reads the answer instead of
  inferring it. **It answered immediately:** from production's clean
  residential IPs, with the full ladder across three fresh identities, the
  combined endpoint returned nothing (26.1s) and then timed out (21.6s),
  while one-way and round-trip searches succeeded in the same process.
  That is the controlled comparison the home-IP bisection could not make:
  combined multi-city pricing genuinely does not come back to this client,
  whatever the mechanism.
- That first instrumented run also exposed a worse problem than the missing
  price: the turn took **117s and shipped zero combos**. Sequencing the
  per-leg fallback after the probe meant paying both, and the probe's
  refusals were tripping the process-wide breaker, poisoning the very leg
  searches meant to rescue the turn. Fixed: the per-leg searches now start
  BESIDE the probe (turn costs the slower of the two, not the sum), the
  probe runs with `_local.suppress_breaker` so one measurably-flaky
  endpoint cannot degrade the healthy ones, and its budget is capped at
  12s — still 2-3 fresh-identity attempts, since refusals return in
  seconds.
- Retained from the first pass (independently useful): the per-leg fallback
  searches every leg as its own one-way when the combined price is
  genuinely unavailable, pairs them as separate tickets with the explicit
  warning, and links Google's own multi-city page for the one-ticket price.
  Page-scraping was evaluated and ruled out: the flights HTML carries only
  config blobs (2-7KB), no fares.

**August 2, 2026 (PEI is not Beijing: geography echoes on every search)**
- The model asserted PEI as a Beijing airport; PEI is Pereira, Colombia,
  and its cheap Bogota two-stops out-ranked the real Beijing fares, so the
  cards presented Colombia as China. Codes are assertions from memory and
  memory misfiles them. Every search result now opens with "Route as
  searched: ATL (Atlanta, US) -> PEI (Pereira, CO) / PEK (Beijing, CN)"
  built from airportsdata, the per-airport breakdown names places too, and
  the prompt orders the model to read the geography before writing prose
  and to re-search without any airport that is not where the user asked —
  plus trusted expansions for Beijing/Shanghai/Seoul alongside NYC/London/
  Tokyo. A wrong code now contradicts itself in the model's own context.

**August 2, 2026 (the missing carrier: unpriced inventory is inventory)**
Evan's screenshots: the app showed Beijing->Chengdu as "$421 across the
board, all China Southern" while Google's own UI led with China Eastern at
$314 and a cheapest tab of $242. Diagnosis ran three layers deep, each
with its own generalizable fix:
- **Chunk union** (`merge_wrb_chunks`): the richest-single-chunk patch
  from July 24 is now the union of every chunk's rows deduped by itinerary
  identity, later snapshots winning on price — immune to per-slice and
  delta chunk styles, identical on cumulative ones.
- **Completeness sentinel** (`verify_completeness`): the date grid prices
  the searched date from an independent endpoint; a list whose cheapest
  fare sits far above it is provably short -> one retry as a fresh
  identity, then an honest COMPLETENESS WARNING the model must relay.
- **The actual root cause — fare withholding**: Google served this session
  China Eastern's full schedule with price null (the browser gets $314),
  and every layer counted only priced rows. Now: unpriced rows rank last
  but ship, the cut cannot erase a fare-withheld carrier
  (rescue_airports over the airline key), the message and
  compact_for_model carry "SCHEDULED BUT UNPRICED" per-carrier counts,
  the prompt forbids calling the priced floor "the going rate" and says
  the Book link prices these in the user's browser, and unpriced cards
  explain themselves instead of showing a bare dash. Verified on the
  exact failing query: 30 unpriced flights across 5 carriers now ship
  and disclose. 16 checks added.
- **The manifest layer** (post-deploy: prod's proxied session got SIX rows
  and no unpriced schedules at all, blinding the rows-based disclosure):
  the response's own airline-filter list (`inner[7][1][1]`) is Google
  testifying which carriers serve the route. When coverage against it is
  structurally poor, ONE supplemental search asks for the missing carriers
  by name (alliance majors first), merges whatever returns, and any
  unfillable gap ships a ROUTE NOTE the model must relay instead of
  treating a thin session's slice as the whole market. The chunk union
  also learned to write rows into bucket 3 when bucket 2 is absent
  (Beijing's actual response shape). 8 more checks.

**August 2, 2026 (the sequential turn, overlapped)**
Evan: cut search time as much as possible with zero quality cost. The
profile showed router (1.9-5s) -> search (1.1-3.2s) -> synthesis (3.1-4.9s),
strictly sequential. Five overlaps, none of which change what the model
sees or says:
- **Eager dispatch**: each search_flights call fires the moment its block
  finishes streaming; the message's remaining blocks (and on comparisons,
  the other 3 calls) generate while Google works.
- **Connection warming**: turn start opens the TLS tunnel to Google
  (~1KB generate_204) on fli's persistent executor AND the round's own
  pool threads — the exact handles the real POSTs use moments later. The
  first search after a quiet spell stops paying a multi-second handshake.
- **Speculation**: "CODE to CODE + date" queries start their search before
  the router runs; the router's identical spec collapses onto it via the
  single-flight cache. A wrong guess wastes ~100-300KB of proxy bandwidth
  and nothing else. Strictly gated (checks pin the negatives).
- **Router diet**: Haiku reads the request-handling half of the prompt
  (6.1k of 10.6k chars) — it emits a tool call; the voice rules were pure
  prefill. Searchy follow-ups ("how about X to Y") now reach it too.
- **A deliberation nudge** in the answering rules: direct prose for a
  single clear search, thinking reserved for comparisons and conflicts.
Kill switches: SPECULATE=0, WARM_CONNECTIONS=0.

**August 2, 2026 (assumptions line removed from cards)**
- The small "· Assumed..." justification line under result sections is gone
  (Evan's call): the reply prose already states assumptions where they
  matter, and the calendar keeps its own trip-length note. The payload
  field stays (the model still receives and states them); only the UI
  footnote is removed.

**August 2, 2026 (Evan's five: masthead, board-first, sans voice, honest typing, boxed replies)**
- **Masthead** is now a pure wordmark — the tilted-plane crest and its
  subtitle are gone ("looks vibe coded and cheap"); the credit line lives in
  the footer colophon beside Evan's name ("Powered by Claude and Google
  Flights" — trimmed at Evan's ask).
- **Round trips open ON the mix & match board** (choosing per direction IS
  the product); "Show top picks only" collapses to the pre-paired cards.
- **The reply voice is back to sans** (the serif experiment lasted one
  commit), and every reply now sits in a **paper panel** that answers the
  user's green bubble — both voices are containers, each hugging its own
  text, ending the boxed-question/floating-answer mismatch.
- **No more type-then-wipe**: prose is emitted once per FINAL answer after
  the call resolves, so "Let me take a look" preambles that precede a
  search are never shown (and the SUGGESTIONS line never types out). The
  typewriter cadence is eased and bounded (3-14 chars/frame) so long
  replies read as typing, not pasting. The rAF watchdog stays load-bearing.

**August 2, 2026 (the professional pass: one system, not vibes)**
Second, deeper aesthetic round on Evan's note that it should read
professionally designed rather than vibe coded. What separates the two is
consistency of system, so this pass removes every competing dialect:
- **The concierge speaks in serif.** Replies are set in Fraunces text cut
  (400, optical sizing) like a paper's own columns, key figures bold in the
  accent; the terracotta border-left crutch is gone. Fraunces now loads
  400/600 upright+italic with opsz.
- **Monochrome airline marks**: hash-colored circles became engraved
  ink-bordered squares (`airlineColor` returns transparent; identity comes
  from type, not paint chips).
- **No clip-art**: the route line's ✈ emoji became a drawn arrowhead.
- **One control language**: every 999px pill squared to 2-4px (toggles,
  chips, selects, stop/fab/nudge, compose input and send); active toggles
  are ink-on-paper instead of green fills (green now belongs to data and
  the user's own voice only); "Show all" traded its dashed border for
  small caps on a hairline.
- **Hierarchy**: section titles up to 1.3rem; price-context is a pure
  typographic line, box removed.
Verified in both themes locally against a live search.

**August 2, 2026 (editorial polish: the broadsheet pass)**
Evan asked for an aesthetic once-over in the direction of the world's best
editorial sites (The Economist, minus the red) without losing the concierge
identity. Discipline, not redesign: a 3px masthead rule under a hairline
(cream-on-espresso in dark mode), a hairline rule opening every results
section the way a broadsheet turns to a new story, tabular numerals on
every price/time/date so columns of figures align, crisper card shadows
(cards should only just clear the paper), unified 8px card radii, a larger
serif card price, brass ::selection and :focus-visible rings, and a
sharper user-message bubble. Verified in both themes locally before push.

**August 2, 2026 (the Gainesville bug: a hub neighbor erased the named airport)**
Evan's transcript: "NYC to Gainesville" produced 33 outbounds, every one to
MCO, and the prose "Gainesville itself (GNV) came back with nothing
through-ticketed" — then a dedicated GNV search found American via Miami
instantly. Google had returned 15 GNV itineraries all along; three layers
buried them (expansion picker, more_outbounds value cut, and a model shown
only MCO). Fixes, each checked:
- `rescue_airports`: every cut (one-way ship cut, round-trip more_outbounds,
  degraded mode) keeps at least the best-ranked option per served airport —
  a cap is a product judgment, an airport silently vanishing is a lie.
- `representative_outbounds` seats each destination airport's best outbound
  before spending slots on nonstops and departure spread, so the named
  field gets real priced pairings, not just a from-price.
- `airport_breakdown`: search messages now state "By destination airport:
  MCO 155 from $215, JAX 130 from $274, GNV 15 from $397" over the FULL
  pre-cut set, and the prompt forbids "came back with nothing" claims from
  a combined search (a user-named airport showing zero requires a
  dedicated single-airport search first), tells the model to LEAD with the
  airport the user actually named, and makes constraint-adding follow-ups
  ("it has to be American or Delta") search immediately instead of
  describing the ledger's slice and asking permission. Verified against
  live Google on the exact transcript query before push.

**July 30, 2026 (API spend: Sonnet 5, deeper caching, and a cost line)**
The outage's post-mortem question was "where does the money go" — answered by
measurement (~4.2k-token fixed prefix, 2-3 calls per turn, the conversation
re-billed at full price on every call), then three levers in one push:
- **`claude-sonnet-5` runs the loop** (was `claude-opus-4-8`): near-Opus on
  tool-driven work at $3/$15 vs $5/$25 per MTok, intro $2/$10 through
  Aug 31, 2026. `ASSISTANT_MODEL` in Vercel env flips it back for an A/B
  with no code push. Two Sonnet-specific adjustments: adaptive thinking is
  ON when the `thinking` param is omitted (Opus 4.8 was off), and
  `max_tokens` caps thinking + text together, so it rose 4000 -> 8000
  (costs nothing unless generated). Deliberately NOT set: `thinking:
  disabled` — Sonnet 5 with thinking off is measurably less willing to call
  tools, and this loop is nothing but tool calls.
- **Caching, both halves:** the system+tools breakpoint moved to the 1h TTL
  (2x write, 0.1x reads all session — the 5-minute default re-paid the
  prefix on every think-gap), and the loop's calls put a 5m breakpoint on
  the conversation tail, so call 2 of a turn reads call 1's messages from
  cache instead of re-billing them. Pre-push review corrected two of my
  claims here: cross-turn message reads can never match (the client replays
  bare prose history while the turn's cached bytes embedded the ledger
  block), so the tail is 5m/1.25x rather than a pointless 1h/2x — and the
  wrap-up call's `tool_choice: none` invalidates the messages cache tier,
  so it sends no tail marker at all. The same review caught that adaptive
  thinking's silent gaps could outlive the SSE stream's old 90s single
  timeout, which would have re-run whole turns via the fallback path
  (a double-bill); the stream now heartbeats every 15s. Post-deploy, the
  first live cost lines caught one more: the Haiku router was writing
  ~4.4k premium-rate cache tokens per plain search that nothing could
  ever read (caches are model-scoped; the calls that follow are Sonnet),
  so the router call now sends no markers. Measured: 8.1 cents for the
  hour's first turn (it writes the org-wide prefix), ~1.5-2 cents warm,
  vs 3-5 cents on Opus.
- **Cost instrumentation:** each call's usage block is priced
  (input/cache-write 2x/cache-read 0.1x/output, per-model list prices) and
  `debug_timings` now reports `est_cost_usd` per turn plus a per-call `~$`
  figure in the phase log — the next spend decision gets data, the way the
  July 24 latency work got a profile first.

**July 30, 2026 (the +2 the model read as +1, and the outage the reply called temporary)**
Two of Evan's catches from one session:
- **Arrival-day arithmetic.** Asked for "the latest departure that still
  lands Jan 3 morning in Beijing", the assistant named KE36 off the ledger —
  whose arrival timestamp read 2027-01-04T09:45. It inferred "next day" from
  the departure instead of reading the date. Every flight summary the model
  sees (live results and the cross-turn ledger, where this misread actually
  happened) now carries `lands_plus_days` matching the +N badge on the card,
  and the prompt instructs: read the arrival date, never derive it, name the
  date outright when it differs from departure, and refuse a near miss
  rather than bend it. Pre-push review caught that the +N badge itself was
  missing from the default round-trip cards, both return pickers, and
  multi-city legs (exactly the cards where +1/+2 is routine) and that a
  dateline -1 never rendered; all now show it, so the card and the model
  agree everywhere.
- **"Temporary service error", four retries in a row.** Every Claude call was
  failing deterministically and the reply hid the status, so the user retried
  at a wall and diagnosis needed a probe cycle. `assistant_error_reply` now
  puts the HTTP code in the reply, the full autopsy (status, type,
  request-id) in the Vercel function log, names credit exhaustion outright,
  and stops asking for retries that cannot work (4xx). The root cause of the
  outage itself: see the next entry once confirmed in prod logs.

**July 28, 2026 (price context never appeared in prod: the cache froze it out)**
Post-push verification did its job. The baseline worked on every local probe
and was absent from all three production probes.
- **Cause 1 (the real one):** it was attached inside `execute_spec`, i.e.
  INSIDE the payload cache. The first search dropped a grid still in flight
  (a residential-proxy handshake costs seconds a direct local fetch does not),
  that context-less payload was cached, and every repeat for the next 4
  minutes served it back. The feature could not converge; local, with no
  proxy, always won the race and so never showed the bug.
- **Cause 2:** on a cache hit `execute_spec` never runs at all, so no grid was
  even attempted on a repeat.
- **Fix:** the baseline is started and attached in `cached_execute_spec`,
  outside the payload cache, on every return path (hit, single-flight waiter,
  and miss). A late grid keeps running, warms its 6-hour cache, and annotates
  the next serve. Verified: first call `landed`, repeat `cached`.
- Every payload now carries `price_context_status`, because "it just isn't
  there" cost a probe cycle to unpick. Checks pin both the status and the
  ordering that keeps the start ahead of the cache lookup.

**July 28, 2026 (memory of the screen, a baseline, and the spacing)**
- **The results stay on screen, so now they stay in context.** History was
  prose only: every card we shipped vanished at the end of the turn, so "the
  second one" or "book the JetBlue" left Claude re-running a search it had
  already paid for, or worse, reconstructing fares from its own sentences.
  Each turn now returns a compact numbered ledger of what it put on screen
  (fare, route, times, stops, flight numbers, and the unpriced-outbound counts
  a round trip must not forget); the browser echoes the last 2 turns back and
  the loop injects them as a bracketed block whose rule is that anything not
  in the record has not been seen and still costs a search.
- **"Is this a good fare?"** Ranking always answered "which of these" and never
  "is today a good day to buy", which is half of why anyone opens Google
  Flights. Fixed-date searches now carry a `SearchDates` baseline over 21 days
  either side, fetched alongside the search and dropped if it is late (3s cap),
  cached 6 hours, standing down while the breaker is open. It says where the
  shipped fare sits among nearby DATES and names the cheapest one. It is not
  price history and never implies it is, in the model's note or on the card.
- **Reply spacing (Evan's report).** The renderer joined the model's soft wraps
  by swapping each newline for a space, leaving the whitespace on either side:
  "for $239 on Nov 28 , with morning departures" and "rises to  $374". The join
  now consumes the whitespace on both sides and drops a space left before
  punctuation. check.py runs the shipped chain, lifted out of index.html, over
  the exact reported sentence.

**July 28, 2026 (README drift checks)**
- The doc now defends itself: check.py gains a "README drift" section that
  blocks any push where the README's turn budget, adaptive timeouts,
  endpoints, Haiku router, round-trip group count, or streaming status
  disagree with the code. CLAUDE.md rule 7: a Changelog line is not a README
  update — the describing SECTION changes in the same commit.

**July 28, 2026 (README audit)**
- Doc-only refresh after the week's shipping: streaming is the primary
  transport in the Stack table (was still "parked"), adaptive 12s/28s
  timeouts and the 58s budget replace the stale 15s/65s references, the
  frontend-features list gains streaming/share-cards/degraded-mode (a July 26
  patch had silently missed), the degraded-mode invariant is listed, and the
  week's durable traps joined "Lessons the hard way" (rAF starvation in
  hidden tabs; total-time timeouts vs slow streams; tenacity bypass).

**July 27, 2026 (share card v2, Evan's notes)**
- Book now outweighs Share everywhere (compact Book links bumped, Share
  shrunk and muted); the pass dropped the "let's book this flight" header
  for a plain trip-type line (and the copied text starts with the route, so
  the sender writes their own pitch); the card is ~30% shorter with the
  whitespace put to work: a dotted flight-arc motif beside the route,
  per-segment durations, the date range in the stub, and a deterministic
  barcode drawn from route+price.

**July 26, 2026 (send this flight)**
- Every Book action now has a quiet Share beside it (cards, board pairing bar,
  multi-city): it renders a boarding-pass PNG on a canvas with the page's own
  fonts (no library, no backend; the card stays cream in both themes, like the
  route maps) — route in Fraunces, OUT/BACK rows, price stub with perforation,
  FLIGHTS.EVANBURKEEN.COM in the footer. Actions: copy image, copy text (a
  clean plaintext version with the booking deep link), save, and the native
  share sheet where the browser supports files. Clipboard failures fall back
  to saving with an honest toast.

**July 25, 2026 (streaming, unshelved)**
- Replies now stream (Evan's green light + spec): the recommendation types out
  letter by letter with an adaptive cadence that speeds up with backlog (the
  typing can never lag the network), and the result cards rise in AFTER the
  prose lands — prose first, then cards, then suggestion chips. Any transport
  problem falls back to the plain JSON turn automatically; a server-side turn
  failure is reported, not re-run. Fixed in the process: a typewriter driven
  by requestAnimationFrame alone never finishes in a hidden tab (browsers
  starve rAF) — a watchdog timer keeps it progressing and a hidden page
  flushes instantly.

**July 25, 2026 (board polish)**
- Return rows on the mix & match board always name the airline (Evan's catch:
  they omitted it when it matched the chosen outbound, which read as unlabeled
  rather than same-carrier).

**July 25, 2026 (late: the blank-screen root cause — good data was being discarded)**
Evan: a plain NYC-FLL round trip spun for a minute and showed NOTHING. Layered
root cause, bottom up:
- **L0 (Google):** slow-stream/soft-refusal wave on that route-class for our
  exits (aggravated by the day's test volume). External, real, passes.
- **L1 (search, the actual bug):** during such a wave the round-trip search is
  all-or-nothing: the outbound page is small and FAST (succeeds, carries true
  from-prices for every flight), while the ten return-pricing expansions are
  heavy and SLOW (all die on timeout). Zero pairings -> the ladder treated the
  attempt as a total failure, THREW AWAY the good outbound list, retired the
  identity, refetched everything, and failed the same way, four times. Fix:
  expansions get patient timeouts on retry attempts (12s -> 22s, wider
  harvest), and a ladder that ends pairing-less but holds an outbound list
  ships it instead of discarding it.
- **L2 (payload):** that case returned "No flights found" — indistinguishable
  from a true data gap. Now `from_priced_only_payload` ships every outbound
  (value-ordered, cap 30) with its honest from-total, `spec_echo`, and a
  message that forbids the model from claiming unavailability. check.py
  enforces it.
- **L3 (assistant):** with L2, Claude summarizes real from-prices and says
  returns price on tap, instead of "nothing came back."
- **L4 (frontend):** `visibleSections` hid results-less sections entirely —
  the blank screen itself. Degraded sections now render a notice + one-tap
  path into the board, where every row prices on demand via `/api/returns`
  (which retries independently and usually succeeds moments later).

**July 25, 2026 (evening: 'ran long' autopsy — the data was done, the prose was gated)**
Evan's screenshot: a turn that showed 8 complete cards + 33 outbounds while
apologizing that "the search ran long." Root causes, in the order they stack:
- **The canned line WAS the bug.** "That search ran long..." was a hardcoded
  string returned when the turn budget expired — even when every search had
  completed and 100% of the data was on screen; the final Claude call was
  simply never made. Now a budget exit with data ALWAYS runs one tool-less
  wrap-up call (tool_choice "none", same tools list so the prompt cache
  holds, ~5s, cannot start new work) fed a per-search status log, so the
  reply states exactly what completed and what, if anything, is missing —
  and is forbidden from apologizing when nothing is. Canned line remains
  only as the exception fallback. Turn budget 65s -> 58s (`TURN_BUDGET_S`).
- **fli's hidden inner retries**: tenacity (3 attempts, exponential) around
  every POST retried ON THE SAME SESSION — doctrine-violating (refusals are
  session-sticky) and up to ~48s inside ONE ladder attempt during timeout
  waves. Bypassed via `__wrapped__`; the identity-rotating ladder is the
  retry layer.
- **The 15s timeout is TOTAL, not idle** — heavy queries STREAM their
  progressive chunks slowly, and a 51KB nearly-complete response was killed
  mid-download and refetched from scratch, attempt after attempt. Timeouts
  are now per-attempt: 12s first (fail fast into a fresh identity), 28s on
  retries (let a slow stream finish); expansions 12s (harvest bounds them).
- **Measured for the record:** the NYC(3)xFLL outbound fetch costs 0.5-0.8s
  and returns 185 outbounds when Google is healthy — multi-airport is NOT
  inherently slow; slow turns are wave-timeouts x retry-stacking, so no
  structural/API change is warranted. Per-search outcomes now ride in
  `debug_timings` phases for the next autopsy.

**July 25, 2026 (round trips v2: optionality on demand, and the serialization bug)**
Evan: still not rich or fast enough to replace Google Flights for round trips.
Two root causes found and fixed:
- **THE latency bug: expansions were serial, not parallel.** The identity
  patch handed ONE shared curl session to every fli worker thread, so the
  "parallel" return-expansion fan-out queued on a single libcurl handle
  (measured: 10 expansions completing at 1.5s, 2.5s ... 36.7s — 37.4s total).
  `_identity_session` now keys handles per (identity, thread) — same exit IP,
  same fingerprint, cookies cloned from the primary jar, like a browser's
  parallel connections — and `RepresentativeSearch` reimplements the expansion
  loop with identity inheritance, a 6-connection browser-like cap, and a
  budgeted harvest (8s + one 7s grace when nothing landed) that ships every
  pairing that's ready and leaves stragglers from-priced. Same search:
  **37.4s -> 1.2-5.5s typical**. Multi-city and separate-ticket leg pricing
  ride the same fix; the adaptive top_n hack is gone (10 everywhere).
- **Tap-to-price (`/api/returns`):** any from-priced outbound now prices in
  place in ~1-4s with no model turn — the endpoint rebuilds fli's expansion
  request statelessly from `spec_echo` (shipped on every itineraries payload)
  plus the outbound's serialized legs, and returns Google's FULL return page
  (up to 40, value-ordered, each with its own tfs deep link). Priced groups
  get "Load every return" via the same endpoint; initial per-outbound returns
  raised 12 -> 20; the returns panel now honors the stops/airline/alliance/
  duration filters and sort. The ask-the-concierge chip remains as fallback.
- **First-call routing:** plainly-route-shaped queries emit their opening
  search call via Haiku 4.5 instead of Opus (the single largest fixed cost of
  a simple turn); if Haiku answers in prose instead of calling the tool, OR
  the API rejects the Haiku call at all, it is silently redone on Opus, so
  advice quality is untouched. Hotfixed minutes after deploy: Haiku 4.5
  rejects web_search_20260209 (an Opus/Sonnet-tier tool), which 400'd every
  plain search until the router call dropped the web tool and gained the
  hard Opus fallback. Lesson: a cheaper model is a different API surface,
  not just a different price.

**July 25, 2026 (round-trip optionality)** — Evan: the round-trip display
"doesn't give me enough optionality"; travelers pick per direction, not from
pre-paired cards.
- **Mix & match board:** expanding a round-trip section now shows every
  outbound on the left (value-ranked, filterable) and the selected outbound's
  returns on the right, each priced as the real total for that exact pairing,
  with a sticky chosen-pairing bar and per-pairing Book deep link. Top-pick
  cards stay as the collapsed view.
- **Representative expansion (data bug):** fli expands `flights[:top_n]` in
  sort order, so only the N cheapest outbounds ever got returns priced — the
  assistant then claimed "no nonstops" on routes with a dozen. Expansion now
  covers cheapest anchors + nonstops + fastest + departure-time spread
  (`representative_outbounds`, top_n 8 -> 10), and every unexpanded outbound
  still ships in `more_outbounds` with its honest Google "from" total (JFK-LHR
  test: 8 mostly one-stop cards -> 34 outbounds, 21 of them nonstop).
- Unpriced rows carry a one-tap "Price the returns for this flight" that asks
  the assistant (a departure-window search); `compact_for_model` now tells
  Claude how many unpriced outbounds and nonstops exist so its prose can never
  again infer absence from the expansion sample. Checks added for both.
- Post-deploy: expansions scale with request weight (10 single-airport pairs,
  8 multi-airport — this morning's proven count), after NYC->London timed out
  3/3 while JFK->LHR at 10 sailed. Continued diagnosis then showed the
  IDENTICAL NYC search timing out locally with no proxy (fli's plain outbound
  fetch, untouched by this change): a Google-side slow wave on heavy
  multi-airport round trips, not a regression — so the scaling is insurance,
  not a proven fix. If those queries still crawl once the wave passes,
  investigate before touching expansion counts again.

**July 25, 2026**
- Price-by-date calendar heatmap (roadmap item): expanding a flexible-date
  section now renders real month calendars, each day tinted by how its fare
  compares to the window's cheapest (ratio tiers, so one outlier cannot wash
  out the scale); cheapest days outlined, round-trip pairing + total in the
  tooltip, legend and honest "totals for N-night trips" note. Collapsed
  best-value chips unchanged.
- Round-trip date grids no longer price same-day returns: searched without a
  duration, Google's grid quotes out-and-back-on-one-date fares, so a flexible
  round trip with no trip_length_days understated every price. Now assumes 7
  nights and says so to both the user (assumptions line, calendar note) and the
  model (search-result note). `flex_grid_params` + 4 checks in check.py.
- Dev stub: English words that are also IATA codes (THE, FOR) are no longer
  parsed as airports, so clicking a calendar date locally no longer searches
  Teresina to Fortaleza.

**July 24, 2026** — long session with Evan; grouped. The *causes* are captured
under "Lessons the hard way" above, which is the part worth reading.
- **Data completeness:** parse the richest `wrb.fr` chunk (carriers were missing on
  thin routes); rank before truncating and cut with `retain_representative` so
  nonstops/cheapest/fastest always survive; state true totals so Claude stops
  inferring absence from a sample.
- **Ranking:** ordered by effective cost (fare + time + stops + warnings) instead
  of fare, with a "Best value" badge and an honest "Sort: best value" label.
  Guardrails: explicit sorts win; the cheapest stays visible.
- **Round trips:** pick an outbound, then a return, with per-option totals and
  deltas — 94 combos across 8 outbounds had been flattened to the 10 cheapest, all
  sharing one outbound. The headline price always matches the pairing shown.
- **Multi-city:** per-leg departure windows so a specific departure can be forced
  into the expansion; priced both as one ticket and separately, quoting the cheaper
  (`price_basis`) with mixed-carrier warnings. We had been promising through-bags
  and delay cover that Google does not sell.
- **Booking:** `tfs` deep links for all three trip types (round-trip return options
  each carry their own link), so Book opens the exact itinerary.
- **Reliability:** identity pool (cookies + exit IP + fingerprint, retired on
  refusal) after proving refusals are session-sticky; circuit breaker; staggering;
  capped breaker waits (uncapped put cards on screen at 32s).
- **Latency:** warm-connection reuse took searches ~6s -> 0.4-2.3s; search cache
  with single-flight dedupe; effort routing; ladder budgets. Time to first useful
  content 12.8s -> ~4.6s.
- **Display:** "Boeing 737MAX 8 Passenger" -> "Boeing 737 MAX 8"; `_7C` -> `7C`.
- **UI:** facelift (card depth, serif headers, dark mode with light default and a
  theme toggle), sticky compose bar + "results ready" nudge, and a fix for the
  seam/dark band that introduced at the bottom of short pages.
- **Process:** `scripts/check.py`, `.githooks/pre-push` (enforces the Changelog
  rule and the checks), and `CLAUDE.md` so a fresh assistant is oriented
  automatically. Two incidents drove this: the seam fix shipped with no Changelog
  entry despite the bold rule, and a verification probe that deliberately zeroed
  `HOUR_VALUE` reached production for ~2 minutes because the value check was too
  weak to notice (the stop penalty alone still ordered the example correctly).
  Reverted, and the check now asserts time is priced at all.

**July 24, 2026 (late, streaming reverted)**
- Reverted the frontend to the non-streaming `/api/search` path (Evan: "not
  fully working right now"). Streaming, progressive card reveal, and
  letter-by-letter typing are preserved on the **`streaming-experiment`**
  branch; the server side (`/api/search/stream`, `run_assistant(emit=...)`)
  stays on main, so switching back is a one-line fetch change plus that
  branch's stream handler. Kept from that work: the search cache,
  warm-connection identity reuse, effort routing, capped breaker waits, and
  the textarea autosize fix (all latency wins are independent of streaming).

**July 24, 2026 (night, latency)**
- Profiled the turn first (opt-in `debug_timings` on /api/search). Baseline
  was strictly sequential: simple search 12.8s = claude_1 2.9 + search 6.0 +
  claude_2 4.0; comparison 15.2s; knowledge question 9.5s (all generation).
- Search 6.0s -> 0.4-2.3s: `checkout_identity` prefers the most recently
  SUCCESSFUL identity so its TLS connection through the residential proxy is
  reused (random choice was paying a fresh handshake nearly every search).
- Search cache (4 min) with single-flight dedupe: repeats inside a
  conversation are instant and identical concurrent searches collapse to one
  upstream request. Key ignores only cosmetic fields, so cabin/dates/stops
  can never serve the wrong result set.
- **Streaming** (`/api/search/stream`, SSE): result cards emit the moment a
  search batch finishes, prose streams while Claude writes it, `text_reset`
  discards a preamble that turned out to precede a search. /api/search kept
  for back-compat.
- First Claude call runs at low effort ONLY when the query is plainly a route
  search (it just emits a tool call); knowledge questions, answered on that
  same call, keep full effort. Quality was not traded for speed.
- Capped breaker waits: an uncapped cooldown put cards on screen at 32s in
  one measured sample. First attempt now pauses <=1.5s, retries absorb <=8s.
- Measured after (4 samples/query, medians): time to first useful content
  12.8s -> 4.6s (search), 15.2s -> 5.5s (comparison), 9.5s -> 1.2s
  (knowledge). Full completion 12.8 -> ~8s and 15.2 -> ~11.9s. Remaining
  critical path is ~90% Claude generation; untaken levers are a faster model
  for the tool-selection call and speculative search (see Roadmap).

**July 24, 2026 (evening)**
- Full anti-refusal stack. Identity pool: 3 identities, each with its own
  cookie jar + residential exit IP + browser fingerprint (chrome/edge/
  safari/firefox variants), bound per-thread; any refusal retires the
  identity so the next attempt is a genuinely different visitor. Circuit
  breaker: 3 consecutive refusals opens a 25s process-wide pause (sustained
  volume is what escalates a cheap session flag into an IP burn — hammering
  makes it worse). Comparison searches now ramp 0.4s apart instead of 5
  simultaneous requests from one address. Warmup page-load deleted (study
  showed no benefit, ~1.8MB proxy bandwidth per cold start).
  Simulated at the measured 1/3 refusal rate: ladder success 65.8% -> 99.0%
  (the independent-attempt ceiling). 11/11 logic checks; live 3/3.

**July 24, 2026 (later)**
- Root-caused the transient tiny-ErrorResponse failures (gRPC code 13):
  they are SESSION-STICKY soft-blocks, not random flicker — a flagged
  cookie jar failed 64/64 requests while brand-new sessions in the same
  seconds went 20/32 OK. Retry ladder now discards the thread's Google
  session on every failed attempt (reset_google_session) in addition to
  rotating the proxy exit, so each retry arrives as a new visitor (new
  residential IP + new cookies in prod). Deep IP-level bursts remain
  (only IP rotation escapes those); warmup showed no benefit in the
  study, so resets do not re-warm (saves ~1.8MB proxy bandwidth each)

**July 24, 2026**
- MAJOR data fix (Evan's catch: app showed 1 of 3 ICN→HRB Jan 1 nonstops while
  Google's UI showed all): Google streams GetShoppingResults as progressive
  wrb chunks in one response (first = early partial snapshot — the "fills in
  after a few seconds" effect); fli parses only the first chunk. Patched in
  api/index.py to parse the richest chunk (34 vs 1 result on that route;
  busy single-chunk routes like JFK→ORD unaffected, 113/113 rows parse)
- Airline codes with digit-leading IATA (Jeju "_7C") lose fli's enum
  underscore in display, and airline include/exclude filters resolve them

**July 23, 2026 (night)**
- Sticky compose bar: input pinned to the viewport bottom (paper background,
  top hairline, safe-area padding) so follow-ups don't require scrolling past
  results; Searches fab and ladder overlay raised to clear it
- "Results ready" nudge: replies still don't auto-scroll, but if a reply lands
  below the fold a green pill appears (click to jump; dismisses on scroll)

**July 23, 2026 (evening)**
- Aircraft names standardized (Evan's request): drop Google's "Passenger"
  suffix, fix MAX spacing ("Boeing 737MAX 8 Passenger" → "Boeing 737 MAX 8");
  variants (777-300ER, A330-900neo) show whenever Google includes them —
  they cannot be inferred when Google sends only the base type
- Section titles above results are no longer italic (upright Fraunces)

**July 23, 2026 (later still)**
- Light mode is now the default for everyone (Evan's call): theme button is a
  simple light/dark toggle, dark only when explicitly chosen (localStorage);
  system prefers-color-scheme no longer consulted

**July 23, 2026 (later)**
- Theme control (Evan's request: dark mode had no off switch): header button
  cycles auto → light → dark, pinned in localStorage; dark styles moved from
  the prefers-color-scheme media query to an html.dark class set by a
  pre-paint head script (no flash, follows system in auto)

**July 23, 2026**
- Facelift: cards lifted off the paper (solid warm-white panels, warm shadows),
  Fraunces italic section headers, cold blue badge → deep teal, muted airline
  avatar palette, lighter/shorter route maps, green active toggles, quieter
  show-all row
- Dark mode via prefers-color-scheme ("midnight concierge"): espresso-green
  ground, copper prices, brass fittings; route maps intentionally stay cream
  (atlas plates) so the JS-generated SVGs need no dark variant

**July 16, 2026**
- Made per-deployment changelog entries an explicit non-negotiable rule (Evan's request)
- Arrival-day guidance corrected (Evan's catch): same-day Asia->US is a
  heuristic that fails for late departures and Middle-East routings; assistant
  now searches both candidate departure days when routings vary
- Pre-handoff audit: scripts/build_world.py committed (world.js was previously
  unregenerable), README ops section updated with live proxy details and the
  current 4-attempt retry ladder
- Search ladder now reachable on every screen size: fixed rail above 1400px,
  floating brass 'Searches' button + overlay panel below it
- Root-caused first-search empties: Google's flights backend returns transient
  ErrorResponse bodies (HTTP 200, ~5-10%, in bursts). Retry ladder now 4
  attempts with jittered spacing; sticky proxy session per instance with IP
  rotation on failure; warmup page-load per cold start. 10/10 cold-start ladder success
- Empty searches (transient hiccups the assistant retried) no longer render as
  hollow sections; prompt discourages overlapping variants after a success
- Search ladder: fixed side rail indexing every results section, click to jump
  (each comparison prong is its own rung); hidden under 1400px viewports
- README gains a "For AI assistants" handoff section (workflow, env, roadmap)


**July 15, 2026 (later)**
- Randomized, query-neutral loading phrases (no more 'consulting live fares' on
  general questions); general aviation Q&A formally in scope in the prompt
- Fix stray mid-sentence line breaks in replies: renderer joins soft wraps into
  flowing text (paragraphs and lists preserved); prompt forbids manual wrapping
- FLI_PROXY activated in production: IPRoyal rotating US-residential proxy for all
  Google traffic — ends the datacenter-IP throttle waves and the region roulette
- Region back to iad1 (US East; Google egress now goes through the proxy, so the
  server region only affects Claude/API latency)

**July 15, 2026**
- Sequential longitude unwrapping — complex routes (FLL-BOS-CDG-PVG) no longer draw across the wrong ocean
- Layover rings on card timelines; layover durations on map dots; capped map padding + 5-wide world tiling
- Region fra1 (fourth US pool burned); README rewritten as living doc

**July 14, 2026**
- Reliability: FLI_PROXY plumbing, USD pinning, Anthropic retry/backoff + polite overload messages, 15s fli timeouts, 65s turn budget, per-search deadlines
- Latency: parallel search execution (3x on comparisons), prompt caching, trimmed retries
- Web search tool for real-world context (event dates/venues)
- Fix airport misstatements (route endpoints now visible to the model); region-vague queries ask one question; small-airport widening
- Round-trip maps draw both directions (dashed return); flexible-date grids collapse to best-value dates
- Concierge voice (no dashes); crest/stationery/boarding-pass styling; map lakes + atlas look; per-itinerary booking links (BDL-JAX bug); alliance dropdown + tags

**July 13, 2026**
- v5 conversational assistant: Claude agent loop with search_flights tool, comparisons (up to 4 searches/turn), suggestion chips, stop/supersede, no-autoscroll
- Multi-city (one-ticket pricing); arrival-day targeting; past-date rolling; via_airports filter (HND hallucination fix); Detailed/Compact + per-section toggles; full-list browse with client-side filters; route maps with real coastlines; portal redesign
- Regions iad1 → sfo1 → cle1 → pdx1 as pools burned; 300s maxDuration

**July 12, 2026**
- v4 rewrite on fli: real round-trip pricing, native filters, flexible dates, Claude tool-use parser; 429 backoff handling; arrival-window enforcement

**May 3, 2026**
- Full rewrite: Next.js/SerpAPI → Python/FastAPI/fli

**January 2026**
- v1–v3: original Next.js + SerpAPI prototypes (two-step round trips, debug eras)
