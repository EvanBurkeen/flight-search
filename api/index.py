import json
import os
import random
import re
import time

_PROCESS_START = time.monotonic()
_process_served = 0  # 0 while this process has not yet answered a request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime
from functools import lru_cache
from urllib.parse import quote

# must precede the fli imports: its default is 60s x 3 retries, which lets a
# Google tarpit hang a request for minutes
os.environ.setdefault("FLI_TIMEOUT", "15")

import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fli.models import (
    Airline,
    Airport,
    Alliance,
    DateSearchFilters,
    FlightSearchFilters,
    FlightSegment,
    MaxStops,
    PassengerInfo,
    SeatType,
    SortBy,
    TimeRestrictions,
    TripType,
)
from fli.models.google_flights.base import PriceLimit
from fli.search import SearchDates, SearchFlights
from fli.search.exceptions import SearchClientError, SearchHTTPError

# --------------------------------------------------------------------------
# Passing Google's doorman: identity pool + circuit breaker
#
# Measured July 24 2026 (100 interleaved requests, autopsy probes on every
# failure). Google refuses with HTTP 200 + a ~94-byte body (gRPC code 13),
# and refusals stack on two independent gates:
#
#   1. SESSION gate — binary. Once a cookie jar is flagged it fails 64/64
#      consecutive requests, while brand-new sessions fired in the same
#      seconds passed 20/32. Retrying on the same jar can never escape.
#   2. IP gate — probabilistic, and it ESCALATES with sustained volume from
#      one address: fresh sessions passed early in the run and were refused
#      later, purely because the address had accumulated suspicion.
#
# Gate 2 is why hammering backfires, and why the response is to back off
# (circuit breaker) rather than retry harder. Each identity below pairs its
# own cookie jar + residential exit + browser fingerprint, so a retry is a
# genuinely different visitor rather than the same one in a new coat.
# Warmup page-loads showed no benefit in the study, so identities are not
# pre-warmed (a warmup is ~1.8 MB of proxy bandwidth each).
# --------------------------------------------------------------------------
import secrets
import threading

from fli.search import client as _fli_client

_proxy_base = os.environ.get("FLI_PROXY") or ""

# Distinct, current fingerprints. Identical clones from one address are a
# pattern; a varied handful is not.
_IMPERSONATIONS = ("chrome", "chrome136", "chrome142", "edge", "safari180", "firefox135")

POOL_SIZE = 3
IDENTITY_MAX_STRIKES = 1  # session-stickiness is binary: one refusal retires it

_identities: list[dict] = []
_pool_lock = threading.Lock()
_local = threading.local()


def _make_proxy_url() -> str:
    """A fresh residential exit. IPRoyal takes a sticky-session suffix in the
    credentials; each identity gets its own so they don't share an IP."""
    if "iproyal" in _proxy_base and "@" in _proxy_base:
        creds, host = _proxy_base.rsplit("@", 1)
        return f"{creds}_session-{secrets.token_hex(4)}_lifetime-30m@{host}"
    return _proxy_base


def _new_identity(idx: int) -> dict:
    return {
        "idx": idx,
        "uid": secrets.token_hex(4),  # stable handle for logs and tests
        "proxy": _make_proxy_url(),
        "impersonate": random.choice(_IMPERSONATIONS),
        "session": None,   # built lazily on first use
        "strikes": 0,
        "born": time.monotonic(),
        "last_ok": 0.0,    # last successful use; drives warm-connection reuse
    }


def _identity_session(ident: dict):
    if ident["session"] is None:
        from curl_cffi import requests as _curl_requests

        s = _curl_requests.Session()
        s.headers.update(_fli_client.Client.DEFAULT_HEADERS)
        if _proxy_base:
            s.proxies = {"http": ident["proxy"], "https": ident["proxy"]}
        ident["session"] = s
    return ident["session"]


def checkout_identity() -> dict:
    """Bind a healthy identity to this thread for the duration of a search.

    Prefers the most recently SUCCESSFUL identity: its curl session still
    holds an open TLS connection to Google through the residential proxy,
    and re-establishing that costs seconds (measured: searches on a cold
    identity ran 3.5-6s in production vs 0.3-1.4s direct). Diversity still
    comes from retirement — a refused identity is destroyed and the next
    checkout necessarily picks a different one.
    """
    with _pool_lock:
        while len(_identities) < POOL_SIZE:
            _identities.append(_new_identity(len(_identities)))
        healthy = [i for i in _identities if i["strikes"] < IDENTITY_MAX_STRIKES]
        if not healthy:
            ident = _retire_all_locked()
        else:
            # warm (has a live session) and most recently used wins
            ident = max(healthy, key=lambda i: (i["session"] is not None, i["last_ok"]))
    _local.identity = ident
    return ident


def _retire_all_locked() -> dict:
    """Every identity is flagged — rebuild the bench and hand back a new one."""
    for i, old in enumerate(_identities):
        _close_identity(old)
        _identities[i] = _new_identity(i)
    return _identities[0]


def _close_identity(ident: dict) -> None:
    s = ident.get("session")
    if s is not None:
        try:
            s.close()
        except Exception:
            pass
    ident["session"] = None


def retire_identity() -> None:
    """Discard the identity this thread is using: new cookies, new exit IP,
    new fingerprint. Called after any refused attempt."""
    ident = getattr(_local, "identity", None)
    if ident is None:
        return
    with _pool_lock:
        _close_identity(ident)
        if ident in _identities:
            _identities[_identities.index(ident)] = _new_identity(ident["idx"])
    _local.identity = None


# Back-compat aliases: the retry ladder speaks in these verbs.
def rotate_proxy_session() -> None:
    retire_identity()


def reset_google_session() -> None:
    retire_identity()


# --- fli plumbing: route its requests through this thread's identity --------
def _session_from_identity(self):
    ident = getattr(_local, "identity", None) or checkout_identity()
    return _identity_session(ident)


_fli_client.Client._session = _session_from_identity

_orig_post, _orig_get = _fli_client.Client.post, _fli_client.Client.get


def _post_as_identity(self, *args, **kwargs):
    ident = getattr(_local, "identity", None)
    if ident:
        kwargs["impersonate"] = ident["impersonate"]
    return _orig_post(self, *args, **kwargs)


def _get_as_identity(self, *args, **kwargs):
    ident = getattr(_local, "identity", None)
    if ident:
        kwargs["impersonate"] = ident["impersonate"]
    return _orig_get(self, *args, **kwargs)


_fli_client.Client.post = _post_as_identity
_fli_client.Client.get = _get_as_identity


# --- circuit breaker -------------------------------------------------------
# Sustained volume during a refusal wave is what escalates a cheap session
# flag into an expensive IP burn, so consecutive refusals buy a pause rather
# than a faster retry.
_attempt_stats: dict = {"ok_on_attempt": []}  # profiling: which try succeeded

BREAKER_TRIP_AT = 3          # consecutive refusals across the process
BREAKER_COOLDOWN = 25.0      # seconds of quiet once tripped
_breaker = {"consecutive": 0, "open_until": 0.0}
_breaker_lock = threading.Lock()


def breaker_wait(cap: float) -> None:
    """Pause if the process is mid-wave, but never longer than `cap`.

    The breaker exists to stop US from hammering Google, not to punish the
    person waiting: a user's FIRST attempt gets at most a token pause, while
    retries (which are the actual hammering) absorb the real cooldown.
    Measured the hard way — an uncapped wait put results on screen at 32s.
    """
    with _breaker_lock:
        remaining = _breaker["open_until"] - time.monotonic()
    if remaining > 0:
        time.sleep(min(remaining, cap))


