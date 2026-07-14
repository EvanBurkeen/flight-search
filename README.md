# Flight Assistant

Conversational flight search at [flights.evanburkeen.com](https://flights.evanburkeen.com) —
talk to Claude, which pulls live Google Flights data to search, compare, and recommend.

## Stack

- **Backend** — Vercel serverless Python ([api/index.py](api/index.py)): FastAPI, an agentic
  Claude loop (`claude-opus-4-8`) with a `search_flights` tool (up to 5 searches per turn),
  [`fli`](https://github.com/punitarani/fli) (PyPI package `flights`) for reverse-engineered
  Google Flights access, `airportsdata` for coordinates.
- **Frontend** — a single static [public/index.html](public/index.html) (Alpine.js, no build
  step). [public/world.js](public/world.js) is a Natural Earth 110m coastline path used for
  the per-flight SVG route maps.
- **Deploys** automatically when `main` is pushed to GitHub (`EvanBurkeen/flight-search`).
  `ANTHROPIC_API_KEY` is set in Vercel project env only.

## Features

One-way / round-trip (true combined pricing) / multi-city (one ticket, 2–5 legs) /
flexible-date grids · arrival-day & arrival-time constraints (enforced app-side; Google's
own arrival filter is clock-hour based and unreliable) · routing filters (`via_airports` —
also the only trustworthy way to assert a routing doesn't exist) · alliances, airline
include/exclude, cabin, passengers, price caps · multi-option comparisons · per-flight route
maps · Detailed/Compact views (global + per-section) · stop/supersede a running search ·
conversation memory within a session.

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt uvicorn
.venv/bin/python scripts/dev_server.py   # serves API + frontend on :8123
```

Without `ANTHROPIC_API_KEY` the Claude loop is stubbed by a pattern parser
(see [scripts/dev_server.py](scripts/dev_server.py)) so search plumbing and UI
work end to end; searches still hit live Google Flights.

## Operational quirks (learned the hard way)

- **Google throttles cloud egress IPs in waves** — symptoms: HTTP 429 or persistently
  empty results while the same code works from a residential IP. Backoff retries are
  built in; if a block persists for many minutes, change `regions` in
  [vercel.json](vercel.json) to a different Vercel region for a fresh IP pool
  (iad1 was blocked → sfo1 worked, July 2026).
- **fli's `SortBy.BEST` intermittently returns `None`** — `run_search` retries and
  falls back to `CHEAPEST`.
- **Multi-city is slow** (Google expands each leg chain): `maxDuration` is 300s and
  the fan-out is capped at `top_n=4`.
- **Model truthfulness**: the assistant only sees the top ~6 results per search
  (`compact_for_model`), so the prompt + tool docs forbid "routing doesn't exist"
  claims unless a `via_airports` search ran.
