# Flight Assistant

Conversational flight concierge at [flights.evanburkeen.com](https://flights.evanburkeen.com) —
talk to Claude, which pulls live Google Flights data to search, compare, and recommend.

> This README is the living record of the project: how it works, how to run it,
> what has shipped, and the operational lessons learned. Update the Changelog
> with every push.

## For AI assistants picking this up

You (an AI coding assistant) are expected to maintain this project from this
document alone. Everything you need:

- **NON-NEGOTIABLE RULE: every deployment gets a Changelog entry in this README,
  in the same commit.** One or two lines: what changed and why (the context or
  bug that motivated it), newest day first. No push without its log line.

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
- **Costs Evan cares about:** Anthropic per-turn (~cents), IPRoyal bandwidth
  (~$6/GB, ~2GB purchased July 2026, months of runway at current usage).
- **Parked work — `streaming-experiment` branch** (`git -C <path> checkout
  streaming-experiment`): SSE turn delivery with progressive result cards,
  held-back reveal after the intro sentence, and letter-by-letter typing.
  Shelved July 24, 2026 because it was not fully reliable in practice. The
  `/api/search/stream` endpoint and `run_assistant(..., emit=...)` are STILL
  ON MAIN and still work, so returning to it is a one-line change in
  `public/index.html` (fetch `/api/search/stream` instead of `/api/search`)
  plus restoring that branch's frontend stream handler and typewriter. Known
  gotcha if you go back: mutate Alpine's reactive proxy
  (`this.messages[this.messages.length - 1]`), never the object you pushed.
- **Roadmap shelf (discussed, not built):** price watches (cron + email),
  trip memory + login, streaming replies, price-by-date calendar heatmap,
  real booking via Duffel, search-result caching.
- **Known trade-offs accepted by Evan:** timeline layover dots use naive local
  times (schematic, not exact); Claude sees only top-6 summaries per search
  (with truncation warning baked in); the value-ranking weights
  (`HOUR_VALUE` etc. at the top of `api/index.py`) are a product judgement, not
  a tuned model — change them there; multi-city is the slow path (leg-2 fan-out
  is 4–6 candidates), so it is the trip type most likely to hit the 65s turn
  budget and return partial results.

## Stack

| Layer | What |
|---|---|
| Hosting | Vercel serverless (Python), auto-deploys on push to `main` (`EvanBurkeen/flight-search`) |
| Backend | [api/index.py](api/index.py) — FastAPI. `/api/search` (JSON) is what the frontend uses; `/api/search/stream` (SSE) still works and is exercised by the parked `streaming-experiment` branch |
| LLM | `claude-opus-4-8` agent loop, `max_retries=4`, effort `medium`, prompt-cached system+tools |
| Flight data | [`fli`](https://github.com/punitarani/fli) (PyPI `flights`) — reverse-engineered Google Flights |
| Web context | Anthropic server-side `web_search` tool (max 3/turn) for event dates, venues, etc. |
| Coordinates | `airportsdata` (IATA → lat/lon) for route maps |
| Frontend | Single static [public/index.html](public/index.html), Alpine.js, no build step |
| Map data | [public/world.js](public/world.js): Natural Earth 110m land + lakes as SVG paths |

`ANTHROPIC_API_KEY` lives only in Vercel env. Optional `FLI_PROXY` env routes all
Google traffic through a proxy (see Operations).

## How a turn works

1. Frontend POSTs `{query, history}` (history = last 12 user/assistant text turns).
2. `run_assistant` runs an agent loop (≤3 search rounds, ≤8 API calls, **65s turn budget**):
   Claude converses and calls `search_flights` (up to 5/turn, executed **concurrently**)
   and/or `web_search`. `pause_turn` (server-side search) is resumed automatically.
3. `execute_spec` per search: roll past dates forward (with a visible note) →
   resolve airports (multi-airport cities supported) → build fli filters
   (cabin, stops, airlines, alliances, price cap, times, currency pinned USD) →
   `run_search` (4-attempt ladder: sort, sort, CHEAPEST, CHEAPEST; 15s fli
   timeout; **whole ladder bounded by `budget_s`**, 35s default / 45s
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
   - serialize up to 50 one-ways / ~8 round-trip OUTBOUND groups (each with its
     own `return_options`, every option priced as the real total for that
     pairing) / 8 multi-city itineraries (priced BOTH as one ticket and as
     separate tickets, quoting the cheaper — `price_basis` says which), each
     with a `tfs` deep link to that exact itinerary, alliance tag, aircraft,
     warnings (tight <45m connections, overnight, self-transfer, airport
     change, mixed-carrier separate-ticket risk), CO2 delta, and
     `route_points` (with per-stop `layover_min`) for the map.
4. Claude sees a **compact top-6 summary per search** (with route endpoints and an
   explicit truncation warning); the browser gets everything.
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
shipped, which is why the cut keeps nonstops/cheapest/fastest) · round-trip
outbound picker with per-return totals and price deltas · Best value badge ·
Book deep-links straight to the chosen itinerary on Google · flexible-date grids show best-value dates
first (within 15% of cheapest) · per-flight atlas maps (land+lakes, graticule,
sequential longitude unwrapping so every leg takes the short way; outbound solid,
return dashed; layover dots with durations) · timeline layover rings · suggestion
chips · search ladder (jump-to index of every results section: fixed rail on
wide screens, floating 'Searches' button + overlay elsewhere) · concierge
styling (Fraunces serif, brass fittings, boarding-pass dividers, greeting).

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt uvicorn
.venv/bin/python scripts/dev_server.py   # API + frontend on :8123
```

No `ANTHROPIC_API_KEY` locally → the Claude loop is stubbed by a pattern parser
(`scripts/dev_server.py`); searches still hit live Google. Stub grammar: airport
codes, "round", "flex/weekend", "compare", "multi A B C".

## Operations

- **Google throttles datacenter IPs in waves.** Symptom: empty results/429s in prod
  while the identical search works from a residential IP (diagnose exactly that way).
  Mitigations built in: retry ladders, 15s fli timeout, 65s turn budget, honest
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
  seconds. Any refusal RETIRES that identity, so the retry is a genuinely new
  visitor. Do not "fix" a refusal by retrying on the same session: refusals are
  session-sticky (a flagged session failed 64/64 while fresh ones passed 20/32
  in the same window — see Changelog July 24).
- Currency is pinned to USD in every search, so non-US regions are safe.
- Anthropic 429/529 overloads surface as a polite try-again message.
- **Google backend transience:** ~5-10% of search POSTs return HTTP 200 with a
  tiny `travel.frontend.flights.ErrorResponse` body (parses to empty), in
  bursts. Handled by the 4-attempt jittered ladder in `run_search` — do not
  mistake these for IP throttling (throttling = persistent, this = flicker).
- fli quirks: `SortBy.BEST` intermittently returns None (fallback to CHEAPEST);
  multi-city is slow (fan-out capped at `top_n=4`, `maxDuration` 300s).
- `world.js` is generated by `scripts/build_world.py` (Natural Earth 110m land +
  lakes; run it, then bump the `?v=N` cache-buster on the script tag in index.html).

## Changelog

**July 24, 2026 (compose-bar seam)**
- LOGGED LATE (commit 1e79fea shipped without its entry, which breaks the rule
  at the top of this file — the entry belongs in the same commit).
- Fixed a visible line and dark band across the bottom of the landing screen.
  Three causes at once: the fixed compose bar filled flat `var(--bg)` while the
  body carried four texture layers (so it was a different shade of cream, with
  the join reading as a line); its `box-shadow: 0 -8px …` cast UPWARD, smudging
  dark over empty paper; and with content shorter than the viewport (684 vs
  720) the body stopped painting texture before the bottom.
- Now one `--paper` variable is shared by the page and the bar (redefined once
  for dark mode so they cannot drift), the bar paints it with
  `background-attachment: fixed` so the texture aligns with what is behind it,
  `body` gets `min-height: 100vh`, and the divider/shadow appear only when the
  page is actually scrollable (`syncBar`, kept current by a ResizeObserver).
  Note: testing "content below the bar's top edge" instead is WRONG — it counts
  the container's own 8rem bottom padding and re-raises the divider on the
  empty landing screen.

**July 24, 2026 (tail latency)**
- Bringing down worst-case turn times. run_search now takes a budget
  (35s default, 45s multi-city) and stops retrying when it is out of time:
  retries re-run the ENTIRE search, and for multi-city that meant four
  attempts x a ~25s fan-out plus 2/4/6s sleeps = 100s+ tails that blew the
  65s turn budget. Ladder sleeps cut to ~0.5-1.3s (worst-case pure sleep
  ~4s, was 12s): each retry is a NEW identity now, so long per-search
  cooling is redundant with the process-wide breaker.
- Multi-city standalone leg pricing runs CONCURRENTLY with the expansion
  (it ran after, adding its whole runtime to the tail); 6s harvest grace,
  degrades to joint price. Fan-out adapts to weather: 6 normally, 4 when
  the breaker has seen recent refusals, so optional work shrinks before
  required work misses the budget. fli expansions already run 10-wide.
- Verified deterministically (budget stops a stubbed always-failing ladder
  at 2 attempts). Local live checks impossible: the dev IP is hard-timing
  out after a full day of testing; production rides the proxy.

**July 24, 2026 (multi-city prices: quote what you can actually buy)**
- Evan: "the 635 is 1022, not 2207". Root cause found by dumping raw part
  prices. In a multi-city expansion, parts[0].price is the "from" total for
  the BEST completion of that leg-1 and parts[-1].price is the total for that
  SPECIFIC pairing, so `max()` correctly reads the JOINT-TICKET price. The
  problem is the joint fare itself: for FLL->ICN (6:35 Delta) + ICN->HRB
  (Asiana) Google quotes $2,207 as one ticket, while the same two flights
  bought leg by leg are $844 + $182 = $1,026 - which is what Google's own
  booking page sells and displays as "Lowest total price / Separate tickets".
  We were quoting a fare nobody would buy.
- Multi-city now prices every leg standalone as well (through the shared
  search cache, so a leg already searched on its own is free) and quotes
  `min(one ticket, separate tickets)`. Payload carries `combined_price`,
  `separate_price` and `price_basis`; when separate is >3% cheaper the card
  and Claude get an explicit warning naming both numbers and the loss of
  through-bags / delay cover. Falls back to the joint price whenever a leg's
  flights can't be matched, so it can never invent a number.
- flight_signature now uses the display airline code so signatures built from
  raw fli results match serialized ones (fli writes _7C for 7C).
- Caveat: multi-city turns are slow (expansion fan-out plus per-leg pricing)
  and can exceed the 65s turn budget, surfacing partial results. Per-leg
  pricing is bounded at 15s each, runs in parallel, and degrades to the
  joint price on timeout.

**July 24, 2026 (multi-city truthfulness)**
- Two Evan catches on the FLL-ICN-HRB trip. (1) The best one-way (6:35 Delta)
  never appeared in combined results: multi-city expands only the first
  `top_n` leg-1 candidates and it sat at rank 5+ among same-price ties.
  Fan-out is now 6, and multi_city_segments accept a per-leg departure_time
  window so a specific departure can be forced into the expansion ("what
  about the 6:35?" now works; it priced at $2,207 combined, which is WHY
  cheapest-first hid it). (2) We claimed "one ticket" and quoted a single
  price; Google's own booking page said "Separate tickets - must be booked
  individually" (Delta=SkyTeam + Asiana=Star don't interline) and showed a
  seller spread ($1,022 OTA vs $1,166 airline-direct vs our $1,042 quote).
- Now: mixed-carrier itineraries whose alliances differ (or a carrier has
  none) carry an explicit separate-tickets warning on the card, in Claude's
  summary, and in the section message; prices are framed as Google's
  search-time quote with seller variance; the prompt forbids promising
  through-bags or misconnect protection on mixed-carrier combinations.
  Same-alliance pairings (Delta + China Eastern) correctly stay unflagged.

**July 24, 2026 (deep links for every trip type)**
- Round-trip and multi-city Book links now open the exact itinerary too, so
  all three trip types are deep-linked. The earlier round-trip failure was a
  misread of the schema: **f2 is a constant 2 and f19 is the TRIP TYPE**
  (1 round trip, 2 one-way, 3 multi-city). Putting the trip type in f2 made
  Google reject the URL and fall back to its home page.
- Confirmed by decoding real Google links Evan supplied for all three types.
  Repeated f3 = journey segments; a segment's repeated f4 entries are the
  connecting flights inside it (so multi-city FLL-ATL-ICN is one segment with
  two f4s). f13/f14 endpoints take {1:1, 2:"IATA"} for an airport or
  {1:3, 2:"/m/..."} for a Knowledge-Graph city; we always emit airports.
- Browser-verified: round trip opens with BOTH legs selected, multi-city
  opens as a "Multi-city trip" with every segment selected and vendors listed.
  The one-way byte-for-byte test still passes, so the encoder is pinned.
- Each round-trip RETURN OPTION carries its own booking link, and picking one
  swaps the card's link, so Book always opens the pairing on screen.

**July 24, 2026 (round-trip picker)**
- Round trips now work like Google's: you choose an OUTBOUND, then a RETURN,
  and the total moves with the choice (Evan: "it's giving me the cheapest
  combos instead of letting me choose"). Previously we flattened combos and
  kept the 10 cheapest, so a search returning 94 combos across 8 outbounds
  showed ten rows that differed only in return leg - hiding seven outbounds
  including the only nonstop.
- `search_fixed_dates` groups combos by outbound; each card is one outbound
  with its own `return_options`, every option carrying the real
  `total_price` for that pairing plus `extra_over_best` vs the group's floor.
  Clicking a return swaps the leg in place and updates the headline.
- The headline price is always the total for the pairing ON SCREEN, never the
  group floor (an earlier build quoted $228 while displaying a $257 pairing).
  `cheapest_total` is kept separately for reference; the floor option is
  labelled "cheapest", not "best", since value ranking may prefer another.

**July 24, 2026 (book the flight you clicked)**
- One-way "Book" links now deep-link to the exact itinerary instead of a
  search page the user has to scan (Evan: clicked a JetBlue fare and "had to
  go search for it"). Google's booking URLs carry `tfs`, a base64url protobuf
  naming the itinerary down to airline and flight number; `itinerary_url()`
  builds it. Schema was recovered from a real Google-issued link and is
  verified by a test that reproduces that link BYTE FOR BYTE, so a future
  schema drift shows up immediately.
- Confirmed in a browser: nonstop and multi-leg (BOS-JFK-FLL) links both open
  Google straight on that flight with its booking options. The `tfu` token in
  Google's own URLs is session-scoped and is NOT required.
- NOT done: round trips. A two-segment tfs with both directions selected is
  rejected (Google falls back to its home page), so round trips keep the old
  search-query URL. To finish it, capture a real Google round-trip booking URL
  and decode its tfs the same way — the return-segment layout is the only
  unknown. Anything unexpected in the data makes `itinerary_url` return None
  and the caller falls back, so this can never produce a dead link.

**July 24, 2026 (representative results)**
- Nonstops could be missing entirely (Evan: "why didn't the nonstops show up?").
  Separate bug from the value ranking: we sliced `results[:50]` BEFORE scoring,
  and Google hands results back cheapest-first. Measured BOS->FLL Nov 22:
  Google returned 98 options with 12 nonstops, but 11 priced above the
  50th-cheapest fare ($227 vs $414/$514), so exactly one survived the cut. The
  "Nonstop only" filter then reported "1 of 50" and Claude told the user
  everything else needed a connection - both truthful about the shipped subset,
  both wrong about reality.
- Now: score the full pool (RANK_POOL=120) and cut afterwards, and cut with
  `retain_representative` - value order decides ORDER, but the shipped set
  always keeps up to NONSTOP_QUOTA=8 nonstops plus the outright cheapest and
  fastest, because the client-side filters run over whatever we ship and must
  not lie about the option space. Rescued rows are re-sorted back into value
  order (filling slots from the back had reversed them).
- Section message now states true totals ("Found 98 options (showing 50). 12 of
  them are nonstop, cheapest nonstop $99.") so Claude stops inferring absence
  from a truncated sample. Round trips get the same pool-then-cut treatment.

**July 24, 2026 (value ranking)**
- Results are ordered by VALUE, not fare (Evan's catch: on BOS->FLL Sept 2 the
  first nonstop sat at rank #21, behind nine near-identical connections that
  cost $25 less and ran 2 to 9 hours longer; the preview shows 8, so it was
  invisible). Ordering is by an effective cost: fare + $25/hour over the
  fastest option + $35 first stop / +$45 each additional + penalties reusing
  the warnings we already generate (self-transfer $55, airport change $60,
  tight connection $40, overnight $35). Constants live at the top of
  `api/index.py` and are a product judgement, tune them there.
- Guardrails: an explicit "cheapest"/"fastest" request still wins outright,
  and the cheapest fare is always pinned into the preview (top 4) so a
  price-first traveler never has to expand the list to find it. The
  "Best value" badge only appears when we actually ranked by value.
- This also fixed the ASSISTANT, not just the cards: `compact_for_model` sends
  Claude the top 6, which were previously the 6 cheapest connections, so it
  never saw a nonstop. Prompt now tells it to lead with best value and to
  price the difference in plain terms when the cheapest is not its pick.
- Frontend: "Sort: best" (which sorted nothing) is now honestly "Sort: best
  value"; new filled-green Best value badge. Round trips ranked the same way.

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
