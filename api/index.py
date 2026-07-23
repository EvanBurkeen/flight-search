import json
import os
import re
import time
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

# Optional egress proxy for Google traffic (set FLI_PROXY in Vercel env to a
# proxy URL). Datacenter IPs get throttled by Google in waves; a residential
# proxy is the durable fix. No proxy -> unchanged behavior.
if os.environ.get("FLI_PROXY"):
    from fli.search import client as _fli_client

    _orig_session = _fli_client.Client._session

    def _session_with_proxy(self):
        session = _orig_session(self)
        session.proxies = {"http": os.environ["FLI_PROXY"], "https": os.environ["FLI_PROXY"]}
        return session

    _fli_client.Client._session = _session_with_proxy

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
- When the user cares about the arrival DAY ("land on Friday"), set arrival_date and pick departure_date by timezone logic (Asia -> US lands the same local day; US -> Asia/Europe overnight lands the next day). When they change or relax the arrival day, immediately re-search with the new dates — do not re-serve the old results or just offer to search.
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


def run_assistant(query: str, history: list | None) -> dict:
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

    started = time.monotonic()
    rounds = 0
    iterations = 0
    # hard turn budget: past ~65s, stop searching and answer with what we have
    # (a Google throttle wave otherwise compounds into multi-minute hangs)
    while rounds < 3 and iterations < 8 and time.monotonic() - started < 65:
        iterations += 1
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=4000,
            output_config={"effort": "medium"},
            # cache breakpoint on system caches tools+system for every call in
            # the loop and across turns (prompt renders tools -> system -> messages)
            system=[{
                "type": "text",
                "text": assistant_system_prompt(),
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[SEARCH_TOOL, web_tool],
            messages=messages,
        )

        # server-side web search can pause mid-loop; re-send to let it resume
        if response.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            text, suggestions = split_suggestions(text)
            return {"message": text or "…", "sections": sections, "suggestions": suggestions}
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
        futures = {tu.id: pool.submit(execute_spec, tu.input) for tu in budgeted}
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
        al = getattr(Airline, code.strip().upper(), None)
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


def serialize_leg(leg) -> dict:
    return {
        "airline": leg.airline.value,
        "airline_code": leg.airline.name,
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

    primary_code = result.primary_airline.name if result.primary_airline else (legs[0]["airline_code"] if legs else None)
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
    # Google intermittently returns an unparseable payload (fli gives None) or
    # rate-limits with a 429 (fli raises after its own fast retries). A retry
    # with a longer cool-down, then a retry sorted by CHEAPEST, recovers most.
    last_exc = None
    for i, attempt_sort in enumerate((sort, sort, SortBy.CHEAPEST)):
        attempt = filters.model_copy(update={"sort_by": attempt_sort})
        try:
            results = search.search(attempt, top_n=top_n, currency="USD")
        except SearchClientError as e:
            last_exc = e
            time.sleep(2 + 2 * i)
            continue
        if results:
            return results
        time.sleep(0.4)
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
        try:
            date_prices = searcher.search(filters, currency="USD")
            last_exc = None
        except SearchClientError as e:
            last_exc = e
            time.sleep(3 + 3 * i)
            continue
        if date_prices:
            break
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


@app.post("/api/search")
async def search(request: Request):
    try:
        body = await request.json()
        query: str = (body.get("query") or "").strip()
        if not query:
            return JSONResponse({"error": "Query is required"}, status_code=400)

        result = run_assistant(query, body.get("history"))
        return JSONResponse({
            "type": "assistant",
            "message": result["message"],
            "sections": result["sections"],
            "suggestions": result.get("suggestions") or [],
        })

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
