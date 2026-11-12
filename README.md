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
  Trip briefings add `OWNER_KEY`, `KV_REST_API_URL` + `KV_REST_API_TOKEN`
  (Upstash, injected by Vercel Storage), `RESEND_API_KEY`, `BRIEFING_FROM`
  and `CRON_SECRET`; see Operations. Without them the feature is inert.
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
- **Roadmap shelf (discussed, not built):** login + saved trips, real
  booking via Duffel. (Price watches shipped Nov 11 as trip briefings: owner
  only, cron + email, no login. Search-result caching shipped
  July 24; the calendar heatmap and streaming replies shipped July 25; the
  cross-turn results ledger — the useful slice of "trip memory" without login
  — and price context shipped July 28; reload persistence + recent-trip
  chips, the no-login slice of saved trips, shipped Aug 8.)
- **KNOWN OPEN BUGS (as of Nov 11, 2026) — pick these up before new features:**
  1. **Google withholds some fares entirely.** The $111 Breeze HVN-FLL fare
     never appears, even from a dedicated single-airport search. Disclosed
     by the manifest layer rather than hidden; probably not fixable
     client-side, but worth re-testing occasionally.
  2. **Google refuses some route pairs to production's session outright.**
     Nov 11: SGN→JFK, then JFK→HAN, refused on every proxied attempt while
     neighbors on the same proxy passed and a home connection got them in
     0.3s. JFK pairs are refused far more than others through the proxy.
     Mitigated, not fixed: a multi-airport search routes around a refused
     pair, and the refused part is asked for again after the turn
     (backfill, and server-side before a briefing) and merged in. Later
     that night prod was refused on EVERY shape (JFK->ORD control too)
     while a home connection answered in 0.3s. Going out directly from
     Vercel was tried and refused 0 for 4, then removed. So this is a
     proxy-pool reputation problem, not a route problem, and our own
     request volume feeds it (Nov 12 cut: see Changelog). Next steps: read
     `debug_timings.attempt_outcomes` (refused vs ok per process) over a
     few days. **IPRoyal's "mobile" pool was trialed Nov 12: about the same
     as residential, not better.** Paced like real use (one search per
     25s, fresh session each) it answered 3 of 4 searches, ~5 of 8
     attempts refused, prod's residential rate that night. Its exits are
     mostly NOT mobile: 8 sessions gave 7 distinct addresses, only 2 on
     T-Mobile, the rest home broadband (Cox, RCN, AT&T wireline). The
     sticky-session suffix works on it unchanged. CAUTION, a test that
     misled first: 40 attempts in two minutes with `breaker_wait`
     disabled read 90% refused; a burst burns any pool, so pace proxy
     tests like real traffic and run a home-connection control. Two
     refusal types: ~95B is the proxy-address one, ~325B the occasional
     fresh-session one (seen from home too, absorbed by the ladder).
     Remaining options: a licensed source, or an exit Google treats like a
     home connection.
     The only true prevention is a licensed source (SerpApi's Google
     Flights API was the candidate priced Nov 12; Amadeus self-service
     shut down July 2026).
  (The Aug 6 list's other two — the multi-city separate-vs-combined
  comparison that never fired, and the passenger count that never reached
  the Book link — were fixed Aug 7; see the Changelog. `leg_price_index`
  and `itinerary_url` both kept their names, so old notes still point at
  real code.)
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
- **A sleeping Mac makes production look hung.** Nov 11: three post-deploy
  `/api/search` probes "hung" (no bytes in 100-150s, one curl reporting 344s
  against `--max-time 120`) while `/api/warm` answered in 0.1s and the stream
  worked. The laptop was on battery and in Maintenance Sleep about 20 times an
  hour (`pmset -g log`); the JSON endpoint is silent for its whole turn, so
  it is the request a sleep kills. A curl that overruns its own `--max-time`
  means the CLIENT froze. Check `pmset -g log` before suspecting the deploy.
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
- **Refusals come in bursts per REQUEST SHAPE.** Nov 11: one three-airport
  round trip was refused 16/16 over two turns while a single-airport round
  trip and the one-ways on the same route were answered minutes apart, and
  repeated trials found no deterministic trigger. Retrying the same shape
  inside a burst only burns the turn; a DIFFERENT shape (each airport alone,
  one-ways instead of a round trip, the same request a few seconds later) is
  the retry that works. Every round trip and multi-airport search now has
  such a decomposition (`rescue_round_trip`, `rescue_empty_multi_airport`).
- **Who asks matters as much as what is asked.** Nov 11: JFK→HAN was
  refused 8/8 through the residential proxy (fresh exits, fresh
  fingerprints) and answered in 0.3s from a home connection, while LGA→HAN
  passed through the same proxy first time. Rotating within one kind of
  visitor cannot escape a refusal keyed on that kind; a different TIME
  (backfill, after the burst) can, and a different KIND of exit might (the
  Vercel datacenter address did not: 0 for 4). Even from home about 1 in 4
  FIRST attempts is refused at random, across every fingerprint (tested Nov
  12, so do not chase fingerprints). When a
  shape is refused, run it from this laptop as the control before
  concluding anything about the request itself.
