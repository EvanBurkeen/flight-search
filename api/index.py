import json
import os
from datetime import datetime

import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fli.models import (
    Airport,
    FlightSearchFilters,
    FlightSegment,
    MaxStops,
    PassengerInfo,
    SeatType,
    SortBy,
    TimeRestrictions,
    TripType,
)
from fli.search import SearchFlights

app = FastAPI()

SKYTEAM = {"DL", "VS", "AF", "KL", "AZ", "AM", "AR", "SU", "CZ", "MU", "VN", "ME", "KQ", "RO", "OK"}
ONEWORLD = {"AA", "BA", "IB", "QR", "QF", "JL", "CX", "FJ", "AY", "AS", "RJ"}
STAR_ALLIANCE = {"UA", "LH", "AC", "SQ", "NH", "OS", "SK", "LX", "TP", "TK", "SA"}

AIRLINE_URLS = {
    "DL": "https://www.delta.com",
    "AA": "https://www.aa.com",
    "UA": "https://www.united.com",
    "B6": "https://www.jetblue.com",
    "NK": "https://www.spirit.com",
    "F9": "https://www.flyfrontier.com",
    "WN": "https://www.southwest.com",
    "AS": "https://www.alaskaair.com",
}

CABIN_MAP = {
    "economy": SeatType.ECONOMY,
    "premium_economy": SeatType.PREMIUM_ECONOMY,
    "business": SeatType.BUSINESS,
    "first": SeatType.FIRST,
}


def get_alliance(code: str) -> str:
    if code in SKYTEAM:
        return "SkyTeam"
    if code in ONEWORLD:
        return "OneWorld"
    if code in STAR_ALLIANCE:
        return "Star Alliance"
    return "Independent"