def note_search_outcome(ok: bool) -> None:
    if ok:
        ident = getattr(_local, "identity", None)
        if ident is not None:
            ident["last_ok"] = time.monotonic()  # keep this warm connection first in line
    with _breaker_lock:
        if ok:
            _breaker["consecutive"] = 0
            _breaker["open_until"] = 0.0
            return
        _breaker["consecutive"] += 1
        if _breaker["consecutive"] >= BREAKER_TRIP_AT:
            _breaker["open_until"] = time.monotonic() + BREAKER_COOLDOWN
            _breaker["consecutive"] = 0


# Google streams GetShoppingResults as PROGRESSIVE wrb chunks in one HTTP
# response: the first chunk is an early partial snapshot, later chunks are
# fuller re-renders of the same search (this is why the real UI "fills in"
# over a few seconds). fli parses only the FIRST chunk, which can miss whole
# carriers — ICN->HRB Jan 1 returned 1 of 3 nonstops (Jeju, no Asiana or
# China Southern; the full 34-row inventory sat in chunks 2-4). Patch it to
# parse the chunk with the most flight rows.
import fli.search.flights as _fli_flights
from fli.search._wire import iter_wrb_chunks as _iter_wrb_chunks


def _parse_richest_wrb_payload(body):
    best, best_rows = None, -1
    for inner in _iter_wrb_chunks(body):
        try:
            rows = sum(
                len(inner[i][0])
                for i in (2, 3)
                if isinstance(inner[i], list) and inner[i] and isinstance(inner[i][0], list)
            )
        except (IndexError, TypeError):
            rows = 0
        if rows > best_rows:
            best, best_rows = inner, rows
    return best


_fli_flights.parse_first_wrb_payload = _parse_richest_wrb_payload


# The old warm_google_session() page-load is gone: the July 24 study found
# unwarmed fresh sessions matched warmed ones (18/32 vs 14/32), so the warmup
# bought nothing and cost ~1.8 MB of proxy bandwidth per cold process.

app = FastAPI()

CABIN_MAP = {
    "economy": SeatType.ECONOMY,
    "premium_economy": SeatType.PREMIUM_ECONOMY,
    "business": SeatType.BUSINESS,
    "first": SeatType.FIRST,
}

STOPS_MAP = {
    "any": MaxStops.ANY,
    "non_stop": MaxStops.NON_STOP,
    "one_or_fewer": MaxStops.ONE_STOP_OR_FEWER,
    "two_or_fewer": MaxStops.TWO_OR_FEWER_STOPS,
}

SORT_MAP = {
    "best": SortBy.BEST,
    "cheapest": SortBy.CHEAPEST,
    "fastest": SortBy.DURATION,
    "departure_time": SortBy.DEPARTURE_TIME,
}

# Alliance rosters (reference data, IATA codes, 2026 — incl. SAS in SkyTeam)
ALLIANCE_MEMBERS = {
    "Star Alliance": [
        "A3", "AC", "AI", "AV", "BR", "CA", "CM", "ET", "LH", "LO", "LX",
        "MS", "NH", "NZ", "OS", "OU", "OZ", "SA", "SN", "SQ", "TG", "TK",
        "TP", "UA", "ZH",
    ],
    "oneworld": [
        "AA", "AS", "AT", "AY", "BA", "CX", "FJ", "IB", "JL", "MH", "QF",
        "QR", "RJ", "UL", "WY",
    ],
    "SkyTeam": [
        "AF", "AM", "AR", "AZ", "CI", "DL", "GA", "KE", "KL", "KQ", "ME",
        "MF", "MU", "OK", "RO", "SK", "SV", "UX", "VN", "VS",
    ],
}
AIRLINE_ALLIANCE = {code: name for name, codes in ALLIANCE_MEMBERS.items() for code in codes}

SEARCH_TOOL = {
    "name": "search_flights",
    "description": (
        "Search live Google Flights data. Call this whenever the user needs prices, schedules, "
        "or availability — never state a price or flight time you did not get from this tool. "
        "You can call it several times in one turn to compare options (different dates, routings, "
        "or a self-arranged stopover, one call per leg). Results are also shown to the user as "
        "cards, so reference them rather than repeating every detail."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "origins": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Origin IATA codes. Expand cities to their major airports, e.g. NYC -> [JFK, LGA, EWR], London -> [LHR, LGW, STN], Tokyo -> [HND, NRT].",
            },
            "destinations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Destination IATA codes, same expansion rule as origins.",
            },
            "trip_type": {"type": "string", "enum": ["one_way", "round_trip", "multi_city"]},
            "multi_city_segments": {
                "type": ["array", "null"],
                "description": "For trip_type multi_city: 2-5 legs in order, each priced together as one ticket. Example: NYC->London, London->Rome, Rome->NYC. When set, top-level origins/destinations/dates are ignored.",
                "items": {
                    "type": "object",
                    "properties": {
                        "origins": {"type": "array", "items": {"type": "string"}},
                        "destinations": {"type": "array", "items": {"type": "string"}},
                        "date": {"type": "string", "description": "YYYY-MM-DD"},
                    },
                    "required": ["origins", "destinations", "date"],
                },
            },
            "departure_date": {
                "type": ["string", "null"],
                "description": "YYYY-MM-DD. Null only when flexible_dates is set.",
            },
            "arrival_date": {
                "type": ["string", "null"],
                "description": "YYYY-MM-DD the outbound must LAND on (local time at destination). Use when the user cares about the arrival day ('land on Friday'). Remember timezones when picking departure_date: eastbound trans-Pacific (Asia -> US) lands the SAME local calendar day, so to land Friday you depart Friday; westbound (US -> Asia/Europe overnight) usually lands the NEXT day, so depart the day before.",
            },
            "return_date": {
                "type": ["string", "null"],
                "description": "YYYY-MM-DD for round trips with a fixed return.",
            },
            "flexible_dates": {
                "type": ["object", "null"],
                "description": "Set when the user is flexible ('sometime in September', 'cheapest weekend'). Finds the cheapest dates in the window instead of specific flights.",
                "properties": {
                    "from_date": {"type": "string", "description": "YYYY-MM-DD window start"},
                    "to_date": {"type": "string", "description": "YYYY-MM-DD window end"},
                    "trip_length_days": {
                        "type": ["integer", "null"],
                        "description": "For flexible round trips: nights between outbound and return.",
                    },
                },
                "required": ["from_date", "to_date"],
            },
            "cabin": {
                "type": "string",
                "enum": ["economy", "premium_economy", "business", "first"],
            },
            "adults": {"type": "integer", "minimum": 1},
            "children": {"type": "integer", "minimum": 0},
            "max_stops": {
                "type": "string",
                "enum": ["any", "non_stop", "one_or_fewer", "two_or_fewer"],
            },
            "airlines_include": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2-letter IATA airline codes the user wants (e.g. DL). Also use for loyalty preferences ('I have Delta status' -> [DL]).",
            },
            "airlines_exclude": {
                "type": "array",
                "items": {"type": "string"},
                "description": "2-letter IATA airline codes to exclude ('no budget airlines' -> [NK, F9, G4]).",
            },
            "alliances": {
                "type": "array",
                "items": {"type": "string", "enum": ["ONEWORLD", "SKYTEAM", "STAR_ALLIANCE"]},
            },
            "via_airports": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Only return itineraries connecting through these airports (IATA codes). Use this whenever the user asks about routing via a specific hub ('through HND', 'via Seoul') — it checks the FULL result set, so it is the only reliable way to establish whether a routing exists.",
            },
            "sort": {"type": "string", "enum": ["best", "cheapest", "fastest", "departure_time"]},
            "max_price": {"type": ["number", "null"]},
            "currency": {"type": "string", "description": "ISO 4217, default USD"},
            "departure_time": {
                "type": ["object", "null"],
                "description": "Outbound departure window in hours 0-23. 'early flight' -> latest 10, 'evening' -> earliest 18.",
                "properties": {
                    "earliest": {"type": ["integer", "null"]},
                    "latest": {"type": ["integer", "null"]},
                },
            },
            "arrival_time": {
                "type": ["object", "null"],
                "description": "Outbound ARRIVAL window at the destination, hours 0-23. Use this (not departure_time) for 'arrive by / be there by X': 'by Thursday morning' -> latest 12.",
                "properties": {
                    "earliest": {"type": ["integer", "null"]},
                    "latest": {"type": ["integer", "null"]},
                },
            },
            "return_time": {
                "type": ["object", "null"],
                "description": "Return departure window in hours 0-23.",
                "properties": {
                    "earliest": {"type": ["integer", "null"]},
                    "latest": {"type": ["integer", "null"]},
                },
            },
            "return_arrival_time": {
                "type": ["object", "null"],
                "description": "Return ARRIVAL window, hours 0-23.",
                "properties": {
                    "earliest": {"type": ["integer", "null"]},
                    "latest": {"type": ["integer", "null"]},
                },
            },
            "max_duration_minutes": {"type": ["integer", "null"]},
            "exclude_basic_economy": {"type": "boolean"},
            "assumptions": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Plain-English notes on anything you inferred from an ambiguous query, e.g. 'Assumed 2026 for 3/21', 'Interpreted NYC as JFK/LGA/EWR'.",
            },
            "summary": {
                "type": "string",
                "description": "One-line human-readable description of the search being run.",
            },
        },
        "required": ["origins", "destinations", "trip_type"],
    },
}

