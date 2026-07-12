import os
import time
from datetime import datetime
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
        "Run a flight search once the query contains (or you can reasonably infer) an origin, "
        "a destination, and a date or date range. Prefer making sensible assumptions and "
        "recording them over asking the user to clarify."
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
            "return_time": {
                "type": ["object", "null"],
                "description": "Return departure window in hours 0-23.",
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

CLARIFY_TOOL = {
    "name": "clarify",
    "description": (
        "Use ONLY when the query is missing something you cannot reasonably assume: "
        "no origin, no destination, or no date information whatsoever. Ask one concise question."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
        },
        "required": ["question"],
    },
}


def parse_query(query: str, history: list | None = None) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today = datetime.now().strftime("%A, %B %d, %Y")

    system_prompt = f"""You are the query parser for a flight search engine backed by live Google Flights data.
Today is {today}. All dates you output must be in the future; a month/day with no year means the nearest future occurrence.

Turn the user's natural-language request into one search_flights call. The user may be vague — that is fine:
- Cities or regions become lists of major airport codes.
- "next weekend", "mid September", "sometime this fall", "cheapest time to go" -> use flexible_dates with a sensible window.
- Loyalty or alliance hints map to airline/alliance filters.
- "cheap"/"budget" -> sort cheapest; "quickest"/"shortest" -> sort fastest; otherwise best.
- Record every leap of inference in assumptions so the user can correct you.

Only use clarify when origin or destination is truly unknowable from the query."""

    messages = list(history or [])
    messages.append({"role": "user", "content": query})

    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=2000,
        system=system_prompt,
        tools=[SEARCH_TOOL, CLARIFY_TOOL],
        tool_choice={"type": "any"},
        messages=messages,
    )

    tool_use = next(b for b in response.content if b.type == "tool_use")
    return {"tool": tool_use.name, "input": tool_use.input}


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


def time_restrictions(window: dict | None) -> TimeRestrictions | None:
    if not window:
        return None
    earliest, latest = window.get("earliest"), window.get("latest")
    if earliest is None and latest is None:
        return None
    return TimeRestrictions(earliest_departure=earliest, latest_departure=latest)


def build_filters(spec: dict, origins: list, destinations: list, filters_cls=FlightSearchFilters, **extra):
    is_round_trip = spec.get("trip_type") == "round_trip"

    segments = [
        FlightSegment(
            departure_airport=[[a, 0] for a in origins],
            arrival_airport=[[a, 0] for a in destinations],
            travel_date=spec.get("departure_date") or datetime.now().strftime("%Y-%m-%d"),
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
    # Google intermittently returns an unparseable payload (fli gives None);
    # a retry, then a retry sorted by CHEAPEST, recovers nearly all of them.
    for attempt_sort in (sort, sort, SortBy.CHEAPEST):
        attempt = filters.model_copy(update={"sort_by": attempt_sort})
        results = search.search(attempt, top_n=top_n)
        if results:
            return results
        time.sleep(1)
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

    if isinstance(results[0], tuple):
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
        return {
            "type": "itineraries",
            "message": f"Found {len(itineraries)} round-trip options. Prices are the real total for both directions.",
            "results": itineraries,
        }

    flights = [serialize_flight(r, url) for r in results[:10]]
    add_highlights(flights)
    return {
        "type": "flights",
        "message": f"Found {len(flights)} flights.",
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
    date_prices = searcher.search(filters)
    if date_prices is None:
        time.sleep(1)
        date_prices = searcher.search(filters)
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


@app.post("/api/search")
async def search(request: Request):
    try:
        body = await request.json()
        query: str = (body.get("query") or "").strip()
        if not query:
            return JSONResponse({"error": "Query is required"}, status_code=400)

        parsed = parse_query(query, body.get("history"))

        if parsed["tool"] == "clarify":
            return JSONResponse({
                "type": "clarify",
                "message": parsed["input"]["question"],
                "results": [],
            })

        spec = parsed["input"]
        origins, bad_origins = resolve_airports(spec.get("origins"))
        destinations, bad_destinations = resolve_airports(spec.get("destinations"))
        invalid = bad_origins + bad_destinations
        if not origins or not destinations:
            return JSONResponse({
                "type": "clarify",
                "message": f"I couldn't recognize these airport codes: {', '.join(invalid)}. Could you rephrase with standard airports or city names?",
                "results": [],
            })

        currency = (spec.get("currency") or "USD").upper()

        if spec.get("flexible_dates"):
            payload = search_flexible_dates(spec, origins, destinations, currency)
        elif spec.get("departure_date"):
            payload = search_fixed_dates(spec, origins, destinations, currency)
        else:
            return JSONResponse({
                "type": "clarify",
                "message": "When would you like to travel? A specific date or a rough window both work.",
                "results": [],
            })

        if spec.get("summary"):
            payload["message"] = f"{spec['summary']}\n\n{payload['message']}"
        payload["assumptions"] = spec.get("assumptions") or []
        return JSONResponse(payload)

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