async def parse_query(query: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    today = datetime.now().strftime("%B %d, %Y")

    system_prompt = f"""You are a flight search query parser. Extract structured information from natural language flight requests.

IMPORTANT: Today's date is {today}. When parsing dates:
- If a month/day like "3/21" is mentioned, assume the nearest future year
- Always output dates in YYYY-MM-DD format

MULTIPLE AIRPORTS:
- If user mentions "JFK/EWR" or "JFK or EWR", use the FIRST one as origin (JFK)
- Note the alternative in special_instructions

TIME PREFERENCES:
- "early" flight = departure before 10am → departure_time_before: 10
- "late" flight = departure after 6pm → departure_time_after: 18
- "morning" = before 12pm
- "afternoon" = 12pm–6pm
- "evening" = after 6pm

REFUNDABILITY:
- "must be refundable" → must_be_refundable: true
- "prefer refundable" → prefer_refundable: true

Return ONLY a JSON object with these fields:
{{
  "origin": "3-letter IATA code",
  "destination": "3-letter IATA code",
  "date": "YYYY-MM-DD",
  "return_date": "YYYY-MM-DD or null",
  "is_roundtrip": true/false,
  "primary_cabin": "economy/premium_economy/business/first",
  "compare_cabins": [],
  "alliance_preference": "SkyTeam/OneWorld/Star Alliance/any",
  "loyalty_program": "delta/united/american/etc or null",
  "specific_airlines": [],
  "exclude_airlines": [],
  "departure_time_after": null,
  "departure_time_before": null,
  "return_time_after": null,
  "return_time_before": null,
  "must_be_refundable": false,
  "prefer_refundable": false,
  "must_be_direct": false,
  "prefer_direct": false,
  "needs_extra_legroom": false,
  "price_sensitivity": "moderate",
  "special_instructions": null,
  "confidence": "high/medium/low"
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": query}],
    )

    text = message.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    return json.loads(text)


def make_segment(
    origin: str,
    destination: str,
    date: str,
    time_after: int | None = None,
    time_before: int | None = None,
) -> FlightSegment:
    dep = getattr(Airport, origin.upper())
    arr = getattr(Airport, destination.upper())

    time_restrictions = None
    if time_after is not None or time_before is not None:
        time_restrictions = TimeRestrictions(
            earliest_departure=time_after,
            latest_departure=time_before,
        )

    return FlightSegment(
        departure_airport=[[dep, 0]],
        arrival_airport=[[arr, 0]],
        travel_date=date,
        time_restrictions=time_restrictions,
    )


def run_fli_search(
    origin: str,
    destination: str,
    date: str,
    cabin: str = "economy",
    must_be_direct: bool = False,
    exclude_airlines: list[str] | None = None,
    time_after: int | None = None,
    time_before: int | None = None,
) -> list:
    segment = make_segment(origin, destination, date, time_after, time_before)

    filters = FlightSearchFilters(
        trip_type=TripType.ONE_WAY,
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[segment],
        stops=MaxStops.NON_STOP if must_be_direct else MaxStops.ANY,
        seat_type=CABIN_MAP.get(cabin, SeatType.ECONOMY),
        sort_by=SortBy.BEST,
        show_all_results=True,
    )

    results = SearchFlights().search(filters, top_n=20) or []

    if exclude_airlines:
        exclude = {a.upper() for a in exclude_airlines}
        results = [
            r for r in results
            if r.legs and r.legs[0].airline.name not in exclude
        ]

    return results


def evaluate_flight(result, criteria: dict, cabin: str) -> dict | None:
    if not result.legs:
        return None

    first = result.legs[0]
    last = result.legs[-1]

    carrier_code = first.airline.name  # e.g. "DL"
    airline_name = first.airline.value  # e.g. "Delta Air Lines"
    alliance = get_alliance(carrier_code)

    if criteria.get("exclude_airlines"):
        if carrier_code in {a.upper() for a in criteria["exclude_airlines"]}:
            return None

    price = result.price or 0
    stops = result.stops
    duration = result.duration  # minutes

    dep_time = first.departure_datetime.strftime("%H:%M") if first.departure_datetime else None
    arr_time = last.arrival_datetime.strftime("%H:%M") if last.arrival_datetime else None

    score = 50
    highlights: list[str] = []

    if stops == 0:
        score += 30

    if alliance != "Independent":
        score += 5
    if criteria.get("alliance_preference") and criteria["alliance_preference"] == alliance:
        score += 15
        highlights.append(f"✓ {alliance}")

    if dep_time:
        dep_hour = int(dep_time.split(":")[0])
        if criteria.get("departure_time_before") and dep_hour < criteria["departure_time_before"]:
            score += 10
            highlights.append(f"✓ Early departure ({dep_time})")
        if criteria.get("departure_time_after") and dep_hour >= criteria["departure_time_after"]:
            score += 10
            highlights.append(f"✓ Late departure ({dep_time})")

    if duration and duration < 300:
        score += 10
        highlights.append(f"✓ Quick ({duration // 60}h {duration % 60}m)")
    else:
        score += 5

    score += 15  # price component

    dep_code = criteria.get("origin", "")
    arr_code = criteria.get("destination", "")
    booking_url = AIRLINE_URLS.get(
        carrier_code,
        f"https://www.google.com/travel/flights?q={dep_code}%20to%20{arr_code}",
    )

    return {
        "airline": airline_name,
        "airline_code": carrier_code,
        "alliance": alliance,
        "price": price,
        "aircraft": "N/A",
        "departure_time": dep_time,
        "arrival_time": arr_time,
        "duration": duration,
        "stops": stops,
        "cabin_class": cabin,
        "score": score,
        "highlights": highlights,
        "warnings": [],
        "booking_url": booking_url,
        "details": {},
    }


@app.post("/api/search")
async def search(request: Request):
    try:
        body = await request.json()
        query: str = body.get("query", "")
        search_type: str = body.get("searchType", "outbound")

        if not query:
            return JSONResponse({"error": "Query is required"}, status_code=400)

        criteria = await parse_query(query)

        if not criteria.get("origin") or not criteria.get("destination") or not criteria.get("date"):
            return JSONResponse({
                "message": "⚠️ I need to know:\n• Origin airport (e.g., JFK)\n• Destination airport (e.g., LHR)\n• Travel date (e.g., 3/21)",
                "results": [],
            })

        is_roundtrip = bool(criteria.get("return_date"))

        if search_type == "return" and is_roundtrip:
            src, dst, date = criteria["destination"], criteria["origin"], criteria["return_date"]
            time_after = criteria.get("return_time_after")
            time_before = criteria.get("return_time_before")
        else:
            src, dst, date = criteria["origin"], criteria["destination"], criteria["date"]
            time_after = criteria.get("departure_time_after")
            time_before = criteria.get("departure_time_before")

        cabin = criteria.get("primary_cabin", "economy")

        try:
            raw = run_fli_search(
                origin=src,
                destination=dst,
                date=date,
                cabin=cabin,
                must_be_direct=criteria.get("must_be_direct", False),
                exclude_airlines=criteria.get("exclude_airlines"),
                time_after=time_after,
                time_before=time_before,
            )
        except AttributeError:
            return JSONResponse({
                "message": f"⚠️ Unknown airport code. Please use standard IATA codes (e.g., JFK, LHR, LAX).",
                "results": [],
            })

        if not raw:
            return JSONResponse({
                "message": "❌ No flights found for your search. Try different dates or airports.",
                "results": [],
            })

        results = [evaluate_flight(f, criteria, cabin) for f in raw]
        results = [r for r in results if r is not None]
        results.sort(key=lambda r: r["score"], reverse=True)

        if not results:
            return JSONResponse({
                "message": "❌ No flights found matching your criteria. Try relaxing requirements or different dates.",
                "results": [],
            })

        has_direct = any(r["stops"] == 0 for r in results)
        has_stops = any(r["stops"] > 0 for r in results)
        if has_direct and has_stops:
            for r in results:
                if r["stops"] == 0:
                    r["highlights"].insert(0, "✓ Direct")

        if search_type == "return":
            message = f"Return flights {src} → {dst}\nDate: {date}\n\nSelect your return flight (prices are one-way):"
        elif is_roundtrip:
            message = f"Outbound flights {src} → {dst}\nDate: {date}\n"
            if criteria.get("special_instructions"):
                message += f"Note: {criteria['special_instructions']}\n"
            message += "\nSelect your outbound flight. After you pick, I'll search return flights:"
        else:
            message = f"Found {len(results)} flights\n\nRoute: {src} → {dst}\nDate: {date}\nCabin: {cabin.replace('_', ' ')}\n"
            if criteria.get("alliance_preference") and criteria["alliance_preference"] != "any":
                message += f"Alliance: {criteria['alliance_preference']}\n"
            if criteria.get("exclude_airlines"):
                message += f"Excluded: {', '.join(criteria['exclude_airlines'])}\n"
            if criteria.get("special_instructions"):
                message += f"Note: {criteria['special_instructions']}\n"
            message += f"\nTop {min(5, len(results))} options ranked by quality and value"

        return JSONResponse({
            "message": message,
            "results": results[:10],
            "isRoundTrip": is_roundtrip and search_type != "return",
        })

    except json.JSONDecodeError:
        return JSONResponse({"error": "Failed to parse flight query"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