def assistant_system_prompt() -> str:
    today = datetime.now().strftime("%A, %B %d, %Y")
    return f"""You are the flight assistant on Evan's flight search site, backed by live Google Flights data via the search_flights tool. Today is {today}.

You are a travel-savvy assistant, not a form. Converse naturally, but ground every price, time, and availability claim in a search_flights result from this conversation — never invent or recall fares.

You also have web_search for real-world context: event dates and venues ("the Oklahoma-Florida game"), which city hosts something, weather-season advice, airport ground-transport basics. Use it when the trip depends on a fact you don't reliably know, then move straight into flight searches without re-asking what you just learned. Don't use it for fares or schedules; those come only from search_flights.

General aviation and travel questions are fully in scope even with no search involved: airport and terminal advice (layouts, connections, ground transport), airline fleets and cabin quality, loyalty programs and alliances, packing and timing wisdom. Answer them as a well-traveled concierge; use web_search when the answer benefits from current facts (recent terminal renovations, fleet changes, rule changes) rather than guessing. Only nudge toward a flight search when it genuinely serves the question.

How to handle requests:
- Vague is fine. Expand cities to their major airports (NYC -> JFK/LGA/EWR). "mid September" or "cheapest time to go" -> flexible_dates. Loyalty hints -> airline filters. A bare month/day means the nearest FUTURE date — if that month/day has already passed this year, it means next year.
- Small regional airports (HVN, ISP, ORH, GNV-class fields) often have NO through-ticketed routes to each other. If a search from/to a small airport returns nothing, don't retry the same pair — immediately widen to the nearby majors in the same search (e.g. New Haven -> HVN,BDL,HPN and even LGA/JFK; Gainesville -> GNV,JAX,MCO) and tell the user the drive trade-off for each option you recommend.
- "arrive by / be there by X" is an ARRIVAL constraint (arrival_time), never a departure cap.
- When the user cares about the arrival DAY ("land on Friday"), set arrival_date and pick departure_date by timezone logic. Rules of thumb, not laws: typical daytime trans-Pacific Asia -> US routings land the same local day, but late-evening departures and long westbound routings (via the Middle East or Europe) land the NEXT day; US -> Asia/Europe overnights land the next day. When candidate routings vary widely, run two searches (departing the arrival day AND the day before, both with the same arrival_date) so no valid routing is missed. When they change or relax the arrival day, immediately re-search with the new dates — do not re-serve the old results or just offer to search.
- "via / through <hub>" questions: search with via_airports. That filter checks every itinerary Google returns; the plain result list you see is only a top-6 sample, so NEVER assert that a routing, hub, or airline "doesn't exist" from the plain list — and never say you "confirmed" or "checked directly" unless a via_airports search actually ran this conversation. If you haven't checked, say so and offer to.
- Comparisons: run one search per option (at most 4 per turn). A self-arranged overnight stopover = one search per leg with the correct date on each. Give each search a summary naming the option ("Option A: Thursday nonstop").
- Empty first results on busy routes are usually transient: retry the SAME search once before broadening. Once a search succeeds, stop; don't also run overlapping broader variants of a question that's already answered. (Empty searches are hidden from the user's page, so never reference "the empty section above.")
- Multi-city trips (A -> B, B -> C, C -> A, or open-jaw): use trip_type multi_city with multi_city_segments — that prices all legs as ONE ticket, usually cheaper than separate one-ways. Use separate one-way searches only when the user wants to compare against self-booking each leg.
- Make reasonable assumptions and state them briefly instead of interrogating the user. City-level vagueness is yours to resolve (airports, date windows, cabin). But ask ONE brief question before searching when the request is genuinely unresolvable: the origin is missing entirely, or the destination is a whole region or continent ("Europe", "Asia", "somewhere warm"). Offer to choose for them in the same breath, e.g. "Anywhere in Europe in particular? If you're open, I'm happy to compare a few favorites like London, Paris, and Lisbon." Never stack multiple questions, and never ask when a sensible assumption exists.
- Airport precision matters: each result's route field states its true endpoints (e.g. FLL-EWR). Quote airports exactly from that field. Never name an airport the data does not show; EWR is not JFK.

Answering:
- Voice: you are a seasoned travel concierge. Courteous, composed, precise, warm but never gushing. Write in full sentences, the way a fine hotel's head concierge would speak. NEVER use em dashes or en dashes anywhere in your replies; use commas, periods, or a colon instead.
- The user sees result cards for every search you run, so don't recite every flight. Lead with your recommendation and the key numbers (totals for multi-leg plans, including a note that hotels/ground costs aren't included), then the trade-offs that matter.
- Mention real caveats from the data: nothing arrives before X, prices are one-way vs round-trip totals, self-transfer risks, tight or overnight layovers.
- If a search fails or is rate-limited, say so plainly and suggest trying again in a moment.
- Keep responses short and conversational — a few sentences, not a report.
- Formatting: you may use **bold** sparingly for the key number or verdict (it renders properly). No other markdown — no headers, no bullet syntax, no italics-by-asterisk. Never insert a line break inside a sentence; use a blank line only between paragraphs.
- End EVERY final reply with exactly one line in this form (it becomes tappable buttons and is stripped from your prose, so don't also ask the same things in the text):
SUGGESTIONS: ["first likely follow-up", "second", "third"]
2-4 items, each under 9 words, phrased as the user would type them ("check Saturday instead", "only nonstops", "what about Newark?"). Predict the most likely next asks given the results: nearby dates, price/speed trade-offs, filters, alternate airports, booking the pick."""


