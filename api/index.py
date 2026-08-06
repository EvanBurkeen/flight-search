import base64
import copy
import json
import os
import random
import re
import time

_PROCESS_START = time.monotonic()
_process_served = 0  # 0 while this process has not yet answered a request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import date, datetime, timedelta
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
from fli.search._concurrency import get_executor as _get_executor

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
    """One curl handle per (identity, thread).

    A single shared handle SERIALIZED every parallel code path in fli —
    measured July 25: a round trip's 10 return-expansions took 37s end to
    end (completions at 1.5s, 2.5s, ... 36.7s) because each request queued
    on the one libcurl handle. Per-thread handles restore real overlap
    while still presenting as ONE visitor: same residential exit, same
    fingerprint, and the primary session's cookies — the same shape as a
    browser's parallel connections sharing its jar.
    """
    tid = threading.get_ident()
    with _pool_lock:
        sessions = ident.setdefault("sessions", {})
        s = sessions.get(tid)
        if s is None:
            from curl_cffi import requests as _curl_requests

            s = _curl_requests.Session()
            s.headers.update(_fli_client.Client.DEFAULT_HEADERS)
            if _proxy_base:
                s.proxies = {"http": ident["proxy"], "https": ident["proxy"]}
            primary = ident.get("session")
            if primary is None:
                ident["session"] = s  # first handle owns the visitor's jar
            else:
                try:
                    s.cookies.update(primary.cookies)
                except Exception:
                    pass
            sessions[tid] = s
    return s


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
    for s in {id(x): x for x in list((ident.get("sessions") or {}).values())
              + ([ident["session"]] if ident.get("session") else [])}.values():
        try:
            s.close()
        except Exception:
            pass
    ident["sessions"] = {}
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

# fli wraps post/get in tenacity (3 attempts, exponential backoff) ON THE
# SAME SESSION. Refusals are session-sticky and a slow wave means 15s
# timeouts, so those inner retries stack up to ~48s of same-identity waiting
# inside ONE ladder attempt — the July 25 NYC turns that "ran long" were
# largely this. The app's ladder (fresh identity per attempt) is the correct
# retry layer; bypass tenacity and let failures surface immediately.
_orig_post = getattr(_fli_client.Client.post, "__wrapped__", _fli_client.Client.post)
_orig_get = getattr(_fli_client.Client.get, "__wrapped__", _fli_client.Client.get)


def _post_as_identity(self, *args, **kwargs):
    ident = getattr(_local, "identity", None)
    if ident:
        kwargs["impersonate"] = ident["impersonate"]
    # fli's 15s timeout is TOTAL time, not idle time — heavy queries STREAM
    # their progressive chunks slowly, and a nearly-complete 50KB response was
    # being killed mid-download and re-fetched from scratch, attempt after
    # attempt. The ladder sets a per-attempt value: fail fast first (a fresh
    # identity usually fixes it), then leave room for a slow stream to finish.
    kwargs.setdefault("timeout", getattr(_local, "fli_timeout", None) or 15.0)
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


def _chunk_rows(inner, i):
    try:
        v = inner[i]
        if isinstance(v, list) and v and isinstance(v[0], list):
            return v[0]
    except (IndexError, TypeError):
        pass
    return []


def _row_identity(row):
    """An itinerary's identity is its legs (row[0][2]: flights, numbers,
    times) — price and booking tokens live elsewhere and vary by snapshot."""
    try:
        return json.dumps(row[0][2], default=str)
    except (IndexError, TypeError):
        return json.dumps(row, default=str)


def merge_wrb_chunks(chunks: list):
    """The UNION of every chunk's flight rows, not the best single chunk.

    August 2, Beijing->Chengdu: the app showed a wall of China Southern at
    $421 while Google's own UI led with China Eastern at $314 and a cheapest
    tab of $242 — an entire carrier missing. The July 24 patch picked the
    chunk with the MOST rows, which is only correct when chunks are
    cumulative re-renders; when Google streams per-slice or delta chunks,
    the biggest one can still be one carrier's inventory. Merging is the
    strict generalization: cumulative chunks dedupe to the same answer, and
    split chunks stop losing airlines. Later chunks win on identity clashes
    (their prices are the fresher snapshot); metadata (session id etc.)
    comes from the row-richest chunk, exactly as before.
    """
    if not chunks:
        return None
    best = max(chunks, key=lambda c: len(_chunk_rows(c, 2)) + len(_chunk_rows(c, 3)))
    if len(chunks) > 1:
        merged: dict = {}
        for c in chunks:  # chronological: a later re-render overwrites
            for i in (2, 3):
                for row in _chunk_rows(c, i):
                    merged[_row_identity(row)] = row
        # the union always wins — even at equal counts a later chunk carries
        # fresher prices for the same itineraries. Rows may live in bucket 2
        # OR 3 (Beijing's responses carry inner[2]=None with rows in [3]):
        # write the union into the first live bucket, empty the other
        if merged:
            try:
                wrote = False
                for i in (2, 3):
                    v = best[i] if i < len(best) else None
                    if isinstance(v, list) and v and isinstance(v[0], list):
                        v[0] = list(merged.values()) if not wrote else []
                        wrote = True
            except (IndexError, TypeError):
                pass  # unexpected shape: fall back to the richest chunk as-is
    return best


def _extract_route_airlines(inner) -> dict | None:
    """inner[7][1][1] is Google's own airline-filter list for the route —
    the response TESTIFYING which carriers serve it, independent of which
    rows this session was given. [(code, name), ...] pairs."""
    try:
        pairs = inner[7][1][1]
        out = {p[0]: p[1] for p in pairs
               if isinstance(p, list) and len(p) >= 2
               and isinstance(p[0], str) and 2 <= len(p[0]) <= 3}
        return out or None
    except (IndexError, TypeError, KeyError):
        return None


def _parse_merged_wrb_payload(body):
    inner = merge_wrb_chunks(list(_iter_wrb_chunks(body)))
    # stash the route's carrier manifest for the caller (thread-local: the
    # main search parses on its own calling thread; expansion threads keep
    # their own copies and never clobber it)
    if inner is not None:
        manifest = _extract_route_airlines(inner)
        if manifest:
            _local.route_airlines = manifest
    return inner


_fli_flights.parse_first_wrb_payload = _parse_merged_wrb_payload


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
                        "departure_time": {
                            "type": ["object", "null"],
                            "description": "Departure window for THIS leg, hours 0-24. Only a handful of leg-1 candidates get expanded into full itineraries, so when the user asks about a specific departure (e.g. 'the 6:35am'), set a tight window like {earliest: 5, latest: 8} to force that neighborhood into the expansion.",
                            "properties": {
                                "earliest": {"type": ["number", "null"]},
                                "latest": {"type": ["number", "null"]},
                            },
                        },
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

def assistant_system_prompt(router: bool = False) -> str:
    """The full concierge prompt — or, for the Haiku router, only the half
    that shapes a search_flights call. The router's single job is emitting
    the right tool call fast; sending it the voice, formatting, and
    suggestion rules is pure prefill latency (measured ~4.4k tokens where
    ~2k carry its job), and its output prose is discarded anyway."""
    today = datetime.now().strftime("%A, %B %d, %Y")
    if router:
        return ROUTER_PROMPT_HEAD.format(today=today) + REQUEST_RULES
    return (FULL_PROMPT_HEAD.format(today=today) + REQUEST_RULES + ANSWER_RULES)


ROUTER_PROMPT_HEAD = """You are the flight assistant on Evan's flight search site, backed by live Google Flights data via the search_flights tool. Today is {today}.

Your job on this call is to translate the request into the right search_flights call(s) and nothing else.

"""

FULL_PROMPT_HEAD = """You are the flight assistant on Evan's flight search site, backed by live Google Flights data via the search_flights tool. Today is {today}.

You are a travel-savvy assistant, not a form. Converse naturally, but ground every price, time, and availability claim in a search_flights result from this conversation — never invent or recall fares.

You also have web_search for real-world context: event dates and venues ("the Oklahoma-Florida game"), which city hosts something, weather-season advice, airport ground-transport basics. Use it when the trip depends on a fact you don't reliably know, then move straight into flight searches without re-asking what you just learned. Don't use it for fares or schedules; those come only from search_flights.

General aviation and travel questions are fully in scope even with no search involved: airport and terminal advice (layouts, connections, ground transport), airline fleets and cabin quality, loyalty programs and alliances, packing and timing wisdom. Answer them as a well-traveled concierge; use web_search when the answer benefits from current facts (recent terminal renovations, fleet changes, rule changes) rather than guessing. Only nudge toward a flight search when it genuinely serves the question.

"""

REQUEST_RULES = """How to handle requests:
- Vague is fine. Expand cities to their major airports (NYC -> JFK/LGA/EWR). "mid September" or "cheapest time to go" -> flexible_dates. Loyalty hints -> airline filters. A bare month/day means the nearest FUTURE date — if that month/day has already passed this year, it means next year.
- Small regional airports (HVN, ISP, ORH, GNV-class fields) often have NO through-ticketed routes to each other. If a search from/to a small airport returns nothing, don't retry the same pair — immediately widen to the nearby majors in the same search (e.g. New Haven -> HVN,BDL,HPN and even LGA/JFK; Gainesville -> GNV,JAX,MCO) and tell the user the drive trade-off for each option you recommend.
- A multi-airport search returns ONE combined pool, and a hub neighbor's cheap nonstops crowd a small field's pricier connections toward the bottom. The result message carries a per-destination-airport breakdown ("By destination airport: MCO 30 from $215, GNV 4 from $397") computed over everything Google returned: consult it before ANY claim about a specific airport. Never say an airport "came back with nothing" or has no through-ticketed service based on a combined search; if an airport the user named shows zero in the breakdown, run a dedicated search with that airport as the ONLY destination before claiming absence, and say what you found.
- When the user names a specific airport and the breakdown shows it served, LEAD with that airport's options; the cheaper neighbor is the alternative, framed as dollars saved against drive time added, never the headline over the place they actually asked for.
- "arrive by / be there by X" is an ARRIVAL constraint (arrival_time), never a departure cap.
- When the user cares about the arrival DAY ("land on Friday"), set arrival_date and pick departure_date by timezone logic. Rules of thumb, not laws: typical daytime trans-Pacific Asia -> US routings land the same local day, but late-evening departures and long westbound routings (via the Middle East or Europe) land the NEXT day; US -> Asia/Europe overnights land the next day. When candidate routings vary widely, run two searches (departing the arrival day AND the day before, both with the same arrival_date) so no valid routing is missed. When they change or relax the arrival day, immediately re-search with the new dates — do not re-serve the old results or just offer to search.
- "via / through <hub>" questions: search with via_airports. That filter checks every itinerary Google returns; the plain result list you see is only a top-6 sample, so NEVER assert that a routing, hub, or airline "doesn't exist" from the plain list — and never say you "confirmed" or "checked directly" unless a via_airports search actually ran this conversation. If you haven't checked, say so and offer to.
- Follow-ups: the cards from earlier turns are STILL on the user's screen. When a turn opens with a bracketed [Results already on this user's screen] block, that block is the record of what they are looking at, numbered as shown. Answer "which was the second one", "how long is that layover", or "book the JetBlue" straight from it rather than searching again. Search afresh the moment the question changes what the record was built from (route, dates, cabin, filters) or needs something it does not contain, and never fill a gap in it by inference. If the conversation has moved on, note that those fares are from the earlier search.
- A follow-up that ADDS a hard constraint (an airline or alliance, nonstop only, a cabin, a specific airport) is a new search, not a reading-comprehension question: run the constrained search immediately in that same turn instead of describing the ledger's thin slice and asking permission to look properly. Describe-then-ask is only right when the constraint is ambiguous.
- Comparisons: run one search per option (at most 4 per turn). A self-arranged overnight stopover = one search per leg with the correct date on each. Give each search a summary naming the option ("Option A: Thursday nonstop").
- Empty first results on busy routes are usually transient: retry the SAME search once before broadening. Once a search succeeds, stop; don't also run overlapping broader variants of a question that's already answered. (Empty searches are hidden from the user's page, so never reference "the empty section above.")
- Multi-city trips (A -> B, B -> C, C -> A, or open-jaw): use trip_type multi_city with multi_city_segments. Google currently withholds combined one-ticket pricing from automated sessions, so the result usually contains each leg priced as its own one-way, paired as separate tickets, plus a direct link to Google's multi-city page for the exact legs. Present the separate-ticket total honestly, warn about bags and missed-connection protection between separate tickets, and ALWAYS point at the link for the one-ticket price ("the combined ticket, sometimes cheaper when one carrier flies every leg, prices in one tap there"). Never claim the through-ticket does not exist, and never present the separate-ticket sum as the only price.
- Make reasonable assumptions and state them briefly instead of interrogating the user. City-level vagueness is yours to resolve (airports, date windows, cabin). But ask ONE brief question before searching when the request is genuinely unresolvable: the origin is missing entirely, or the destination is a whole region or continent ("Europe", "Asia", "somewhere warm"). Offer to choose for them in the same breath, e.g. "Anywhere in Europe in particular? If you're open, I'm happy to compare a few favorites like London, Paris, and Lisbon." Never stack multiple questions, and never ask when a sensible assumption exists.
- Airport precision matters: each result's route field states its true endpoints (e.g. FLL-EWR). Quote airports exactly from that field. Never name an airport the data does not show; EWR is not JFK.
- IATA codes are assertions from memory, and memory misfiles them (PEI is Pereira, Colombia, not Beijing). Every result opens with "Route as searched" naming each airport's real city and country: READ it before writing a word of prose. If any airport there is not in the place the user asked for, say so plainly, ignore its flights entirely, and re-search without it in the same turn. Reliable expansions: NYC -> JFK/LGA/EWR, London -> LHR/LGW/STN, Tokyo -> HND/NRT, Beijing -> PEK/PKX, Shanghai -> PVG/SHA, Seoul -> ICN/GMP.
"""

ANSWER_RULES = """
Answering:
- Deliberation is for hard calls. A single completed search with clear results needs the recommendation written directly; save extended thinking for multi-option comparisons, tricky constraints, or conflicting data.
- Voice: you are a seasoned travel concierge. Courteous, composed, precise, warm but never gushing. Write in full sentences, the way a fine hotel's head concierge would speak. NEVER use em dashes or en dashes anywhere in your replies; use commas, periods, or a colon instead.
- The user sees result cards for every search you run, so don't recite every flight. Lead with your recommendation and the key numbers (totals for multi-leg plans, including a note that hotels/ground costs aren't included), then the trade-offs that matter.
- Results arrive ranked by value, not by fare: the fare plus what its duration and stops actually cost the traveler. Recommend the best-value option rather than reflexively the cheapest, and when the cheapest fare is not your pick, price the difference in plain terms ("$25 more saves you eight hours and a connection"). Flying is tiring; a long layover to save a few dollars is rarely the favour it looks like. If they ask for the cheapest, give them the cheapest without argument.
- Scheduled-but-unpriced flights (price null, or an "UNPRICED" carrier note) are real inventory whose fares Google withheld from THIS session, not from the user: their Book links show live prices in the user's browser, and the withheld carriers often hold the cheaper fares. Never present the cheapest PRICED fare as the route's going rate while unpriced carriers exist; name those carriers, say roughly when they fly, and tell the user their fares appear on Google via Book.
- When a result carries price_context, you know where its cheapest fare sits among NEARBY DATES, and one clause of that belongs in the recommendation ("$239 is among the cheapest dates in this window, and the 14th is only $18 less"). It compares dates, not history: never call a fare good "for this route" in general, never call it a deal in the abstract, and never predict which way prices will move. With no price_context, say nothing at all about whether the fare is good.
- Timestamps carry their own dates: read a flight's arrival DAY from the arrival timestamp itself, never by adding "a day or so" to the departure. lands_plus_days states how many calendar days after departure a flight lands (+1, +2; a dateline crossing can be -1) and matches the +N badge on the user's card. For any "arrive by / land on <day>" question, check each candidate's arrival timestamp against the target day before naming it, and when the arrival date differs from departure, say it outright ("lands the morning of Jan 4, two days after the Jan 2 departure"). If no on-screen option satisfies the day, say so and search rather than bending a near miss.
- Mention real caveats from the data: nothing arrives before X, prices are one-way vs round-trip totals, self-transfer risks, tight or overnight layovers.
- If a search fails or is rate-limited, say so plainly and suggest trying again in a moment.
- Keep responses short and conversational — a few sentences, not a report.
- Formatting: you may use **bold** sparingly for the key number or verdict (it renders properly). No other markdown — no headers, no bullet syntax, no italics-by-asterisk. Never insert a line break inside a sentence; use a blank line only between paragraphs.
- End EVERY final reply with exactly one line in this form (it becomes tappable buttons and is stripped from your prose, so don't also ask the same things in the text):
SUGGESTIONS: ["first likely follow-up", "second", "third"]
2-4 items, each under 9 words, phrased as the user would type them ("check Saturday instead", "only nonstops", "what about Newark?"). Predict the most likely next asks given the results: nearby dates, price/speed trade-offs, filters, alternate airports, booking the pick."""


