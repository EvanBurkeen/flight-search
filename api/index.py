import json
import os
import time
from datetime import datetime
from functools import lru_cache
from urllib.parse import quote

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
            "trip_type": {"type": "string", "enum": ["one_way", "round_trip"]},
            "departure_date": {
                "type": ["string", "null"],
                "description": "YYYY-MM-DD. Null only when flexible_dates is set.",
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

How to handle requests:
- Vague is fine. Expand cities to their major airports (NYC -> JFK/LGA/EWR). "mid September" or "cheapest time to go" -> flexible_dates. Loyalty hints -> airline filters. A bare month/day means the nearest future date.
- "arrive by / be there by X" is an ARRIVAL constraint (arrival_time), never a departure cap.
- Comparisons: run one search per option (at most 4 per turn). A self-arranged overnight stopover = one search per leg with the correct date on each. Give each search a summary naming the option ("Option A: Thursday nonstop").
- Make reasonable assumptions and state them briefly instead of interrogating the user; ask a question only when origin or destination is truly unknowable.

Answering:
- The user sees result cards for every search you run, so don't recite every flight. Lead with your recommendation and the key numbers (totals for multi-leg plans, including a note that hotels/ground costs aren't included), then the trade-offs that matter.
- Mention real caveats from the data: nothing arrives before X, prices are one-way vs round-trip totals, self-transfer risks, tight or overnight layovers.
- If a search fails or is rate-limited, say so plainly and suggest trying again in a moment.
- Keep responses short and conversational — a few sentences, not a report.
- Formatting: you may use **bold** sparingly for the key number or verdict (it renders properly). No other markdown — no headers, no bullet syntax, no italics-by-asterisk."""


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
    rows = [_leg_summary(f) for f in (payload.get("results") or [])[:6]]
    return json.dumps({"kind": "flights", "note": payload.get("message"), "options": rows})


def _leg_summary(f: dict) -> dict:
    legs = f.get("legs") or []
    return {
        "airline": f.get("airline"),
        "price": f.get("price"),
        "currency": f.get("currency"),
        "depart": legs[0]["departure"] if legs else None,
        "arrive": legs[-1]["arrival"] if legs else None,
        "duration_min": f.get("duration"),
        "stops": f.get("stops"),
        "via": [lo["airport"] for lo in (f.get("layovers") or [])],
        "warnings": f.get("warnings") or [],
    }


def run_assistant(query: str, history: list | None) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in (history or [])[-12:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    messages.append({"role": "user", "content": query})

    sections: list[dict] = []
    searches_used = 0

    for _ in range(3):  # at most 3 tool rounds per turn
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=4000,
            output_config={"effort": "medium"},
            system=assistant_system_prompt(),
            tools=[SEARCH_TOOL],
            messages=messages,
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            text = "\n".join(b.text for b in response.content if b.type == "text").strip()
            return {"message": text or "…", "sections": sections}

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for tu in tool_uses:
            if searches_used >= 5:
                tool_results.append({
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": "Search budget for this turn exhausted; answer with what you have.",
                    "is_error": True,
                })
                continue
            searches_used += 1
            try:
                payload = execute_spec(tu.input)
                sections.append(payload)
                tool_results.append({
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": compact_for_model(payload),
                })
            except SearchHTTPError as e:
                detail = "Google Flights is rate-limiting right now (HTTP 429)." if e.status_code == 429 \
                    else f"Google Flights returned HTTP {e.status_code}."
                tool_results.append({
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": detail, "is_error": True,
                })
            except SearchClientError:
                tool_results.append({
                    "type": "tool_result", "tool_use_id": tu.id,
                    "content": "Couldn't reach Google Flights for this search.", "is_error": True,
                })
        messages.append({"role": "user", "content": tool_results})

    return {
        "message": "That took more searching than one turn allows — here's what I found so far. Ask me to continue if you need more.",
        "sections": sections,
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


def arrival_ok(result, travel_date: str | None, window: dict | None) -> bool:
    if not window or not (window.get("earliest") is not None or window.get("latest") is not None):
        return True
    arr = result.legs[-1].arrival_datetime if result.legs else None
    if not arr:
        return False
    if travel_date and arr.strftime("%Y-%m-%d") != travel_date:
        return False  # arrives on a different day than requested
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


def booking_url(origins: list, destinations: list, spec: dict) -> str:
    o = origins[0].name
    d = destinations[0].name
    q = f"flights from {o} to {d}"
    if spec.get("departure_date"):
        q += f" on {spec['departure_date']}"
    if spec.get("trip_type") == "round_trip" and spec.get("return_date"):
        q += f" returning {spec['return_date']}"
    elif spec.get("trip_type") == "one_way":
        q += " one way"
    cabin = spec.get("cabin")
    if cabin and cabin != "economy":
        q += f" {cabin.replace('_', ' ')}"
    return f"https://www.google.com/travel/flights?q={quote(q)}"


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
    return [p for p in (coords_for(c) for c in codes) if p]


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
        "legroom": leg.legroom_short,
        "overnight": leg.overnight,
    }


def serialize_flight(result, url: str) -> dict:
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

    return {
        "airline": result.primary_airline_name or (legs[0]["airline"] if legs else None),
        "airline_code": result.primary_airline.name if result.primary_airline else (legs[0]["airline_code"] if legs else None),
        "price": result.price,
        "currency": result.currency or "USD",
        "duration": result.duration,
        "stops": result.stops,
        "legs": legs,
        "layovers": layovers,
        "warnings": warnings,
        "highlights": [],
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
            results = search.search(attempt, top_n=top_n)
        except SearchClientError as e:
            last_exc = e
            time.sleep(3 + 3 * i)
            continue
        if results:
            return results
        time.sleep(1)
    if last_exc:
        raise last_exc
    return []


def search_fixed_dates(spec: dict, origins: list, destinations: list, currency: str) -> dict:
    filters = build_filters(spec, origins, destinations, show_all_results=True)
    sort = SORT_MAP.get(spec.get("sort"), SortBy.CHEAPEST)
    url = booking_url(origins, destinations, spec)
    results = run_search(SearchFlights(), filters, sort, top_n=8)

    if not results:
        return {
            "type": "flights",
            "message": "No flights found for that search. Try different dates, nearby airports, or fewer filters.",
            "results": [],
        }

    aw, rw = spec.get("arrival_time"), spec.get("return_arrival_time")
    dep_date = spec.get("departure_date")
    ret_date = spec.get("return_date") or dep_date
    arrival_note = None

    if isinstance(results[0], tuple):
        strict = [c for c in results if arrival_ok(c[0], dep_date, aw) and arrival_ok(c[-1], ret_date, rw)]
        if strict:
            results = strict
        elif aw or rw:
            arrival_note = "Nothing meets the arrival-time cutoff exactly — showing the closest arrivals instead."
            results = sorted(results, key=lambda c: c[0].legs[-1].arrival_datetime or datetime.max)

        itineraries = []
        for combo in results[:10]:
            out, ret = combo[0], combo[-1]
            total = max(p for p in [out.price, ret.price, 0] if p is not None)
            itineraries.append(
                {
                    "total_price": total,
                    "currency": out.currency or ret.currency or currency,
                    "outbound": serialize_flight(out, url),
                    "return": serialize_flight(ret, url),
                    "booking_url": url,
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

    strict = [r for r in results if arrival_ok(r, dep_date, aw)]
    if strict:
        results = strict
    elif aw:
        arrival_note = "Nothing arrives by that cutoff — showing the closest arrivals instead."
        results = sorted(results, key=lambda r: r.legs[-1].arrival_datetime or datetime.max)

    flights = [serialize_flight(r, url) for r in results[:10]]
    add_highlights(flights)
    message = f"Found {len(flights)} flights."
    if arrival_note:
        message = f"{arrival_note}\n{message}"
    return {
        "type": "flights",
        "message": message,
        "results": flights,
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
            date_prices = searcher.search(filters)
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
        "message": "Cheapest dates in your window — pick one to see actual flights.",
        "dates": dates,
    }


def execute_spec(spec: dict) -> dict:
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
    payload["assumptions"] = spec.get("assumptions") or []
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
        })

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