def compact_for_model(payload: dict) -> str:
    """Condensed search result for Claude's context — the user sees full cards."""
    kind = payload.get("type")
    if kind == "dates":
        dates = payload.get("dates") or []
        cheapest = sorted(dates, key=lambda d: d["price"])[:10]
        return json.dumps({
            "kind": "date_prices",
            "note": payload.get("message"),
            "count": len(dates),
            "cheapest_dates": cheapest,
        })
    if kind == "multicity":
        rows = [
            {
                "total_price": it["total_price"], "currency": it["currency"],
                "legs": [_leg_summary(p) for p in it["parts"]],
            }
            for it in (payload.get("results") or [])[:5]
        ]
        return json.dumps({"kind": "multi_city", "note": payload.get("message"), "options": rows})
    if kind == "itineraries":
        rows = [
            {
                "total_price": it["total_price"], "currency": it["currency"],
                "outbound": _leg_summary(it["outbound"]),
                "return": _leg_summary(it["return"]),
            }
            for it in (payload.get("results") or [])[:6]
        ]
        return json.dumps({"kind": "round_trips", "note": payload.get("message"), "options": rows})
    results = payload.get("results") or []
    rows = [_leg_summary(f) for f in results[:6]]
    out = {"kind": "flights", "note": payload.get("message"), "options": rows}
    if len(results) > 6:
        out["sample_note"] = (
            f"Showing 6 of {len(results)} results by the requested sort. Routings absent from "
            "this sample may still exist — never claim a hub/airline/routing is unavailable "
            "unless a via_airports-filtered search says so."
        )
    return json.dumps(out)


def _leg_summary(f: dict) -> dict:
    legs = f.get("legs") or []
    return {
        "airline": f.get("airline"),
        "price": f.get("price"),
        "currency": f.get("currency"),
        "route": f"{legs[0]['from']}-{legs[-1]['to']}" if legs else None,
        "depart": legs[0]["departure"] if legs else None,
        "arrive": legs[-1]["arrival"] if legs else None,
        "duration_min": f.get("duration"),
        "stops": f.get("stops"),
        "via": [lo["airport"] for lo in (f.get("layovers") or [])],
        "warnings": f.get("warnings") or [],
    }


def split_suggestions(text: str) -> tuple[str, list[str]]:
    m = re.search(r"\n?\s*SUGGESTIONS:\s*(\[.*?\])\s*$", text, re.S)
    if not m:
        return text, []
    try:
        raw = json.loads(m.group(1))
        suggestions = [s.strip() for s in raw if isinstance(s, str) and s.strip()][:4]
    except (json.JSONDecodeError, TypeError):
        return text[: m.start()].rstrip(), []
    return text[: m.start()].rstrip(), suggestions


_QUESTION_OPENERS = (
    "what", "why", "how", "is ", "are ", "can ", "do ", "does ", "should ",
    "which airline", "tell me about", "explain",
)
_ROUTE_HINT = re.compile(r"\b([A-Z]{3})\b.*\b([A-Z]{3})\b|\b\w+\s+to\s+\w+", re.I)


def looks_like_plain_search(query: str) -> bool:
    """Conservative: only true for 'fly me from A to B' style requests.

    False negatives just mean we keep full effort (today's behaviour), so the
    failure mode of this heuristic is 'no speedup', never 'worse answer'.
    """
    q = (query or "").strip()
    if len(q) > 140:
        return False
    low = q.lower()
    if any(low.startswith(op) for op in _QUESTION_OPENERS):
        return False
    return bool(_ROUTE_HINT.search(q))