def _with_price_ctx(data: dict, payload: dict) -> dict:
    ctx = payload.get("price_context")
    if ctx:
        data["price_context"] = ctx
    return data


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
                "price_basis": it.get("price_basis"),
                "one_ticket_price": it.get("combined_price"),
                "separate_tickets_price": it.get("separate_price"),
                "legs": [_leg_summary(p) for p in it["parts"]],
                **({"ticketing_warning": it["warnings"][0]} if it.get("warnings") else {}),
            }
            for it in (payload.get("results") or [])[:5]
        ]
        return json.dumps(_with_price_ctx(
            {"kind": "multi_city", "note": payload.get("message"), "options": rows}, payload))
    if kind == "itineraries":
        rows = [
            {
                "total_price": it["total_price"], "currency": it["currency"],
                "outbound": _leg_summary(it["outbound"]),
                "return": _leg_summary(it["return"]),
                "return_choices": len(it.get("return_options") or []),
            }
            for it in (payload.get("results") or [])[:6]
        ]
        data = {"kind": "round_trips", "note": payload.get("message"), "options": rows}
        more = payload.get("more_outbounds") or []
        if more:
            # without this, the model inferred "no nonstops exist" from an
            # expansion set that simply never included them
            data["unpriced_outbounds"] = {
                "count": len(more),
                "nonstops": sum(1 for m in more if (m.get("stops") or 0) == 0),
                "cheapest_from": min(m["from_total"] for m in more),
                "note": "Listed to the user with from-prices; returns not priced this turn.",
            }
        return json.dumps(_with_price_ctx(data, payload))
    results = payload.get("results") or []
    rows = [_leg_summary(f) for f in results[:6]]
    out = {"kind": "flights", "note": payload.get("message"), "options": rows}
    unpriced = {}
    for f in results:
        if f.get("price") is None:
            unpriced[f.get("airline") or "?"] = unpriced.get(f.get("airline") or "?", 0) + 1
    if unpriced:
        # the Beijing lesson: a fare-withheld carrier must reach the model as
        # inventory, or the priced floor gets declared "the going rate"
        out["unpriced_carriers"] = {
            "counts": unpriced,
            "note": ("Scheduled flights whose fares Google withheld from this "
                     "session; the user's Book link shows real prices. Do not "
                     "call the cheapest priced fare the going rate."),
        }
    if len(results) > 6:
        out["sample_note"] = (
            f"Showing 6 of {len(results)}, ordered by best value (fare plus the cost of the "
            "time and stops it asks of the traveler), so option 1 is the one to lead with "
            "unless they asked for something specific. The cheapest fare is always included "
            "in this sample: if it is not option 1, say what its lower price costs in hours "
            "or stops so they can judge. Routings absent from this sample may still exist — "
            "never claim a hub/airline/routing is unavailable unless a via_airports-filtered "
            "search says so."
        )
    return json.dumps(_with_price_ctx(out, payload))


def _plus_days(dep: str | None, arr: str | None) -> int:
    """Calendar days between departure and arrival, each in its own local time.

    The model was asked "latest departure that still lands Jan 3 morning" and
    answered with a flight whose arrival timestamp read 2027-01-04T09:45 —
    it inferred "next day" from the departure instead of reading the date
    (Evan's catch, July 30). Long-haul east-bound routinely lands +2; a
    dateline crossing can land -1. State the offset; never make the model
    do the calendar math.
    """
    try:
        return (datetime.fromisoformat(arr).date() - datetime.fromisoformat(dep).date()).days
    except (TypeError, ValueError):
        return 0


