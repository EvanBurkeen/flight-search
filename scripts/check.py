#!/usr/bin/env python
"""Offline regression checks for the invariants this project has already paid for.

Every check here exists because something broke in production once. They run
with no network and no API key, in about a second:

    .venv/bin/python scripts/check.py

The pre-push hook runs this. If you are an AI assistant working on this repo:
a failure means you have reintroduced a bug that a human already found. Read
the check's docstring and the Changelog entry it names before "fixing" the test.
"""
from __future__ import annotations

import base64
import os
import sys
import threading
import time
import types
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "api"))
os.environ.setdefault("ANTHROPIC_API_KEY", "offline-checks")
os.environ.pop("FLI_PROXY", None)

import index as app  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n{title}")


# --------------------------------------------------------------------------
# helpers: fake fli results, rich enough for serialize_flight()
# --------------------------------------------------------------------------
def leg(frm: str, to: str, dep: str, arr: str, airline: str, number: str):
    return types.SimpleNamespace(
        departure_airport=types.SimpleNamespace(name=frm),
        arrival_airport=types.SimpleNamespace(name=to),
        departure_datetime=datetime.fromisoformat(dep),
        arrival_datetime=datetime.fromisoformat(arr),
        airline=types.SimpleNamespace(name=airline, value=airline),
        flight_number=number, aircraft="Boeing 737MAX 8 Passenger",
        overnight=False, operating_airline=None,
    )


def result(legs: list, price, stops=0, duration=200):
    first = legs[0].airline
    return types.SimpleNamespace(
        legs=legs, price=price, currency="USD", stops=stops, duration=duration,
        layovers=[], warnings=[], primary_airline=first, primary_airline_name=first.value,
        co2_emissions_delta_pct=None, self_transfer=False, mixed_cabin=False,
        alliance=None, airport_change=False,
    )


def row(price, duration, stops, warnings=None):
    return {"price": price, "duration": duration, "stops": stops,
            "warnings": warnings or [], "highlights": []}


# --------------------------------------------------------------------------
section("Google deep links (tfs)  — Changelog: 'book the flight you clicked', 'deep links for every trip type'")
# --------------------------------------------------------------------------
# A real Google-issued URL. Byte-identity is the canary for schema drift: if
# Google changes the format, this fails loudly instead of shipping dead links.
REAL_ONE_WAY = ("CBwQAhpAEgoyMDI2LTExLTI5IiAKA0ZMTBIKMjAyNi0xMS0yORoDSkZLKgJCNjIEMTgwMmoHCAESA0ZM"
                "THIHCAESA0pGS0ABSAFwAYIBCwj___________8BmAEC")
one_way = app.itinerary_url([[leg("FLL", "JFK", "2026-11-29T10:00", "2026-11-29T13:00", "B6", "1802")]],
                            trip_type=2)
check("one-way tfs is byte-identical to a real Google link",
      one_way and one_way.split("tfs=")[1] == REAL_ONE_WAY)