def run_assistant(query: str, history: list | None, emit=None) -> dict:
    """Run the agent loop. `emit(event, data)` receives progress as it happens:

      sections     a results batch is ready (cards can render immediately —
                   this lands seconds before the prose is written)
      text_delta   a chunk of the reply as Claude writes it
      text_reset   the text so far was a preamble to a search; discard it

    With emit=None the behaviour is identical, just silent.
    """
    emit = emit or (lambda *_: None)
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=4)
    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in (history or [])[-12:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    messages.append({"role": "user", "content": query})

    sections: list[dict] = []
    searches_used = 0
    web_tool = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}

    # Routing the first call: when the query is plainly "search this route",
    # that call only has to emit a tool_use, which needs no deep reasoning.
    # Knowledge questions ("what's Delta's baggage allowance") are ANSWERED on
    # that same call, so they keep full effort — brevity there would cost the
    # quality that is the product.
    first_effort = "low" if looks_like_plain_search(query) else "medium"

    started = time.monotonic()
    timings: list = []   # [(phase, seconds, detail)] — surfaced for profiling
    rounds = 0
    iterations = 0
    # hard turn budget: past ~65s, stop searching and answer with what we have
    # (a Google throttle wave otherwise compounds into multi-minute hangs)
    while rounds < 3 and iterations < 8 and time.monotonic() - started < 65:
        iterations += 1
        _t = time.monotonic()
        streamed_any = False
        with client.messages.stream(
            model="claude-opus-4-8",
            max_tokens=4000,
            # synthesis (any call after results are in) always runs at full
            # effort: that is where the recommendation is actually written
            output_config={"effort": first_effort if iterations == 1 else "medium"},
            # cache breakpoint on system caches tools+system for every call in
            # the loop and across turns (prompt renders tools -> system -> messages)
            system=[{
                "type": "text",
                "text": assistant_system_prompt(),
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[SEARCH_TOOL, web_tool],
            messages=messages,
        ) as stream:
            for chunk in stream.text_stream:
                if chunk:
                    streamed_any = True
                    emit("text_delta", chunk)
            response = stream.get_final_message()

        u = getattr(response, "usage", None)
        timings.append((
            f"claude_{iterations}", round(time.monotonic() - _t, 2),
            f"in={getattr(u, 'input_tokens', '?')} "
            f"cache_r={getattr(u, 'cache_read_input_tokens', '?')} "
            f"out={getattr(u, 'output_tokens', '?')} stop={response.stop_reason}",
        ))

        # server-side web search can pause mid-loop; re-send to let it resume
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if tool_uses and streamed_any:
            # whatever was streamed was a lead-in to a search, not the answer
            emit("text_reset", None)
        if response.stop_reason != "tool_use" or not tool_uses:
            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            text, suggestions = split_suggestions(text)
            return {"message": text or "…", "sections": sections,
                    "suggestions": suggestions, "timings": timings}
        rounds += 1

        messages.append({"role": "assistant", "content": response.content})

        # run this round's searches CONCURRENTLY — a 3-option comparison takes
        # as long as its slowest search instead of the sum of all three
        budgeted = []
        tool_results_by_id: dict = {}
        for tu in tool_uses:
            if searches_used >= 5:
                tool_results_by_id[tu.id] = {
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": "Search budget for this turn exhausted; answer with what you have.",
                    "is_error": True,
                }
            else:
                searches_used += 1
                budgeted.append(tu)

        # no context manager: its exit would JOIN hung threads and defeat the
        # timeouts below. shutdown(wait=False) abandons stragglers instead.
        pool = ThreadPoolExecutor(max_workers=4)

        def _staggered(spec, delay):
            # five simultaneous requests from one address is the least human
            # thing we do; a short ramp keeps the burst off Google's radar
            # while still overlapping the slow part of each search
            if delay:
                time.sleep(delay)
            return cached_execute_spec(spec)

        _tsearch = time.monotonic()
        futures = {
            tu.id: pool.submit(_staggered, tu.input, n * 0.4)
            for n, tu in enumerate(budgeted)
        }
        batch_deadline = time.monotonic() + 55

        for tu in budgeted:
            try:
                payload = futures[tu.id].result(timeout=max(1.0, batch_deadline - time.monotonic()))
                sections.append(payload)
                tool_results_by_id[tu.id] = {
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": compact_for_model(payload),
                }
            except FuturesTimeout:
                tool_results_by_id[tu.id] = {
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": "Search timed out: Google Flights is responding very slowly right now. Tell the user plainly and suggest trying again shortly; do not retry now.",
                    "is_error": True,
                }
            except SearchHTTPError as e:
                detail = "Google Flights is rate-limiting right now (HTTP 429)." if e.status_code == 429 \
                    else f"Google Flights returned HTTP {e.status_code}."
                tool_results_by_id[tu.id] = {
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": detail, "is_error": True,
                }
            except SearchClientError:
                tool_results_by_id[tu.id] = {
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": "Couldn't reach Google Flights for this search.", "is_error": True,
                }
            except Exception as e:  # bad tool input etc. — let Claude correct itself
                today = datetime.now().strftime("%Y-%m-%d")
                tool_results_by_id[tu.id] = {
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": f"Search failed: {str(e)[:300]} (today is {today}). Fix the input and retry.",
                    "is_error": True,
                }
        pool.shutdown(wait=False, cancel_futures=True)
        timings.append((f"searches_{rounds}", round(time.monotonic() - _tsearch, 2), f"n={len(budgeted)}"))
        # cards can render now, well before the prose that describes them
        fresh = [s for s in sections if (s.get("results") or s.get("dates"))]
        if fresh:
            emit("sections", fresh)
        messages.append({"role": "user", "content": [tool_results_by_id[tu.id] for tu in tool_uses]})

    if sections:
        return {
            "message": "That search ran long, so here is what I have so far. The cards below are live results; say the word and I'll keep digging.",
            "sections": sections,
            "suggestions": ["keep digging"],
        }
    return {
        "message": "Google Flights is responding slowly at the moment and that search ran past my patience. Give it a minute and try again; your question was perfectly fine.",
        "sections": [],
        "suggestions": ["try again"],
    }


def resolve_airports(codes: list[str]) -> tuple[list, list[str]]:
    valid, invalid = [], []
    for code in codes or []:
        ap = getattr(Airport, code.strip().upper(), None)
        (valid.append(ap) if ap else invalid.append(code.upper()))
    return valid, invalid


def resolve_airlines(codes: list[str] | None) -> list:
    resolved = []
    for code in codes or []:
        c = code.strip().upper()
        # digit-leading IATA codes live under a "_" prefix in fli's enum (7C -> _7C)
        al = getattr(Airline, c, None) or getattr(Airline, "_" + c, None)
        if al:
            resolved.append(al)
    return resolved


def time_restrictions(departure: dict | None) -> TimeRestrictions | None:
    fields = {
        "earliest_departure": (departure or {}).get("earliest"),
        "latest_departure": (departure or {}).get("latest"),
    }
    if all(v is None for v in fields.values()):
        return None
    return TimeRestrictions(**fields)


def arrival_ok(result, target_date: str | None, window: dict | None) -> bool:
    has_window = bool(window) and (window.get("earliest") is not None or window.get("latest") is not None)
    if not has_window and not target_date:
        return True
    arr = result.legs[-1].arrival_datetime if result.legs else None
    if not arr:
        return False
    if target_date and arr.strftime("%Y-%m-%d") != target_date:
        return False  # arrives on a different day than requested
    if not has_window:
        return True
    hour = arr.hour + arr.minute / 60
    lo, hi = window.get("earliest"), window.get("latest")
    return (lo is None or hour >= lo) and (hi is None or hour <= hi)


def build_filters(spec: dict, origins: list, destinations: list, filters_cls=FlightSearchFilters, **extra):
    is_round_trip = spec.get("trip_type") == "round_trip"

    segments = [
        FlightSegment(
            departure_airport=[[a, 0] for a in origins],
            arrival_airport=[[a, 0] for a in destinations],
            travel_date=spec.get("departure_date") or datetime.now().strftime("%Y-%m-%d"),
            # arrival windows are enforced app-side (arrival_ok): Google's arrival
            # filter matches clock hours only, so a 2 AM next-day arrival passes "by noon"
            time_restrictions=time_restrictions(spec.get("departure_time")),
        )
    ]
    if is_round_trip:
        segments.append(
            FlightSegment(
                departure_airport=[[a, 0] for a in destinations],
                arrival_airport=[[a, 0] for a in origins],
                travel_date=spec.get("return_date") or spec.get("departure_date") or datetime.now().strftime("%Y-%m-%d"),
                time_restrictions=time_restrictions(spec.get("return_time")),
            )
        )

    alliances = [getattr(Alliance, a) for a in spec.get("alliances") or [] if hasattr(Alliance, a)]
    max_price = spec.get("max_price")

    return filters_cls(
        trip_type=TripType.ROUND_TRIP if is_round_trip else TripType.ONE_WAY,
        passenger_info=PassengerInfo(
            adults=spec.get("adults") or 1,
            children=spec.get("children") or 0,
        ),
        flight_segments=segments,
        stops=STOPS_MAP.get(spec.get("max_stops"), MaxStops.ANY),
        seat_type=CABIN_MAP.get(spec.get("cabin"), SeatType.ECONOMY),
        airlines=resolve_airlines(spec.get("airlines_include")) or None,
        airlines_exclude=resolve_airlines(spec.get("airlines_exclude")) or None,
        alliances=alliances or None,
        price_limit=PriceLimit(max_price=int(max_price)) if max_price else None,
        max_duration=spec.get("max_duration_minutes"),
        exclude_basic_economy=bool(spec.get("exclude_basic_economy")),
        **extra,
    )


def google_flights_url(dep_code: str, arr_code: str, dep_date: str | None,
                       ret_date: str | None = None, cabin: str | None = None) -> str:
    q = f"flights from {dep_code} to {arr_code}"
    if dep_date:
        q += f" on {dep_date}"
    q += f" returning {ret_date}" if ret_date else " one way"
    if cabin and cabin != "economy":
        q += f" {cabin.replace('_', ' ')}"
    return f"https://www.google.com/travel/flights?q={quote(q)}"


def multi_city_url(parts: list, cabin: str | None = None) -> str:
    segs = []
    for p in parts:
        legs = p.legs or []
        if not legs:
            continue
        date = legs[0].departure_datetime.strftime("%Y-%m-%d") if legs[0].departure_datetime else ""
        segs.append(f"{legs[0].departure_airport.name} to {legs[-1].arrival_airport.name} on {date}")
    q = "multi-city flights " + ", then ".join(segs)
    if cabin and cabin != "economy":
        q += f" {cabin.replace('_', ' ')}"
    return f"https://www.google.com/travel/flights?q={quote(q)}"


def result_booking_url(result, cabin: str | None = None, ret_date: str | None = None) -> str:
    # built from the itinerary's OWN legs — multi-airport searches mean each
    # result can have a different origin/destination than the search defaults
    legs = result.legs or []
    if not legs:
        return "https://www.google.com/travel/flights"
    dep = legs[0].departure_airport.name
    arr = legs[-1].arrival_airport.name
    dep_date = legs[0].departure_datetime.strftime("%Y-%m-%d") if legs[0].departure_datetime else None
    return google_flights_url(dep, arr, dep_date, ret_date, cabin)


@lru_cache(maxsize=1)
def airport_coords() -> dict:
    import airportsdata
    return airportsdata.load("IATA")


def coords_for(code: str) -> dict | None:
    a = airport_coords().get(code)
    return {"code": code, "lat": a["lat"], "lon": a["lon"]} if a else None


def route_points(result) -> list[dict]:
    codes = []
    for leg in result.legs or []:
        for code in (leg.departure_airport.name, leg.arrival_airport.name):
            if not codes or codes[-1] != code:
                codes.append(code)
    points = [p for p in (coords_for(c) for c in codes) if p]
    # intermediate stops carry their layover length for the map labels
    layovers = list(result.layovers or [])
    for i, p in enumerate(points[1:-1]):
        if i < len(layovers) and layovers[i].duration:
            p["layover_min"] = layovers[i].duration
    return points


def airline_code(airline) -> str | None:
    # fli's Airline enum prefixes digit-leading IATA codes with "_" (7C -> _7C)
    # because Python identifiers can't start with a digit; strip for display
    return airline.name.lstrip("_") if airline else None


def serialize_leg(leg) -> dict:
    return {
        "airline": leg.airline.value,
        "airline_code": airline_code(leg.airline),
        "flight_number": leg.flight_number,
        "from": leg.departure_airport.name,
        "to": leg.arrival_airport.name,
        "departure": leg.departure_datetime.isoformat() if leg.departure_datetime else None,
        "arrival": leg.arrival_datetime.isoformat() if leg.arrival_datetime else None,
        "aircraft": leg.aircraft,
        "overnight": leg.overnight,
        "operated_by": getattr(leg.operating_airline, "value", leg.operating_airline),
    }


def serialize_flight(result, cabin: str | None = None, ret_date: str | None = None) -> dict:
    url = result_booking_url(result, cabin, ret_date)
    legs = [serialize_leg(l) for l in result.legs]
    layovers = [
        {
            "airport": lo.airport.name,
            "city": lo.city,
            "duration": lo.duration,
            "overnight": lo.overnight,
            "change_of_airport": lo.change_of_airport,
        }
        for lo in (result.layovers or [])
    ]

    warnings = []
    if result.self_transfer:
        warnings.append("Self-transfer: separate tickets, you handle the connection")
    if result.mixed_cabin:
        warnings.append("Mixed cabin classes across legs")
    for lo in layovers:
        if lo["change_of_airport"]:
            warnings.append(f"Airport change during layover in {lo['city']}")
        if lo["overnight"]:
            warnings.append(f"Overnight layover in {lo['city']}")
        if lo["duration"] and lo["duration"] < 45 and not lo["overnight"]:
            warnings.append(f"Tight {lo['duration']}-minute connection in {lo['city']}")

    primary_code = airline_code(result.primary_airline) if result.primary_airline else (legs[0]["airline_code"] if legs else None)
    return {
        "airline": result.primary_airline_name or (legs[0]["airline"] if legs else None),
        "airline_code": primary_code,
        "alliance": AIRLINE_ALLIANCE.get(primary_code),
        "price": result.price,
        "currency": result.currency or "USD",
        "duration": result.duration,
        "stops": result.stops,
        "legs": legs,
        "layovers": layovers,
        "warnings": warnings,
        "highlights": [],
        "co2_delta_pct": result.co2_emissions_delta_pct,
        "booking_url": url,
        "route_points": route_points(result),
    }


def add_highlights(flights: list[dict]) -> None:
    if not flights:
        return
    priced = [f for f in flights if f.get("price")]
    cheapest = min((f["price"] for f in priced), default=None)
    fastest = min((f["duration"] for f in flights if f.get("duration")), default=None)
    for f in flights:
        if f["stops"] == 0:
            f["highlights"].append("Direct")
        if cheapest and f.get("price") == cheapest:
            f["highlights"].append("Cheapest option")
        if fastest and f.get("duration") == fastest:
            f["highlights"].append("Fastest option")
    prices = sorted(f["price"] for f in priced)
    for f in flights:
        if f.get("price") and prices:
            rank = prices.index(f["price"]) / max(len(prices) - 1, 1)
            f["score"] = round(95 - rank * 55)
        else:
            f["score"] = 40


def run_search(search: SearchFlights, filters: FlightSearchFilters, sort: SortBy, top_n: int):
    # Refusals come as HTTP 200 + a tiny body (parses to empty) or as raised
    # client errors. Both gates are answered the same way: retire the identity
    # (cookies + exit IP + fingerprint) so the retry is a different visitor,
    # and let the breaker impose quiet if the whole process is mid-wave.
    last_exc = None
    for i, attempt_sort in enumerate((sort, sort, SortBy.CHEAPEST, SortBy.CHEAPEST)):
        breaker_wait(1.5 if i == 0 else 8.0)
        checkout_identity()
        attempt = filters.model_copy(update={"sort_by": attempt_sort})
        try:
            results = search.search(attempt, top_n=top_n, currency="USD")
        except SearchClientError as e:
            last_exc = e
            retire_identity()
            note_search_outcome(False)
            time.sleep(2 + 2 * i)
            continue
        if results:
            note_search_outcome(True)
            _attempt_stats["ok_on_attempt"].append(i + 1)
            return results
        retire_identity()
        note_search_outcome(False)
        time.sleep(0.6 * (i + 1) + random.uniform(0, 0.5))
    if last_exc:
        raise last_exc
    return []


def search_fixed_dates(spec: dict, origins: list, destinations: list, currency: str) -> dict:
    filters = build_filters(spec, origins, destinations, show_all_results=True)
    sort = SORT_MAP.get(spec.get("sort"), SortBy.CHEAPEST)
    results = run_search(SearchFlights(), filters, sort, top_n=8)

    if not results:
        return {
            "type": "flights",
            "message": "No flights found for that search. Try different dates, nearby airports, or fewer filters.",
            "results": [],
        }

    via = {c.strip().upper() for c in spec.get("via_airports") or []}

    def routes_via(result) -> bool:
        return any(lo.airport.name in via for lo in (result.layovers or []))

    if via:
        if isinstance(results[0], tuple):
            results = [c for c in results if routes_via(c[0]) or routes_via(c[-1])]
        else:
            results = [r for r in results if routes_via(r)]
        if not results:
            return {
                "type": "flights",
                "message": f"Checked every itinerary Google returned: nothing routes via {', '.join(sorted(via))} on that day.",
                "results": [],
            }

    aw, rw = spec.get("arrival_time"), spec.get("return_arrival_time")
    dep_date = spec.get("departure_date")
    ret_date = spec.get("return_date") or dep_date
    arr_date = spec.get("arrival_date")
    has_win = lambda w: bool(w) and (w.get("earliest") is not None or w.get("latest") is not None)
    # enforce the arrival calendar day when explicitly requested, or when a
    # time-of-day window is set (a window is meaningless across the wrong day)
    out_target = arr_date or (dep_date if has_win(aw) else None)
    ret_target = ret_date if has_win(rw) else None
    arrival_note = None

    if isinstance(results[0], tuple):
        strict = [c for c in results if arrival_ok(c[0], out_target, aw) and arrival_ok(c[-1], ret_target, rw)]
        if strict:
            results = strict
        elif aw or rw or arr_date:
            arrival_note = "Nothing meets the arrival-day/time constraint exactly — showing the closest arrivals instead."
            results = sorted(results, key=lambda c: c[0].legs[-1].arrival_datetime or datetime.max)

        itineraries = []
        for combo in results[:10]:
            out, ret = combo[0], combo[-1]
            total = max(p for p in [out.price, ret.price, 0] if p is not None)
            itineraries.append(
                {
                    "total_price": total,
                    "currency": out.currency or ret.currency or currency,
                    "outbound": (out_f := serialize_flight(
                        out, spec.get("cabin"),
                        ret_date=ret.legs[0].departure_datetime.strftime("%Y-%m-%d")
                        if ret.legs and ret.legs[0].departure_datetime else spec.get("return_date"),
                    )),
                    "return": serialize_flight(ret, spec.get("cabin")),
                    "booking_url": out_f["booking_url"],
                }
            )
        itineraries.sort(key=lambda i: i["total_price"] or 1e9)
        for i, itin in enumerate(itineraries):
            itin["score"] = round(95 - (i / max(len(itineraries) - 1, 1)) * 55)
            for leg_key in ("outbound", "return"):
                if itin[leg_key]["stops"] == 0:
                    itin[leg_key]["highlights"].append("Direct")
        message = f"Found {len(itineraries)} round-trip options. Prices are the real total for both directions."
        if arrival_note:
            message = f"{arrival_note}\n{message}"
        return {
            "type": "itineraries",
            "message": message,
            "results": itineraries,
        }

    strict = [r for r in results if arrival_ok(r, out_target, aw)]
    if strict:
        results = strict
    elif aw or arr_date:
        arrival_note = "Nothing meets the arrival-day/time constraint exactly — showing the closest arrivals instead."
        results = sorted(results, key=lambda r: r.legs[-1].arrival_datetime or datetime.max)

    # ship the full reasonable list — the frontend previews a few and lets the
    # user expand/filter/sort the rest instantly, no re-search needed
    flights = [serialize_flight(r, spec.get("cabin")) for r in results[:50]]
    add_highlights(flights)
    message = f"Found {len(flights)} flights."
    if arrival_note:
        message = f"{arrival_note}\n{message}"
    return {
        "type": "flights",
        "message": message,
        "results": flights,
    }


def search_multi_city(spec: dict, currency: str) -> dict:
    resolved = []
    invalid: list[str] = []
    for seg in (spec.get("multi_city_segments") or [])[:5]:
        o, bad_o = resolve_airports(seg.get("origins"))
        d, bad_d = resolve_airports(seg.get("destinations"))
        invalid += bad_o + bad_d
        if o and d and seg.get("date"):
            resolved.append((o, d, seg["date"]))
    if invalid or len(resolved) < 2:
        return {
            "type": "clarify",
            "message": (f"I couldn't recognize these airport codes: {', '.join(invalid)}. " if invalid else "")
                       + "A multi-city trip needs at least two legs, each with airports and a date.",
            "results": [],
        }

    segments = [
        FlightSegment(
            departure_airport=[[a, 0] for a in o],
            arrival_airport=[[a, 0] for a in d],
            travel_date=date_,
        )
        for o, d, date_ in resolved
    ]
    filters = FlightSearchFilters(
        trip_type=TripType.MULTI_CITY,
        passenger_info=PassengerInfo(
            adults=spec.get("adults") or 1, children=spec.get("children") or 0,
        ),
        stops=STOPS_MAP.get(spec.get("max_stops"), MaxStops.ANY),
        seat_type=CABIN_MAP.get(spec.get("cabin"), SeatType.ECONOMY),
        airlines=resolve_airlines(spec.get("airlines_include")) or None,
        airlines_exclude=resolve_airlines(spec.get("airlines_exclude")) or None,
        alliances=[getattr(Alliance, a) for a in spec.get("alliances") or [] if hasattr(Alliance, a)] or None,
        flight_segments=segments,
        show_all_results=True,
    )
    sort = SORT_MAP.get(spec.get("sort"), SortBy.CHEAPEST)
    # multi-city expands every leg chain — keep the fan-out small to stay inside
    # the serverless time budget
    results = run_search(SearchFlights(), filters, sort, top_n=4)

    if not results:
        return {
            "type": "multicity",
            "message": "No multi-city itineraries found. Try shifting a date or splitting the legs into separate one-way searches.",
            "results": [],
        }

    itineraries = []
    for combo in results[:8]:
        parts = list(combo) if isinstance(combo, tuple) else [combo]
        prices = [p.price for p in parts if p.price]
        itineraries.append({
            "total_price": max(prices) if prices else None,
            "currency": parts[0].currency or currency,
            "parts": [serialize_flight(p, spec.get("cabin")) for p in parts],
            "booking_url": multi_city_url(parts, spec.get("cabin")),
        })
    itineraries.sort(key=lambda i: i["total_price"] or 1e9)
    for i, itin in enumerate(itineraries):
        itin["score"] = round(95 - (i / max(len(itineraries) - 1, 1)) * 55)
    route_text = " → ".join([resolved[0][0][0].name] + [d[0].name for _, d, _ in resolved])
    return {
        "type": "multicity",
        "message": f"Found {len(itineraries)} multi-city itineraries ({route_text}). Prices are the total for all legs on one ticket.",
        "results": itineraries,
    }


def search_flexible_dates(spec: dict, origins: list, destinations: list, currency: str) -> dict:
    flex = spec["flexible_dates"]
    is_round_trip = spec.get("trip_type") == "round_trip"
    spec = {**spec, "departure_date": flex["from_date"], "return_date": flex["from_date"]}

    extra: dict = {"from_date": flex["from_date"], "to_date": flex["to_date"]}
    if is_round_trip and flex.get("trip_length_days"):
        extra["duration"] = flex["trip_length_days"]

    filters = build_filters(spec, origins, destinations, filters_cls=DateSearchFilters, **extra)
    searcher = SearchDates()
    date_prices = None
    last_exc = None
    for i in range(3):
        breaker_wait(1.5 if i == 0 else 8.0)
        checkout_identity()
        try:
            date_prices = searcher.search(filters, currency="USD")
            last_exc = None
        except SearchClientError as e:
            last_exc = e
            retire_identity()
            note_search_outcome(False)
            time.sleep(3 + 3 * i)
            continue
        if date_prices:
            note_search_outcome(True)
            break
        retire_identity()
        note_search_outcome(False)
        time.sleep(1)
    if last_exc:
        raise last_exc
    date_prices = date_prices or []
    if not date_prices:
        return {
            "type": "dates",
            "message": "Couldn't get date pricing for that window. Try a narrower range.",
            "dates": [],
        }

    dates = [
        {
            "date": dp.date[0].strftime("%Y-%m-%d"),
            "return_date": dp.date[1].strftime("%Y-%m-%d") if len(dp.date) > 1 else None,
            "price": dp.price,
            "currency": dp.currency or currency,
        }
        for dp in date_prices
        if dp.price
    ]
    cheapest = min((d["price"] for d in dates), default=None)
    for d in dates:
        d["cheapest"] = d["price"] == cheapest
    return {
        "type": "dates",
        "message": "The best-value dates are shown first; expand for the full calendar. Pick one to see actual flights.",
        "dates": dates,
    }


def roll_past_dates(spec: dict) -> tuple[dict, list[str]]:
    """A month/day that already passed this year means next year — fix it and say so."""
    today = datetime.now().strftime("%Y-%m-%d")
    notes = []

    def roll(ds: str | None) -> str | None:
        if not ds or ds >= today:
            return ds
        try:
            d = datetime.strptime(ds, "%Y-%m-%d")
        except ValueError:
            return ds
        while d.strftime("%Y-%m-%d") < today:
            try:
                d = d.replace(year=d.year + 1)
            except ValueError:  # Feb 29
                d = d.replace(year=d.year + 1, day=28)
        rolled = d.strftime("%Y-%m-%d")
        notes.append(f"'{ds}' is in the past — interpreted as {rolled}")
        return rolled

    spec = dict(spec)
    for key in ("departure_date", "return_date", "arrival_date"):
        spec[key] = roll(spec.get(key))
    if spec.get("multi_city_segments"):
        spec["multi_city_segments"] = [
            {**seg, "date": roll(seg.get("date"))} for seg in spec["multi_city_segments"]
        ]
    if spec.get("flexible_dates"):
        flex = dict(spec["flexible_dates"])
        flex["from_date"] = roll(flex.get("from_date"))
        flex["to_date"] = roll(flex.get("to_date"))
        spec["flexible_dates"] = flex
    return spec, notes


# --------------------------------------------------------------------------
# Short-lived search cache with single-flight dedupe.
#
# Two wins, both free of extra Google load: a repeated search inside the same
# conversation (stop/supersede, "show me that again", overlapping comparison
# prongs) returns instantly, and two identical searches issued concurrently
# collapse into ONE upstream request instead of racing. TTL is deliberately
# short so a quoted fare is never stale enough to mislead.
# --------------------------------------------------------------------------
SEARCH_CACHE_TTL = 240.0
_search_cache: dict = {}
_inflight: dict = {}
_cache_lock = threading.Lock()

# Cosmetic fields don't change what Google is asked, so they must not split
# the cache key. Everything else does affect the result set.
_KEY_IGNORE = {"summary", "assumptions"}


def _spec_key(spec: dict) -> str:
    return json.dumps(
        {k: v for k, v in sorted(spec.items()) if k not in _KEY_IGNORE},
        sort_keys=True, default=str,
    )


def cached_execute_spec(spec: dict) -> dict:
    import copy

    key = _spec_key(spec)
    with _cache_lock:
        hit = _search_cache.get(key)
        if hit and hit[0] > time.monotonic():
            return copy.deepcopy(hit[1])
        waiter = _inflight.get(key)
        if waiter is None:
            waiter = threading.Event()
            _inflight[key] = waiter
            owner = True
        else:
            owner = False

    if not owner:
        # someone else is already asking Google this exact question
        waiter.wait(timeout=45)
        with _cache_lock:
            hit = _search_cache.get(key)
        if hit:
            return copy.deepcopy(hit[1])
        # the owner failed; fall through and try it ourselves

    try:
        payload = execute_spec(spec)
        if payload.get("results") or payload.get("dates"):
            with _cache_lock:
                _search_cache[key] = (time.monotonic() + SEARCH_CACHE_TTL, payload)
                if len(_search_cache) > 64:  # bound memory in a long-lived process
                    for k in sorted(_search_cache, key=lambda k: _search_cache[k][0])[:16]:
                        _search_cache.pop(k, None)
        return copy.deepcopy(payload)
    finally:
        with _cache_lock:
            _inflight.pop(key, None)
        waiter.set()


def execute_spec(spec: dict) -> dict:
    spec, date_notes = roll_past_dates(spec)
    currency_mc = (spec.get("currency") or "USD").upper()

    if spec.get("trip_type") == "multi_city" or spec.get("multi_city_segments"):
        payload = search_multi_city(spec, currency_mc)
        if spec.get("summary"):
            payload["message"] = f"{spec['summary']}\n\n{payload['message']}"
        payload["assumptions"] = (spec.get("assumptions") or []) + date_notes
        return payload

    origins, bad_origins = resolve_airports(spec.get("origins"))
    destinations, bad_destinations = resolve_airports(spec.get("destinations"))
    invalid = bad_origins + bad_destinations
    if not origins or not destinations:
        return {
            "type": "clarify",
            "message": f"I couldn't recognize these airport codes: {', '.join(invalid)}. Could you rephrase with standard airports or city names?",
            "results": [],
        }

    currency = (spec.get("currency") or "USD").upper()

    if spec.get("flexible_dates"):
        payload = search_flexible_dates(spec, origins, destinations, currency)
    elif spec.get("departure_date"):
        payload = search_fixed_dates(spec, origins, destinations, currency)
    else:
        return {
            "type": "clarify",
            "message": "When would you like to travel? A specific date or a rough window both work.",
            "results": [],
        }

    if spec.get("summary"):
        payload["message"] = f"{spec['summary']}\n\n{payload['message']}"
    payload["assumptions"] = (spec.get("assumptions") or []) + date_notes
    return payload


@app.post("/api/search/stream")
async def search_stream(request: Request):
    """Same turn, delivered as it happens.

    The agent loop is blocking, so it runs on a worker thread and pushes
    events into a queue that this generator drains into SSE frames.
    """
    import queue as _queue
    from starlette.responses import StreamingResponse

    body = await request.json()
    query: str = (body.get("query") or "").strip()
    if not query:
        return JSONResponse({"error": "Query is required"}, status_code=400)
    history = body.get("history")

    events: "_queue.Queue" = _queue.Queue()

    def worker():
        try:
            result = run_assistant(query, history, emit=lambda ev, data: events.put((ev, data)))
            events.put(("done", {
                "message": result["message"],
                "sections": result["sections"],
                "suggestions": result.get("suggestions") or [],
            }))
        except anthropic.APIStatusError as e:
            overloaded = e.status_code in (429, 529) or getattr(e, "type", "") == "overloaded_error"
            events.put(("done", {
                "message": "My reasoning service is momentarily congested. Give it a few seconds and send that again; nothing was lost."
                if overloaded else "I hit a temporary service error on my side. Please try that once more.",
                "sections": [], "suggestions": ["try again"],
            }))
        except anthropic.APIConnectionError:
            events.put(("done", {
                "message": "I couldn't reach my reasoning service just now. One more try should do it.",
                "sections": [], "suggestions": ["try again"],
            }))
        except Exception as e:
            events.put(("error", {"error": str(e)}))
        finally:
            events.put((None, None))

    threading.Thread(target=worker, daemon=True).start()

    def frames():
        while True:
            try:
                ev, data = events.get(timeout=90)
            except Exception:
                break
            if ev is None:
                break
            yield f"event: {ev}\ndata: {json.dumps(data)}\n\n"

    return StreamingResponse(frames(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # don't let a proxy sit on the stream
    })


@app.post("/api/search")
async def search(request: Request):
    try:
        body = await request.json()
        query: str = (body.get("query") or "").strip()
        if not query:
            return JSONResponse({"error": "Query is required"}, status_code=400)

        global _process_served
        cold = _process_served == 0
        _process_served += 1
        t0 = time.monotonic()
        result = run_assistant(query, body.get("history"))
        payload = {
            "type": "assistant",
            "message": result["message"],
            "sections": result["sections"],
            "suggestions": result.get("suggestions") or [],
        }
        if body.get("debug_timings"):
            payload["timings"] = {
                "total": round(time.monotonic() - t0, 2),
                "cold_process": cold,
                "since_process_start": round(time.monotonic() - _PROCESS_START, 2),
                "phases": result.get("timings") or [],
                "search_ok_on_attempt": _attempt_stats["ok_on_attempt"][-6:],
                "cached_specs": len(_search_cache),
            }
        return JSONResponse(payload)

    except anthropic.APIStatusError as e:
        if e.status_code in (429, 529) or getattr(e, "type", "") == "overloaded_error":
            msg = "My reasoning service is momentarily congested. Give it a few seconds and send that again; nothing was lost."
        else:
            msg = "I hit a temporary service error on my side. Please try that once more."
        return JSONResponse({"type": "assistant", "message": msg, "sections": [], "suggestions": ["try again"]})
    except anthropic.APIConnectionError:
        return JSONResponse({
            "type": "assistant",
            "message": "I couldn't reach my reasoning service just now. One more try should do it.",
            "sections": [], "suggestions": ["try again"],
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