- **A partial answer must not look or live like a whole one.** The NYC to
  Hanoi set without JFK was cached for the full 4 minutes, so "try again"
  re-served it in 1.6s, and the reply ranked it as complete ("the only
  one-stop", "the cheapest overall"). Partial sets now cache for 20s and
  carry STILL LOADING until the missing part lands or is named.
- **Refused is not an answer.** A refusal parses to no chunk at all; Google
  saying "no flights" is a well-formed chunk with no rows. Only the second is
  evidence of absence (Nov 11: a dedicated SGN→JFK search was refused every
  time while the route has 100+ flights, and the airport probe had been
  calling that "VERIFIED NO SERVICE"). `run_search` records the verdict per
  attempt; read it before believing an empty.
- **A combined request collapses to its worst member** — date grids (Aug 8,
  HVN) and flight lists (Nov 11, SGN→JFK refused, taking EWR and LGA down
  with it) alike. Split per airport pair before calling anything empty.
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
- **The date grid streams PER-SLICE chunks: one day per chunk, out of
  order, ending in unpriced placeholders** (Aug 28, HVN→BNA: 19 chunks, 31
  items, 18 priced days — fli's parser read chunk one and shipped one day
  while Google's own UI showed the month). `merge_calendar_chunks` unions
  every chunk's day items; day items are found BY SHAPE (a list opening
  with a YYYY-MM-DD string) because chunk tails are heterogeneous — keying
  on chunk[-1] once ingested a bare `31` and overwrote real days. A later
  chunk's entry wins only when priced. What looked like "HVN's grid is
  sparse" (Aug 8) was mostly this parse loss.
- **A multi-airport DATE GRID can still collapse or come back empty** for a
  degraded session, so the insurance layers stay: thin grids retry as
  fresh identities; a thin OR EMPTY combined multi-airport grid rebuilds
  from per-pair grids merged at the min price per day; sparse airports are
  named so their gaps never read as no-service; and an incurably short
  grid ships with a coverage warning. `grid_probe` in the payload says
  what happened.
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
- **`f8` is the PASSENGER LIST (one varint per traveler, adult=1, child=2,
  adults first) and `f9` is the seat class (1 economy … 4 first).** The code
  had these two swapped for weeks and every byte-identity test passed anyway,
  because the 1-adult economy default encodes as `1` under BOTH readings —
  a fixture can only catch what makes bytes differ. Settled by decoding
  Google's own tokens for a 2-adult + 2-child search in two cabins
  (f8 stayed `[1,1,2,2]`, f9 flipped 1→3). Until then, premium-cabin links
  had encoded "one lap infant, economy." Do not encode infants: the enum
  values for in-seat vs on-lap are disputed between reverse-engineered
  schemas, and the tool only accepts adults + children.

**Model behavior**
- **A reply can arrive in pieces.** Adaptive thinking may pause mid-sentence,
  splitting one answer into several text blocks; join them as fragments
  (`join_text_blocks`), never as paragraphs.
- **The screen never shows the model's scratch text.** Section titles were the
  model's free-text `summary`; on Nov 11 one read "verify date year context".
  Titles are now built from the search spec (`section_header`), and the
  summary only labels the search for the model and the logs.
- **A frozen example in a prompt or schema ages into a wrong instruction.**
  "Assumed 2026 for 3/21" was a fine example in July; by November it taught
  the model that March meant a past year. Examples that involve dates are
  computed from today (the year rule in the prompt head) or stated without a
  year.
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
- **`x-show` rewrites an element's `display`.** Layout that lives in an inline
  `style="display: block"` is lost the moment Alpine shows the element (Nov 11:
  the multi-city "price the combined ticket" link collapsed to an inline box
  overlapping the leg board). Put display in the stylesheet.
- **The bottom edge belongs to the compose bar and the sticky board bars.**
  A separately floating button there will collide with one of them (the old
  "Searches" fab sat on top of the board's Share); controls go inside the
  compose bar instead.
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
- **A mobile SSE socket dies SILENTLY.** When the radio sleeps or the network
  hands off, the connection drops with no error and no FIN, so `reader.read()`
  never rejects and never resolves — the reply spins forever. A fetch signal
  only aborts on user-stop, so nothing rescued it; this was the "first search
  never returns" bug. `streamTurn` now runs an inactivity watchdog (reset on
  any byte, including the server's `: ping`) that aborts a dead stream and
  falls back. Two traps it hid: the fallback must use a FRESH AbortController
  (the watchdog aborted the old one, so a reused fetch returns nothing), and
  the watchdog must DISARM the instant `done` arrives or the local typewriter
  drain trips it on a long reply.
- **Cold serverless is the common first-search cost.** An idle Vercel function
  is torn down; the first request pays the boot plus a cold Anthropic TLS and
  the multi-second residential-proxy handshake to Google. In-turn warming is
  too late for that turn. `GET /api/warm`, pinged on load/focus/tab-return,
  opens the tunnels first; it must stay free of API spend (connections only,
  no search, no prompt).

## Invariants (`scripts/check.py` enforces these)

- One-way `tfs` links stay byte-identical to a real Google-issued URL.
- Time is genuinely priced: a cheaper flight that is hours longer must lose.
- An explicit "cheapest"/"fastest" request is obeyed verbatim, and "Best value"
  only appears when we actually ranked by value.
- The outright cheapest stays inside the preview; the shipped cut always keeps the
  cheapest, the fastest, and up to 8 nonstops, in value order.
- Cabin/dates/stops split the search-cache key; only cosmetic fields do not.
- Multi-city quotes the cheaper of one-ticket and separate-ticket, and says which —
  and never quotes a combined price above the purchasable per-leg sum: the
  **separate-ticket floor** (cheapest priced flight per leg, from the parallel
  `leg_futs` searches) ships as its own honestly-labeled option when it wins,
  its DIFFERENT-flights warning intact, while every one-ticket card keeps its
  own price (a displayed price belongs to the itinerary displayed beside it).
  Every separate-ticket quote books leg by leg on each leg's own deep link,
  never on a one-ticket or multi-city search page priced at the joint fare.
- Multi-city ships every leg's own one-way result set; a leg that fails never
  erases the legs that worked (it is retried once, then named); all legs share
  one deadline inside the agent's 55s batch; combos are value-ranked and keep
  the outright cheapest pairing; the route text keeps every city of an
  open-jaw trip.
- A round trip Google refuses never ends with nothing while any shape is
  answered: a late full retry, each airport as its own round trip, then both
  directions as one-ways (bookable separate-ticket totals, Google's
  round-trip page linked), all inside the agent's 55s batch.
- "No service" is licensed only by a search Google ANSWERED with no flights.
  A refused or failed search, or probe, is reported as not verified — never
  as absence — and a multi-airport one-way that comes back with nothing is
  re-run per airport pair before anyone is told it is empty.
- Rolled dates stay coherent: a return, arrival target, or flexible `to_date`
  can never land before the (possibly year-rolled) date it depends on.
- A flexible-dates grid covering less than half its window is retried, then
  rebuilt per airport pair when multiple airports are in play, and ships
  with a coverage warning if still short — never as if complete.
- Prices are the **party total** for the searched travelers (server-verified:
  2 adults + 1 child priced 3.00x solo), every Book link carries the same
  party in `f8`, and every "per person" label switches to "total for N
  travelers" the moment the party exceeds one.
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
- A part of a one-way search Google refused is never silently dropped: it
  ships as a backfill piece (with Google's own link to it), is cached 20s
  rather than 240s, is asked for again after the turn and merged by the
  same ranking the turn used, and until it lands every superlative in the
  reply is scoped to what loaded.
- Trip briefings are owner-only: every `/api/watches` route but unsubscribe
  answers 401 without the site key, and on Vercel an unset `OWNER_KEY` turns
  the whole feature off (503) rather than open. The cron runs only with
  `CRON_SECRET`. A check that got no fares never sends an email (the next run
  retries), a trip whose dates have passed stops itself, a threshold
  suppresses a briefing until a price moves by at least that much, and every
  recipient gets their own one-click unsubscribe token (RFC 8058 headers)
  that cannot touch anyone else's subscription. Only a POST unsubscribes;
  a GET (what mail scanners send) shows the confirm button and changes nothing.
  A briefing never prices part of a trip as the whole: refused airports are
  retried on the server first; a scheduled run that still lacks one sends
  nothing, a forced one says what is missing everywhere it names a price,
  and only checks covering the same airport pairs are ever compared.

## Stack

| Layer | What |
|---|---|
| Hosting | Vercel serverless (Python), auto-deploys on push to `main` (`EvanBurkeen/flight-search`) |
| Backend | [api/index.py](api/index.py) — FastAPI. `/api/search/stream` (SSE) is what the frontend uses (prose types first, cards land on `done`; sends `: open` immediately then `: ping` every 15s so the client can tell a live-but-slow turn from a dead socket), with `/api/search` (JSON) as its automatic fallback and the prod-verification probe; `/api/returns` prices every return Google pairs with ONE outbound (no model in the loop — the board's tap-to-price, ~1-4s); `/api/backfill` re-asks Google for one refused part of a one-way search after the turn and merges it into the rows the browser sends back (no model, stateless); `GET /api/warm` opens the Google + Anthropic tunnels before the first search (no API spend), pinged by the frontend on load/focus/tab-return |
| LLM | `claude-sonnet-5` agent loop (near-Opus on tool-driven work, 40% less per token; `ASSISTANT_MODEL` env var overrides it — set `claude-opus-4-8` in Vercel for an A/B, no code push), `max_retries=4`, effort `medium`, adaptive thinking on by default (hence `max_tokens` 8000: it caps thinking + text together); prompt caching: system+tools at 1h TTL (the only prefix that survives across turns), plus a 5m breakpoint on the conversation tail so a turn's later calls read its earlier messages at 0.1x — WITHIN-turn only, since the client replays history as bare prose and the wrap-up's `tool_choice: none` invalidates the messages tier (so that call sends no tail marker); plainly-route-shaped queries emit their first tool call via `claude-haiku-4-5` (no `output_config` — Haiku rejects effort), with an automatic loop-model redo if Haiku answers in prose instead of calling the tool |
| Flight data | [`fli`](https://github.com/punitarani/fli) (PyPI `flights`) — reverse-engineered Google Flights |
| Web context | Anthropic server-side `web_search` tool (max 3/turn) for event dates, venues, etc. |
| Coordinates | `airportsdata` (IATA → lat/lon) for route maps |
| Frontend | Single static [public/index.html](public/index.html), Alpine.js, no build step |
| Map data | [public/world.js](public/world.js): Natural Earth 110m land + lakes as SVG paths |
| Trip briefings | `/api/watches` (owner-only CRUD + send now), `/api/watches/unsubscribe` (per-recipient token), `/api/watches/run` (the daily Vercel cron, `0 12 * * *` = 12:00 UTC, ±59 min on Hobby); storage is Upstash Redis over REST (a JSON file in the temp dir locally), email is Resend's HTTP API (written to a local outbox folder when no key is set) |

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
   - serialize up to 50 one-ways / ~6 round-trip OUTBOUND groups (a
     REPRESENTATIVE set — cheapest anchors, nonstops, fastest, departure-time
     spread via `representative_outbounds`, because fli expands `flights[:top_n]`
     in sort order and "the 10 cheapest" once hid every nonstop; each group
     carries its own `return_options`, every option priced as the real total
     for that pairing, PLUS up to 24 `more_outbounds` — every outbound Google
     listed but we did not expand, shipped with its honest "from" total) /
     8 multi-city itineraries (priced BOTH as one ticket and as
     separate tickets, quoting the cheaper — `price_basis` says which; a
     separate-ticket quote books per leg) PLUS every leg's own one-way
     result set as `legs`, the per-leg board's data, each with a `status`
     and `detail` — every item with a `tfs` deep link to that exact
     itinerary, alliance tag, aircraft,
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
shipped, which is why the cut keeps nonstops/cheapest/fastest) · **bag-aware
pricing** (say "with a carry-on" or "one checked bag" and Google reprices
every fare to include the fee, with a section note that the shown prices are
bag-inclusive) · **price
context line** above a fixed-date section (where its cheapest fare sits among
nearby dates, the cheapest nearby date, and an explicit "not price history"
footnote) · round-trip
**mix & match board** (the DEFAULT round-trip view since Aug 2: every
outbound on the left — tapping a from-priced row prices its returns IN
PLACE via `/api/returns`, ~1-4s, no model turn, with an ask-the-concierge
fallback — the selected outbound's returns on the right, each priced as the
real pairing total and filterable/sortable, "Load every return" for the
full Google list, sticky chosen-pairing bar with Book; "Show top picks
only" collapses to the pre-paired cards) · multi-city **leg board** (the
DEFAULT multi-city view since Nov 11: one column per leg with that leg's full
one-way list under the shared filter bar, a sticky separate-ticket total with
Book leg N on each leg's own link, the cheapest one-ticket option named, and
a one-tap re-search for a leg that did not come back) · Best value badge ·
Book deep-links straight to the chosen itinerary on Google · flexible-date grids show best-value dates
first (within 15% of cheapest) and expand to month calendars heatmapped by price
(tiers relative to the window's cheapest; cheapest days outlined; round-trip
pairings in tooltips) · per-flight atlas maps (land+lakes, graticule,
sequential longitude unwrapping so every leg takes the short way; outbound solid,
return dashed; layover dots with durations) · timeline layover rings · suggestion
chips · search ladder (jump-to index of every results section: fixed rail on
wide screens, an index button inside the compose bar elsewhere) · **memory across reloads** (the
conversation and its freshest cards restore on load; the landing shows
recent-trip chips — route, date, the fare you saw and how long ago — that
re-search live on tap; New trip clears the chat, keeps the recents) ·
**streaming
replies** (the reply types out once the answer is settled — a preamble that
turns out to precede a search is never shown at all — at an eased, bounded
cadence, then the cards rise in, then the chips; automatic fallback to the
JSON turn on any transport problem) · **"Send this
flight"** (a quiet Share beside every Book renders a boarding-pass PNG on
canvas — trip type, route with flight-arc motif, segment rows with durations,
date-range + price + barcode stub, flights.evanburkeen.com footer — with copy
image / copy text incl. booking link / save / native share) · degraded round
trips render a notice + tap-to-price board instead of a blank section ·
**design system** (Nov 11 redesign): one typeface (Inter, variable weights),
warm stone neutrals with white surfaces, ink-coloured figures, one burnt-orange
accent for action and selection (Book, send, chosen rows, the brand mark),
green for good fares, amber for caution; every colour is a token redefined for
dark mode, including the route maps; tabular numerals on all figures; the
landing offers tappable example prompts that fill the compose box; real
airline logos (Google's own marks, the IATA code underneath as a fallback);
every results section opens with a type icon, a title built from the search
itself (place names, then codes, trip type and dates with the year) and tags
for every filter; the concierge's replies carry the brand mark as an avatar;
the best-value card is outlined in the accent · **late airports**
(when Google refuses part of a one-way search, the section says so and the
page asks again 5s, 20s and 45s after the reply; flights that arrive are
merged and re-ranked in place, with a note naming the new best value; if
Google keeps refusing, Try again and a link to that exact search on Google
Flights; a fully refused search still shows as its notice) · **trip briefings** (owner
only, invisible to visitors: the footer's "Trip briefings" link takes the
site key once per device, kept in localStorage as `fa_owner_key`; then a
Watch button sits on every results section and a Briefings button in the
header. Watch takes recipients as chips (you plus friends, up to 10, comma or
Enter), a cadence of Daily / Every 3 days / Weekly, and an optional "only
when a price moves by $X"; the first briefing goes out at once. The email
names the best-value, cheapest, fastest (as a footnote when it costs more
than twice the best value) and cheapest-nonstop flights with Book links, what changed since the last briefing, a cheaper nearby date when
the price context found one, a trend strip once there are 3 checks, a short
summary written by Haiku from the numbers only, and an "Open in Flight
Assistant" link (`/?q=<the query>`, which runs the search on load). The
Briefings panel edits recipients, sends now, pauses, resumes and stops.)

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt uvicorn
.venv/bin/python scripts/dev_server.py   # API + frontend on :8123
.venv/bin/python scripts/check.py       # invariant checks (the hook runs these)
```

No `ANTHROPIC_API_KEY` locally → the Claude loop is stubbed by a pattern parser
(`scripts/dev_server.py`); searches still hit live Google. Stub grammar: airport
codes, "round", "flex/weekend", "compare", "multi A B C", "carry-on/bag" and
"checked" (common English words that are also IATA codes — THE, FOR, AND — are
ignored). The stub emits the same SSE events as the real loop, so streaming is
fully exercisable locally.

Trip briefings work locally with no accounts: the site key is `dev` while
`OWNER_KEY` is unset (never on Vercel), watches live in
`$TMPDIR/flight_watches.json` (`WATCH_LOCAL_STORE` overrides), and emails
are written as HTML files to `$TMPDIR/flight_outbox/` (`WATCH_OUTBOX`
overrides) instead of being sent. Open one in a browser to review a briefing.

## Operations

- **Pin what you patch or display.** Vercel can keep a cached install of an
  unpinned package (Nov 11: an airport table that named HAN "Hà Nội", in no
  published release). `airportsdata` and `flights` are pinned; check.py fails
  if the pins drift from the local install. Upgrade them deliberately, then
  re-run the checks.
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
  round trips v2 — so never "simplify" back to one session per identity.
  Every identity leaves through the proxy: a direct-from-Vercel fallback was
  tried Nov 11-12 and refused 0 for 4, then removed. `debug_timings.attempt_outcomes`
  counts every attempt's ok/refused/empty/error since the process started;
  the refused share is the number to watch when judging a proxy change.
  Any refusal RETIRES that identity, so the retry is a genuinely new
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
- **Trip briefings setup (one time, Evan only; keys never go in git or chat):**
  1. Resend (resend.com): add and verify a sending domain (DNS records at the
     registrar; a subdomain such as `mail.evanburkeen.com` keeps it apart
     from personal mail), create an API key, and set `RESEND_API_KEY` and
     `BRIEFING_FROM` (e.g. `Flight Assistant <briefings@mail.evanburkeen.com>`)
     in Vercel. Until a domain is verified, Resend's sandbox sender
     `onboarding@resend.dev` delivers ONLY to the Resend account's own
     address, so friends get nothing. Free tier: 3,000 emails a month, 100 a day.
  2. Vercel project, Storage tab: add Upstash Redis (free tier is plenty).
     It injects `KV_REST_API_URL` and `KV_REST_API_TOKEN`; the code also
     accepts `UPSTASH_REDIS_REST_URL` / `_TOKEN`, and either pair under a
     custom prefix from the connect dialog (`STORAGE_KV_REST_API_URL`).
     Tick Production when connecting, and redeploy after. If it still says
     "isn't connected", the error lists the storage variable NAMES the
     deployment can see (never values), which tells you which step failed.
  3. Set `OWNER_KEY` (a long random string, e.g. `openssl rand -hex 32`) and
     `CRON_SECRET` (another one; Vercel sends it as `Authorization: Bearer`
     to the cron), then redeploy.
  4. On each device, footer "Trip briefings", paste the site key once.
  Hobby runs the cron once a day within the scheduled hour, which is why
  Daily is the fastest cadence; a watch is due again after its period minus
  3 hours of slack, so cron jitter never skips a day. One cron run checks
  due watches 3 at a time inside a 240s budget (`maxDuration` is 300s); a
  watch that misses the budget is simply due again tomorrow. A watch whose
  search has a refused part spends up to 80s more winning it back
  (`BRIEFING_COMPLETE_S`), so "Send now" can take a minute or two during a
  refusal wave; that is the price of never mailing half a trip.

## Changelog

**November 12, 2026 (ask Google less)**
- Evan: "what do I need to change to prevent google from refusing
  production... do what you think is best". Nothing in code can prevent it
  while we scrape; what we control is how much we ask. Measured from a
  home connection with every request counted: a healthy one-way JFK->ORD
  made 5 requests to Google where 1-2 answer it, and a round trip made 12.
- The carrier probe now fires only on a THIN session (fewer than 15 rows,
  or under half the route's carriers with fewer than 40 rows,
  `MANIFEST_PROBE_MAX_ROWS`). On busy routes Google's manifest is mostly
  codeshare sellers (JFK->ORD lists Iberia, Saudia, Gulf Air...), so the
  old "covered < 0.5" rule fired on 122 healthy rows, cost a second
  ladder, and produced the misleading "Iberia... serves this route" note.
  The Beijing case it was built for (6 rows) still probes; big routes are
  guarded by the completeness sentinel (the grid's price for the day).
- Round trips pre-price 6 outbounds, not 10 (12 requests became 8): the
  picker still keeps top picks, each airport, a nonstop, the fastest and a
  departure spread, which is all the model ever sees; every other
  outbound ships from-priced and prices on tap.
- A refused round trip's one-way insurance (two searches plus their date
  grids) used to fire at once, and was thrown away whenever the late retry
  answered, which is usually. It now waits for that retry
  (`RT_LEGS_GRACE_S`, 8s past it) and is bought only if it failed.
- Re-measured after: JFK->ORD 1 search request (plus the grid), a round
  trip 8 instead of 12.
- The direct-from-Vercel fallback is gone: Google refused it every time
  prod used it (0 for 4). `debug_timings.attempt_outcomes` now counts every
  attempt's ok/refused/empty/error per process, the number to watch when
  judging a proxy change.
- Tested and ruled out: a bad browser fingerprint. Safari looked guilty
  (refused 2/2) but passed 3/4 on another route while plain Chrome was
  refused once; about 1 in 4 first attempts is refused at random even from
  home, which the ladder absorbs.

**November 12, 2026 (a briefing never prices half a trip as the whole)**
- Evan's first delivered briefing, "New York to Hanoi: $817 best value",
  was built on EWR alone: Google had refused JFK and LGA for that check.
  It called United $817 the best value and Delta $704 the cheapest while
  JFK had Korean Air at $473, it became the baseline (so the next full
  check would have announced a fake "$344 drop"), and it showed a $6,581
  "Fastest" to save 2h 45m. "wrong info AGAIN!!!" The page's backfill
  could not help: an email has no page to ask again.
- `complete_for_briefing`: the backfill, run on the server before anything
  is written, for up to 80s per watch (gaps 4s/12s/25s, alternating shape,
  merged by `rank_and_cut_one_way`).
- If a part still will not load: a scheduled run sends NOTHING (status
  `incomplete`, named in the panel, retried next run); "Send now" sends,
  with the missing part in the subject ("best of what loaded"), an amber
  banner, "(of what loaded)" on every label, and "Not compared".
- Snapshots record `coverage` (the airport pairs priced) and `missing`.
  Two checks compare only with the same coverage (`briefing_comparable`);
  a partial check never becomes the baseline or a trend point, and a
  snapshot from before coverage existed starts a fresh baseline. Evan's
  existing watch resets that way on its next full check.
- A "Fastest" costing more than 2x the best value is a footnote line
  ("$5,764 more than the best value to save 2h 45m"), not a pick card.
- 7 checks pin it.

**November 12, 2026 (first real briefing: Resend 403, error code 1010)**
- Evan's first live briefing failed for all three recipients with "Resend
  refused the email (403): error code: 1010". That is not Resend: it is
  Cloudflare in front of Resend's API refusing the client's signature, and
  urllib's default `Python-urllib/3.x` User-Agent is blocked. Verified with
  no key at all: the default UA got 403/1010, a named UA reached Resend and
  got its own 401 "Missing API Key". Email and storage calls now send
  `flight-assistant/1.0` (`_HTTP_UA`); one check pins both.

**November 11, 2026 (the airport Google turned away)**
- Evan, with Google Flights side by side: "nyc to hanoi march 4 ... clearly
  these aren't the best flights. wtf happened?" Our list had LGA and EWR
  only; Google's top picks were all JFK (Asiana $442 and Korean Air $473
  with one stop). The reply called a $817 United flight "the only one-stop
  on offer" and Alaska "the cheapest fare overall".
- Read from prod, not guessed: the combined search was refused, the
  per-airport rescue got LGA (76) and EWR (98), and JFK->HAN was refused
  even alone (8 of 8 attempts over two turns, ~95-byte bodies). The date
  grid was refused too, so the completeness sentinel had nothing to compare
  against. Control: from a home connection every shape answered first time
  (JFK->HAN 91 flights in 0.3s, all three airports 265). Same request,
  different asker. Prod was in a broad wave too (successes on attempts 2-4,
  JFK->LAX 46s). LGA/EWR matched home exactly, and Air India ($403/$421)
  was missing from BOTH: that is open bug 1 (withheld fares), not this.
- Fixed as a class, five ways:
  1. **Ask again later.** A part of a one-way search Google refuses
     (`_note_missing_piece`: a rescue pair, a probed airport, the whole
     search) ships as a `backfill` piece. The page asks for it again 5s,
     20s and 45s after the turn via the new `POST /api/backfill`, which
     merges the browser's rows with the new ones and re-ranks them with the
     turn's own rules (`rank_and_cut_one_way`, now shared). In-turn retries
     all land inside the same burst; this is the one retry that outlives it.
     It also alternates SHAPE: even attempts ask for the piece alone, odd
     ones re-run the whole search and keep the piece's rows. Post-deploy,
     prod showed why: JFK->HAN alone was refused 4/4, answered 3/3 a minute
     later, refused again the minute after, while the combined search
     containing it was answered first time.
  2. **Ask from somewhere else.** After two proxied refusals in a ladder,
     the third attempt goes out directly from the function (`go_direct`,
     tagged `direct` in `search_diag`), capped at 10s. A refused direct
     attempt rests that route for 300s, so it is a rare fallback, never a
     second pool. `debug_timings.direct_route` counts its ok/refused/error
     per process: read it before deciding whether to use it earlier.
  3. **Do not cache a partial answer.** A payload with backfill pieces is
     cached 20s (enough to merge duplicate calls in one turn), not 240s.
  4. **Do not rank a partial answer as whole.** STILL LOADING in the
     message and a standing prompt rule: every superlative is about what
     loaded and says so, and the reply says the rest may still appear.
  5. **Say what changed.** The section shows a status note: retrying,
     then what the late airport added and the new best value (computed from
     the numbers), or, if Google keeps refusing, a Try again button and a
     link to that exact search on Google Flights. The cross-turn ledger is
     updated so the next reply knows the late flights are on screen.
- The owner's Briefings header button is hidden at phone width (it pushed
  the view toggle off-screen); the footer link opens the same panel.
- Backfill never adds volume to a wave: it stands down while the
  process's breaker is open and asks lean (two attempts per ladder), since
  the page asks again anyway.
- Post-deploy evidence, same night: production then refused EVERYTHING,
  the JFK->ORD control included (0 rows, no success in 4 minutes on that
  instance), while this laptop got JFK->ORD in 0.3s. The direct route was
  refused too (0 for 2, a 322-byte body unlike the proxy's ~95). So the
  direct route is not the cure; it stays as a rare, rested, counted
  fallback until `direct_route` says otherwise. Proxy-wide waves are the
  real open problem (open bug 2).
- 21 checks under "The airport Google turned away"; README drift checks
  pin `/api/backfill` and the direct-route cooldown.

**November 11, 2026 (briefing storage setup, first real run)**
- Evan's first setup hit "Briefing storage isn't connected yet" with
  Upstash added. The code only read the exact names `KV_REST_API_URL` /
  `UPSTASH_REDIS_REST_URL`, and Vercel's connect dialog can prefix them;
  the error gave no way to tell a prefix from a missing redeploy or an
  unticked Production box. `_kv_conf` now matches either pair by suffix
  (unprefixed wins), and the error lists the storage variable names the
  deployment can see, never values, and says when it sees none at all or a
  plain Redis URL with no REST pair. Three checks pin it.

**November 11, 2026 (trip briefings by email)**
- Evan: "I want a button which will allow me to get X frequency emails
  thats a briefing like 'here's the flight or trip you asked me to look at,
  here is the best flight, cheapest flight, here's how it's changed'", for
  himself, with friends' emails added as recipients. Shipped as **trip
  briefings**: Watch on any results section, Daily / Every 3 days / Weekly
  (Vercel Hobby crons run once a day, so Daily is the floor), optional
  "only when a price moves by $X", a short Haiku summary written from the
  numbers.
- Re-runs the SAME search, not a re-asked question: `execute_spec` now
  ships `watch_spec` (the rolled spec, minus prose fields) and the watch
  stores that, so a briefing compares like with like and never pays for a
  model turn to search. Section titles come from `section_header`, so the
  email and the panel name the trip exactly as the page did.
- Owner-only without logins: a private site key (`OWNER_KEY`, sent as
  `X-Owner-Key`, compared in constant time) gates every write; visitors see
  no Watch button at all. On Vercel an unset key turns the feature off
  rather than open. Friends never need the site: each recipient gets an
  unsubscribe token of their own, honoured by the confirm button behind the
  footer link or the mail client's one-click List-Unsubscribe. Opening the
  link only shows the button: mail security scanners (Safe Links, Mimecast)
  open every link in a message, and a link that acted on open would drop a
  friend the first time a briefing reached a corporate inbox.
- Honest by construction: a run with no fares sends nothing, the "since
  last time" deltas compare against what was last SENT (not merely
  checked), the nearby-date line comes from the existing price context,
  and the fallback summary (no API key) is a plain restatement of the
  same numbers.
- `/?q=<query>` now runs that search on load, so the email's "Open in
  Flight Assistant" lands on live results.
- 19 new checks under "Trip briefings" pin the gate, the unsubscribe
  isolation (and that merely opening the link changes nothing), the no-data
  rule, expiry, the threshold, HTML escaping, cron auth and the frontend
  wiring; README drift checks pin the new routes, env vars and cron schedule.

**November 11, 2026 (the round trip Google would not answer)**
- Evan: "is round trip NYC-Hanoi March 4-20 cheaper?" got "Google didn't
  answer", twice, and then an OFFER to search one-ways. "The whole value add
  of this website is reliability." The new per-attempt verdict named it in
  one read: the three-airport round trip was refused 16/16 across two turns
  (Google's ~96-byte error body, 1-3s each) while the JFK->ORD control
  passed. Bisected in prod and locally: JFK->HAN alone and the one-ways were
  answered minutes apart; repeated trials showed no deterministic trigger
  (sort, airport) — refusals come in BURSTS per request shape, and the
  ladder's four attempts land inside one burst.
- Fix, as a class: a search Google refuses is tried as DIFFERENT shapes,
  spread across the window, and ships whatever is real.
  - `run_search(..., refusal_exit=2)`: when a decomposition is waiting
    (every round trip, every multi-airport one-way), two consecutive refusals
    hand over instead of burning the turn on the same shape.
  - `rescue_round_trip`: in parallel, the full round trip again 12s later
    (`RT_LATE_RETRY_AT_S`), each airport pair as its own round trip, and both
    directions as one-ways (each with its own airport rescue), all done by
    48s (`RT_RESCUE_END_S`). Preference: full > per-airport (merged, the
    refused pairs named, never "no service") > two one-ways.
  - `round_trip_as_one_ways`: the last resort ships both directions on the
    per-leg board with real, bookable separate-ticket totals, Google's
    ROUND-TRIP page for the bundled price, a "Priced as two one-way tickets"
    tag, and a note telling the model the one-ways are already done.
  - Prompt: when a search fails and an obvious alternative exists, run it in
    the same turn; never end a reply offering a search you could have run.
- Measured live on the exact failing round trip (which got 0 before): three
  runs, three answers: per-airport rescue 10 pairings + 24 outbounds in
  17.2s; late retry 7 pairings in 26.0s; first try 3 pairings in 12.4s.
  6 checks added.

**November 11, 2026 (Hà Nội, and the reply that changed its mind)**
- Post-deploy verification of the section titles passed its own test (two
  sections, titles built from the search, no "verify" section, 15s turn) and
  surfaced two more problems:
  - **Production titled HAN "Hà Nội" and SGN "Hồ Chí Minh City"** while
    every published `airportsdata` release, old and new, says "Hanoi". The
    route echo proved prod's airport table itself carried those names: an
    UNPINNED dependency let Vercel keep a copy nothing here had verified.
    `airportsdata` and `flights` (whose internals index.py patches) are now
    pinned to the versions the checks run, a check fails if they ever differ,
    and `debug_timings` reports the airportsdata version.
  - **The reply stumbled** ("the best value is China Airlines... actually,
    the top pick is Asiana", "$442 ."). With adaptive thinking a reply can
    arrive as text blocks split mid-sentence, and `"\n".join` broke them.
    `join_text_blocks` joins fragments as one answer (space only where words
    would collide, none before punctuation); the prompt now says to finish
    weighing before the first word and never revise mid-answer.
  5 checks added.

**November 11, 2026 (verify date year context, and less sterile)**
- Evan's screenshot had a third results section titled "verify date year
  context": the model's note to itself, shown because section titles came
  from its free-text `summary`. It had burned a search double-checking that
  "March 4" meant 2027, because the tool schema's frozen example ("Assumed
  2026 for 3/21") had taught it March meant a past year (hence the reply's
  odd "since March 2026 has already passed, I've booked this against next
  year"). Fixed as classes:
  - Titles are built from the search (`section_header`): "New York to Hanoi"
    over "JFK · LGA · EWR → HAN · One way · Thu, Mar 4, 2027", with tags for
    every filter (cabin, stops, airlines, time windows, travelers, bags) so
    comparison prongs stay distinguishable. Scratch text cannot reach the
    screen.
  - The cache/dedupe key rolls dates first, so "2026-03-04" (rolled) and
    "2027-03-04" are one search, and a repeated search inside one turn adds
    no second section (the richer result wins).
  - The prompt head states the year rule with a LIVE example computed from
    today and forbids searches that only double-check something; the frozen
    schema example is gone.
- Design, on Evan's "looks a little sterile": real airline logos on cards,
  compact cards and board rows; a type icon tile on every section; the brand
  mark as the concierge's avatar; the best-value card outlined in orange; a
  warmer stone ground for more depth against white cards; on phones the
  per-section Detail/Compact switch hides so titles keep the full width.
  7 checks added.

**November 11, 2026 (the redesign: a product, not stationery)**
- Evan: "redesign the website, make it look prettier, not like AI slop but a
  professionally completed webpage", then mid-pass: "I want a warmer color,
  not hospital-like". The old look was the house style of generated pages
  (beige paper texture, Fraunces italics, terracotta, brass, uppercase
  letter-spaced labels, ✦ ornaments). Replaced wholesale with a token-driven
  system: Inter only (Fraunces dropped, one fewer font download), warm stone
  neutrals and white surfaces, warm-black ink, burnt orange (#c4470f, 4.9:1
  on white) as the single action colour, green for good fares, golden amber
  for warnings. The first pass used cool greys and cobalt; Evan's note moved
  it warm without returning to cream.
- Structure: a header with a drawn mark (a route arc between two points),
  segmented view controls, an icon theme toggle; a landing with a plain
  headline, recent trips, and tappable example prompts that FILL the compose
  box rather than send; flight cards with an ink price hierarchy, a dotted
  timeline, quiet semantic badges and an orange Book button; a pill compose
  bar with an icon send button.
- Route maps now take their colours from CSS classes, so they follow the
  theme (they were hard-coded sepia "atlas plates" in both themes); the share
  card PNG uses the same palette, with the ✈ emoji and serif removed.
- Two layout bugs found while verifying, each now pinned: the multi-city
  one-ticket link collapsed because its `display` lived in an inline style
  that `x-show` rewrites, and the floating "Searches" button sat on top of
  the sticky board bars (the index now opens from a button in the compose
  bar). Also: the board bars group Book and Share on the right, and the
  compose box no longer shows a scrollbar under a wrapped placeholder.
- Verified locally in both themes and at phone width across every result type
  (one-way, round-trip board, date grid, multi-city board). No behaviour
  changed; every class name and checked string survived.

**November 11, 2026 (NYC to Hanoi, Saigon to NYC: the leg that was never there)**
Evan's multi-city NYC→Hanoi Mar 4 / Saigon→NYC Mar 20: the first attempt
showed nothing but a Google link, and the retry's nine combos used TWO of
the 120 NYC→Hanoi flights Google had. Reproduced in production first (two
runs: one shipped zero results twice in a 76s turn, "leg 2 came back
empty"; one shipped 8 combos as "JFK → HAN → JFK"). Then the controls, which
moved the diagnosis twice:
- **Saigon→NYC failed in prod as a plain one-way**, no multi-city involved:
  every attempt EMPTY, while the JFK→ORD control passed in the same window
  and the identical search returns 52 flights locally. Bisected in prod:
  Saigon→EWR alone 55 flights, **Saigon→JFK alone refused on every
  attempt**. A size theory (multi-airport long-haul responses are 1.3-1.5MB
  vs 224KB for JFK→ORD) was ruled out by the same bisection — the 522KB
  SGN→JFK response is the one that fails. Root cause on Google's side is
  still unknown; see KNOWN OPEN BUGS.
- **The wire verdict.** "EMPTY" conflated two different things. A refusal
  parses to no chunk at all; Google genuinely answering "no flights" is a
  well-formed chunk with no rows. `run_search` now records a per-attempt
  verdict (`answered-empty` / `refused` / exception) on the thread
  (`search_verdict()`), on raised exceptions (`e.search_diag`), on every
  empty payload (`search_diag`), and in the turn's status lines — so
  `debug_timings` in prod says WHICH kind of empty it was.
- **"VERIFIED NO SERVICE" was not.** `probe_missing_airports` did
  `except Exception: rows = []` and called the result verified absence —
  even a probe that never reached Google. Now only an ANSWERED empty
  licenses it; a refused or failed probe ships "NOT VERIFIED ... NOT
  evidence of no service". Same rule for a whole search: one Google never
  answered no longer says "No flights found".
- **One refused pair no longer kills a multi-airport search** (the class of
  the Aug 8 HVN date grid: a combined request collapses to its worst
  member). A multi-airport one-way that comes back with nothing is re-run
  per airport pair inside the remaining time, merged, and every pair is
  reported honestly. Its own ladder is capped at 20s so the rescue fits
  the agent's 55s batch (`MULTI_COMBINED_BUDGET_S`, `SEARCH_END_S`).
- **Multi-city ships every leg whole, as a per-leg board.** Separate
  tickets are independent purchases, so choosing per leg is the honest
  model, exactly as for round trips. The legs (each a normal one-way with
  every honesty layer) ship as `legs`; the section opens ON the board (one
  column per leg, the shared filter bar, a sticky separate-ticket total,
  **Book leg N** on each leg's own exact deep link, the cheapest one-ticket
  option named in the bar); "Show top picks only" collapses to the combos.
- **A failed leg no longer erases the legs that worked.** `harvest_mc_legs`
  waits on every leg against ONE deadline (48s from the start of the
  multi-city search; the old fallback waited 50s per leg, in sequence),
  retries an empty leg once immediately, ships partial truth with the
  missing leg named for the model (and a one-tap "Search this leg again"
  on the card), and records each leg's outcome in `detail`.
- **Combos are value-ranked, not rank-zipped.** The fallback paired leg-1
  rank k with leg-2 rank k: unsorted, and it could omit the cheapest
  pairing ($1,095 purchasable, the list opened at $1,163). `pair_mc_legs`
  scores each leg's best few by effective cost and always keeps the
  outright cheapest pairing.
- **Every separate-ticket quote books per leg.** The floor option linked to
  Google's multi-city SEARCH page, and combos where the same flights were
  cheaper bought separately linked to the one-ticket itinerary — both open
  at the JOINT fare, so the card's price did not belong to the itinerary
  its button opened.
- Also: the open-jaw route text ("JFK → HAN → JFK" erased Saigon from the
  model's view) now reads "JFK/LGA/EWR → HAN, then SGN → JFK/LGA/EWR"; the
  model gets `legs_overview` (counts, cheapest, best three per leg) and a
  prompt rule to answer leg questions from it; the ledger records
  `leg_boards` so "the 12:00 Korean Air to Hanoi" resolves next turn; the
  success payload now reports `combined_probe` too.
- **Post-deploy verification found the turn still took 102s** (first round:
  both legs "still searching" at the 48s deadline; the model's retry then
  waited 42.5s on a probe budgeted 12s while the legs sat cached). Two
  efficiency bugs, fixed in a second push: (1) every multi-city search ran
  each leg TWICE — the price index's spec lacked keys the leg spec carried
  as None, and the cache key treated None and absent as different, so
  single-flight never collapsed them (also a family bug: the index priced
  one adult against a party-total fare). `_spec_key` now ignores None and
  the index searches with the leg's own spec. (2) `run_search` checks its
  budget only BETWEEN attempts, so the probe now runs on its own thread
  while the legs are harvested, and is waited on only until the legs are
  done or `MC_PROBE_WAIT_S` (25s) from the start, whichever is later.
  Measured locally after: 12.1s, legs at 1.4s/3.1s, 2 upstream one-way
  searches instead of 4.
- **The check suite had been failing since September**, blocking every
  push: three time bombs (a 2026-09-24 departure fli now rejects; a
  speculation check hard-coding "this year" for Sept 18). Fixed with
  computed dates. Two checks updated deliberately: the fallback's combo
  COUNT pinned rank-zip (intent kept: the cheapest separate pairing leads),
  and the children check counted duplicated source lines that are now one
  `_mc_leg_spec`. 32 checks added (291 -> 323).

**September 5, 2026 (bag-aware pricing: the cheapest fare that was not)**
- The cheapest headline fare is usually a budget carrier that charges for a
  bag, so ranking by it quietly lies to anyone with luggage. `search_flights`
  now takes `carry_on` (bool) and `checked_bags` (int); set them and Google
  reprices every fare to include the fee, which is the honest answer to "is
  this basic economy". Verified live (FLL->BOS): $90 becomes $135 with a
  carry-on + a checked bag, the real +$45 that used to be invisible, and a
  bag-inclusive search can flip which carrier wins.
- Threaded everywhere a price lives: the fares split the search cache and the
  price-context grid cache (never serve a bagless price for a bag search),
  ride `spec_echo` so tap-to-price returns stay bag-inclusive, and reach the
  multi-city per-leg searches. Every result payload carries `bags` and the
  model is told the prices include them; the card shows "Fares include 1
  carry-on + 1 checked bag" so nobody mistakes a repriced fare for the base.
  The prompt teaches the model to set bags from plain language and to ask once
  on a budget-heavy route when the user has not said. The default (no bags)
  is byte-for-byte unchanged. 8 checks added.
- Also: `scripts/check.py` went from ~59s to ~17s. Its grid and completeness
  sections drive the real retry ladders, whose per-attempt `breaker_wait`
  slept up to 8s each; that pause changes timing, not logic, so it is
  neutralized for the offline suite (`time.sleep` is left alone, since a
  monotonic-deadline busy-wait would spin without it).

**September 5, 2026 (the first search that took forever, or never returned)**
- Evan's #1, worst on phones and the exact thing that fails when showing the
  tool off: the first search after an idle spell either crawled or spun
  forever with nothing. Two independent root causes, each now guarded.
- **Never returns (the real bug):** on mobile the SSE socket dies SILENTLY
  when the radio sleeps or the network hands off (no error, no FIN), and
  `reader.read()` in `streamTurn` hung indefinitely because the only abort
  was user-stop. The fallback to `/api/search` fired on a thrown error, never
  on a stall. Added a client watchdog: 30s of TOTAL byte silence (the stream
  now sends `: open` immediately and `: ping` every 15s, so real silence
  means a dead socket) aborts and falls back on a FRESH AbortController
  (reusing the aborted one made the fallback fetch return nothing, found in
  test); a 110s ceiling with bytes still trickling gives up with a one-tap
  retry of the exact query instead of an endless spinner. The plain-turn
  fallback is itself time-bounded so it cannot become a second silent hang.
- **Takes forever (cold start):** Vercel spins the Python function down when
  idle, so the first request paid the process boot, a cold Anthropic TLS
  handshake, and the residential-proxy handshake to Google (seconds on its
  own). New `GET /api/warm` opens those tunnels ahead of the search with no
  API spend; the frontend pings it on page load, on compose focus, and when
  a backgrounded tab returns, so the first real search lands warm. Warming
  is best-effort across serverless instances but guarantees a hot one exists.
- Verified in the browser against a simulated dead socket: stall detected at
  30s, auto-recovered through the plain turn with real cards and no ghost
  bubble; happy path and stop() unaffected. 10 checks added.

**August 28, 2026 (the calendar was in the response all along)**
- Evan put Google's own full October calendar (18 priced days, HVN area to
  Nashville) next to our one-chip section and said iterate. The Aug 8
  fan-out was treating a symptom: the ROOT CAUSE is that `GetCalendarGraph`
  streams PER-SLICE wrb chunks — measured: 19 chunks, 31 day items, ONE DAY
  PER CHUNK, out of order (first chunk was Oct 30), ending in a sweep of 13
  unpriced placeholders — and fli's dates parser reads only the first
  chunk. Dense routes fit their whole month in chunk 1, which is why
  BDL→FLL looked fine and the "HVN is sparse" story survived; thin routes
  stream, so we shipped one day and told the user Google was being sparse.
  `merge_calendar_chunks` is the calendar edition of `merge_wrb_chunks`:
  union every chunk's day items, keyed by (date, return_date), a later
  entry winning only when PRICED (the placeholder sweep must not erase a
  priced day), sorted back into calendar order.
- Two traps found while landing it, each now pinned: chunk tails are
  HETEROGENEOUS (multi-origin responses interleave chunks whose tail is a
  bare count — the first union keyed on chunk[-1], ingested `31`, and
  overwrote a healthy chunk's days with it), so day items are identified by
  SHAPE (a list opening with a YYYY-MM-DD string), never by position; and
  an EMPTY combined grid was early-returning before the Aug 8 per-pair
  fan-out could run — empty is the extreme of thin, so multi-airport
  windows now always reach the rescue. Verified live: HVN→BNA 18/18 priced
  days matching Google's UI number for number, HVN/BDL→BNA 31/31 from the
  combined grid alone, round-trip pairs 21/21. The fan-out and the
  coverage warning remain as insurance for genuinely degraded sessions.
- The suite itself had a midnight bomb: a check hard-coded a departure date
  that fli's past-date validator began rejecting when the date passed,
  killing every check run since. All spec dates in check.py are now
  computed relative to today, and a "Suite hygiene" self-scan fails A WEEK
  BEFORE any remaining date literal expires — mechanism over instruction,
  again. (It immediately caught a second literal four days from blowing.)

**August 8, 2026 (one chip where seventeen belonged: the poisoned date grid)**
- Evan's screen: "mid-late December, HVN/BDL/NYC to FLL, best dates" produced
  a cheapest-dates section holding ONE chip (Dec 20, $146) while the same
  turn's fixed-date searches found $135 one square away. Two layers, both
  now fixed as classes:
  - **The ladder accepted any NON-EMPTY grid as complete.** The grid
    degrades exactly like the list (a documented lesson), but only an empty
    grid retried. Now a grid short of HALF its window retries as a fresh
    identity (richest attempt kept, data never discarded), and an incurably
    short grid ships with a GRID COVERAGE WARNING to the model plus an
    on-card note — never as if whole.
  - **A multi-airport grid collapses to its worst member.** Measured live:
    BDL→FLL alone returned 31/31 days; HVN alone returned 1; HVN+BDL+JFK
    returned 1 — one sparse-grid airport poisons the combined calendar.
    Same cure as probe_missing_airports on the list side: when the combined
    grid is thin, fan out per airport pair (bounded, ≤6 single-attempt
    grids in parallel), merge by date at the min price, and name any
    airport whose calendar is thin even alone — its gaps must never read
    as "no service" (fixed-date searches price HVN fine). Verified on the
    exact failing case: 1 day combined → 31/31 merged, HVN disclosed.
    `grid_probe` rides in the payload so the next diagnosis is free.
- Display: a bounded window (≤21 priced days — "mid-late December" is ~17)
  now shows EVERY priced day as chips by default, cheapest outlined;
  best-value collapsing is reserved for month-plus windows.
- Also answered in-session: "same question, different answer" is three
  stacked sources — the concierge plans its own searches each turn (LLM,
  temperature pinned by adaptive thinking), Google serves different slices
  per session (this very bug: a 1-day grid one run, fuller the next), and
  since Aug 8 a repeat ask inside one conversation carries the first
  answer's history + ledger, so the model legitimately treats it as a
  follow-up. The grid fix removes the largest data-side variance for
  date-hunt queries.

**August 8, 2026 (memory: the workspace survives a reload)**
- Evan's ask, refined together: "a way to look at preloaded routes — recent
  searches." The conversation (prose, suggestion chips, and the freshest
  cards) now persists per-device in localStorage and restores on load, and
  the landing screen greets a returning user with **recent trips** — small
  boarding-stub chips (route, date, "saw $124", "2d ago") that re-ask the
  original query through the normal search path when tapped.
- The honesty rules are the feature: a remembered fare always carries its
  age; a conversation resumed after 45+ minutes opens with a one-line note
  that its fares were quoted then; tapping a recent trip re-prices LIVE
  rather than re-showing stale cards; and nothing auto-refreshes on load
  (each refresh is a real proxied Google search — "preloaded" means the
  context is preloaded, not that bandwidth is spent silently).
- Mechanics: stored cards keep the ledger's own budget (full sections only
  on the last 2 card-bearing replies; older turns keep prose and chips);
  runtime keys (underscore-prefixed) are stripped on save and section
  anchors re-stamped on restore, so boards, maps, and filters rebuild from
  data; the model's `history` and `ledgers` restore too, so "book the
  JetBlue one" still works after a reboot; quota overflow degrades to
  transcript-only, then to session-only. A **New trip** header control
  clears the chat but keeps the recents. This is the no-login slice of the
  roadmap's "login + saved trips".

**August 7, 2026 (both open bugs closed, and the audit's harvest)**
A full-codebase audit (multi-agent review, every finding adversarially
verified before touching code) closed the two actionable KNOWN OPEN BUGS and
a set of smaller ones:
- **Multi-city now quotes what you can actually buy.** The exact-signature
  comparison (`leg_price_index`) stays authoritative when it fires, but it
  usually cannot (combined itineraries rarely reuse the standalone one-ways'
  flights), so the per-leg searches already running beside the probe
  (`leg_futs`) now provide a **separate-ticket floor**: when the cheapest
  purchasable per-leg sum beats every combined option it ships as its own
  "separate tickets" option (DIFFERENT-flights warning attached), pricier
  one-ticket cards name both numbers, and the model is forbidden from
  presenting a one-ticket price above it as best value. The success payload
  also gained `combined_check_url`, and the compact card stops hardcoding
  "as 2 separate tickets" for 3+ legs.
- **The Book link finally books the family.** `itinerary_url` and every
  caller (all four sites, plus `multi_city_search_url`, which the README's
  bug list had missed) now thread `adults`/`children` — and fixing that
  exposed a deeper bug: the tfs schema comment had **f8/f9 semantically
  swapped** (f8 is one varint per traveler, f9 is the seat), undetectable by
  the byte-identity fixture because 1-adult economy encodes identically both
  ways. Verified against Google's own tokens (see Lessons). Premium-cabin
  deep links are correct for the first time. Prices are party totals
  (live-verified 3.00x for 2 adults + 1 child), so payloads carry `party`,
  the message tells the model, and every "per person" label becomes "total
  for N travelers" when N > 1 — cards, board, and share passes alike. Leg
  sub-specs stopped dropping `children`; the price-context cache key now
  splits on it.
- **A crafted ledger could hang the server.** One oversized option stalled
  `ledger_context`'s halving loop at `[:1]` forever, before any model call —
  a single POST pinned the request and its SSE worker (reproduced, then
  fixed: the loop must shrink the body every pass or drop the ledger).
- **Dates roll coherently now.** Each date rolled independently, so "Aug 1
  to Aug 20" asked on Aug 7 became depart-2027/return-2026 — an impossible
  filter reported as "No flights found"; flexible windows could invert the
  same way (fli silently swaps them into an ~11-month grid). A rolled
  departure now drags its return/arrival/`to_date` forward with a visible
  note. Speculation had the mirror bug: past month/days were guessed with
  the CURRENT year, guaranteeing a cache miss against the router's rolled
  spec — a pure waste of a Google search per affected query.
- **The degraded multi-city section now renders.** `visibleSections` dropped
  sections with no rows, but the all-legs-empty fallback ships exactly that
  plus the Google link the prose promises ("it is in this section" — it was
  not). Sections carrying `combined_check_url` now show.
- **Stop actually stops.** Stopping before any text landed left a permanent
  empty reply bubble, and stopping during the typewriter drain (fetch
  already done) let results finalize after the "Search stopped" note; stop
  now invalidates the turn and drops the empty placeholder.
- **Wire diet:** the SSE `sections` frame shipped the full results payload
  the client deliberately discards (~340KB measured on a 10x20 round trip)
  — now a `{"count": N}` marker; SSE JSON compacts its separators (~7%);
  `route_points` coordinates round to 3 decimals (~110m, invisible at card
  size); `world.js` (21.5KB gzipped) no longer blocks first paint (defer)
  and is immutable-cached since it is `?v=` busted.
- **Paper cuts:** IME users' Enter no longer sends mid-composition; a chip
  or calendar tap no longer wipes a half-typed draft; share's image actions
  hide when the canvas render failed (Save no longer downloads a junk file
  from `href="null"`); `/api/returns` returns 400, not a raw 500, for
  unknown airport codes; multi-city ledger entries record the real route
  instead of the literal string " to ".
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