def tfs_field(url: str, field: int):
    """Read a top-level varint field out of the tfs protobuf.

    Note the field KEY is itself a varint: field 19's key is 0x98 0x01, so a
    parser that reads one byte per key desynchronises on anything past field 15.
    """
    raw = url.split("tfs=")[1]
    b = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))

    def varint(i: int) -> tuple[int, int]:
        v = 0; s = 0
        while i < len(b):
            c = b[i]; i += 1
            v |= (c & 0x7F) << s; s += 7
            if not c & 0x80:
                return v, i
        raise ValueError("truncated varint")

    i, found = 0, None
    while i < len(b):
        key, i = varint(i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = varint(i)
            if f == field:
                found = v
        elif w == 2:
            ln, i = varint(i)
            i += ln
        else:
            break
    return found


rt = app.itinerary_url([[leg("LGA", "FLL", "2026-08-05T10:00", "2026-08-05T13:00", "DL", "2465")],
                        [leg("FLL", "LGA", "2026-08-29T10:00", "2026-08-29T13:00", "DL", "2133")]],
                       trip_type=1)
mc = app.itinerary_url([[leg("FLL", "ATL", "2026-12-28T06:35", "2026-12-28T08:30", "DL", "1215"),
                         leg("ATL", "ICN", "2026-12-28T11:00", "2026-12-29T15:30", "DL", "189")],
                        [leg("ICN", "HRB", "2027-01-01T12:20", "2027-01-01T13:35", "OZ", "339")]],
                       trip_type=3)
# f19 is the trip type. Putting it in f2 made Google reject the URL and fall
# back to its home page — that cost a full debugging cycle.
check("trip type lives in f19, not f2 (2=one-way, 1=round, 3=multi-city)",
      (tfs_field(one_way, 19), tfs_field(rt, 19), tfs_field(mc, 19)) == (2, 1, 3),
      f"f19={tfs_field(one_way,19)},{tfs_field(rt,19)},{tfs_field(mc,19)}")
check("f2 stays constant at 2 for every trip type",
      {tfs_field(one_way, 2), tfs_field(rt, 2), tfs_field(mc, 2)} == {2})
check("a leg with no departure time yields no link rather than a broken one",
      app.itinerary_url([[types.SimpleNamespace(departure_datetime=None)]]) is None)

# --------------------------------------------------------------------------
section("Value ranking  — Changelog: 'value ranking', 'representative results'")
# --------------------------------------------------------------------------
# The BOS->FLL case: $129 for 11h59m with a stop vs $154 nonstop in 3h20m.
fastest = 200
check("a nonstop beats a cheaper option that costs hours",
      app.trip_cost(154, 200, 0, [], fastest) < app.trip_cost(129, 719, 1, [], fastest),
      f"{app.trip_cost(154,200,0,[],fastest):.0f} vs {app.trip_cost(129,719,1,[],fastest):.0f}")
check("a huge fare premium does NOT buy a small time saving",
      app.trip_cost(900, 200, 0, [], fastest) > app.trip_cost(300, 320, 1, [], fastest))
# HOUR_VALUE=0 slipped past an earlier version of these checks because the
# stop penalty alone still ordered the BOS->FLL example correctly. This case has
# no stop difference, so it fails the moment time stops being valued.
check("time is genuinely priced (a cheaper flight 10h longer must lose)",
      app.trip_cost(200, 200, 0, [], fastest) < app.trip_cost(150, 800, 0, [], fastest),
      f"{app.trip_cost(200,200,0,[],fastest):.0f} vs {app.trip_cost(150,800,0,[],fastest):.0f}")
check("HOUR_VALUE is a real, documented product weight (>= $10/hour)",
      app.HOUR_VALUE >= 10, f"HOUR_VALUE={app.HOUR_VALUE}")
check("documented warnings raise the effective cost",
      app.trip_cost(200, 200, 1, ["Self-transfer: separate tickets"], fastest)
      > app.trip_cost(200, 200, 1, [], fastest))

rows = [row(129, 719, 1), row(154, 200, 0), row(140, 313, 1), row(600, 205, 0)]
ordered = app.order_by_value(rows, price_of=lambda r: r["price"], duration_of=lambda r: r["duration"],
                             stops_of=lambda r: r["stops"], warnings_of=lambda r: r["warnings"],
                             requested_sort=None)
check("best value leads by default", ordered[0]["price"] == 154)
check("an explicit 'cheapest' request is obeyed verbatim",
      app.order_by_value(rows, price_of=lambda r: r["price"], duration_of=lambda r: r["duration"],
                         stops_of=lambda r: r["stops"], warnings_of=lambda r: r["warnings"],
                         requested_sort="cheapest") == rows)
check("the outright cheapest stays inside the preview",
      ordered.index(next(r for r in ordered if r["price"] == 129)) < app.PREVIEW_GUARANTEE)

# The Nov 22 case: nonstops priced above the 50th-cheapest fare were dropped
# before scoring, so "Nonstop only" reported 1 of 50 when 12 existed.
many = [row(100 + i, 500, 1) for i in range(60)] + [row(514, 210, 0), row(414, 205, 0), row(444, 208, 0)]
# mirror production: rank by value first, THEN cut
ranked = app.order_by_value(many, price_of=lambda r: r["price"], duration_of=lambda r: r["duration"],
                            stops_of=lambda r: r["stops"], warnings_of=lambda r: r["warnings"],
                            requested_sort=None)
kept = app.retain_representative(ranked, 50, price_of=lambda r: r["price"],
                                 duration_of=lambda r: r["duration"], stops_of=lambda r: r["stops"])
check("the cut keeps nonstops even when they price above the cut line",
      sum(1 for r in kept if r["stops"] == 0) == 3, f"{sum(1 for r in kept if r['stops']==0)} kept")
check("the cut keeps the cheapest and the fastest",
      min(r["price"] for r in kept) == min(r["price"] for r in many)
      and min(r["duration"] for r in kept) == min(r["duration"] for r in many))
# forcing rows into the tail filled slots backwards once, which listed a $514
# nonstop above a $414 one; the cut must preserve the ranking it was given
rank_of = {id(r): i for i, r in enumerate(ranked)}
check("the cut preserves value order (rescued rows are not reversed)",
      [rank_of[id(r)] for r in kept] == sorted(rank_of[id(r)] for r in kept))

# --------------------------------------------------------------------------
section("Search cache  — Changelog: 'tail latency'")
# --------------------------------------------------------------------------
calls: list[int] = []
app._search_cache.clear()
_real_execute = app.execute_spec
app.execute_spec = lambda spec: (calls.append(1), time.sleep(0.4),
                                 {"type": "flights", "results": [{"price": 1}], "message": "m"})[-1]
spec = {"origins": ["JFK"], "destinations": ["ORD"], "departure_date": "2026-09-18"}
threads = [threading.Thread(target=lambda: app.cached_execute_spec(dict(spec))) for _ in range(3)]
[t.start() for t in threads]
[t.join() for t in threads]
check("identical concurrent searches collapse to one upstream call", len(calls) == 1, f"{len(calls)} call(s)")
app.cached_execute_spec(dict(spec))
check("a repeat search is served from cache", len(calls) == 1)
got = app.cached_execute_spec(dict(spec))
got["results"][0]["price"] = 999
check("the cache hands out copies, so callers cannot poison it",
      app.cached_execute_spec(dict(spec))["results"][0]["price"] == 1)
app.execute_spec = _real_execute
check("cosmetic fields do not split the cache key",
      app._spec_key({**spec, "summary": "a", "assumptions": ["x"]})
      == app._spec_key({**spec, "summary": "b", "assumptions": []}))
check("cabin DOES split the key (never serve the wrong cabin)",
      app._spec_key(spec) != app._spec_key({**spec, "cabin": "business"}))

# --------------------------------------------------------------------------
section("Multi-city pricing  — Changelog: 'multi-city prices: quote what you can actually buy'")
# --------------------------------------------------------------------------
# Google quoted $2,207 to fly these as one ticket; the same flights cost
# $844 + $182 bought separately, which is what its booking page sells.
p1 = result([leg("FLL", "ICN", "2026-12-28T06:35", "2026-12-29T15:30", "DL", "201")], 2207, stops=1, duration=1135)
p2 = result([leg("ICN", "HRB", "2027-01-01T12:20", "2027-01-01T13:35", "OZ", "339")], 2207, stops=0, duration=135)
_real_run, _real_index = app.run_search, app.leg_price_index
app.run_search = lambda *a, **k: [(p1, p2)]
app.leg_price_index = lambda origins, dests, date_, cabin, win=None: (
    {(("DL", "201"),): 844.0} if "FLL" in list(origins) else {(("OZ", "339"),): 182.0})
mc_out = app.search_multi_city({"multi_city_segments": [
    {"origins": ["FLL"], "destinations": ["ICN"], "date": "2026-12-28"},
    {"origins": ["ICN"], "destinations": ["HRB"], "date": "2027-01-01"}]}, "USD")
app.run_search, app.leg_price_index = _real_run, _real_index
it = (mc_out.get("results") or [{}])[0]
check("quotes the purchasable price, not the joint fare nobody buys",
      it.get("total_price") == 1026, f"quoted {it.get('total_price')}")
check("says which basis it quoted", it.get("price_basis") == "separate tickets")
check("warns that mixed carriers may be separate tickets",
      any("separate tickets" in w for w in it.get("warnings") or []))

# --------------------------------------------------------------------------
section("Round-trip expansion breadth  — Changelog: 'the board, and representative expansion'")
# --------------------------------------------------------------------------
# fli expands flights[:top_n] in sort order (usually cheapest-first), so the
# expansion set was "the N cheapest outbounds": a pricier nonstop never got
# returns priced, and the assistant told the user nonstops did not exist.
def _ob(price, dur, stops, hour):
    return types.SimpleNamespace(price=price, duration=dur, stops=stops,
                                 legs=[types.SimpleNamespace(
                                     departure_datetime=datetime(2026, 11, 17, hour, 0))])

_obs = [_ob(600 + i, 500, 1, 19) for i in range(15)] + \
       [_ob(900, 420, 0, 8), _ob(950, 400, 0, 14), _ob(880, 380, 1, 7)]
_kept = app.representative_outbounds(_obs, 10)
check("expansion keeps every nonstop, not just the N cheapest outbounds",
      sum(1 for f in _kept if f.stops == 0) == 2, f"{sum(1 for f in _kept if f.stops == 0)} of 2")
check("expansion keeps the fastest outbound", any(f.duration == 380 for f in _kept))
check("the sort's own top pick still leads (an explicit 'cheapest' stays obeyed)",
      _kept[0].price == 600)
check("expansion covers the departure buckets the data offers",
      len({0 if f.legs[0].departure_datetime.hour < 6 else
           1 if f.legs[0].departure_datetime.hour < 12 else
           2 if f.legs[0].departure_datetime.hour < 18 else 3 for f in _kept}) >= 3)
check("expansion never exceeds its budget", len(_kept) == 10, f"{len(_kept)}")

# a round-trip search that got the outbound list but no pairings must ship
# the outbounds from-priced (tap-to-price), never "No flights found" — the
# July 25 NYC-FLL blank screens were the good half of the data being discarded
_deg_outs = [result([leg("JFK", "FLL", "2026-08-08T07:34", "2026-08-08T10:37", "B6", "1112")], 277.0),
             result([leg("EWR", "FLL", "2026-08-08T09:00", "2026-08-08T12:05", "UA", "310")], 297.0)]
_real_run_search = app.run_search
def _fake_run_search(search, filters, sort, top_n, budget_s=35.0):
    search.last_outbounds = _deg_outs
    return []
app.run_search = _fake_run_search
_o, _ = app.resolve_airports(["JFK", "EWR"]); _d, _ = app.resolve_airports(["FLL"])
_deg = app.search_fixed_dates({"trip_type": "round_trip", "origins": ["JFK", "EWR"],
                               "destinations": ["FLL"], "departure_date": "2026-08-08",
                               "return_date": "2026-08-11"}, _o, _d, "USD")
app.run_search = _real_run_search
check("a pairing-less round trip ships from-priced outbounds, not 'No flights found'",
      _deg.get("type") == "itineraries" and _deg.get("results") == []
      and len(_deg.get("more_outbounds") or []) == 2,
      f"type={_deg.get('type')} more={len(_deg.get('more_outbounds') or [])}")
check("...each with its honest from-total", (_deg.get("more_outbounds") or [{}])[0].get("from_total") == 277.0)
check("...and the spec_echo the tap-to-price endpoint needs",
      (_deg.get("spec_echo") or {}).get("origins") == ["JFK", "EWR"]
      and (_deg.get("spec_echo") or {}).get("return_date") == "2026-08-11")
check("...while telling the model it must not claim flights are unavailable",
      "NOT claim" in (_deg.get("message") or ""))

# unexpanded outbounds must reach the model, or it infers absence from a sample
_it_payload = {
    "type": "itineraries", "message": "m",
    "results": [{"total_price": 609, "currency": "USD",
                 "outbound": {"airline": "FI", "legs": [], "duration": 500, "stops": 1, "warnings": []},
                 "return": {"airline": "FI", "legs": [], "duration": 500, "stops": 1, "warnings": []},
                 "return_options": [1, 2]}],
    "more_outbounds": [{"stops": 0, "from_total": 745.0}, {"stops": 1, "from_total": 700.0}],
}
_compact = app.compact_for_model(_it_payload)
check("the model is told about outbounds whose returns were not priced",
      '"unpriced_outbounds"' in _compact and '"nonstops": 1' in _compact and '"cheapest_from": 700.0' in _compact)

# --------------------------------------------------------------------------
section("Flexible-date grids  — Changelog: 'round-trip grids priced same-day returns'")
# --------------------------------------------------------------------------
# Searched without a duration, Google's date grid prices departing AND flying
# home on the same date. Every "round trip" fare in the calendar was for a
# 0-night stay — understated and unbuyable in spirit.
_flex = {"from_date": "2026-09-01", "to_date": "2026-09-30"}
_extra, _assumed = app.flex_grid_params(_flex, is_round_trip=True)
check("a round-trip grid without a trip length prices a real stay, not a same-day return",
      _extra.get("duration", 0) >= 2, f"duration={_extra.get('duration')}")
check("...and the assumed nights are surfaced, not silent",
      _assumed == _extra.get("duration"))
_extra, _assumed = app.flex_grid_params({**_flex, "trip_length_days": 10}, is_round_trip=True)
check("an explicit trip length is used verbatim, with nothing to disclose",
      _extra.get("duration") == 10 and _assumed is None)
_extra, _assumed = app.flex_grid_params(_flex, is_round_trip=False)
check("one-way grids take no duration at all",
      "duration" not in _extra and _assumed is None)

# --------------------------------------------------------------------------
section("Display details that were each a reported bug")
# --------------------------------------------------------------------------
check("digit-leading airline codes lose fli's underscore (_7C -> 7C)",
      app.airline_code(types.SimpleNamespace(name="_7C")) == "7C")
check("aircraft names drop 'Passenger' and fix MAX spacing",
      app.cleanAircraft("Boeing 737MAX 8 Passenger") == "Boeing 737 MAX 8"
      if hasattr(app, "cleanAircraft") else True)
check("a plain route search is detected (drives low-effort routing)",
      app.looks_like_plain_search("JFK to ORD sept 18 cheapest"))
check("a knowledge question is NOT treated as a route search",
      not app.looks_like_plain_search("what is the baggage allowance on Delta domestic?"))

# --------------------------------------------------------------------------
section("README drift  — the doc must match the code it describes")
# --------------------------------------------------------------------------
# The pre-push hook forces a README edit per code push, but a Changelog line
# satisfies it while the DESCRIPTIVE sections rot — the July 28 audit found
# stale timeouts, a 'parked' streaming note, and a missing feature. These
# checks pin the doc's verifiable facts to the code's constants: change a
# constant and the push blocks until the matching section is updated too.
import re as _re

_readme = open(os.path.join(ROOT, "README.md")).read()
_src = open(os.path.join(ROOT, "api", "index.py")).read()
_fe = open(os.path.join(ROOT, "public", "index.html")).read()

_m = _re.search(r'TURN_BUDGET_S"\) or (\d+)', _src)
check("README states the code's turn budget",
      bool(_m) and f"{_m.group(1)}s" in _readme,
      f"code default {_m.group(1) if _m else '?'}s — update 'How a turn works', "
      "Operations, and trade-offs if this changed")
_t = _re.search(r"_local\.fli_timeout = (\d+)\.0 if i == 0 else (\d+)\.0", _src)
check("README states the adaptive request timeouts",
      bool(_t) and f"{_t.group(1)}s" in _readme and f"{_t.group(2)}s" in _readme,
      "update the run_search description and Operations")
for _ep in ("/api/returns", "/api/search/stream"):
    check(f"README documents {_ep}", _ep in _src and _ep in _readme)
check("README and code agree on the Haiku first-call router",
      ("claude-haiku-4-5" in _src) == ("claude-haiku-4-5" in _readme))
_n = _re.search(r"run_search\(searcher, filters, sort, top_n=(\d+)\)", _src)
check("README's round-trip group count matches top_n",
      bool(_n) and f"~{_n.group(1)} round-trip OUTBOUND groups" in _readme,
      f"code expands top_n={_n.group(1) if _n else '?'}")
check("README documents streaming as live iff the frontend consumes it",
      ("streamTurn" in _fe) == ("Streaming is LIVE" in _readme))

# --------------------------------------------------------------------------
section("Process guards")
# --------------------------------------------------------------------------
hooks = os.popen(f"git -C {ROOT} config --get core.hooksPath").read().strip()
check("pre-push hook is installed (git config core.hooksPath = .githooks)",
      hooks == ".githooks",
      "run: git -C <repo> config core.hooksPath .githooks" if hooks != ".githooks" else "")

print()
if FAILURES:
    print(f"{len(FAILURES)} CHECK(S) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    print("\nEach of these encodes a bug a human already found in production.")
    sys.exit(1)
print("all checks passed")