def _leg_summary(f: dict) -> dict:
    legs = f.get("legs") or []
    out = {
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
    plus = _plus_days(out["depart"], out["arrive"])
    if plus:
        # the "+2" the user sees on the card, stated rather than implied
        out["lands_plus_days"] = plus
    return out


# --------------------------------------------------------------------------
# What the user is still looking at
#
# History was prose only: the frontend replays the reply text, so every
# structured result we shipped vanished at the end of the turn. A follow-up
# ("the second one", "the 6:35 JetBlue", "book that") then left Claude two
# bad options — re-run a search it already paid for, or reconstruct fares
# from its own sentences, which is exactly the fabrication this project
# guards against everywhere else. The ledger is the middle path: a compact,
# NUMBERED record of the cards still on screen, carried into the next turn.
#
# It is deliberately a record of what was SHIPPED, not of what exists: the
# prompt rule that travels with it forbids inferring anything the ledger does
# not literally contain, so a follow-up outside its four corners still costs
# a real search.
# --------------------------------------------------------------------------
LEDGER_ENTRIES = 3        # searches remembered per turn
LEDGER_OPTIONS = 6        # options remembered per search (what the model saw)
LEDGER_TURNS = 2          # turns of history the client echoes back
LEDGER_MAX_CHARS = 12000  # hard cap on client-supplied ledger text


def _ledger_flight(f: dict, n: int | None = None) -> dict:
    legs = f.get("legs") or []
    out = {
        "n": n,
        "airline": f.get("airline"),
        "price": f.get("total_price") if f.get("total_price") is not None else f.get("price"),
        "route": f"{legs[0]['from']}-{legs[-1]['to']}" if legs else None,
        "depart": legs[0].get("departure") if legs else None,
        "arrive": legs[-1].get("arrival") if legs else None,
        "stops": f.get("stops"),
        "duration_min": f.get("duration"),
        # flight numbers make "book the B6 1802" answerable without a search
        "flights": [f"{l.get('airline_code') or ''}{l.get('flight_number') or ''}".strip()
                    for l in legs] or None,
        "warnings": f.get("warnings") or None,
    }
    # arrive-by follow-ups are answered from THIS record, so the day offset
    # must survive into it — the July 30 misread happened one turn after the
    # search, off the ledger, not off the live results
    plus = _plus_days(out["depart"], out["arrive"])
    if plus:
        out["lands_plus_days"] = plus
    return {k: v for k, v in out.items() if v is not None}


def ledger_entry(spec: dict, payload: dict) -> dict | None:
    """One search, as the user still sees it on screen."""
    kind = payload.get("type")
    entry: dict = {
        "search": spec.get("summary") or None,
        "route": f"{'/'.join(spec.get('origins') or [])} to {'/'.join(spec.get('destinations') or [])}",
        "depart": spec.get("departure_date"),
        # a one-way spec often still carries a return_date (the models and the
        # stub both set one); recording it would file a one-way search in the
        # ledger as a round trip
        "return": spec.get("return_date") if spec.get("trip_type") == "round_trip" else None,
        "cabin": spec.get("cabin"),
    }
    entry = {k: v for k, v in entry.items() if v}

    if kind == "dates":
        dates = payload.get("dates") or []
        if not dates:
            return None
        entry["kind"] = "date_prices"
        entry["cheapest_dates"] = sorted(dates, key=lambda d: d["price"])[:LEDGER_OPTIONS]
        return entry
    if kind == "multicity":
        rows = payload.get("results") or []
        if not rows:
            return None
        entry["kind"] = "multi_city"
        entry["options"] = [
            {"n": i + 1, "total_price": it.get("total_price"),
             "price_basis": it.get("price_basis"),
             "legs": [_ledger_flight(p) for p in it.get("parts") or []]}
            for i, it in enumerate(rows[:LEDGER_OPTIONS])
        ]
        return entry
    if kind == "itineraries":
        rows = payload.get("results") or []
        more = payload.get("more_outbounds") or []
        if not rows and not more:
            return None
        entry["kind"] = "round_trips"
        entry["options"] = [
            {"n": i + 1, "total_price": it.get("total_price"),
             "outbound": _ledger_flight(it.get("outbound") or {}),
             "return": _ledger_flight(it.get("return") or {}),
             "return_choices": len(it.get("return_options") or [])}
            for i, it in enumerate(rows[:LEDGER_OPTIONS])
        ]
        if more:
            # the same trap compact_for_model closes: without this, a later
            # turn reads the priced sample as the whole option space
            entry["unpriced_outbounds"] = {
                "count": len(more),
                "nonstops": sum(1 for m in more if (m.get("stops") or 0) == 0),
                "cheapest_from": min(m["from_total"] for m in more if m.get("from_total")),
                "note": "Still on screen, from-priced only; returns price on tap.",
            }
        return entry

    rows = payload.get("results") or []
    if not rows:
        return None
    entry["kind"] = "flights"
    entry["options"] = [_ledger_flight(f, i + 1) for i, f in enumerate(rows[:LEDGER_OPTIONS])]
    if len(rows) > LEDGER_OPTIONS:
        entry["not_listed"] = (f"{len(rows) - LEDGER_OPTIONS} further options are on the "
                               "user's screen but not recorded here; re-search to describe them.")
    return entry


def _turn_ledger(entries: list) -> dict | None:
    """What this turn leaves on screen, for the client to echo back next turn."""
    return {"entries": entries[-LEDGER_ENTRIES:]} if entries else None


_LEDGER_FIELDS = ("search", "route", "depart", "return", "cabin", "kind",
                  "options", "cheapest_dates", "unpriced_outbounds", "not_listed")


def _sanitize_entry(entry) -> dict | None:
    """Reshape one client-supplied entry to the size and shape we emit.

    The client is the user's own browser, so this is not a trust boundary
    (they can type anything into the query anyway) — it is a SIZE boundary.
    Without it a single entry carrying thousands of options walks straight
    into the prompt, and the trim below can only drop whole entries.
    """
    if not isinstance(entry, dict):
        return None
    out: dict = {}
    for k in _LEDGER_FIELDS:
        v = entry.get(k)
        if v is None:
            continue
        if isinstance(v, list):
            v = v[:LEDGER_OPTIONS]
        elif isinstance(v, str):
            v = v[:300]
        out[k] = v
    return out or None


def ledger_context(ledgers: list | None) -> str | None:
    """The bracketed block that carries earlier results into this turn."""
    if not ledgers:
        return None
    entries: list = []
    for led in list(ledgers)[-LEDGER_TURNS:]:
        if isinstance(led, dict):
            entries.extend(led.get("entries") or [])
        elif isinstance(led, list):
            entries.extend(led)
    entries = [e for e in (_sanitize_entry(e) for e in entries) if e][-(LEDGER_ENTRIES * LEDGER_TURNS):]
    if not entries:
        return None
    body = json.dumps({"searches": entries}, default=str)
    while len(body) > LEDGER_MAX_CHARS:
        if len(entries) > 1:
            entries = entries[1:]  # oldest goes first
        elif any(entries[0].get(k) for k in ("options", "cheapest_dates")):
            for k in ("options", "cheapest_dates"):  # then thin the survivor
                if entries[0].get(k):
                    entries[0][k] = entries[0][k][:max(1, len(entries[0][k]) // 2)]
        else:
            return None  # nothing left worth carrying
        body = json.dumps({"searches": entries}, default=str)
    return (
        "[Results already on this user's screen from earlier in this conversation, "
        "numbered as they were shown. Quote these fares, times, and flight numbers "
        "directly when they refer back to one (\"the second one\", \"the 6:35 JetBlue\", "
        "\"book that\") instead of running the search again. You have NOT seen anything "
        "absent from this record: never infer a price, a schedule, a seat, or "
        "availability from it, and run a fresh search whenever the question changes the "
        "route, dates, cabin, or filters. Each price is as of the search that produced "
        "it, so treat it as a quote from earlier in the conversation, not a live fare.]\n"
        + body
    )


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


# searchy follow-ups wear question clothes: "how about jfk to bos friday" is
# a route search, not a knowledge question, and deserves the fast router
_FOLLOWUP_PREFIXES = ("how about ", "what about ", "and ", "also ", "ok ",
                      "okay ", "now ", "then ", "try ")


def looks_like_plain_search(query: str) -> bool:
    """Conservative: only true for 'fly me from A to B' style requests.

    False negatives just mean we keep full effort (today's behaviour), so the
    failure mode of this heuristic is 'no speedup', never 'worse answer'.
    A wrong True is also safe: the router's no-tool-call guard redoes the
    call on the loop model, so the cost of a miss is latency, never quality.
    """
    q = (query or "").strip()
    if len(q) > 140:
        return False
    low = q.lower()
    for p in _FOLLOWUP_PREFIXES:
        if low.startswith(p):
            low = low[len(p):]
            q = q[len(p):]
            break
    if any(low.startswith(op) for op in _QUESTION_OPENERS):
        return False
    return bool(_ROUTE_HINT.search(q))


# --------------------------------------------------------------------------
# What a turn costs, and the knobs that control it
#
# The July 30 outage was an empty API balance, and the post-mortem question
# was "where does the money go?" — answered by measurement, like the July 24
# latency work: ~4.2k tokens of system+tools prefix on 2-3 calls per turn,
# the conversation re-billed at full price on every call, and Opus output at
# the top of the price sheet. Three levers shipped together (July 30):
#
#   - ASSISTANT_MODEL (env-overridable): the loop runs Sonnet 5 — near-Opus
#     on tool-driven work at 40% less ($3/$15 vs $5/$25 per MTok, intro
#     $2/$10 through Aug 31, 2026). Flip the env var back to
#     claude-opus-4-8 in Vercel for an A/B; no code push needed. NOTE:
#     Sonnet 5 runs ADAPTIVE THINKING by default when `thinking` is omitted
#     (Opus 4.8 did not), and max_tokens caps thinking + text TOGETHER —
#     hence the raised MAX_TOKENS. Do not add `thinking: disabled`: with
#     thinking off Sonnet 5 is measurably less willing to call tools, and
#     this loop is nothing but tool calls.
#   - 1h cache TTL on the prefix (write 2x, read 0.1x): traffic here is a
#     session of turns then hours of quiet; the 5-minute default re-paid the
#     prefix write on every think-gap longer than a coffee sip. The prefix is
#     the ONLY thing that survives across turns: the client replays history
#     as bare prose while the server's turn embedded the ledger block and
#     tool traffic, so the message bytes diverge and cross-turn message
#     reads never match. Do not claim otherwise; review caught this once.
#   - a 5-MINUTE breakpoint on the conversation tail: without it the messages
#     section (history + ledger + tool results) re-billed at FULL price on
#     every call of every turn — a search turn is 2-3 calls seconds apart,
#     which is exactly what a 5m entry covers. 1h here would double the
#     write cost to feed cross-turn reads that (see above) cannot happen.
#
# call_cost_usd() turns each call's usage into dollars so debug_timings
# reports real spend per turn; decisions after this one get data, not
# char-count estimates.
# --------------------------------------------------------------------------
ASSISTANT_MODEL = os.environ.get("ASSISTANT_MODEL") or "claude-sonnet-5"
ROUTER_MODEL = "claude-haiku-4-5"
MAX_TOKENS = 8000  # caps thinking + text together on Sonnet 5; raising costs nothing

MODEL_PRICES = {  # USD per 1M tokens (input, output) — LIST prices.
    # Sonnet 5 bills at an introductory $2/$10 through 2026-08-31, so real
    # spend runs ~1/3 below this estimate until then; estimates never go
    # silently stale-low after the intro lapses.
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-5": (5.0, 25.0),
}
CACHE_WRITE_1H = 2.0    # the system+tools prefix
CACHE_WRITE_5M = 1.25   # the conversation tail
CACHE_READ_MULT = 0.1

_CACHE_1H = {"type": "ephemeral", "ttl": "1h"}
_CACHE_5M = {"type": "ephemeral", "ttl": "5m"}


def call_cost_usd(model: str, usage) -> float:
    """Estimated cost of one API call from its usage block, in USD."""
    prices = MODEL_PRICES.get(model)
    if not prices or usage is None:
        return 0.0
    inp, out = prices
    g = lambda o, f: (getattr(o, f, 0) or 0)
    # mixed TTLs bill writes differently; the usage breakdown says which was
    # which, and its absence falls back to the dearer multiplier so the
    # estimate errs high, never silently low
    cc = getattr(usage, "cache_creation", None)
    if cc is not None and (getattr(cc, "ephemeral_5m_input_tokens", None) is not None
                           or getattr(cc, "ephemeral_1h_input_tokens", None) is not None):
        writes = (g(cc, "ephemeral_5m_input_tokens") * CACHE_WRITE_5M
                  + g(cc, "ephemeral_1h_input_tokens") * CACHE_WRITE_1H)
    else:
        writes = g(usage, "cache_creation_input_tokens") * CACHE_WRITE_1H
    return (
        g(usage, "input_tokens") * inp
        + writes * inp
        + g(usage, "cache_read_input_tokens") * inp * CACHE_READ_MULT
        + g(usage, "output_tokens") * out
    ) / 1e6


def cache_tail(messages: list) -> list:
    """The same messages, with a 5m cache breakpoint on the LAST content block.

    WITHIN-turn caching only: call 2 of a turn reads call 1's messages at
    0.1x instead of re-billing them at full price, and the loop's calls are
    seconds apart, so the 5-minute TTL covers them at the cheaper 1.25x
    write. Across turns this entry can never be read — the client replays
    history as bare prose while this turn's bytes embedded the ledger block
    and tool traffic, and history[-12:] slides the front — so a dearer 1h
    write would buy nothing. Copies, never mutates: the loop keeps appending
    to the original list, and a breakpoint frozen into it would pin stale
    mid-conversation markers (only 4 are allowed per request).
    """
    if not messages:
        return messages
    out = list(messages)
    last = out[-1]
    if not isinstance(last, dict):
        return messages
    content = last.get("content")
    if isinstance(content, str) and content:
        blocks = [{"type": "text", "text": content, "cache_control": _CACHE_5M}]
    elif isinstance(content, list) and content and isinstance(content[-1], dict):
        blocks = list(content)
        blocks[-1] = {**blocks[-1], "cache_control": _CACHE_5M}
    else:
        # SDK block objects (a pause_turn resume) — skip rather than mutate
        return messages
    out[-1] = {**last, "content": blocks}
    return out


# --------------------------------------------------------------------------
# Latency: overlap everything the doctrine allows
#
# The August 2 profile of a simple turn was strictly sequential again:
# router 1.9-5s -> search 1.1-3.2s -> synthesis 3.1-4.9s. Three overlaps
# recover most of it, none of which touch what the model sees:
#   - EAGER DISPATCH: a search leaves the moment its tool_use block finishes
#     STREAMING, not when the whole message ends — on comparisons, search 1
#     runs while the model is still writing calls 2-4.
#   - CONNECTION WARMING: the first search after a quiet spell pays a cold
#     TLS handshake through the residential proxy (seconds). At turn start we
#     open Google connections on the exact threads that will carry the real
#     POSTs (fli's persistent executor + this round's pool) with a ~1KB
#     generate_204 — NOT the deleted 1.8MB page-load warmup, which was about
#     cookies and showed no benefit; this is only the TCP/TLS tunnel.
#   - SPECULATION: an unmistakable "JFK to ORD sept 18" query starts its
#     search before the router even runs; the single-flight cache collapses
#     the router's identical spec onto the in-flight request. A mismatched
#     guess is a wasted ~100-300KB cache entry and nothing else — the
#     model's spec always wins. SPECULATE=0 kills it.
# --------------------------------------------------------------------------
def _warm_one() -> None:
    """Open the TLS tunnel to Google on THIS thread's identity handle."""
    try:
        ident = checkout_identity()
        _identity_session(ident).get("https://www.google.com/generate_204", timeout=6)
    except Exception:
        pass  # warming is a bonus; never let it matter


def warm_google_connections(pool=None, n: int = 2) -> None:
    if os.environ.get("WARM_CONNECTIONS") == "0":
        return
    try:
        ex = _get_executor()  # fli's persistent threads carry the expansions
        for _ in range(n):
            ex.submit(_warm_one)
    except Exception:
        pass
    if pool is not None:  # and this pool's threads carry the main POSTs
        try:
            for _ in range(n):
                pool.submit(_warm_one)
        except Exception:
            pass


_MONTHS = {m: i + 1 for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}
_SPECULATE_RE = re.compile(r"^\s*([A-Za-z]{3})\s+to\s+([A-Za-z]{3})\b(.*)$")
_DATE_WORD_RE = re.compile(r"\b([a-z]{3,9})\.?\s+(\d{1,2})\b|\b(\d{1,2})\s*/\s*(\d{1,2})\b")


def speculative_spec(query: str) -> dict | None:
    """The spec the router is about to emit, when the query is unmistakable.

    Deliberately narrow: two literal IATA codes, ' to ' between them, one
    parseable date, no round-trip/flex words. Anything less certain returns
    None — a speculation that guessed WRONG would only waste a cache entry,
    but a narrow trigger keeps the proxy bandwidth honest.
    """
    m = _SPECULATE_RE.match((query or "").strip())
    if not m:
        return None
    a, b, rest = m.group(1).upper(), m.group(2).upper(), m.group(3).lower()
    if not hasattr(Airport, a) or not hasattr(Airport, b):
        return None
    if any(w in rest for w in ("round", "return", "back", "flex", "week", "month", "multi")):
        return None
    d = _DATE_WORD_RE.search(rest)
    if not d:
        return None
    try:
        if d.group(1) is not None:
            mo = _MONTHS.get(d.group(1)[:3])
            day = int(d.group(2))
        else:
            mo, day = int(d.group(3)), int(d.group(4))
        if not mo or not 1 <= day <= 31:
            return None
        date_ = f"{datetime.now().year}-{mo:02d}-{day:02d}"
        datetime.strptime(date_, "%Y-%m-%d")  # reject 2/30-style nonsense
    except (TypeError, ValueError):
        return None
    spec = {"origins": [a], "destinations": [b], "trip_type": "one_way",
            "departure_date": date_}
    if "cheapest" in rest:
        spec["sort"] = "cheapest"
    elif "fastest" in rest:
        spec["sort"] = "fastest"
    return spec


def run_assistant(query: str, history: list | None, emit=None,
                  ledgers: list | None = None) -> dict:
    """Run the agent loop. `emit(event, data)` receives progress as it happens:

      sections     a results batch is ready (cards can render immediately —
                   this lands seconds before the prose is written)
      text_delta   the reply's prose, emitted ONCE per final answer after the
                   call resolves — never live. Live token streaming showed
                   "Let me take a look" and then wiped it the moment a tool
                   call followed (Evan: "what's the point"); prose is now
                   held until a call ends without tools, and the client
                   typewriter supplies the motion. The SUGGESTIONS line is
                   stripped before emitting, so it never types out either.

    With emit=None the behaviour is identical, just silent.
    """
    emit = emit or (lambda *_: None)
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=4)
    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in (history or [])[-12:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    # the cards from earlier turns are still on the user's screen, so they are
    # context, not history to be re-derived; the block is bracketed the same
    # way as the wrap-up status note so it reads as an annotation, not a request
    ledger_note = ledger_context(ledgers)
    messages.append({"role": "user",
                     "content": f"{ledger_note}\n\n{query}" if ledger_note else query})

    sections: list[dict] = []
    ledger_entries: list[dict] = []  # what this turn puts on screen, for the next one
    search_log: list[str] = []  # per-search outcome lines; feeds the wrap-up call
    searches_used = 0
    web_tool = {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}

    # Routing the first call: when the query is plainly "search this route",
    # that call only has to emit a tool_use, which needs no deep reasoning —
    # Haiku emits it in a fraction of the loop model's time (July 25 profile:
    # claude_1 was the single largest fixed cost of a simple turn). A guard
    # below retries on the loop model if Haiku answers in prose instead of
    # calling the tool, so quality is never traded, only latency. Knowledge
    # questions are ANSWERED on the first call, so they keep full effort.
    plain_search = looks_like_plain_search(query)
    first_effort = "low" if plain_search else "medium"
    haiku_router = plain_search  # flips off after one failed Haiku attempt

    # unmistakable queries start their search before the router even runs;
    # the single-flight cache collapses the router's identical spec onto it
    if os.environ.get("SPECULATE") != "0":
        _spec_guess = speculative_spec(query)
        if _spec_guess:
            threading.Thread(target=cached_execute_spec, args=(_spec_guess,),
                             daemon=True).start()

    started = time.monotonic()
    timings: list = []   # [(phase, seconds, detail)] — surfaced for profiling
    cost_usd = 0.0       # estimated API spend this turn, from real usage blocks
    rounds = 0
    iterations = 0
    # turn budget bounds NEW SEARCH ROUNDS only (a Google throttle wave
    # otherwise compounds into multi-minute hangs). It no longer bounds the
    # final prose: when data is on screen, the wrap-up call below ALWAYS runs
    # — the July 25 'ran long' turns had 100% of their data and still ended
    # on a canned line because prose was gated behind this check.
    turn_budget_s = float(os.environ.get("TURN_BUDGET_S") or 58)
    while rounds < 3 and iterations < 8 and time.monotonic() - started < turn_budget_s:
        iterations += 1
        _t = time.monotonic()
        use_haiku = haiku_router and iterations == 1
        call_kwargs: dict = {
            "model": ROUTER_MODEL if use_haiku else ASSISTANT_MODEL,
            "max_tokens": MAX_TOKENS,
            # cache breakpoint on system caches tools+system for every call in
            # the loop and across turns (prompt renders tools -> system -> messages);
            # 1h TTL because think-gaps between turns routinely exceed the 5m default
            "system": [{
                "type": "text",
                "text": assistant_system_prompt(),
                "cache_control": _CACHE_1H,
            }],
            # Haiku 4.5 rejects web_search_20260209 (Opus/Sonnet-tier tool) —
            # the router call only exists to emit search_flights anyway; if
            # the query turns out to need web context, the no-tool-call guard
            # below redoes it on the loop model with the full toolset
            "tools": [SEARCH_TOOL] if use_haiku else [SEARCH_TOOL, web_tool],
            # tail breakpoint: the conversation reads from cache on every call
            # after the one that wrote it, instead of re-billing at full price.
            # NOT on the Haiku router call: caches are model-scoped, so its
            # entries can never be read by the Sonnet calls that follow — the
            # first live probes showed it writing ~4.4k premium-rate tokens
            # per plain search that nothing could ever read.
            "messages": messages if use_haiku else cache_tail(messages),
        }
        if use_haiku:
            # the router only routes: it gets the request-handling half of the
            # prompt (roughly half the tokens, none of the voice rules it will
            # never use), and no cache marker — its prefix sits under Haiku's
            # 4096-token cache minimum anyway
            call_kwargs["system"] = [{"type": "text", "text": assistant_system_prompt(router=True)}]
        else:
            # synthesis (any call after results are in) always runs at full
            # effort: that is where the recommendation is actually written.
            # (Haiku 4.5 rejects output_config.effort — loop model only.)
            call_kwargs["output_config"] = {"effort": first_effort if iterations == 1 else "medium"}

        # the pool exists BEFORE the model call so that (a) its threads can
        # pre-open TLS to Google while the model writes, and (b) each search
        # can leave the moment its tool_use block finishes streaming
        pool = ThreadPoolExecutor(max_workers=4)
        if iterations == 1:
            warm_google_connections(pool)
        fired: dict = {}  # tool_use_id -> Future, dispatched mid-stream

        def _staggered(spec, delay):
            # five simultaneous requests from one address is the least human
            # thing we do; a short ramp keeps the burst off Google's radar
            # while still overlapping the slow part of each search
            if delay:
                time.sleep(delay)
            return cached_execute_spec(spec)

        def _fire(block) -> None:
            nonlocal searches_used
            if block.id in fired or searches_used >= 5:
                return
            searches_used += 1
            fired[block.id] = pool.submit(_staggered, dict(block.input or {}), len(fired) * 0.4)

        try:
            # no live token emission: whether this text is the answer or a
            # doomed preamble is only knowable once the call resolves. The
            # events are still read one by one so every completed
            # search_flights block dispatches IMMEDIATELY — on a comparison,
            # search 1 runs while the model is still writing calls 2-4.
            with client.messages.stream(**call_kwargs) as stream:
                for event in stream:
                    if getattr(event, "type", "") != "content_block_stop":
                        continue
                    snap = getattr(stream, "current_message_snapshot", None)
                    blocks = getattr(snap, "content", None) or []
                    idx = getattr(event, "index", None)
                    if idx is None or idx >= len(blocks):
                        continue
                    b = blocks[idx]
                    if getattr(b, "type", "") == "tool_use" and getattr(b, "name", "") == "search_flights":
                        _fire(b)
                response = stream.get_final_message()
        except anthropic.APIStatusError:
            # never cancel fired work: it lands in the search cache and the
            # redo (or the user's retry) collapses onto it
            pool.shutdown(wait=False)
            if not use_haiku:
                raise
            # the router is an optimization, never a failure mode: any API
            # rejection of the Haiku call redoes it on the loop model
            haiku_router = False
            iterations -= 1
            timings.append(("haiku_fallback", round(time.monotonic() - _t, 2), "api error; redoing on the loop model"))
            continue

        u = getattr(response, "usage", None)
        cost_usd += call_cost_usd(call_kwargs["model"], u)
        timings.append((
            f"claude_{iterations}", round(time.monotonic() - _t, 2),
            f"in={getattr(u, 'input_tokens', '?')} "
            f"cache_w={getattr(u, 'cache_creation_input_tokens', '?')} "
            f"cache_r={getattr(u, 'cache_read_input_tokens', '?')} "
            f"out={getattr(u, 'output_tokens', '?')} stop={response.stop_reason} "
            f"~${call_cost_usd(call_kwargs['model'], u):.4f}",
        ))

        # server-side web search can pause mid-loop; re-send to let it resume
        if response.stop_reason == "pause_turn":
            pool.shutdown(wait=False)  # anything fired keeps warming the cache
            messages.append({"role": "assistant", "content": response.content})
            continue

        tool_uses = [b for b in response.content if b.type == "tool_use"]

        # the Haiku router exists only to emit a search call fast; if it chose
        # to answer instead, discard that answer and redo the call on the loop
        # model so reply quality is exactly what it would have been without it
        if use_haiku and not tool_uses:
            pool.shutdown(wait=False)
            haiku_router = False
            iterations -= 1
            timings.append(("haiku_fallback", 0.0, "no tool call; redoing on the loop model"))
            continue
        if response.stop_reason != "tool_use" or not tool_uses:
            pool.shutdown(wait=False)
            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            text, suggestions = split_suggestions(text)
            if response.stop_reason == "max_tokens" and not text:
                # the cap can land mid-think (thinking and text share it), and
                # a bare ellipsis is not an answer anyone can act on
                text = ("That one took more thinking room than I gave it. "
                        "Ask me again, or narrow the question a little.")
            if text:
                emit("text_delta", text)  # the whole answer, once; never a wipe
            return {"message": text or "…", "sections": sections,
                    "suggestions": suggestions, "timings": timings,
                    "cost_usd": round(cost_usd, 4),
                    "ledger": _turn_ledger(ledger_entries)}
        rounds += 1

        messages.append({"role": "assistant", "content": response.content})

        # most searches are already in flight (dispatched as their blocks
        # finished streaming); this pass fires any the event loop missed and
        # writes the budget refusals for the rest
        tool_results_by_id: dict = {}
        for tu in tool_uses:
            if tu.id not in fired:
                _fire(tu)
            if tu.id not in fired:
                tool_results_by_id[tu.id] = {
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": "Search budget for this turn exhausted; answer with what you have.",
                    "is_error": True,
                }
        budgeted = [tu for tu in tool_uses if tu.id in fired]
        futures = fired

        _tsearch = time.monotonic()
        batch_deadline = time.monotonic() + 55

        for tu in budgeted:
            label = (tu.input or {}).get("summary") or "search"
            try:
                payload = futures[tu.id].result(timeout=max(1.0, batch_deadline - time.monotonic()))
                sections.append(payload)
                entry = ledger_entry(tu.input or {}, payload)
                if entry:
                    ledger_entries.append(entry)
                n = len(payload.get("results") or payload.get("dates") or [])
                search_log.append(f"'{label}': completed, {n} options on screen"
                                  if n else f"'{label}': came back EMPTY (transient hiccup or no service)")
                tool_results_by_id[tu.id] = {
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": compact_for_model(payload),
                }
            except FuturesTimeout:
                search_log.append(f"'{label}': TIMED OUT, no data")
                tool_results_by_id[tu.id] = {
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": "Search timed out: Google Flights is responding very slowly right now. Tell the user plainly and suggest trying again shortly; do not retry now.",
                    "is_error": True,
                }
            except SearchHTTPError as e:
                detail = "Google Flights is rate-limiting right now (HTTP 429)." if e.status_code == 429 \
                    else f"Google Flights returned HTTP {e.status_code}."
                search_log.append(f"'{label}': FAILED ({detail})")
                tool_results_by_id[tu.id] = {
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": detail, "is_error": True,
                }
            except SearchClientError:
                search_log.append(f"'{label}': FAILED (couldn't reach Google)")
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
        timings.append((f"searches_{rounds}", round(time.monotonic() - _tsearch, 2),
                        f"n={len(budgeted)} fired mid-stream; this phase is only the wait "
                        "AFTER the model call | " + " | ".join(search_log[-len(budgeted):])))
        # cards can render now, well before the prose that describes them
        fresh = [s for s in sections if (s.get("results") or s.get("dates"))]
        if fresh:
            emit("sections", fresh)
        messages.append({"role": "user", "content": [tool_results_by_id[tu.id] for tu in tool_uses]})

    # Out of budget for more searching. If data landed, the user still gets a
    # real recommendation: one final call with tool_choice "none" (same tools
    # list keeps the prompt-cache prefix; "none" means it cannot start more
    # work, so this adds ~5s, never a hang). The status note makes the prose
    # precise about what, if anything, is actually missing.
    if sections:
        try:
            status = "; ".join(search_log) or "none"
            messages.append({"role": "user", "content": (
                "[turn status: no time left for additional searches this turn. "
                f"Search outcomes: {status}. Write your reply now from the completed "
                "results above. State plainly whether anything the user asked for is "
                "missing; if every requested search completed, do NOT apologize or "
                "mention slowness at all. If something is missing, name exactly what, "
                "and offer to fetch it as a follow-up.]"
            )})
            _t = time.monotonic()
            with client.messages.stream(
                model=ASSISTANT_MODEL,
                max_tokens=MAX_TOKENS,
                output_config={"effort": "medium"},
                system=[{
                    "type": "text",
                    "text": assistant_system_prompt(),
                    "cache_control": _CACHE_1H,
                }],
                tools=[SEARCH_TOOL, web_tool],
                # tool_choice invalidates the MESSAGES cache tier (system and
                # tools still read), so a tail write here could never be read
                # back: plain messages, no marker, no wasted 2x write
                tool_choice={"type": "none"},
                messages=messages,
            ) as stream:
                response = stream.get_final_message()
            cost_usd += call_cost_usd(ASSISTANT_MODEL, getattr(response, "usage", None))
            timings.append(("claude_wrapup", round(time.monotonic() - _t, 2), ""))
            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            text, suggestions = split_suggestions(text)
            if text:
                emit("text_delta", text)
                return {"message": text, "sections": sections,
                        "suggestions": suggestions or ["keep digging"], "timings": timings,
                        "cost_usd": round(cost_usd, 4),
                        "ledger": _turn_ledger(ledger_entries)}
        except Exception:
            pass  # canned fallback below; never lose the data over prose
        return {
            "message": "That search ran long, so here is what I have so far. The cards below are live results; say the word and I'll keep digging.",
            "sections": sections,
            "suggestions": ["keep digging"],
            "timings": timings,
            "cost_usd": round(cost_usd, 4),
            "ledger": _turn_ledger(ledger_entries),
        }
    return {
        "message": "Google Flights is responding slowly at the moment and that search ran past my patience. Give it a minute and try again; your question was perfectly fine.",
        "sections": [],
        "suggestions": ["try again"],
        "timings": timings,
        "cost_usd": round(cost_usd, 4),
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


def multi_city_search_url(segments: list, cabin: str | None = None) -> str:
    """The Google multi-city SEARCH page for these legs (no flights chosen).

    August 2: Google gated the multi-city GetShoppingResults endpoint behind
    BotGuard attestation (verified by header capture in a live browser:
    identical body 133 bytes without the token, 35,770 with). A server
    cannot mint that token, so the combined one-ticket price now lives one
    tap away instead of inline: this tfs deep link (search-level: date and
    endpoints per segment, trip type 3, no flight numbers) loads Google's
    own pricing for exactly these legs — browser-verified today."""
    try:
        seat = CABIN_MAP.get(cabin or "economy", SeatType.ECONOMY).value
        body = _pb_int(1, 28) + _pb_int(2, 2)
        for seg in segments:
            org = (seg.get("origins") or [None])[0]
            dst = (seg.get("destinations") or [None])[0]
            if not org or not dst or not seg.get("date"):
                return "https://www.google.com/travel/flights"
            body += _pb_msg(3,
                _pb_str(2, seg["date"])
                + _pb_msg(13, _pb_int(1, 1) + _pb_str(2, str(org).upper()))
                + _pb_msg(14, _pb_int(1, 1) + _pb_str(2, str(dst).upper())))
        body += _pb_int(8, seat) + _pb_int(9, 1) + _pb_int(14, 1)
        body += _pb_msg(16, _pb_int(1, -1))
        body += _pb_int(19, 3)
        tfs = base64.urlsafe_b64encode(body).decode().rstrip("=")
        return f"https://www.google.com/travel/flights?tfs={tfs}&curr=USD"
    except Exception:
        return "https://www.google.com/travel/flights"


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


# --------------------------------------------------------------------------
# Deep links to the exact itinerary
#
# "Book" used to hand Google a text query ("flights from FLL to JFK on ..."),
# which lands on a results page where the user has to hunt for the flight they
# just clicked. Google's own booking URLs carry a `tfs` parameter: a base64url
# protobuf naming the precise itinerary, right down to airline and flight
# number. We build that ourselves, so Book opens on the chosen flight with its
# booking options already showing.
#
# Schema recovered from a real Google-issued link and verified by reproducing
# that link byte for byte (see _tfs_segment for the per-segment layout):
#   1: 28 (constant)   2: 2 (constant)
#   3: segment (repeated: one per journey leg; a segment's own repeated f4
#      entries are the connecting flights that make it up)
#   8: seat class   9: adults   14: 1 (constant)   16: {1: -1} (no max price)
#   19: TRIP TYPE — 1 round trip, 2 one-way, 3 multi-city
# Note f2 is NOT the trip type: putting it there made Google reject the URL
# and fall back to its home page. Verified against real Google links for all
# three trip types.
# The `tfu` token in Google's own links is session-scoped; it is not required.
# --------------------------------------------------------------------------
def _pb_varint(n: int) -> bytes:
    if n < 0:
        n += 1 << 64
    out = b""
    while True:
        chunk = n & 0x7F
        n >>= 7
        out += bytes([chunk | (0x80 if n else 0)])
        if not n:
            return out


def _pb_int(field: int, value: int) -> bytes:
    return _pb_varint(field << 3) + _pb_varint(value)


def _pb_str(field: int, value: str) -> bytes:
    raw = str(value).encode()
    return _pb_varint((field << 3) | 2) + _pb_varint(len(raw)) + raw


def _pb_msg(field: int, body: bytes) -> bytes:
    return _pb_varint((field << 3) | 2) + _pb_varint(len(body)) + body


def _tfs_segment(legs: list) -> bytes:
    """One flown segment: its date, every leg it is made of, and its endpoints."""
    first, last = legs[0], legs[-1]
    body = _pb_str(2, first.departure_datetime.strftime("%Y-%m-%d"))
    for lg in legs:
        body += _pb_msg(4, (
            _pb_str(1, lg.departure_airport.name)
            + _pb_str(2, lg.departure_datetime.strftime("%Y-%m-%d"))
            + _pb_str(3, lg.arrival_airport.name)
            + _pb_str(5, airline_code(lg.airline) or "")
            + _pb_str(6, str(lg.flight_number))
        ))
    body += _pb_msg(13, _pb_int(1, 1) + _pb_str(2, first.departure_airport.name))
    body += _pb_msg(14, _pb_int(1, 1) + _pb_str(2, last.arrival_airport.name))
    return body


def itinerary_url(segments: list, cabin: str | None = None, trip_type: int = 2,
                  adults: int = 1) -> str | None:
    """Deep link straight to these exact flights, or None if we can't build one."""
    try:
        seat = CABIN_MAP.get(cabin or "economy", SeatType.ECONOMY).value
        body = _pb_int(1, 28) + _pb_int(2, 2)
        for legs in segments:
            if not legs or not legs[0].departure_datetime:
                return None
            body += _pb_msg(3, _tfs_segment(legs))
        body += _pb_int(8, seat) + _pb_int(9, max(1, adults)) + _pb_int(14, 1)
        body += _pb_msg(16, _pb_int(1, -1))
        body += _pb_int(19, trip_type)
        tfs = base64.urlsafe_b64encode(body).decode().rstrip("=")
        return f"https://www.google.com/travel/flights?tfs={tfs}"
    except Exception:
        return None  # any surprise in the data falls back to the search URL


def result_booking_url(result, cabin: str | None = None, ret_date: str | None = None) -> str:
    # built from the itinerary's OWN legs — multi-airport searches mean each
    # result can have a different origin/destination than the search defaults
    legs = result.legs or []
    if not legs:
        return "https://www.google.com/travel/flights"
    dep = legs[0].departure_airport.name
    arr = legs[-1].arrival_airport.name
    dep_date = legs[0].departure_datetime.strftime("%Y-%m-%d") if legs[0].departure_datetime else None
    # one-way deep link; round trips are linked as a pair by the caller
    if not ret_date:
        deep = itinerary_url([legs], cabin)
        if deep:
            return deep
    return google_flights_url(dep, arr, dep_date, ret_date, cabin)


@lru_cache(maxsize=1)
def airport_coords() -> dict:
    import airportsdata
    return airportsdata.load("IATA")


def coords_for(code: str) -> dict | None:
    a = airport_coords().get(code)
    return {"code": code, "lat": a["lat"], "lon": a["lon"]} if a else None


def airport_place(code: str) -> str:
    """'PEK (Beijing, CN)' — the fact that unmasks a hallucinated code.

    August 2: the model emitted PEI for Beijing. PEI is Pereira, Colombia;
    its cheap two-stops via Bogota out-ranked the real Beijing fares and
    the cards presented Colombia as China. A code is an assertion the model
    makes from memory, so every search result now states each airport's
    actual place, making a wrong code contradict itself in-context."""
    a = airport_coords().get(code)
    return f"{code} ({a['city']}, {a['country']})" if a and a.get("city") else code


def route_echo(origins: list, destinations: list) -> str:
    return ("Route as searched: "
            + " / ".join(airport_place(a.name) for a in origins)
            + " -> "
            + " / ".join(airport_place(a.name) for a in destinations)
            + ". If any airport is NOT in the place the user asked for, say so and re-search without it immediately.")


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


# --------------------------------------------------------------------------
# What a trip actually costs you
#
# Ranking by fare alone buries the flight a reasonable person would pick. On
# BOS->FLL Sept 2 the first nonstop sat at rank #21, behind nine near-identical
# connections that cost $25 less and took 2 to 9 hours longer — and because the
# model only sees the top 6, Claude never saw a nonstop either.
#
# So the default order is by EFFECTIVE cost: the fare plus a plain dollar value
# on the time and hassle the itinerary asks of you. The weights are deliberately
# conservative and legible; they are a product judgement, not a tuned model.
# Anyone who wants pure price can still say "cheapest" or use the sort control,
# and the outright cheapest fare is always kept visible in the preview.
# --------------------------------------------------------------------------
HOUR_VALUE = 25.0          # one hour of a leisure traveler's day, in USD
FIRST_STOP_COST = 35.0     # deplaning, re-boarding, misconnect risk
EXTRA_STOP_COST = 45.0     # each stop beyond the first hurts more than the last
WARNING_COST = {           # keyed against the warnings we already generate
    "self-transfer": 55.0,     # you carry the risk if the first leg slips
    "airport change": 60.0,    # a ground transfer mid-trip
    "tight": 40.0,             # sub-45-minute connection
    "overnight": 35.0,         # a night spent in transit
}
PREVIEW_GUARANTEE = 4      # the cheapest fare never falls below this position
# an explicitly requested axis wins over our ranking, and when it does we must
# not claim the top row is "best value" — we did not rank by value at all
EXPLICIT_SORTS = ("cheapest", "fastest", "departure_time")
RANK_POOL = 120            # how many raw results we score before cutting
SHIP_LIMIT = 50            # how many we send to the browser after ranking
COMBO_POOL = 30            # same idea for round-trip combinations
NONSTOP_QUOTA = 8          # nonstops guaranteed a place in what we ship


def trip_cost(price, duration_min, stops, warnings, fastest_min) -> float:
    """Fare plus the value of the time and hassle this itinerary costs you."""
    if price is None:
        return float("inf")
    cost = float(price)
    if duration_min and fastest_min:
        cost += max(0.0, (duration_min - fastest_min) / 60.0) * HOUR_VALUE
    if stops:
        cost += FIRST_STOP_COST + EXTRA_STOP_COST * max(0, int(stops) - 1)
    for w in warnings or []:
        low = str(w).lower()
        for key, penalty in WARNING_COST.items():
            if key in low:
                cost += penalty
                break
    return cost


def order_by_value(rows: list, price_of, duration_of, stops_of, warnings_of,
                   requested_sort: str | None) -> list:
    """Best value first, with the outright cheapest fare kept in the preview.

    An explicitly requested axis ("cheapest", "fastest") always wins: if the
    traveler asked for the cheapest, that is what leads.
    """
    if requested_sort in EXPLICIT_SORTS:
        return rows
    durations = [d for d in (duration_of(r) for r in rows) if d]
    fastest = min(durations) if durations else None
    scored = sorted(rows, key=lambda r: trip_cost(
        price_of(r), duration_of(r), stops_of(r), warnings_of(r), fastest))

    # A price-first traveler must still see the cheapest fare without expanding
    # the list, even when its time cost sinks it in the value ranking.
    priced = [r for r in scored if price_of(r) is not None]
    if priced:
        cheapest = min(priced, key=price_of)
        idx = next(i for i, r in enumerate(scored) if r is cheapest)
        if idx >= PREVIEW_GUARANTEE:
            scored.insert(PREVIEW_GUARANTEE - 1, scored.pop(idx))
    return scored


def retain_representative(rows: list, limit: int, price_of, duration_of, stops_of) -> list:
    """Cut to `limit` while keeping the list an honest sample of the options.

    The frontend's filters (nonstop only, cheapest, fastest) run over whatever
    we ship, so the shipped set has to represent the whole option space or the
    filters quietly lie. Measured BOS->FLL Nov 22: 12 nonstops existed but 11
    priced above the 50th-cheapest fare, so "Nonstop only" reported "1 of 50"
    and Claude told the user everything else required a connection.

    Value order still decides the ORDER; this only protects what survives.
    """
    if len(rows) <= limit:
        return rows
    keep = rows[:limit]
    present = lambda x: any(r is x for r in keep)
    slot = limit - 1

    def force(pick):
        nonlocal slot
        if pick is None or slot < 0 or present(pick):
            return
        keep[slot] = pick
        slot -= 1

    # nonstops first: it is the most-asked-for filter in flight search, and a
    # pricier nonstop is a legitimate answer to "just get me there"
    kept_nonstops = sum(1 for r in keep if stops_of(r) == 0)
    for r in rows:                       # rows are already value-ordered
        if kept_nonstops >= NONSTOP_QUOTA:
            break
        if stops_of(r) == 0 and not present(r):
            force(r)
            kept_nonstops += 1
    force(min((r for r in rows if price_of(r) is not None), key=price_of, default=None))
    force(min((r for r in rows if duration_of(r)), key=duration_of, default=None))
    # forcing fills slots from the back, which would leave the rescued rows in
    # reverse value order; restore the ranking we computed
    rank = {id(r): i for i, r in enumerate(rows)}
    keep.sort(key=lambda r: rank.get(id(r), len(rows)))
    return keep


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


# expansion phase: ship what's priced at the deadline; one grace extension
# when nothing has landed at all (a cold proxy handshake can eat seconds)
# --------------------------------------------------------------------------
# Multi-airport searches: the named airport must survive the neighbors
#
# The Gainesville bug (Evan's catch, Aug 2): "NYC to Gainesville" searched
# [GNV, MCO] together, Google returned GNV service (American via MIA, $397),
# and every layer of the pipeline then buried it — the expansion picker chose
# only MCO flights (cheapest/nonstop/fastest are all MCO when the neighbor
# is a hub), the value-ordered more_outbounds cut dropped the pricier GNV
# one-stops entirely, and the model, shown 33 MCO rows, told the user GNV
# "came back with nothing through-ticketed." A dedicated GNV search found
# the flights instantly. The shipped set must represent the option space —
# and in a multi-airport search that means EVERY airport with service keeps
# a seat, and the model is told the per-airport truth outright.
# --------------------------------------------------------------------------
def _dest_of_result(f):
    legs = getattr(f, "legs", None)
    return getattr(getattr(legs[-1], "arrival_airport", None), "name", None) if legs else None


def _dest_of_row(f: dict):
    legs = f.get("legs") or []
    return legs[-1]["to"] if legs else None


def rescue_airports(kept: list, ranked_all: list, dest_of) -> list:
    """Re-insert the best-ranked row for any airport the cut erased.

    `ranked_all` is already value-ordered; rescued rows keep that order and
    the list may grow a few rows past its cap — a cap is a product judgment,
    an airport silently vanishing is a lie. dest_of may return None to
    exempt a row's airport from the guarantee.
    """
    have = {dest_of(r) for r in kept}
    rescued = list(kept)
    kept_ids = {id(r) for r in kept}
    for r in ranked_all:
        a = dest_of(r)
        if a is not None and a not in have and id(r) not in kept_ids:
            rescued.append(r)
            have.add(a)
    if len(rescued) > len(kept):
        rank = {id(r): i for i, r in enumerate(ranked_all)}
        rescued.sort(key=lambda r: rank.get(id(r), 1 << 30))
    return rescued


def carrier_note(flights: list) -> str | None:
    """The by-airline truth, including the fares Google withheld.

    Beijing->Chengdu, Aug 2, the actual root cause: Google served this
    session China Eastern's SCHEDULE (12 flights) but not its fares, and
    every layer counted only priced rows — so the reply called $421 'the
    going rate across the board' while the cheaper carrier sat unpriced in
    the same payload. Unpriced inventory is inventory: the user's own
    browser WILL price it through the Book link. Say what is known."""
    priced: dict = {}
    unpriced: dict = {}
    for f in flights:
        a = f.get("airline") or "?"
        if f.get("price") is not None:
            n, best = priced.get(a, (0, None))
            priced[a] = (n + 1, f["price"] if best is None or f["price"] < best else best)
        else:
            unpriced[a] = unpriced.get(a, 0) + 1
    if not unpriced:
        return None
    parts = [f"{a} {n} from ${best:.0f}" for a, (n, best) in
             sorted(priced.items(), key=lambda kv: kv[1][1] or 1e9)]
    up = [f"{a} {n}" for a, n in sorted(unpriced.items(), key=lambda kv: -kv[1])]
    return (
        ("By airline: " + ", ".join(parts) + ". " if parts else "")
        + "SCHEDULED BUT UNPRICED: " + ", ".join(up)
        + " — Google withheld these carriers' fares from this session, NOT from "
          "the user: each flight's Book link will show its real price in their "
          "browser, and withheld carriers often hold the cheaper fares. Never "
          "present the cheapest priced fare as the route's going rate; name the "
          "unpriced carriers and say their fares appear on Google."
    )


def airport_breakdown(items: list, dest_of, price_of) -> str | None:
    """'By destination: MCO 30 from $215, GNV 4 from $397' — or None if
    only one airport is in play. Computed over the FULL pre-cut set, so the
    model knows the truth even about options that did not ship."""
    stats: dict = {}
    for it in items:
        a = dest_of(it)
        if not a:
            continue
        p = price_of(it)
        n, best = stats.get(a, (0, None))
        stats[a] = (n + 1, p if p is not None and (best is None or p < best) else best)
    if len(stats) < 2:
        return None
    parts = [f"{airport_place(a)} {n} from ${best:.0f}" if best is not None
             else f"{airport_place(a)} {n}"
             for a, (n, best) in sorted(stats.items(), key=lambda kv: -kv[1][0])]
    return "By destination airport: " + ", ".join(parts) + "."


EXPANSION_BUDGET_S = 8.0
EXPANSION_GRACE_S = 7.0


def representative_outbounds(flights: list, top_n: int) -> list:
    """Choose WHICH outbounds get their returns priced.

    fli expands flights[:top_n], and the list arrives in the search's sort
    order (usually cheapest-first), so the expansion set used to be "the N
    cheapest outbounds" — a $700 nonstop never got its returns priced, and
    the assistant then told the user nonstops didn't exist. Same disease as
    cutting before ranking, one stage earlier.

    Keeps, in order: the sort's own top picks (an explicit "cheapest" stays
    obeyed), each destination airport's cheapest (the Gainesville bug: a
    hub neighbor's nonstops otherwise claim every slot and the named field
    never gets a single return priced), nonstops, the fastest, then one
    flight from any departure bucket (before 6, 6-12, 12-18, after 18)
    still unrepresented.
    """
    if len(flights) <= top_n:
        return list(flights)
    chosen: list = []
    seen: set = set()

    def add(f) -> None:
        if id(f) not in seen and len(chosen) < top_n:
            seen.add(id(f))
            chosen.append(f)

    for f in flights[: max(3, top_n // 3)]:
        add(f)
    airports_covered = {_dest_of_result(f) for f in chosen}
    for f in flights:  # each served airport's best-ranked outbound gets a seat
        a = _dest_of_result(f)
        if a not in airports_covered:
            add(f)
            airports_covered.add(a)
    for f in flights:  # nonstops, cheapest first; leave 2 slots for the rest
        if len(chosen) >= top_n - 2:
            break
        if (f.stops or 0) == 0:
            add(f)
    add(min(flights, key=lambda f: f.duration or 1e9))

    def bucket(f) -> int:
        dt = f.legs[0].departure_datetime if f.legs else None
        return -1 if dt is None else (0 if dt.hour < 6 else 1 if dt.hour < 12 else 2 if dt.hour < 18 else 3)

    covered = {bucket(f) for f in chosen}
    for f in flights:
        b = bucket(f)
        if b >= 0 and b not in covered:
            add(f)
            covered.add(b)
    for f in flights:  # backfill with the sort's own order
        add(f)
    return chosen


class RepresentativeSearch(SearchFlights):
    """SearchFlights with three app-side fixes to next-leg expansion:

    1. Round trips expand a REPRESENTATIVE outbound set (not the N cheapest)
       and keep the full outbound list so unexpanded outbounds still ship
       with honest "from" totals.
    2. Workers INHERIT the search's identity. fli's pool threads had no
       bound identity, so each worker checked out "the warmest" — usually
       right, but nondeterministic the moment two searches overlap.
    3. Expansion fan-out capped at 6 concurrent requests — a browser's
       per-host connection count — now that per-thread handles actually
       let them overlap (see _identity_session).

    This reimplements fli's _expand_multi_leg loop; if fli's version grows
    new behavior, revisit this override.
    """

    last_outbounds: list = []

    def _expand_multi_leg(self, flights, filters, *, top_n,
                          currency=None, language=None, country=None):
        num_segments = len(filters.flight_segments)
        selected = sum(1 for s in filters.flight_segments if s.selected_flight is not None)
        if selected >= num_segments - 1:
            return flights
        if selected == 0 and num_segments == 2:
            self.last_outbounds = list(flights)
            flights = representative_outbounds(flights, top_n)
        parent_ident = getattr(_local, "identity", None)
        candidates = list(flights[:top_n])

        # a slow-stream wave makes every expansion miss a tight timeout while
        # the (small, fast) outbound fetch succeeds; on retry attempts, give
        # expansions room to actually finish instead of re-dying at 12s
        attempt = getattr(self, "_attempt", 0)
        exp_timeout = 12.0 if attempt == 0 else 22.0
        exp_budget = EXPANSION_BUDGET_S if attempt == 0 else 12.0
        exp_grace = EXPANSION_GRACE_S if attempt == 0 else 14.0

        def expand(outbound):
            if parent_ident is not None:
                _local.identity = parent_ident  # same visitor, own curl handle
            _local.fli_timeout = exp_timeout  # harvest abandons stragglers anyway
            next_filters = copy.deepcopy(filters)
            next_filters.flight_segments[selected].selected_flight = outbound
            sub = self._fetch_flights(next_filters, currency=currency, language=language,
                                      country=country, capture_session=False)
            if sub is None:
                return outbound, None
            if selected + 1 < num_segments - 1:
                return outbound, self._expand_multi_leg(
                    sub, next_filters, top_n=top_n,
                    currency=currency, language=language, country=country)
            return outbound, sub

        # Budgeted harvest: a couple of slow-pathed or retried expansions used
        # to hold the WHOLE search hostage (runs measured 6.9-17.8s on the
        # same data). Ship every pairing that's ready at the deadline; the
        # stragglers simply stay in more_outbounds with from-prices, one tap
        # from priced. Zero successes extend the wait rather than fail fast.
        import concurrent.futures as _cf
        ex = _get_executor()
        pending = {ex.submit(expand, ob) for ob in candidates}
        expansions, first_exc, graced = [], None, False
        deadline = time.monotonic() + exp_budget
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if expansions or graced:
                    break
                graced = True  # nothing landed yet: extend once, then ship regardless
                deadline = time.monotonic() + exp_grace
                remaining = exp_grace
            done, pending = _cf.wait(pending, timeout=remaining,
                                     return_when=_cf.FIRST_COMPLETED)
            for f in done:
                try:
                    expansions.append(f.result())
                except Exception as e:  # noqa: BLE001 — surfaced below if nothing worked
                    first_exc = first_exc or e
        combos = []
        for outbound, nxt in expansions:
            if nxt is None:
                continue
            for n_ in nxt:
                combos.append((outbound,) + n_ if isinstance(n_, tuple) else (outbound, n_))
        if not combos and first_exc is not None:
            raise first_exc  # every expansion failed: let the ladder retire the identity
        return combos


def run_search(search: SearchFlights, filters: FlightSearchFilters, sort: SortBy,
               top_n: int, budget_s: float = 35.0):
    # Refusals come as HTTP 200 + a tiny body (parses to empty) or as raised
    # client errors. Both gates are answered the same way: retire the identity
    # (cookies + exit IP + fingerprint) so the retry is a different visitor,
    # and let the breaker impose quiet if the whole process is mid-wave.
    #
    # budget_s bounds the WHOLE ladder. Retries re-run the entire search —
    # for multi-city that includes the full leg-2 fan-out, so four attempts of
    # a 25s expansion plus the old 2/4/6s sleeps produced 100s+ tails that
    # blew the 65s turn budget and reached the user as "that ran long".
    # Sleeps between attempts are short now: each retry is a NEW identity, so
    # per-search cooling is redundant with the process-wide breaker.
    started = time.monotonic()
    last_exc = None
    _local.route_airlines = None  # each search reads only its own manifest
    for i, attempt_sort in enumerate((sort, sort, SortBy.CHEAPEST, SortBy.CHEAPEST)):
        if i > 0 and time.monotonic() - started > budget_s:
            break  # out of time: surface what we have rather than dig the hole deeper
        breaker_wait(1.5 if i == 0 else 6.0)
        checkout_identity()
        # attempt 1 fails fast into a fresh identity; retries get room for
        # Google's slow progressive streams to actually finish downloading
        _local.fli_timeout = 12.0 if i == 0 else 28.0
        search._attempt = i  # expansion patience scales with the attempt too
        attempt = filters.model_copy(update={"sort_by": attempt_sort})
        try:
            results = search.search(attempt, top_n=top_n, currency="USD")
        except SearchClientError as e:
            last_exc = e
            retire_identity()
            note_search_outcome(False)
            time.sleep(0.5 + 0.4 * i + random.uniform(0, 0.4))
            continue
        if results:
            note_search_outcome(True)
            _attempt_stats["ok_on_attempt"].append(i + 1)
            return results
        retire_identity()
        note_search_outcome(False)
        time.sleep(0.4 * (i + 1) + random.uniform(0, 0.4))
    if last_exc:
        raise last_exc
    return []


def missing_from_manifest(results: list, manifest: dict | None) -> dict:
    """Carriers Google's own route data names but this session's rows lack."""
    if not manifest:
        return {}
    seen = set()
    for r in results:
        items = r if isinstance(r, tuple) else (r,)
        for it in items:
            for lg in getattr(it, "legs", None) or []:
                code = airline_code(getattr(lg, "airline", None))
                if code:
                    seen.add(code)
    return {c: n for c, n in manifest.items() if c not in seen}


def should_probe_missing(results: list, manifest: dict | None) -> bool:
    """Fire the supplemental search only when the gap is structural.

    Beijing (prod, via the proxy): 6 rows against a 38-carrier manifest.
    BOS->MIA: ~7 of 9 carriers present, the absentees being interline
    oddities (Emirates on a domestic hop) — no probe. The gate is coverage,
    not the existence of any gap."""
    if not manifest or len(manifest) < 4:
        return False
    missing = missing_from_manifest(results, manifest)
    if not missing:
        return False
    covered = 1 - len(missing) / len(manifest)
    return covered < 0.5 or len(results) < 15


def _rt_spec_echo(spec: dict, origins: list, destinations: list) -> dict:
    """Everything /api/returns needs to price any outbound on demand."""
    dep = spec.get("departure_date")
    return {k: spec.get(k) for k in
            ("cabin", "max_stops", "airlines_include", "airlines_exclude",
             "alliances", "max_price", "adults", "children")
            } | {"origins": [a.name for a in origins],
                 "destinations": [a.name for a in destinations],
                 "departure_date": dep,
                 "return_date": spec.get("return_date") or dep,
                 "trip_type": "round_trip"}


def from_priced_only_payload(spec: dict, origins: list, destinations: list,
                             outs: list, currency: str) -> dict | None:
    """Round-trip degraded mode: Google listed the outbounds (each carrying
    its true best-achievable round-trip total) but was too slow to price
    return pairings before our budgets ran out. A slow-stream wave makes
    exactly this split — the outbound page is small and fast, the ten
    expansion fetches are heavy and die — and discarding the good half
    produced July 25's blank 'No flights found' screens on NYC-FLL. Every
    outbound ships from-priced and tap-to-price instead."""
    via = {c.strip().upper() for c in spec.get("via_airports") or []}
    if via:
        outs = [f for f in outs
                if any(lo.airport.name in via for lo in (f.layovers or []))]
    extras = []
    for f in outs:
        if f.price is None or not f.legs:
            continue
        x = serialize_flight(f, spec.get("cabin"), ret_date=spec.get("return_date"))
        x["from_total"] = f.price
        extras.append(x)
    extras_ranked = order_by_value(
        extras,
        price_of=lambda x: x.get("from_total"),
        duration_of=lambda x: x.get("duration"),
        stops_of=lambda x: x.get("stops") or 0,
        warnings_of=lambda x: x.get("warnings"),
        requested_sort=spec.get("sort"),
    )
    extras = rescue_airports(extras_ranked[:30], extras_ranked, dest_of=_dest_of_row)
    if not extras:
        return None
    ns = sum(1 for x in extras if (x.get("stops") or 0) == 0)
    bd = airport_breakdown(extras_ranked, dest_of=_dest_of_row,
                           price_of=lambda x: x.get("from_total"))
    return {
        "type": "itineraries",
        "message": (f"Google listed {len(extras)} outbounds"
                    + (f" ({ns} nonstop)" if ns else "")
                    + ", each with its true round-trip from-price, but was too slow to price "
                      "return pairings on this pass. The user IS seeing every outbound and can "
                      "tap any of them to price its returns on demand. Do NOT claim flights are "
                      "unavailable and do NOT say nothing came back; summarize the from-prices, "
                      "say returns price on tap, and offer a re-run for full pairings."
                    + (f" {bd}" if bd else "")),
        "results": [],
        "more_outbounds": extras,
        "spec_echo": _rt_spec_echo(spec, origins, destinations),
    }


def search_fixed_dates(spec: dict, origins: list, destinations: list, currency: str) -> dict:
    filters = build_filters(spec, origins, destinations, show_all_results=True)
    sort = SORT_MAP.get(spec.get("sort"), SortBy.CHEAPEST)
    searcher = RepresentativeSearch()
    # expansions run genuinely in parallel now (per-thread curl handles), so
    # 10 costs about what 2 did serially; the July 25 multi-airport timeouts
    # were the serialized fan-out, not the count
    try:
        results = run_search(searcher, filters, sort, top_n=10)
    except SearchClientError:
        # the ladder failed outright, but if any attempt got the outbound
        # list, the degraded path below still turns it into a usable answer
        if spec.get("trip_type") == "round_trip" and getattr(searcher, "last_outbounds", None):
            results = []
        else:
            raise
    route_manifest = getattr(_local, "route_airlines", None)

    # THE SUPPLEMENTAL PROBE (one-way only; round trips disclose below):
    # when the response's own carrier manifest names airlines this session
    # got no rows for, ask for them BY NAME once. Beijing proved the pool
    # exists behind the filter (25 China Eastern schedules, fares withheld)
    # even when the unfiltered response omits them entirely.
    manifest_note = None
    if results and not isinstance(results[0], tuple) and spec.get("trip_type") != "round_trip":
        missing = missing_from_manifest(results, route_manifest)
        if missing and should_probe_missing(results, route_manifest):
            # majors first: alphabetical truncation once probed "9 Air, Air
            # Macau" while leaving China Eastern outside the cap
            probe_codes = sorted(missing, key=lambda c: (c not in AIRLINE_ALLIANCE, c))[:8]
            try:
                pf = build_filters({**spec, "airlines_include": probe_codes},
                                   origins, destinations, show_all_results=True)
                extra = run_search(SearchFlights(), pf, SortBy.CHEAPEST,
                                   top_n=10, budget_s=16.0) or []
            except Exception:
                extra = []
            if extra:
                have = {flight_signature(r) for r in results}
                added = [r for r in extra if flight_signature(r) not in have]
                results = list(results) + added
                print(f"[manifest] probe recovered {len(added)} rows for {probe_codes}")
            still = missing_from_manifest(results, route_manifest)
            if still:
                names = ", ".join(n for c, n in sorted(
                    still.items(), key=lambda kv: (kv[0] not in AIRLINE_ALLIANCE, kv[1]))[:6])
                manifest_note = (
                    f" ROUTE NOTE: Google's own route data lists {names} as serving this "
                    "route, but returned none of their flights to this session even when "
                    "asked directly. Cheaper fares on those carriers may exist on Google "
                    "itself; if the listed fares look high, say so and suggest the user "
                    "also check the route in their browser."
                )

    if not results:
        if spec.get("trip_type") == "round_trip":
            degraded = from_priced_only_payload(
                spec, origins, destinations,
                getattr(searcher, "last_outbounds", None) or [], currency)
            if degraded:
                return degraded
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

        # Google's round-trip flow, and the right one: you choose an OUTBOUND
        # (each priced at the best total it can reach), then a RETURN, watching
        # the total move. Combos share outbounds - one search returned 94 combos
        # across just 8 outbounds - so flattening and keeping the 10 cheapest
        # showed ten rows that differed only in their return leg, hiding seven
        # outbounds including the only nonstop. Group by outbound instead.
        def out_key(out):
            return (
                out.legs[0].departure_datetime.isoformat() if out.legs and out.legs[0].departure_datetime else "",
                tuple((lg.airline.name, lg.flight_number) for lg in out.legs),
            )

        grouped: dict = {}
        for combo in results:
            out, ret = combo[0], combo[-1]
            if out.price is None and ret.price is None:
                continue
            total = max(p for p in [out.price, ret.price, 0] if p is not None)
            g = grouped.setdefault(out_key(out), {"out": out, "returns": []})
            g["returns"].append((ret, total))

        itineraries = []
        for g in grouped.values():
            out = g["out"]
            g["returns"].sort(key=lambda rt: rt[1] or 1e9)
            best_ret, best_total = g["returns"][0]
            out_f = serialize_flight(
                out, spec.get("cabin"),
                ret_date=best_ret.legs[0].departure_datetime.strftime("%Y-%m-%d")
                if best_ret.legs and best_ret.legs[0].departure_datetime else spec.get("return_date"),
            )
            # every return that pairs with THIS outbound, each priced as the
            # real total so the cost of a better time is visible
            options = []
            for ret, total in g["returns"][:20]:
                ret_f = serialize_flight(ret, spec.get("cabin"))
                ret_f["total_price"] = total
                ret_f["extra_over_best"] = round((total or 0) - (best_total or 0))
                # each pairing books as its own itinerary, so the link has to
                # travel with the option the user picks
                ret_f["booking_url"] = (itinerary_url([out.legs, ret.legs], spec.get("cabin"), trip_type=1)
                                        or out_f["booking_url"])
                options.append(ret_f)
            options = order_by_value(
                options,
                price_of=lambda r: r.get("total_price"),
                duration_of=lambda r: r.get("duration"),
                stops_of=lambda r: r.get("stops") or 0,
                warnings_of=lambda r: r.get("warnings"),
                requested_sort=spec.get("sort"),
            )
            shown = options[0] if options else serialize_flight(best_ret, spec.get("cabin"))
            itineraries.append({
                # the headline must be the total for the pairing actually on
                # screen, not the group's floor, or the card quotes a price for
                # an itinerary it is not showing
                "total_price": shown.get("total_price", best_total),
                "cheapest_total": best_total,
                "currency": out.currency or best_ret.currency or currency,
                "outbound": out_f,
                "return": shown,
                "return_options": options,
                "booking_url": shown.get("booking_url") or out_f["booking_url"],
            })
        # round trips get the same treatment: a cheap fare that costs you a
        # whole extra day across two legs is not the better trip
        itineraries.sort(key=lambda i: i["total_price"] or 1e9)
        itineraries = order_by_value(
            itineraries,
            price_of=lambda it: it.get("total_price"),
            duration_of=lambda it: (it["outbound"].get("duration") or 0) + (it["return"].get("duration") or 0),
            stops_of=lambda it: max(it["outbound"].get("stops") or 0, it["return"].get("stops") or 0),
            warnings_of=lambda it: (it["outbound"].get("warnings") or []) + (it["return"].get("warnings") or []),
            requested_sort=spec.get("sort"),
        )
        itineraries = retain_representative(
            itineraries, 10,
            price_of=lambda it: it.get("total_price"),
            duration_of=lambda it: (it["outbound"].get("duration") or 0) + (it["return"].get("duration") or 0),
            stops_of=lambda it: max(it["outbound"].get("stops") or 0, it["return"].get("stops") or 0),
        )
        for i, itin in enumerate(itineraries):
            itin["score"] = round(95 - (i / max(len(itineraries) - 1, 1)) * 55)
            for leg_key in ("outbound", "return"):
                if itin[leg_key]["stops"] == 0:
                    itin[leg_key]["highlights"].append("Direct")

        # every outbound Google listed but we did not expand still ships, with
        # its honest "from" total (the outbound row of a round-trip search IS
        # the best total from that outbound). The shipped set must represent
        # the option space: "no nonstops" must never again be an artifact of
        # which handful of flights got their returns priced.
        extras = []
        for f in getattr(searcher, "last_outbounds", None) or []:
            if out_key(f) in grouped or f.price is None:
                continue
            if via and not routes_via(f):
                continue
            if not arrival_ok(f, out_target, aw):
                continue
            x = serialize_flight(f, spec.get("cabin"), ret_date=spec.get("return_date"))
            x["from_total"] = f.price
            extras.append(x)
        extras_ranked = order_by_value(
            extras,
            price_of=lambda x: x.get("from_total"),
            duration_of=lambda x: x.get("duration"),
            stops_of=lambda x: x.get("stops") or 0,
            warnings_of=lambda x: x.get("warnings"),
            requested_sort=spec.get("sort"),
        )
        # the Gainesville bug lived here: a hub neighbor's cheap nonstops
        # filled all 24 slots and the named field's pricier connections
        # vanished — count what the PRICED groups already cover, then make
        # sure the cut keeps a seat for every other airport Google served
        covered_by_groups = [{"legs": it["outbound"].get("legs") or []} for it in itineraries]
        extras = rescue_airports(
            extras_ranked[:24], extras_ranked,
            dest_of=lambda x: (_dest_of_row(x)
                               if _dest_of_row(x) not in {_dest_of_row(g) for g in covered_by_groups}
                               else None))

        pairings = sum(len(it["return_options"]) for it in itineraries)
        message = (f"Found {len(itineraries)} outbounds with returns priced "
                   f"({pairings} pairings; every price is the real total for that exact pairing).")
        _missing_rt = missing_from_manifest(results, route_manifest)
        if _missing_rt and should_probe_missing(results, route_manifest):
            message += (" ROUTE NOTE: Google's route data also lists "
                        + ", ".join(sorted(_missing_rt.values())[:6])
                        + " as serving this route, but returned none of their flights to "
                          "this session — do not treat this list as the whole market.")
        if extras:
            ns = sum(1 for x in extras if (x.get("stops") or 0) == 0)
            message += (f" {len(extras)} more outbounds are listed with from-prices only"
                        + (f", including {ns} nonstop{'s' if ns != 1 else ''}" if ns else "")
                        + ". Their returns were NOT priced this turn, so never claim those options "
                          "do not exist; to price one, re-search with departure_time narrowed to its hour.")
        # per-airport truth over EVERYTHING Google returned, cut or not —
        # "GNV came back with nothing" must never again be an artifact
        bd = airport_breakdown(
            (getattr(searcher, "last_outbounds", None) or []),
            dest_of=_dest_of_result, price_of=lambda f: f.price)
        if bd:
            message += " " + bd + " (from-prices; all still bookable via the board)"
        if arrival_note:
            message = f"{arrival_note}\n{message}"
        return {
            "type": "itineraries",
            "message": message,
            "results": itineraries,
            "more_outbounds": extras,
            # everything /api/returns needs to price any outbound on demand,
            # so the board never depends on server-side session state
            "spec_echo": _rt_spec_echo(spec, origins, destinations),
        }

    strict = [r for r in results if arrival_ok(r, out_target, aw)]
    if strict:
        results = strict
    elif aw or arr_date:
        arrival_note = "Nothing meets the arrival-day/time constraint exactly — showing the closest arrivals instead."
        results = sorted(results, key=lambda r: r.legs[-1].arrival_datetime or datetime.max)

    # RANK BEFORE TRUNCATING. Google hands results back cheapest-first, so
    # slicing to 50 up front silently discards every option that costs more
    # than the 50th-cheapest fare — on BOS->FLL Nov 22 that meant 12 nonstops
    # existed but 11 sat at price ranks 83-98, so the list we shipped (and the
    # summary Claude saw) contained exactly one. Score the whole set, then cut.
    flights = [serialize_flight(r, spec.get("cabin")) for r in results[:RANK_POOL]]
    add_highlights(flights)
    flights = order_by_value(
        flights,
        price_of=lambda f: f.get("price"),
        duration_of=lambda f: f.get("duration"),
        stops_of=lambda f: f.get("stops") or 0,
        warnings_of=lambda f: f.get("warnings"),
        requested_sort=spec.get("sort"),
    )
    if (spec.get("sort") not in EXPLICIT_SORTS
            and len(flights) > 1 and flights[0].get("price") is not None):
        flights[0].setdefault("highlights", []).insert(0, "Best value")
    total_found, total_nonstop = len(flights), sum(1 for f in flights if f["stops"] == 0)
    cheapest_nonstop = min((f["price"] for f in flights
                            if f["stops"] == 0 and f.get("price") is not None), default=None)
    ranked_all = flights
    flights = retain_representative(
        flights, SHIP_LIMIT,
        price_of=lambda f: f.get("price"),
        duration_of=lambda f: f.get("duration"),
        stops_of=lambda f: f.get("stops") or 0,
    )
    # a hub neighbor must not erase the named field from the shipped set
    # (the Gainesville bug, in its one-way form)
    flights = rescue_airports(flights, ranked_all, dest_of=_dest_of_row)
    # ...and a CARRIER must not vanish either: unpriced rows rank last by
    # design (trip_cost None -> inf), so the cut would drop every trace of a
    # fare-withheld airline. rescue_airports is generic over its key.
    flights = rescue_airports(flights, ranked_all,
                              dest_of=lambda f: f.get("airline"))
    # state the true totals: the shipped list is a sample, and without this
    # Claude reads "everything else needs a connection" off a truncated set
    message = f"Found {total_found} options"
    message += f" (showing {len(flights)})." if total_found > len(flights) else "."
    if total_nonstop:
        message += (f" {total_nonstop} of them are nonstop"
                    + (f", cheapest nonstop ${cheapest_nonstop:.0f}." if cheapest_nonstop else "."))
    else:
        message += " None are nonstop."
    bd = airport_breakdown(ranked_all, dest_of=_dest_of_row, price_of=lambda f: f.get("price"))
    if bd:
        message += " " + bd
    cn = carrier_note(ranked_all)
    if cn:
        message += " " + cn
    if manifest_note:
        message += manifest_note
    if arrival_note:
        message = f"{arrival_note}\n{message}"
    return {
        "type": "flights",
        "message": message,
        "results": flights,
    }


def format_money(amount, currency: str = "USD") -> str:
    try:
        return f"${amount:,.0f}" if (currency or "USD") == "USD" else f"{amount:,.0f} {currency}"
    except Exception:
        return str(amount)


def flight_signature(result) -> tuple:
    """Identify a specific itinerary by the flights it is actually made of.

    Uses the display airline code so a signature built from a raw fli result
    matches one built from our serialized form (fli prefixes digit-leading
    codes with an underscore: _7C).
    """
    return tuple((airline_code(lg.airline), str(lg.flight_number)) for lg in (result.legs or []))


def serialized_signature(flight: dict) -> tuple:
    return tuple((lg.get("airline_code"), str(lg.get("flight_number")))
                 for lg in (flight.get("legs") or []))


def leg_price_index(origins: list, destinations: list, date_: str, cabin: str | None,
                    dep_window: dict | None = None) -> dict:
    """Standalone one-way price for every flight on this leg, by signature.

    Multi-city prices an itinerary as ONE ticket. When the carriers do not
    ticket jointly that joint fare can be absurd next to simply buying each
    leg: measured FLL->ICN->HRB, Google quoted $2,207 combined for flights
    that cost $844 + $182 = $1,026 bought separately, which is exactly what
    Google's own booking page sells them as. So we price the legs separately
    too and quote whichever is actually purchasable.
    """
    # goes through the shared search cache, so a leg the user already searched
    # on its own costs nothing here
    try:
        payload = cached_execute_spec({
            "trip_type": "one_way", "departure_date": date_,
            "origins": list(origins), "destinations": list(destinations),
            "cabin": cabin, "departure_time": dep_window,
        })
    except Exception:
        return {}
    idx: dict = {}
    for r in payload.get("results") or []:
        price = r.get("price")
        sig = serialized_signature(r)
        if price is None or not sig:
            continue
        if sig not in idx or price < idx[sig]:
            idx[sig] = price
    return idx


# The combined multi-city endpoint gets the same treatment as everything
# else: retry as a fresh identity, and let MEASURED behaviour (not a guess)
# decide when to stop. Three consecutive refusals buy a quiet period; any
# success clears it, so recovery is automatic.
MC_PROBE_BUDGET_S = 20.0     # room for ~3 identity-rotating attempts
MC_PROBE_STRIKES = 3
MC_PROBE_QUIET_S = 900.0
_mc_probe = {"fails": 0, "quiet_until": 0.0}
_mc_lock = threading.Lock()


def note_mc_probe(ok: bool) -> None:
    with _mc_lock:
        if ok:
            _mc_probe["fails"] = 0
            _mc_probe["quiet_until"] = 0.0
            return
        _mc_probe["fails"] += 1
        if _mc_probe["fails"] >= MC_PROBE_STRIKES:
            _mc_probe["quiet_until"] = time.monotonic() + MC_PROBE_QUIET_S


def multi_city_fallback(spec: dict, currency: str, leg_search=None,
                        probe_note: str | None = None) -> dict:
    """When Google withholds combined multi-city pricing (BotGuard-gated
    since Aug 2026), fall back to what is still fully knowable: each leg
    searched as its own one-way — every honesty layer applies per leg —
    paired by rank into separate-ticket combos, with Google's own
    multi-city page linked for the one-ticket price. The truth we can get,
    plus a tap to the truth we cannot."""
    leg_search = leg_search or cached_execute_spec
    raw_segs = (spec.get("multi_city_segments") or [])[:5]
    if len(raw_segs) < 2:
        return {"type": "multicity",
                "message": "A multi-city trip needs at least two legs.", "results": []}

    pool = ThreadPoolExecutor(max_workers=min(4, len(raw_segs)))
    futs = [pool.submit(leg_search, {
        "origins": seg.get("origins") or [], "destinations": seg.get("destinations") or [],
        "trip_type": "one_way", "departure_date": seg.get("date"),
        "cabin": spec.get("cabin"), "adults": spec.get("adults"),
        "departure_time": seg.get("departure_time"),
    }) for seg in raw_segs]
    leg_payloads: list = []
    for fu in futs:
        try:
            leg_payloads.append(fu.result(timeout=50))
        except Exception:
            leg_payloads.append(None)
    pool.shutdown(wait=False, cancel_futures=True)

    leg_results = [(p.get("results") or []) if isinstance(p, dict) else [] for p in leg_payloads]
    check_url = multi_city_search_url(raw_segs, spec.get("cabin"))
    if not all(leg_results):
        empty = [i + 1 for i, r in enumerate(leg_results) if not r]
        return {
            "type": "multicity",
            "message": (f"Google is not returning combined multi-city pricing to this session, and "
                        f"leg {', '.join(map(str, empty))} came back empty on its own too. Tell the "
                        "user plainly and give them the direct link to price the whole trip on "
                        "Google (it is in this section), and offer to retry shortly."),
            "results": [],
            "combined_check_url": check_url,
            "combined_probe": probe_note,
        }

    priced = [[f for f in legs if f.get("price") is not None] or legs for legs in leg_results]
    combos = []
    for rank in range(min(5, min(len(x) for x in priced))):
        parts = [legs[rank] for legs in priced]
        prices = [p.get("price") for p in parts]
        total = sum(p for p in prices if p is not None) if all(p is not None for p in prices) else None
        combos.append({
            "total_price": total, "currency": currency, "price_basis": "separate tickets",
            "separate_price": total, "combined_price": None,
            "parts": parts,
            "warnings": ["Booked as separate tickets: no through-checked bags or "
                          "missed-connection protection between legs"],
            "booking_url": check_url,
        })
    return {
        "type": "multicity",
        "message": (
            "Google now withholds combined one-ticket multi-city pricing from automated "
            "sessions, so each leg was searched as its own one-way instead (fully priced "
            "below) and paired by rank as separate tickets. IMPORTANT: a through-ticketed "
            "single itinerary may exist at a different price, sometimes cheaper, especially "
            "when one carrier serves every leg. The section carries a direct link to "
            "Google's own multi-city page for these exact legs and dates: tell the user "
            "the one-ticket price is one tap away there, and never claim the combined "
            "ticket does not exist."),
        "results": combos,
        "combined_check_url": check_url,
        "combined_probe": probe_note,
    }


def search_multi_city(spec: dict, currency: str) -> dict:
    resolved = []
    invalid: list[str] = []
    for seg in (spec.get("multi_city_segments") or [])[:5]:
        o, bad_o = resolve_airports(seg.get("origins"))
        d, bad_d = resolve_airports(seg.get("destinations"))
        invalid += bad_o + bad_d
        if o and d and seg.get("date"):
            resolved.append((o, d, seg["date"], time_restrictions(seg.get("departure_time"))))
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
            time_restrictions=tr,
        )
        for o, d, date_, tr in resolved
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

    # start standalone leg pricing NOW, beside the expansion rather than after
    # it — run serially it added its whole runtime to the multi-city tail
    raw_segs = (spec.get("multi_city_segments") or [])[:5]
    price_pool = ThreadPoolExecutor(max_workers=min(4, max(1, len(raw_segs))))
    price_futs = [price_pool.submit(leg_price_index, seg.get("origins") or [],
                                    seg.get("destinations") or [], seg.get("date"),
                                    spec.get("cabin"), seg.get("departure_time"))
                  for seg in raw_segs]

    # fan-out: only this many leg-1 candidates get expanded into full
    # itineraries. 4 lost the best one-way (a 6:35 departure sat at rank 5
    # among eight same-price ties); warm-connection reuse made expansions cheap
    # enough to widen. In bad weather (the breaker has seen refusals) do less
    # optional work so the required work still fits the turn budget.
    with _breaker_lock:
        rough_weather = _breaker["consecutive"] > 0 or _breaker["open_until"] > time.monotonic()
    fanout = 4 if rough_weather else 6
    # RepresentativeSearch for the parallel expansion + identity inheritance.
    # (For 2-leg trips its representative reorder of leg-1 candidates fires
    # too — deliberate: the expansion set should represent the leg, not just
    # be its cheapest corner.)
    #
    # The combined endpoint keeps its FULL identity-rotating ladder. An
    # earlier version of this line cut it to a single attempt on the theory
    # that multi-city was permanently gated — a conclusion drawn from a
    # home IP that turned out to be soft-blocked (its one-way control was
    # failing too, unnoticed). Refusals here are session-sticky like
    # everywhere else, so retrying as a NEW identity is exactly right, and
    # removing that made the common transient failure permanent.
    #
    # What replaces the guesswork: measure. Every probe records its outcome,
    # and only OBSERVED consecutive refusals (not an assumption) buy a quiet
    # period, so a genuine gate costs ~20s three times rather than forever,
    # while an un-gating recovers on its own.
    probe_note, results = None, []
    with _mc_lock:
        quiet = _mc_probe["quiet_until"] > time.monotonic()
        strikes = _mc_probe["fails"]
    if quiet:
        probe_note = f"skipped (combined endpoint refused {strikes}x recently)"
    else:
        _tp = time.monotonic()
        try:
            results = run_search(RepresentativeSearch(), filters, sort,
                                 top_n=fanout, budget_s=MC_PROBE_BUDGET_S)
            probe_note = (f"priced {len(results)} combinations in {time.monotonic() - _tp:.1f}s"
                          if results else f"returned nothing in {time.monotonic() - _tp:.1f}s")
        except SearchClientError as e:
            probe_note = f"{type(e).__name__} after {time.monotonic() - _tp:.1f}s"
        note_mc_probe(bool(results))

    if not results:
        price_pool.shutdown(wait=False, cancel_futures=True)
        return multi_city_fallback(spec, currency, probe_note=probe_note)

    # harvest the leg prices that finished while the expansion ran; anything
    # still pending gets only a short grace before we degrade to joint prices
    indexes: list = [{} for _ in raw_segs]  # per-leg standalone prices
    for i, fu in enumerate(price_futs):
        try:
            indexes[i] = fu.result(timeout=6) or {}
        except Exception:
            indexes[i] = {}
    price_pool.shutdown(wait=False, cancel_futures=True)

    itineraries = []
    for combo in results[:8]:
        parts = list(combo) if isinstance(combo, tuple) else [combo]
        prices = [p.price for p in parts if p.price]
        serialized = [serialize_flight(p, spec.get("cabin")) for p in parts]

        # what these exact flights cost bought leg by leg
        separate_total, all_matched = 0.0, bool(indexes) and len(parts) == len(indexes)
        if all_matched:
            for part, idx in zip(parts, indexes):
                standalone = idx.get(flight_signature(part))
                if standalone is None:
                    all_matched = False
                    break
                separate_total += standalone
        combined_total = max(prices) if prices else None
        separate_total = round(separate_total) if all_matched else None
        # Ticketing honesty: Google prices these as a combined trip, but when
        # the legs are on carriers that do not ticket jointly it sells them as
        # SEPARATE tickets (its own booking page says "must be booked
        # individually" for e.g. Delta + Asiana). No through-bags, no
        # misconnect protection. Flag exactly that case.
        alliances = {s.get("alliance") for s in serialized}
        carriers = {s.get("airline_code") for s in serialized}
        ticket_warnings = []
        if len(carriers) > 1 and (len(alliances) > 1 or None in alliances):
            names = " + ".join(sorted({s.get("airline") or "?" for s in serialized}))
            ticket_warnings.append(
                f"Mixed carriers ({names}) that do not ticket jointly: sellers may issue "
                "separate tickets, so bags and onward connections may not be protected. "
                "Check the booking page."
            )
        # quote what is actually purchasable, not the joint fare nobody buys
        candidates = [p for p in (combined_total, separate_total) if p is not None]
        best_total = min(candidates) if candidates else None
        if (separate_total is not None and combined_total is not None
                and separate_total < combined_total * 0.97):
            ticket_warnings.insert(0, (
                f"Cheaper booked as two separate tickets: {format_money(separate_total, currency)} "
                f"leg by leg versus {format_money(combined_total, currency)} on one ticket. "
                "Separate tickets mean no through-checked bags and no protection if a leg is late."
            ))

        itineraries.append({
            "total_price": best_total,
            "combined_price": combined_total,
            "separate_price": separate_total,
            "price_basis": ("separate tickets" if best_total == separate_total
                            and separate_total is not None else "one ticket"),
            "currency": parts[0].currency or currency,
            "parts": serialized,
            "warnings": ticket_warnings,
            "booking_url": (itinerary_url([p.legs for p in parts], spec.get("cabin"), trip_type=3)
                            or multi_city_url(parts, spec.get("cabin"))),
        })
    itineraries.sort(key=lambda i: i["total_price"] or 1e9)
    for i, itin in enumerate(itineraries):
        itin["score"] = round(95 - (i / max(len(itineraries) - 1, 1)) * 55)
    route_text = " → ".join([resolved[0][0][0].name] + [d[0].name for _, d, _, _ in resolved])
    return {
        "type": "multicity",
        "message": (
            f"Found {len(itineraries)} itineraries ({route_text}). total_price is the CHEAPER of "
            "buying one combined ticket or buying each leg separately (price_basis says which, and "
            "both numbers are given). Google's search-time quote varies by seller. Mixed-carrier "
            "combinations are often issued as separate tickets: no through bags, no delay cover."
        ),
        "results": itineraries,
    }


DEFAULT_FLEX_TRIP_NIGHTS = 7


def flex_grid_params(flex: dict, is_round_trip: bool) -> tuple[dict, int | None]:
    """SearchDates params for a flexible window, plus the nights we assumed.

    A round-trip grid searched WITHOUT a duration prices SAME-DAY returns
    (out and back on one date), which is never what "flexible round trip"
    means and quietly understates every fare. When the model omits
    trip_length_days, assume a week and surface the assumption.
    """
    extra: dict = {"from_date": flex["from_date"], "to_date": flex["to_date"]}
    assumed = None
    if is_round_trip:
        nights = flex.get("trip_length_days")
        if not nights:
            nights = assumed = DEFAULT_FLEX_TRIP_NIGHTS
        extra["duration"] = nights
    return extra, assumed


def search_flexible_dates(spec: dict, origins: list, destinations: list, currency: str) -> dict:
    flex = spec["flexible_dates"]
    is_round_trip = spec.get("trip_type") == "round_trip"
    spec = {**spec, "departure_date": flex["from_date"], "return_date": flex["from_date"]}

    extra, assumed_nights = flex_grid_params(flex, is_round_trip)

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
            time.sleep(0.6 + 0.6 * i)
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
    payload = {
        "type": "dates",
        "message": "The best-value dates are shown first; expand for the full calendar. Pick one to see actual flights.",
        "dates": dates,
    }
    if assumed_nights:
        payload["message"] += (
            f" Prices are round-trip totals for {assumed_nights}-night trips"
            " (assumed; re-search with trip_length_days if the user has a different stay in mind)."
        )
        payload["assumptions"] = [f"Assumed {assumed_nights}-night trips; tell me your trip length to reprice"]
    return payload


# --------------------------------------------------------------------------
# Is this a good fare?
#
# Value ranking answers "which of these", never "is this a good day to buy",
# which is the other half of why people open Google Flights (it says so
# outright: "prices are currently low"). The machinery was already here: the
# SearchDates grid behind flexible dates prices every nearby date, and where
# the quoted fare sits in that spread is an honest, checkable answer.
#
# Constraints this obeys, because it is an optional nicety riding on a search
# that is not:
#   - it NEVER delays the cards. The grid is fetched alongside the search and
#     the payload ships without it when it is late (PRICE_CTX_WAIT_S).
#   - it never retries, and stands down entirely while the breaker is open.
#     An extra request during a refusal wave is precisely the sustained volume
#     that escalates a session flag into an IP burn.
#   - it is cached for HOURS, not the search cache's minutes: a distribution
#     ages far more slowly than a live fare, and one grid per route-window is
#     the whole point at ~100-300KB of proxy bandwidth each.
#   - it compares DATES IN A WINDOW, never price history, and the note it
#     hands the model says so. "Cheaper than usual" would be a claim about
#     data we do not have, which is the failure mode this project keeps
#     paying for.
# --------------------------------------------------------------------------
PRICE_CTX_SPREAD_D = 21      # days either side of the requested departure
PRICE_CTX_TTL = 6 * 3600.0   # a fare distribution ages in hours, not minutes
PRICE_CTX_WAIT_S = 3.0       # the most we hold cards back waiting for it
PRICE_CTX_MAX_INFLIGHT = 2   # extra load on Google, deliberately bounded
PRICE_CTX_MIN_SAMPLE = 8     # fewer dates than this cannot characterize a spread

_price_ctx_cache: dict = {}
_price_ctx_lock = threading.Lock()
_price_ctx_state = {"inflight": 0}
_price_ctx_pool = ThreadPoolExecutor(max_workers=PRICE_CTX_MAX_INFLIGHT)


def price_ctx_window(dep: str, spread: int = PRICE_CTX_SPREAD_D) -> tuple[str, str] | None:
    """The date window we price around a departure, never reaching the past."""
    try:
        d = datetime.strptime(dep, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    start = max(d - timedelta(days=spread), date.today() + timedelta(days=1))
    end = d + timedelta(days=spread)
    if start > d or end < d:
        return None
    return start.isoformat(), end.isoformat()


def _trip_nights(spec: dict) -> int | None:
    if spec.get("trip_type") != "round_trip":
        return None
    try:
        out = datetime.strptime(spec["departure_date"], "%Y-%m-%d").date()
        back = datetime.strptime(spec["return_date"], "%Y-%m-%d").date()
    except (KeyError, TypeError, ValueError):
        return None
    nights = (back - out).days
    return nights if 1 <= nights <= 60 else None


def _price_ctx_key(spec: dict, origins: list, destinations: list,
                   nights: int | None, window: tuple) -> str:
    return json.dumps({
        "o": sorted(a.name for a in origins), "d": sorted(a.name for a in destinations),
        "cabin": spec.get("cabin") or "economy", "nights": nights, "window": window,
        # a capped or filtered search prices a different question entirely
        "stops": spec.get("max_stops"), "airlines": spec.get("airlines_include"),
        "alliances": spec.get("alliances"), "adults": spec.get("adults") or 1,
    }, sort_keys=True, default=str)


def _fetch_price_grid(spec: dict, origins: list, destinations: list,
                      nights: int | None, window: tuple) -> list:
    """One date grid, one attempt. Returns [] on anything unexpected."""
    grid_spec = {**spec, "departure_date": window[0], "return_date": window[0]}
    extra: dict = {"from_date": window[0], "to_date": window[1]}
    if nights:
        extra["duration"] = nights
    try:
        filters = build_filters(grid_spec, origins, destinations,
                                filters_cls=DateSearchFilters, **extra)
        checkout_identity()
        _local.fli_timeout = 12.0
        rows = SearchDates().search(filters, currency="USD") or []
    except Exception:
        # NOT retired: this thread may share an identity with the live search
        # it is annotating, and closing that handle mid-request would break a
        # real answer to improve an optional one. The outcome is still noted,
        # so a genuine wave still trips the breaker.
        note_search_outcome(False)
        return []
    note_search_outcome(bool(rows))
    out = []
    for dp in rows:
        if not dp.price or not dp.date:
            continue
        out.append({"date": dp.date[0].strftime("%Y-%m-%d"),
                    "return_date": dp.date[1].strftime("%Y-%m-%d") if len(dp.date) > 1 else None,
                    "price": dp.price})
    return out


def start_price_context(spec: dict, origins: list, destinations: list):
    """Begin the baseline fetch alongside the search it will annotate."""
    if spec.get("flexible_dates") or spec.get("multi_city_segments"):
        return None  # a flexible search already shows the whole distribution
    if spec.get("trip_type") == "multi_city" or not spec.get("departure_date"):
        return None
    with _breaker_lock:
        if _breaker["open_until"] > time.monotonic():
            return None  # mid-wave: do not add optional load
    window = price_ctx_window(spec["departure_date"])
    if not window:
        return None
    nights = _trip_nights(spec)
    key = _price_ctx_key(spec, origins, destinations, nights, window)

    with _price_ctx_lock:
        hit = _price_ctx_cache.get(key)
        if hit and hit[0] > time.monotonic():
            grid = hit[1]
            fut: object = None
        elif _price_ctx_state["inflight"] >= PRICE_CTX_MAX_INFLIGHT:
            return None
        else:
            grid, fut = None, True
            _price_ctx_state["inflight"] += 1
    if fut is None:
        return {"window": window, "nights": nights, "grid": grid, "future": None,
                "dep": spec["departure_date"]}

    def work():
        try:
            grid_ = _fetch_price_grid(spec, origins, destinations, nights, window)
            if grid_:
                with _price_ctx_lock:
                    _price_ctx_cache[key] = (time.monotonic() + PRICE_CTX_TTL, grid_)
                    if len(_price_ctx_cache) > 32:
                        for k in sorted(_price_ctx_cache, key=lambda k: _price_ctx_cache[k][0])[:8]:
                            _price_ctx_cache.pop(k, None)
            return grid_
        finally:
            with _price_ctx_lock:
                _price_ctx_state["inflight"] -= 1

    return {"window": window, "nights": nights, "grid": None,
            "dep": spec["departure_date"], "future": _price_ctx_pool.submit(work)}


def payload_anchor_price(payload: dict):
    """The cheapest fare this search actually put on screen."""
    prices: list = []
    if payload.get("type") == "itineraries":
        prices += [it.get("total_price") for it in payload.get("results") or []]
        prices += [m.get("from_total") for m in payload.get("more_outbounds") or []]
    else:
        prices += [f.get("price") for f in payload.get("results") or []]
    prices = [p for p in prices if isinstance(p, (int, float)) and p > 0]
    return min(prices) if prices else None


def price_verdict(anchor, grid: list, window: tuple, nights: int | None,
                  dep: str | None = None) -> dict | None:
    """Where `anchor` sits among the cheapest fares for nearby dates."""
    prices = sorted(g["price"] for g in grid if g.get("price"))
    if not anchor or len(prices) < PRICE_CTX_MIN_SAMPLE:
        return None
    below = sum(1 for p in prices if p < anchor)
    pct = below / len(prices)
    band = ("among the cheapest dates in this window" if pct <= 0.15 else
            "below the going rate for nearby dates" if pct <= 0.40 else
            "about the going rate for nearby dates" if pct <= 0.60 else
            "above the going rate for nearby dates" if pct <= 0.85 else
            "among the priciest dates in this window")
    mid = prices[len(prices) // 2]
    cheapest = min(grid, key=lambda g: g["price"])
    cheaper = sorted((g for g in grid if g["price"] <= anchor * 0.92),
                     key=lambda g: g["price"])[:3]
    return {
        # the fare being characterized is the CHEAPEST one on screen for the
        # searched date, which is not the same as the top card's price (value
        # ranking leads with the best trip, not the lowest number)
        "quoted": round(anchor),
        "quoted_is": "cheapest fare this search found" + (f" for {dep}" if dep else ""),
        "searched_date": dep,
        "verdict": band,
        "percentile": round(pct * 100),
        "median_nearby": round(mid),
        "cheapest_nearby": {"date": cheapest["date"], "price": round(cheapest["price"]),
                            **({"return_date": cheapest["return_date"]} if cheapest.get("return_date") else {})},
        "cheaper_dates": [{"date": g["date"], "price": round(g["price"]),
                           "saves": round(anchor - g["price"])} for g in cheaper],
        "window": {"from": window[0], "to": window[1]},
        "dates_priced": len(prices),
        "basis": (f"round-trip totals for {nights}-night trips" if nights
                  else "one-way fares"),
        "note": ("Compares the cheapest fare this search found against the cheapest fare "
                 "Google lists for every other date in the window. This is NOT price "
                 "history: say where the fare sits among these DATES, never that it is "
                 "cheap or dear 'for this route' in general, and never predict which way "
                 "prices will move."),
    }


COMPLETENESS_SLACK = 1.15  # grid staleness tolerance before a list is "short"


def grid_price_for(handle, grid) -> float | None:
    """The grid's own price for THE SEARCHED DATE — same route, same date,
    same stay length, from an independent Google endpoint."""
    dep = (handle or {}).get("dep")
    if not dep or not grid:
        return None
    prices = [g["price"] for g in grid if g.get("date") == dep and g.get("price")]
    return min(prices) if prices else None


def verify_completeness(spec: dict, payload: dict, handle, rerun=None) -> dict:
    """Catch ANY 'flights that should exist don't appear' failure, whatever
    the cause. August 2, Beijing->Chengdu: the list said $421 across the
    board while Google's own UI had China Eastern at $314 and a cheapest
    tab of $242 — an entire carrier lost somewhere between Google and the
    cards. Chunk parsing was one cause; a degraded session, a parse drift,
    or a throttled response would look identical. The date grid we already
    fetch beside every fixed-date search prices the searched date itself
    from a separate endpoint, so: cheapest-listed far above the grid's
    number for the same date = the list is PROVABLY short. One retry as a
    genuinely new identity (degradation is session-linked doctrine), keep
    whichever set is honest-cheapest, and if it is STILL short, the model
    is told to disclose it rather than present the floor of a partial list
    as the going rate.
    """
    if not isinstance(payload, dict) or payload.get("type") not in ("flights", "itineraries"):
        return payload
    if not (payload.get("results") or payload.get("more_outbounds")):
        return payload
    try:
        grid = (handle or {}).get("grid")
        fut = (handle or {}).get("future")
        if grid is None and fut is not None:
            grid = fut.result(timeout=2.5)
    except Exception:
        return payload
    ref = grid_price_for(handle, grid)
    anchor = payload_anchor_price(payload)
    if not ref or not anchor or anchor <= ref * COMPLETENESS_SLACK + 5:
        return payload

    print(f"[completeness] cheapest listed ${anchor:.0f} vs grid ${ref:.0f} "
          f"for {handle.get('dep')} — retrying as a new identity")
    retire_identity()
    try:
        second = (rerun or execute_spec)(spec)
    except Exception:
        second = None
    a2 = payload_anchor_price(second) if isinstance(second, dict) else None
    if a2 is not None and a2 < anchor:
        payload, anchor = second, a2
    if anchor > ref * COMPLETENESS_SLACK + 5:
        payload["message"] = (payload.get("message") or "") + (
            f" COMPLETENESS WARNING: Google's own date calendar prices this exact date "
            f"from ${ref:.0f}, but the cheapest fare in this list is ${anchor:.0f} — "
            "cheaper flights (possibly whole airlines) are likely MISSING from this "
            "list right now. Tell the user plainly that options around "
            f"${ref:.0f} likely exist but did not load, suggest re-running shortly, "
            "and do NOT present the cheapest listed fare as the going rate."
        )
        payload["completeness_warning"] = {"grid_from": ref, "listed_from": anchor}
    return payload


def attach_price_context(payload: dict, handle, deadline: float) -> None:
    """Annotate the payload if the baseline landed in time. Never raises."""
    if not isinstance(payload, dict):
        return
    if not handle:
        payload["price_context_status"] = "off"
        return
    # one word of why, on every payload: production disagreed with local on
    # this feature and "it just isn't there" cost a probe cycle to unpick
    try:
        grid, status = handle.get("grid"), "cached"
        if grid is None and handle.get("future") is not None:
            grid = handle["future"].result(timeout=max(0.0, deadline - time.monotonic()))
            status = "landed"
        if not grid:
            payload["price_context_status"] = "empty"
            return
        ctx = price_verdict(payload_anchor_price(payload), grid,
                            handle["window"], handle.get("nights"), handle.get("dep"))
        payload["price_context_status"] = status if ctx else "thin"
        if ctx:
            payload["price_context"] = ctx
    except FuturesTimeout:
        # still in flight: it keeps running, warms the 6h cache, and the next
        # serve of this route-window gets annotated instead
        payload["price_context_status"] = "late"
    except Exception:
        payload["price_context_status"] = "error"
        return  # a baseline is a bonus; it can never cost the user their search


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


def _start_ctx_for(spec: dict):
    """Kick off the price baseline for a spec, on the dates we will search."""
    rolled, _ = roll_past_dates(spec)
    if not rolled.get("departure_date") or rolled.get("flexible_dates"):
        return None
    origins, _bad = resolve_airports(rolled.get("origins"))
    destinations, _bad2 = resolve_airports(rolled.get("destinations"))
    if not origins or not destinations:
        return None
    return start_price_context(rolled, origins, destinations)


def cached_execute_spec(spec: dict) -> dict:
    import copy

    # The baseline rides OUTSIDE the payload cache, deliberately. Attaching it
    # inside execute_spec froze the answer into the cached payload: production
    # showed the first search dropping a grid that was still in flight (a
    # residential-proxy handshake costs seconds that a direct local fetch does
    # not) and every repeat inside the 4-minute TTL then serving that same
    # context-less payload, so the feature could never appear at all. Started
    # here, a grid that lands late still warms its 6-hour cache and annotates
    # the NEXT serve, cache hit or not.
    ctx = _start_ctx_for(spec)

    def annotate(payload: dict) -> dict:
        attach_price_context(payload, ctx, time.monotonic() + PRICE_CTX_WAIT_S)
        return payload

    key = _spec_key(spec)
    with _cache_lock:
        hit = _search_cache.get(key)
        if hit and hit[0] > time.monotonic():
            return annotate(copy.deepcopy(hit[1]))
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
            return annotate(copy.deepcopy(hit[1]))
        # the owner failed; fall through and try it ourselves

    try:
        payload = execute_spec(spec)
        # the sentinel runs before the cache write, so a provably-short list
        # is retried (and at worst honestly labeled) rather than served to
        # every repeat inside the TTL
        payload = verify_completeness(spec, payload, ctx)
        if payload.get("results") or payload.get("dates"):
            with _cache_lock:
                _search_cache[key] = (time.monotonic() + SEARCH_CACHE_TTL, payload)
                if len(_search_cache) > 64:  # bound memory in a long-lived process
                    for k in sorted(_search_cache, key=lambda k: _search_cache[k][0])[:16]:
                        _search_cache.pop(k, None)
        return annotate(copy.deepcopy(payload))
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

    # geography before prose: a hallucinated code must contradict itself
    # before the model writes a word about the "destination"
    payload["message"] = f"{route_echo(origins, destinations)}\n{payload.get('message') or ''}"
    if spec.get("summary"):
        payload["message"] = f"{spec['summary']}\n\n{payload['message']}"
    # search fns may add their own notes (e.g. assumed trip length) — keep them
    payload["assumptions"] = (spec.get("assumptions") or []) + date_notes + (payload.get("assumptions") or [])
    return payload


def assistant_error_reply(e: "anthropic.APIStatusError") -> dict:
    """An API failure the user can actually act on, and one we can diagnose.

    The July 30 outage taught the shape of the old message the hard way: every
    Claude call was failing, the reply said "temporary ... try that once more",
    Evan retried four times against a deterministic error, and diagnosis cost
    a probe cycle because nothing anywhere said WHICH error. Now the status
    code rides in the reply, the full detail goes to the function log (with
    request id, for Anthropic support), and a 4xx — which retrying cannot fix
    — stops claiming to be temporary.
    """
    body = getattr(e, "body", None)
    err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
    if not isinstance(err, dict):
        err = {}  # proxies fronting an outage return {"error": "a string"} shapes
    etype = str(err.get("type") or getattr(e, "type", "") or "")
    detail = str(err.get("message") or e)[:300]
    req_id = ""
    try:
        req_id = e.response.headers.get("request-id") or ""
    except Exception:
        pass
    # Vercel keeps function logs; this line is the autopsy the reply can't hold
    print(f"[assistant] APIStatusError {e.status_code} {etype} req={req_id}: {detail}")

    low = detail.lower()
    if e.status_code in (429, 529) or etype == "overloaded_error":
        msg = ("My reasoning service is momentarily congested. Give it a few seconds "
               "and send that again; nothing was lost.")
    elif "credit" in low or "billing" in low:
        # the one failure only the owner can fix; name it so nobody retries at it
        msg = ("The Anthropic API account this site runs on is out of credit, so I "
               "can't think about flights until Evan tops it up in the Anthropic "
               "console. Retrying won't help until then; sorry for the wall.")
    elif e.status_code == 401:
        msg = ("My reasoning service is rejecting this site's API key (HTTP 401). "
               "That needs Evan to fix the key in the deployment settings; "
               "retrying won't change it.")
    elif 400 <= e.status_code < 500:
        msg = (f"My reasoning service rejected that request (HTTP {e.status_code}"
               + (f", {etype}" if etype else "")
               + "). That's a configuration problem on my side rather than traffic, "
                 "so retrying is unlikely to help; it has been logged for a fix.")
    else:
        msg = (f"My reasoning service returned an error (HTTP {e.status_code}). "
               "Please try that once more; if it keeps happening the fault is on "
               "my side, not with your question.")
    return {"message": msg, "sections": [], "suggestions": ["try again"],
            "error_detail": f"HTTP {e.status_code} {etype}".strip() + (f" req={req_id}" if req_id else "")}


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
    ledgers = body.get("ledgers")

    events: "_queue.Queue" = _queue.Queue()

    def worker():
        try:
            result = run_assistant(query, history, emit=lambda ev, data: events.put((ev, data)),
                                   ledgers=ledgers)
            events.put(("done", {
                "message": result["message"],
                "sections": result["sections"],
                "suggestions": result.get("suggestions") or [],
                "ledger": result.get("ledger"),
            }))
        except anthropic.APIStatusError as e:
            events.put(("done", assistant_error_reply(e)))
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
        # Adaptive thinking streams nothing while it thinks, so a quiet 90s
        # gap is now possible mid-turn. The old single 90s get() would break
        # the stream WITHOUT a done event; the frontend then fell back to
        # /api/search and RE-RAN the whole turn while this worker kept
        # billing to completion — a double-spend. Heartbeat comments (":"
        # lines are ignored by SSE parsers) keep the stream alive; the hard
        # deadline below is the only way out without a done event.
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                ev, data = events.get(timeout=15)
            except Exception:
                yield ": ping\n\n"
                continue
            if ev is None:
                break
            yield f"event: {ev}\ndata: {json.dumps(data)}\n\n"

    return StreamingResponse(frames(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",  # don't let a proxy sit on the stream
    })


def fetch_returns_for_outbound(spec: dict, legs_in: list, from_total=None) -> dict:
    """Every return Google pairs with ONE outbound, priced as pairing totals.

    This is fli's expansion request, reconstructed statelessly from the
    spec_echo the search shipped plus the outbound's serialized legs — so
    the board can price any from-priced row on demand without a model turn
    and without server-side session state.
    """
    import types as _types

    spec = {**spec, "trip_type": "round_trip"}
    origins, _bad = resolve_airports(spec.get("origins"))
    destinations, _bad2 = resolve_airports(spec.get("destinations"))
    if not origins or not destinations:
        raise ValueError("unrecognized airports in spec")
    filters = build_filters(spec, origins, destinations, show_all_results=True)

    def leg_ns(l: dict):
        code = str(l.get("airline_code") or "").strip().upper()
        airline = getattr(Airline, code, None) or getattr(Airline, "_" + code, None)
        if airline is None or not l.get("departure"):
            raise ValueError(f"unrecognized leg: {code} {l.get('flight_number')}")
        return _types.SimpleNamespace(
            departure_airport=getattr(Airport, str(l["from"]).upper()),
            arrival_airport=getattr(Airport, str(l["to"]).upper()),
            departure_datetime=datetime.fromisoformat(l["departure"]),
            arrival_datetime=datetime.fromisoformat(l["arrival"]) if l.get("arrival") else None,
            airline=airline,
            flight_number=str(l.get("flight_number") or ""),
        )

    sel_legs = [leg_ns(l) for l in legs_in]
    filters.flight_segments[0].selected_flight = _types.SimpleNamespace(legs=sel_legs)

    searcher = RepresentativeSearch()
    rows, last_exc = None, None
    for i in range(2):
        breaker_wait(1.5 if i == 0 else 6.0)
        checkout_identity()
        _local.fli_timeout = 12.0 if i == 0 else 24.0
        try:
            rows = searcher._fetch_flights(filters, currency="USD", language=None,
                                           country=None, capture_session=False)
        except SearchClientError as e:
            last_exc = e
            retire_identity()
            note_search_outcome(False)
            time.sleep(0.5)
            continue
        if rows:
            note_search_outcome(True)
            break
        retire_identity()
        note_search_outcome(False)
    if not rows:
        if last_exc:
            raise last_exc
        return {"return_options": [], "cheapest_total": None,
                "message": "Google returned no pairings for that outbound just now."}

    options, seen = [], set()
    for ret in rows:
        if ret.price is None or not ret.legs:
            continue
        key = (ret.legs[0].departure_datetime.isoformat() if ret.legs[0].departure_datetime else "",
               tuple((lg.airline.name, lg.flight_number) for lg in ret.legs))
        if key in seen:
            continue
        seen.add(key)
        # in an expansion the return row carries the pairing's cumulative total
        total = max(p for p in [from_total, ret.price, 0] if p is not None)
        rf = serialize_flight(ret, spec.get("cabin"))
        rf["total_price"] = total
        rf["booking_url"] = (itinerary_url([sel_legs, ret.legs], spec.get("cabin"), trip_type=1)
                             or rf["booking_url"])
        options.append(rf)

    cheapest = min((o["total_price"] for o in options), default=None)
    for o in options:
        o["extra_over_best"] = round((o["total_price"] or 0) - (cheapest or 0))
    options = order_by_value(
        options,
        price_of=lambda r: r.get("total_price"),
        duration_of=lambda r: r.get("duration"),
        stops_of=lambda r: r.get("stops") or 0,
        warnings_of=lambda r: r.get("warnings"),
        requested_sort=None,
    )[:40]
    return {
        "return_options": options,
        "cheapest_total": cheapest,
        "currency": options[0]["currency"] if options else "USD",
        "count": len(options),
    }


@app.post("/api/returns")
async def price_returns(request: Request):
    from starlette.concurrency import run_in_threadpool

    try:
        body = await request.json()
        spec = body.get("spec") or {}
        legs_in = (body.get("outbound") or {}).get("legs") or []
        if not legs_in or not spec.get("origins") or not spec.get("destinations"):
            return JSONResponse({"error": "spec and outbound legs are required"}, status_code=400)
        payload = await run_in_threadpool(
            fetch_returns_for_outbound, spec, legs_in, body.get("from_total"))
        return JSONResponse(payload)
    except SearchClientError:
        return JSONResponse({"error": "Google is slow right now; try that outbound again in a moment."},
                            status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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
        result = run_assistant(query, body.get("history"), ledgers=body.get("ledgers"))
        payload = {
            "type": "assistant",
            "message": result["message"],
            "sections": result["sections"],
            "suggestions": result.get("suggestions") or [],
            "ledger": result.get("ledger"),
        }
        if body.get("debug_timings"):
            payload["timings"] = {
                "total": round(time.monotonic() - t0, 2),
                "cold_process": cold,
                "since_process_start": round(time.monotonic() - _PROCESS_START, 2),
                # estimated from each call's real usage block at list prices;
                # Sonnet 5's intro pricing (through 2026-08-31) bills lower
                "est_cost_usd": result.get("cost_usd"),
                "model": ASSISTANT_MODEL,
                "phases": result.get("timings") or [],
                "search_ok_on_attempt": _attempt_stats["ok_on_attempt"][-6:],
                "cached_specs": len(_search_cache),
            }
        return JSONResponse(payload)

    except anthropic.APIStatusError as e:
        return JSONResponse({"type": "assistant", **assistant_error_reply(e)})
    except anthropic.APIConnectionError:
        return JSONResponse({
            "type": "assistant",
            "message": "I couldn't reach my reasoning service just now. One more try should do it.",
            "sections": [], "suggestions": ["try again"],
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
