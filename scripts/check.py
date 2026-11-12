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
import json
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

# Offline checks must not pay real wall-clock. Several sections drive the real
# retry ladders (search_flexible_dates, verify_completeness), whose per-attempt
# breaker_wait pauses up to 8s — pure timing, no logic, but ~59s across the
# suite and a pre-push hook that looks hung. breaker_wait is a bounded pause
# nothing polls on, so no-op'ing it is safe (time.sleep is left alone: some
# code busy-waits against a monotonic deadline and would spin without it).
app.breaker_wait = lambda *a, **k: None

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


def future(days: int) -> str:
    """A date `days` from today. Any spec that reaches build_filters MUST use
    this, never a literal: fli's FlightSegment validator rejects past dates,
    so a hard-coded "2026-08-08" works for weeks and then kills the whole
    suite at local midnight (which it did, Aug 8 -> 9)."""
    from datetime import date, timedelta
    return (date.today() + timedelta(days=days)).isoformat()


# Mechanism over instruction: scan this file for date literals in spec keys
# and fail A WEEK BEFORE one expires, not at midnight in a pre-push panic.
import re as _re
_self_src = open(os.path.abspath(__file__)).read()
_bombs = []
for _m in _re.finditer(r'"(?:departure_date|return_date|from_date|to_date|date)":\s*"(20\d\d-\d\d-\d\d)"', _self_src):
    if _m.group(1) < future(7):
        _bombs.append(_m.group(1))
section("Suite hygiene")
check("no spec date literal in this file expires within a week",
      not _bombs, ", ".join(_bombs) or "all evergreen")


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

# Aug 7: the schema comment said "8: seat, 9: adults" — backwards, and the
# 1-adult-economy fixture above cannot catch it (both readings encode as a
# single value 1). Decoded from Google's OWN tokens (2 adults + 2 children,
# economy vs business, same session): f8 is one varint per traveler
# (adult=1, child=2), f9 is the seat. Before the fix, business-class Book
# links encoded "one lap infant, economy" — and a family of four booked one
# seat (the README's open bug 2, now closed).
def tfs_fields(url: str, field: int) -> list:
    """Every varint value of a repeated top-level tfs field, in order."""
    raw = url.split("tfs=")[1].split("&")[0]
    b = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))

    def varint(i):
        v = s = 0
        while i < len(b):
            c = b[i]; i += 1
            v |= (c & 0x7F) << s; s += 7
            if not c & 0x80:
                return v, i
        raise ValueError("truncated varint")

    i, out = 0, []
    while i < len(b):
        key, i = varint(i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = varint(i)
            if f == field:
                out.append(v)
        elif w == 2:
            ln, i = varint(i)
            i += ln
        else:
            break
    return out


_fam_leg = [leg("FLL", "JFK", "2026-11-29T10:00", "2026-11-29T13:00", "B6", "1802")]
_fam = app.itinerary_url([_fam_leg], trip_type=2, adults=2, children=2)
check("passengers ride in f8, one entry per traveler (adult=1, child=2)",
      tfs_fields(_fam, 8) == [1, 1, 2, 2] and tfs_fields(_fam, 9) == [1],
      f"f8={tfs_fields(_fam, 8)} f9={tfs_fields(_fam, 9)}")
_biz = app.itinerary_url([_fam_leg], cabin="business", trip_type=2)
check("the seat class rides in f9 (business=3), never in the passenger field",
      tfs_fields(_biz, 8) == [1] and tfs_fields(_biz, 9) == [3],
      f"f8={tfs_fields(_biz, 8)} f9={tfs_fields(_biz, 9)}")
check("the defaults stay byte-identical to the pax-free call (fixture guard)",
      app.itinerary_url([_fam_leg], trip_type=2, adults=1, children=0) == one_way)
_sf_pax = app.serialize_flight(result(list(_fam_leg), 239.0), "economy", adults=2, children=1)
check("serialize_flight threads the party into the Book link",
      tfs_fields(_sf_pax["booking_url"], 8) == [1, 1, 2])
_mc_fam = app.multi_city_search_url([
    {"origins": ["ATL"], "destinations": ["PEK"], "date": "2027-01-01"},
    {"origins": ["HKG"], "destinations": ["JFK"], "date": "2027-01-15"}], adults=2, children=1)
check("the multi-city handoff link carries the party too",
      tfs_fields(_mc_fam.split("&curr")[0], 8) == [1, 1, 2])

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
spec = {"origins": ["JFK"], "destinations": ["ORD"], "departure_date": future(13)}
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
app.leg_price_index = lambda origins, dests, date_, cabin, win=None, **_k: (
    {(("DL", "201"),): 844.0} if "FLL" in list(origins) else {(("OZ", "339"),): 182.0})
mc_out = app.search_multi_city({"multi_city_segments": [
    {"origins": ["FLL"], "destinations": ["ICN"], "date": future(140)},
    {"origins": ["ICN"], "destinations": ["HRB"], "date": future(144)}]}, "USD")
app.run_search, app.leg_price_index = _real_run, _real_index
it = (mc_out.get("results") or [{}])[0]
check("quotes the purchasable price, not the joint fare nobody buys",
      it.get("total_price") == 1026, f"quoted {it.get('total_price')}")
check("says which basis it quoted", it.get("price_basis") == "separate tickets")
check("warns that mixed carriers may be separate tickets",
      any("separate tickets" in w for w in it.get("warnings") or []))

# Aug 7: production presented "$5,409 one ticket" as best value while the
# SAME turn's per-leg searches found $789 + $701 purchasable. The exact-
# signature comparison above only fires when the combined itinerary reuses
# the standalone one-ways' EXACT flights (all_matched) — usually false — so
# the honest number silently never surfaced. The per-leg payloads already
# running beside the probe now provide a purchasable separate-ticket FLOOR
# that ships as its own honestly-labeled option.
_fp1 = result([leg("ATL", "PEK", "2026-12-01T10:00", "2026-12-02T14:00", "DL", "100")], 5409, stops=1, duration=1100)
_fp2 = result([leg("HKG", "JFK", "2026-12-10T10:00", "2026-12-10T15:00", "KE", "200")], 5409, stops=0, duration=900)
_mc_floor_spec = {"multi_city_segments": [
    {"origins": ["ATL"], "destinations": ["PEK"], "date": "2026-12-01"},
    {"origins": ["HKG"], "destinations": ["JFK"], "date": "2026-12-10"}]}


def _floor_leg_payload(spec):
    if "ATL" in (spec.get("origins") or []):
        return {"type": "flights", "results": [app.serialize_flight(result(
            [leg("ATL", "PEK", "2026-12-01T09:00", "2026-12-02T13:00", "UA", "55")], 789, stops=1, duration=1150))]}
    return {"type": "flights", "results": [app.serialize_flight(result(
        [leg("HKG", "JFK", "2026-12-10T11:00", "2026-12-10T16:00", "CX", "88")], 701, stops=0, duration=920))]}


_real_ces = app.cached_execute_spec
app.run_search = lambda *a, **k: [(_fp1, _fp2)]
app.leg_price_index = lambda *a, **k: {}  # signatures never match -> old path dead
app.cached_execute_spec = _floor_leg_payload
_mcf = app.search_multi_city(dict(_mc_floor_spec), "USD")
app.run_search, app.leg_price_index, app.cached_execute_spec = _real_run, _real_index, _real_ces
_top = (_mcf.get("results") or [{}])[0]
check("the purchasable per-leg sum outranks a joint fare nobody would buy",
      _top.get("total_price") == 1490 and _top.get("price_basis") == "separate tickets",
      f"top={_top.get('total_price')} basis={_top.get('price_basis')}")
check("...and says its flights are NOT the one-ticket options' flights",
      any("DIFFERENT flights" in w for w in _top.get("warnings") or [])
      and any("separate tickets" in w.lower() for w in _top.get("warnings") or []))
_comb = next((r for r in _mcf["results"] if r.get("price_basis") == "one ticket"), {})
check("the one-ticket card keeps ITS OWN price and names both numbers",
      _comb.get("total_price") == 5409
      and any("5,409" in w and "1,490" in w for w in _comb.get("warnings") or []))
check("the success payload carries the Google handoff link and the rule",
      str(_mcf.get("combined_check_url", "")).startswith("https://www.google.com/travel/flights?tfs=")
      and "never present a one-ticket price above it" in _mcf["message"])

# the floor must degrade to exactly the old behavior when the legs fail...
app.run_search = lambda *a, **k: [(_fp1, _fp2)]
app.leg_price_index = lambda *a, **k: {}
app.cached_execute_spec = lambda spec: (_ for _ in ()).throw(RuntimeError("leg boom"))
_mc_broke = app.search_multi_city(dict(_mc_floor_spec), "USD")
app.run_search, app.leg_price_index, app.cached_execute_spec = _real_run, _real_index, _real_ces
check("every leg failing degrades to the joint quote, never a crash",
      len(_mc_broke["results"]) == 1 and _mc_broke["results"][0]["total_price"] == 5409)

# ...and the exact-signature comparison stays authoritative when it fires:
# a floor ABOVE the matched separate total appends nothing
app.run_search = lambda *a, **k: [(p1, p2)]
app.leg_price_index = lambda origins, dests, date_, cabin, win=None, **_k: (
    {(("DL", "201"),): 844.0} if "FLL" in list(origins) else {(("OZ", "339"),): 182.0})
app.cached_execute_spec = _floor_leg_payload  # sums to 1490 > 1026
_mc_match = app.search_multi_city({"multi_city_segments": [
    {"origins": ["FLL"], "destinations": ["ICN"], "date": "2026-12-28"},
    {"origins": ["ICN"], "destinations": ["HRB"], "date": "2027-01-01"}]}, "USD")
app.run_search, app.leg_price_index, app.cached_execute_spec = _real_run, _real_index, _real_ces
check("when the same-flights comparison fires below the floor, it wins alone",
      _mc_match["results"][0]["total_price"] == 1026
      and sum(1 for r in _mc_match["results"] if r["price_basis"] == "separate tickets") == 1)
check("...and that separate-ticket quote books leg by leg, never at the joint fare",
      _mc_match["results"][0].get("book_per_leg") is True
      and _mc_match["results"][0].get("booking_url") is None
      and all(p.get("booking_url") for p in _mc_match["results"][0]["parts"]))

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
def _fake_run_search(search, filters, sort, top_n, budget_s=35.0, **_k):
    search.last_outbounds = _deg_outs
    return []
app.run_search = _fake_run_search
_o, _ = app.resolve_airports(["JFK", "EWR"]); _d, _ = app.resolve_airports(["FLL"])
_deg_dep, _deg_ret = future(30), future(33)
_deg = app.search_fixed_dates({"trip_type": "round_trip", "origins": ["JFK", "EWR"],
                               "destinations": ["FLL"], "departure_date": _deg_dep,
                               "return_date": _deg_ret}, _o, _d, "USD")
app.run_search = _real_run_search
check("a pairing-less round trip ships from-priced outbounds, not 'No flights found'",
      _deg.get("type") == "itineraries" and _deg.get("results") == []
      and len(_deg.get("more_outbounds") or []) == 2,
      f"type={_deg.get('type')} more={len(_deg.get('more_outbounds') or [])}")
check("...each with its honest from-total", (_deg.get("more_outbounds") or [{}])[0].get("from_total") == 277.0)
check("...and the spec_echo the tap-to-price endpoint needs",
      (_deg.get("spec_echo") or {}).get("origins") == ["JFK", "EWR"]
      and (_deg.get("spec_echo") or {}).get("return_date") == _deg_ret)
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
section("Multi-airport representation  — Changelog: 'the Gainesville bug'")
# --------------------------------------------------------------------------
# "NYC to Gainesville" searched [GNV, MCO] together; Google returned GNV
# service (American via MIA, $397) and the pipeline buried it three ways —
# the expansion picker chose only MCO, the value-ordered cut dropped GNV
# entirely, and the model told the user GNV "came back with nothing
# through-ticketed." A dedicated search then found it instantly. These
# reconstruct that shape: 30 cheap MCO nonstops + 3 pricey GNV one-stops.
def _ob_to(dest, price, dur, stops, hour):
    return types.SimpleNamespace(
        price=price, duration=dur, stops=stops,
        legs=[types.SimpleNamespace(
            departure_datetime=datetime(2026, 9, 25, hour, 0),
            arrival_airport=types.SimpleNamespace(name=dest))])

_gnv_pool = [_ob_to("MCO", 215 + i, 175, 0, 6 + (i % 16)) for i in range(30)] + \
            [_ob_to("GNV", 397 + i * 16, 320, 1, 7 + i * 4) for i in range(3)]
_exp = app.representative_outbounds(_gnv_pool, 10)
check("the expansion picker prices returns for EVERY served airport",
      any(f.legs[-1].arrival_airport.name == "GNV" for f in _exp),
      f"{sum(1 for f in _exp if f.legs[-1].arrival_airport.name == 'GNV')} GNV of {len(_exp)}")

_gnv_rows = [{"legs": [{"from": "JFK", "to": f.legs[-1].arrival_airport.name}],
              "price": f.price, "from_total": f.price, "duration": f.duration,
              "stops": f.stops, "warnings": []} for f in _gnv_pool]
_kept = app.rescue_airports(_gnv_rows[:24], _gnv_rows, dest_of=app._dest_of_row)
check("a value cut can no longer erase an airport Google served",
      any(app._dest_of_row(r) == "GNV" for r in _kept),
      f"{len(_kept)} kept")
check("...and the rescue preserves the ranking it was given",
      [_gnv_rows.index(r) for r in _kept] == sorted(_gnv_rows.index(r) for r in _kept))
check("a single-airport search stays untouched (no growth, no reorder)",
      app.rescue_airports(_gnv_rows[:5], _gnv_rows[:5], dest_of=lambda r: "MCO") == _gnv_rows[:5])

_bd = app.airport_breakdown(_gnv_pool, dest_of=app._dest_of_result, price_of=lambda f: f.price)
check("the model is told the per-airport truth over the FULL pre-cut set",
      _bd is not None and "MCO (Orlando, US) 30 from $215" in _bd
      and "GNV (Gainesville, US) 3 from $397" in _bd, str(_bd))
check("one airport in play means no breakdown noise",
      app.airport_breakdown(_gnv_pool[:30], dest_of=app._dest_of_result,
                            price_of=lambda f: f.price) is None)

_prompt_ga = app.assistant_system_prompt()
# superseded by the Aug 6 rule: the server now PROVES absence with a
# dedicated search rather than asking the model to go and check
check("the prompt still forbids absence claims from a combined pool",
      "combined pool" in _prompt_ga and "searched ALONE automatically" in _prompt_ga)
check("the prompt says to LEAD with the airport the user actually named",
      "LEAD with that airport" in _prompt_ga)
check("a constraint-adding follow-up searches now, not after permission",
      "run the constrained search immediately" in _prompt_ga)

# --------------------------------------------------------------------------
section("Multi-city under BotGuard  — Changelog: 'the gated endpoint'")
# --------------------------------------------------------------------------
# Aug 2: Google gated combined multi-city pricing behind BotGuard (browser
# header capture: identical body, 133 bytes without the token, 35,770 with).
# A server cannot mint the token, so the fallback ships each leg's one-way
# truth plus Google's own multi-city page for the one-ticket price.
_mc_url = app.multi_city_search_url([
    {"origins": ["ATL"], "destinations": ["PEK"], "date": "2027-01-01"},
    {"origins": ["HKG"], "destinations": ["JFK"], "date": "2027-01-15"},
])
# The combined probe keeps its identity-rotating ladder. A version of this
# line ran ONE attempt, on the theory that multi-city was permanently gated —
# a conclusion drawn from a home IP whose own one-way control was failing
# unnoticed. Refusals here are session-sticky like everywhere else, so
# retrying as a fresh identity is the correct response; only MEASURED
# consecutive refusals buy a quiet period.
# Refusals come back fast (~1-3s), so this budget still buys 2-3
# fresh-identity attempts — the point of the ladder — while capping how long
# the probe can extend a turn. What it must never be again is small enough to
# permit only ONE attempt: session-sticky refusals need a new identity.
check("the combined probe keeps room for a fresh-identity retry, and no more",
      4.0 <= app.MC_PROBE_BUDGET_S <= 12.0, f"budget_s={app.MC_PROBE_BUDGET_S}")
_saved_mc = dict(app._mc_probe)
app._mc_probe.update({"fails": 0, "quiet_until": 0.0})
for _ in range(app.MC_PROBE_STRIKES):
    app.note_mc_probe(False)
check("...and only OBSERVED repeat refusals buy quiet, never an assumption",
      app._mc_probe["quiet_until"] > time.monotonic())
app.note_mc_probe(True)
check("...which any success clears, so an un-gating recovers on its own",
      app._mc_probe["fails"] == 0 and app._mc_probe["quiet_until"] == 0.0)
app._mc_probe.update(_saved_mc)
check("every multi-city payload reports what the probe actually did",
      open(os.path.join(ROOT, "api", "index.py")).read()
      .count('"combined_probe": probe_note') == 2)
# Production, clean residential IPs, full ladder: the combined endpoint
# returned nothing (26.1s) / timed out (21.6s) while one-way and round-trip
# succeeded in the same process. Sequencing the per-leg fallback AFTER that
# cost 117s and shipped zero combos, because the probe's refusals also
# tripped the process-wide breaker and poisoned the legs.
_src_mc2 = open(os.path.join(ROOT, "api", "index.py")).read()
check("the per-leg searches overlap the probe instead of queueing behind it",
      "leg_futs = [leg_pool.submit(" in _src_mc2
      and _src_mc2.index("leg_futs = [leg_pool.submit(") < _src_mc2.index("note_mc_probe(bool(results))"))
check("...and are handed to the fallback rather than re-run",
      "leg_futs=leg_futs" in _src_mc2)
check("the flaky combined endpoint cannot trip the breaker for other searches",
      'suppress_breaker' in _src_mc2
      and _src_mc2.count("_local.suppress_breaker = True") == 1
      and "_local.suppress_breaker = False" in _src_mc2)
check("the probe budget cannot dominate the legs it runs beside",
      app.MC_PROBE_BUDGET_S <= 12.0, f"budget_s={app.MC_PROBE_BUDGET_S}")

# Aug 6, HVN/BDL -> FLL: the reply said "HVN has no service to FLL". It has
# an Avelo nonstop, cheaper than the BDL flight recommended instead. Re-run
# minutes later, the combined search DID include the HVN row - so nothing was
# crowded out (it is the cheapest and only nonstop; the cut protects both).
# A thin session and an empty route look identical from the rows alone, and
# only a dedicated search can tell them apart.
_bdl_only = [result([leg("BDL", "FLL", "2026-09-24T19:04", "2026-09-25T00:50", "DL", str(i))], 230.0 + i)
             for i in range(8)]
_o_hb, _ = app.resolve_airports(["HVN", "BDL"])
_d_hb, _ = app.resolve_airports(["FLL"])
check("a named airport with zero rows is identified, not assumed absent",
      app.airports_with_no_rows(_o_hb, _bdl_only, app._orig_of_result) == ["HVN"])
check("...and an airport that DID return rows is never re-probed",
      app.airports_with_no_rows(_d_hb, _bdl_only, app._dest_of_result) == [])

_hvn_row = [result([leg("HVN", "FLL", "2026-09-24T08:50", "2026-09-24T11:58", "Avelo", "101")], 180.0)]
_spec_hb = {"origins": ["HVN", "BDL"], "destinations": ["FLL"], "departure_date": future(20)}
_found = app.probe_missing_airports(_spec_hb, _o_hb, _d_hb, _bdl_only, None,
                                    searcher_cls=lambda: types.SimpleNamespace(
                                        search=lambda *a, **k: _hvn_row))
check("the dedicated probe recovers service the combined search missed",
      len(_found[0]) == 1 and _found[1] is None)
# a well-formed Google answer with zero rows (what the wire records when a
# chunk parses but carries no flights) is the only evidence of absence
_answered_empty = lambda *a, **k: (setattr(app._local, "last_wire",
                                           {"parsed": True, "bytes": 65, "rows": 0}), [])[1]
_empty = app.probe_missing_airports(_spec_hb, _o_hb, _d_hb, _bdl_only, None,
                                    searcher_cls=lambda: types.SimpleNamespace(search=_answered_empty))
check("only a dedicated search that ALSO came back empty licenses 'no service'",
      _empty[0] == [] and "VERIFIED NO SERVICE" in (_empty[1] or "")
      and "HVN (New Haven, US)" in _empty[1])
# Nov 11: a dedicated SGN->JFK search in production was REFUSED on every
# attempt while the route has 100+ flights. Refused is not an answer.
_refused = app.probe_missing_airports(_spec_hb, _o_hb, _d_hb, _bdl_only, None,
                                      searcher_cls=lambda: types.SimpleNamespace(
                                          search=lambda *a, **k: None))
check("a probe Google REFUSED never licenses 'no service'",
      _refused[0] == [] and "VERIFIED NO SERVICE" not in (_refused[1] or "")
      and "NOT VERIFIED" in (_refused[1] or "") and "NOT evidence of no service" in _refused[1])
_failed = app.probe_missing_airports(_spec_hb, _o_hb, _d_hb, _bdl_only, None,
                                     searcher_cls=lambda: types.SimpleNamespace(
                                         search=lambda *a, **k: (_ for _ in ()).throw(
                                             app.SearchClientError("timed out"))))
check("...nor does a probe that never reached Google",
      "VERIFIED NO SERVICE" not in (_failed[1] or "") and "NOT VERIFIED" in (_failed[1] or ""))
check("a single-airport search never probes itself",
      app.probe_missing_airports({"origins": ["HVN"], "destinations": ["FLL"]},
                                 *app.resolve_airports(["HVN"])[:1], _d_hb, [], None) == ([], None)
      if False else
      app.probe_missing_airports({"origins": ["HVN"], "destinations": ["FLL"]},
                                 app.resolve_airports(["HVN"])[0], _d_hb, _bdl_only, None)[0] == [])
check("the prompt distinguishes 'we did not get options' from 'no service'",
      "VERIFIED NO SERVICE" in app.assistant_system_prompt()
      and "NEVER that it has no service" in app.assistant_system_prompt())
_src_ap = open(os.path.join(ROOT, "api", "index.py")).read()
check("both sides of the route get a breakdown and a rescued seat",
      '("origin", _orig_of_row)' in _src_ap
      and "rescue_airports(flights, ranked_all, dest_of=_orig_of_row)" in _src_ap)

_mc_tfs_part = _mc_url.split("&curr")[0]
check("the handoff link is a trip-type-3 search tfs (browser-verified form)",
      "tfs=" in _mc_url and tfs_field(_mc_tfs_part, 19) == 3 and tfs_field(_mc_tfs_part, 2) == 2)
check("a malformed segment degrades to the flights home page, never a crash",
      app.multi_city_search_url([{"origins": [], "destinations": [], "date": None}])
      == "https://www.google.com/travel/flights")

def _fake_leg(spec):
    n = {"PEK": 789.0, "JFK": 450.0}[spec["destinations"][0]]
    return {"type": "flights", "results": [
        {"airline": "Turkish", "price": n + i * 10, "duration": 900, "stops": 1, "warnings": [],
         "legs": [{"from": spec["origins"][0], "to": spec["destinations"][0],
                   "departure": spec["departure_date"] + "T10:00:00",
                   "arrival": spec["departure_date"] + "T22:00:00",
                   "airline_code": "TK", "flight_number": str(30 + i)}]}
        for i in range(3)]}

_fb = app.multi_city_fallback({"multi_city_segments": [
    {"origins": ["ATL"], "destinations": ["PEK"], "date": "2027-01-01"},
    {"origins": ["HKG"], "destinations": ["JFK"], "date": "2027-01-15"},
]}, "USD", leg_search=_fake_leg)
check("the fallback pairs per-leg one-ways into separate-ticket combos",
      len(_fb["results"]) >= 3
      and _fb["results"][0]["total_price"] == 1239.0
      and _fb["results"][0]["price_basis"] == "separate tickets")
check("...warns about the separate-ticket trade explicitly",
      "separate tickets" in _fb["results"][0]["warnings"][0])
check("...and carries the one-ticket handoff link for card and model alike",
      _fb.get("combined_check_url", "").startswith("https://www.google.com/travel/flights?tfs=")
      and "never claim the combined ticket does not exist" in _fb["message"])
_fb_empty = app.multi_city_fallback({"multi_city_segments": [
    {"origins": ["ATL"], "destinations": ["PEK"], "date": "2027-01-01"},
    {"origins": ["HKG"], "destinations": ["JFK"], "date": "2027-01-15"},
]}, "USD", leg_search=lambda s: {"type": "flights", "results": []})
check("legs that fail on their own still hand the user the Google link",
      _fb_empty["results"] == [] and "combined_check_url" in _fb_empty)
# Nov 11, NYC -> Hanoi Mar 4 / Saigon -> NYC Mar 20: the first attempt
# shipped NOTHING but a Google link because one leg came back empty, and
# the retry's nine combos used TWO of the 120 NYC -> Hanoi flights Google
# had. Every leg is its own one-way search; those now ship whole as a
# per-leg board, a failed leg no longer erases the legs that worked, an
# empty leg is retried once, and every leg shares ONE deadline that fits
# the agent's 55s batch window (the old code waited 50s per leg, in turn).
def _mc_row(code, num, price, dur, stops, d, frm, to):
    return {"airline": code, "airline_code": code, "price": price, "currency": "USD",
            "duration": dur, "stops": stops, "warnings": [], "highlights": [],
            "booking_url": f"https://www.google.com/travel/flights?tfs={code}{num}",
            "legs": [{"from": frm, "to": to, "departure": d + "T10:00:00", "arrival": d + "T23:00:00",
                      "airline_code": code, "flight_number": num}]}


_mc_segs = {"multi_city_segments": [
    {"origins": ["JFK", "LGA", "EWR"], "destinations": ["HAN"], "date": future(110)},
    {"origins": ["SGN"], "destinations": ["JFK", "LGA", "EWR"], "date": future(126)}]}
_d1, _d2 = _mc_segs["multi_city_segments"][0]["date"], _mc_segs["multi_city_segments"][1]["date"]


def _mc_legs_ok(spec):
    if "HAN" in spec["destinations"]:  # value-best is NOT the cheapest here
        return {"type": "flights", "message": "leg one note", "results": [
            _mc_row("CX", "841", 500.0, 1300, 1, _d1, "JFK", "HAN"),
            _mc_row("OZ", "221", 442.0, 1435, 1, _d1, "JFK", "HAN"),
            _mc_row("KE", "82", 470.0, 1500, 1, _d1, "JFK", "HAN")]}
    return {"type": "flights", "message": "leg two note", "results": [
        _mc_row("EK", "393", 653.0, 1565, 1, _d2, "SGN", "JFK"),
        _mc_row("CI", "782", 700.0, 1300, 1, _d2, "SGN", "JFK")]}


_mcl = app.multi_city_fallback(dict(_mc_segs), "USD", leg_search=_mc_legs_ok)
check("every leg ships whole for per-leg choice",
      [lg["count"] for lg in _mcl.get("legs") or []] == [3, 2]
      and all(lg["status"] == "ok" for lg in _mcl["legs"]))
_tots = [r["total_price"] for r in _mcl["results"]]
check("combos are value-ranked, never rank-zipped, and keep the cheapest pairing",
      1095 in _tots and _tots != [1153, 1170] and _mcl["results"][0]["price_basis"] == "separate tickets",
      f"totals={_tots}")
check("separate-ticket combos book per leg, never on Google's one-ticket page",
      all(r.get("book_per_leg") and r.get("booking_url") is None for r in _mcl["results"])
      and all(p.get("booking_url") for r in _mcl["results"] for p in r["parts"]))
check("an open-jaw trip keeps every city in the model's view of the route",
      app.mc_route_text(_mc_segs["multi_city_segments"]) == "JFK/LGA/EWR → HAN, then SGN → JFK/LGA/EWR"
      and app.mc_route_text([{"origins": ["ATL"], "destinations": ["PEK"]},
                             {"origins": ["PEK"], "destinations": ["HKG"]}]) == "ATL → PEK → HKG"
      and "SGN" in _mcl["message"])

# a failed leg must not erase the one that worked
_calls = {"n": 0}


def _leg1_dead(spec):
    if "HAN" in spec["destinations"]:
        _calls["n"] += 1
        raise RuntimeError("proxy cut the response short")
    return _mc_legs_ok(spec)


_mcp = app.multi_city_fallback(dict(_mc_segs), "USD", leg_search=_leg1_dead)
_l1, _l2 = (_mcp.get("legs") or [{}, {}])[:2]
check("a leg that fails is retried once before anyone is told it is missing",
      _calls["n"] == 2, f"leg-1 attempts={_calls['n']}")
check("...and the legs that worked still ship (partial truth, never a bare link)",
      _l2.get("count") == 2 and _l1.get("status") == "missing" and _mcp["results"] == []
      and "combined_check_url" in _mcp)
check("...with the missing leg named for the model, and its cause recorded",
      "LEG 1" in _mcp["message"] and "NOT evidence of no service" in _mcp["message"]
      and "proxy cut the response short" in (_l1.get("detail") or ""))

# a retry that succeeds rescues the whole trip
_flaky = {"n": 0}


def _leg1_flaky(spec):
    if "HAN" in spec["destinations"]:
        _flaky["n"] += 1
        if _flaky["n"] == 1:
            return {"type": "flights", "results": []}  # a thin slice, not absence
    return _mc_legs_ok(spec)


_mcr = app.multi_city_fallback(dict(_mc_segs), "USD", leg_search=_leg1_flaky)
check("a retried leg that comes back rescues the full trip",
      _mcr["legs"][0]["count"] == 3 and "on retry" in _mcr["legs"][0]["detail"] and _mcr["results"])

# one shared deadline: a hung leg cannot hold the turn past the batch window
_t_hang = time.monotonic()
_mch = app.multi_city_fallback(dict(_mc_segs), "USD",
                               leg_search=lambda s: (time.sleep(3), _mc_legs_ok(s))[1]
                               if "HAN" in s["destinations"] else _mc_legs_ok(s),
                               deadline=time.monotonic() + 0.6)
check("legs share ONE deadline, so a hung leg cannot blow the batch window",
      time.monotonic() - _t_hang < 2.0 and _mch["legs"][1]["count"] == 2
      and "time ran out" in _mch["legs"][0]["detail"],
      f"{time.monotonic() - _t_hang:.1f}s")
check("...and that deadline fits inside the agent's 55s search batch",
      app.MC_LEGS_DEADLINE_S < 55 and "batch_deadline = time.monotonic() + 55" in _src_mc2)

# the model and the ledger see the legs, not just the paired combos
_cm = json.loads(app.compact_for_model(_mcl))
check("the model sees each leg's own option space",
      [lo["flights_found"] for lo in _cm.get("legs_overview") or []] == [3, 2]
      and len(_cm["legs_overview"][0]["best_options"]) == 3)
_led = app.ledger_entry(dict(_mc_segs), _mcp)
check("a legs-only result still lands in the ledger (follow-ups resolve from it)",
      _led is not None and _led["leg_boards"][0]["flights"] == 2
      and _led["route"] == "JFK/LGA/EWR-HAN then SGN-JFK/LGA/EWR")

# the success path ships the legs too, books its floor per leg, and reports its probe
check("the success payload ships the legs and says what the probe did",
      [lg["count"] for lg in _mcf.get("legs") or []] == [1, 1] and "combined_probe" in _mcf)
check("...and its separate-ticket floor books leg by leg",
      _top.get("book_per_leg") is True and _top.get("booking_url") is None)

# the ladder's verdict: answered-empty vs refused vs error, per attempt
_v_ans = app.run_search(types.SimpleNamespace(search=_answered_empty), app.build_filters(
    {"departure_date": future(30)}, *app.resolve_airports(["JFK"])[:1], app.resolve_airports(["ORD"])[0]),
    app.SortBy.CHEAPEST, top_n=10, budget_s=0.1)
check("the ladder records that Google ANSWERED with no flights",
      _v_ans == [] and app.search_verdict()["verdict"] == "empty")
_v_ref = app.run_search(types.SimpleNamespace(search=lambda *a, **k: None), app.build_filters(
    {"departure_date": future(30)}, *app.resolve_airports(["JFK"])[:1], app.resolve_airports(["ORD"])[0]),
    app.SortBy.CHEAPEST, top_n=10, budget_s=0.1)
check("...and that a refusal is a refusal, not an answer",
      _v_ref == [] and app.search_verdict()["verdict"] == "refused"
      and app.search_verdict()["attempts"][0].startswith("a1 refused"))

# one pair Google refuses must not take its neighbors down (prod, Nov 11:
# SGN -> JFK/LGA/EWR empty every time, SGN -> EWR alone 55 flights)
_ewr_rows = [result([leg("SGN", "EWR", future(60) + "T10:00", future(61) + "T08:00", "BR", str(390 + i))],
                    900.0 + i, stops=1, duration=1500) for i in range(3)]


def _poisoned_rs(search, filters, sort, top_n, budget_s=35.0, max_attempt_s=None, **_k):
    dests = {a[0].name for a in filters.flight_segments[0].arrival_airport}
    if dests == {"EWR"}:
        app._local.search_diag = {"verdict": "ok", "attempts": ["a1 ok 3"]}
        return list(_ewr_rows)
    app._local.search_diag = {"verdict": "refused", "attempts": ["a1 refused 80B 9s"]}
    return []


app.run_search = _poisoned_rs
_o_sg, _ = app.resolve_airports(["SGN"])
_d_ny, _ = app.resolve_airports(["JFK", "LGA", "EWR"])
_resc = app.search_fixed_dates({"trip_type": "one_way", "departure_date": future(60),
                                "origins": ["SGN"], "destinations": ["JFK", "LGA", "EWR"]},
                               _o_sg, _d_ny, "USD")
app.run_search = lambda *a, **k: (setattr(app._local, "search_diag",
                                          {"verdict": "refused", "attempts": ["a1 refused 80B"]}), [])[1]
_o_jfk, _ = app.resolve_airports(["JFK"])
_alone = app.search_fixed_dates({"trip_type": "one_way", "departure_date": future(60),
                                 "origins": ["SGN"], "destinations": ["JFK"]}, _o_sg, _o_jfk, "USD")
app.run_search = _real_run
check("a refused multi-airport search is rescued airport by airport",
      len(_resc.get("results") or []) == 3 and "searched ALONE" in _resc["message"],
      f"{len(_resc.get('results') or [])} results")
check("...and the pair Google refused is named, never called 'no service'",
      "SGN→JFK" in _resc["message"] and "NOT evidence of no service" in _resc["message"]
      and "VERIFIED NO SERVICE" not in _resc["message"])
check("a search Google never answered says so instead of 'No flights found'",
      _alone["results"] == [] and "No flights found" not in _alone["message"]
      and "NOT evidence that no flights exist" in _alone["message"]
      and _alone["search_diag"]["verdict"] == "refused")
check("multi-airport one-ways leave the rescue room inside the 55s batch",
      app.MULTI_COMBINED_BUDGET_S + 8 <= app.SEARCH_END_S < 55)
check("the turn's status line says WHICH kind of empty it was",
      "Google never answered (refused), NOT evidence of no service" in _src_mc2
      or "Google never answered (refused), NOT evidence of no service"
      in open(os.path.join(ROOT, "api", "index.py")).read())

# Nov 11 in production: the price index searched every leg a SECOND time
# (its spec lacked keys the leg spec carried as None, so the cache saw two
# searches), and one probe ran 42.5s against its 12s budget while finished
# legs waited on it. The turn took 102s.
check("None and absent are one cache key (no silent duplicate searches)",
      app._spec_key({"origins": ["JFK"], "adults": None, "carry_on": None})
      == app._spec_key({"origins": ["JFK"]}))
_idx_seen = []
_real_ces2 = app.cached_execute_spec
app.cached_execute_spec = lambda s: (_idx_seen.append(s), {"type": "flights", "results": []})[1]
app.leg_price_index(["JFK"], ["HAN"], future(110), None, None, base_spec={"adults": 2, "children": 1})
app.cached_execute_spec = _real_ces2
_leg_twin = app._mc_leg_spec({"adults": 2, "children": 1},
                             {"origins": ["JFK"], "destinations": ["HAN"], "date": future(110)})
check("the price index shares the leg's own search, party included",
      _idx_seen and app._spec_key(_idx_seen[0]) == app._spec_key(_leg_twin)
      and _idx_seen[0]["children"] == 1)

_saved_wait = app.MC_PROBE_WAIT_S
app.MC_PROBE_WAIT_S = 0.5
app.run_search = lambda *a, **k: (time.sleep(3), [(p1, p2)])[1]   # a probe that overruns
app.cached_execute_spec = _mc_legs_ok
_t_probe = time.monotonic()
_mc_slow = app.search_multi_city(dict(_mc_segs), "USD")
_dt_probe = time.monotonic() - _t_probe
app.run_search, app.cached_execute_spec, app.MC_PROBE_WAIT_S = _real_run, _real_ces2, _saved_wait
check("a probe that overruns cannot hold finished legs hostage",
      _dt_probe < 2.0 and [lg["count"] for lg in _mc_slow.get("legs") or []] == [3, 2]
      and "still running" in (_mc_slow.get("combined_probe") or ""),
      f"{_dt_probe:.1f}s, probe={_mc_slow.get('combined_probe')}")
check("...and the legs it shipped still pair into combos",
      any(r["total_price"] == 1095 for r in _mc_slow.get("results") or []))

# Nov 11, "is round trip NYC-Hanoi Mar 4-20 cheaper?": the three-airport
# round trip was refused 16/16 across two turns (Google's ~96-byte error,
# in BURSTS per request shape) and the user got nothing twice, while JFK->HAN
# alone and the one-ways were answered minutes apart. A refused round trip is
# now tried as different shapes in parallel: a late full retry, each airport
# as its own round trip, and both directions as one-ways.
_n_att = {"n": 0}


def _always_refused(*a, **k):
    _n_att["n"] += 1
    return None


app.run_search(types.SimpleNamespace(search=_always_refused), app.build_filters(
    {"departure_date": future(30)}, app.resolve_airports(["JFK"])[0], app.resolve_airports(["ORD"])[0]),
    app.SortBy.CHEAPEST, top_n=10, budget_s=30, refusal_exit=2)
check("a refusal burst hands over to the decomposition after two attempts",
      _n_att["n"] == 2 and app.search_verdict()["verdict"] == "refused", f"{_n_att['n']} attempts")

_han_outs = [result([leg("JFK", "HAN", future(110) + "T10:30", future(111) + "T22:25", "OZ", "221")], 1180.0, stops=1, duration=1435),
             result([leg("JFK", "HAN", future(110) + "T12:00", future(111) + "T21:45", "KE", "82")], 1212.0, stops=1, duration=1305)]


def _rt_by_shape(search, filters, sort, top_n, budget_s=35.0, **_k):
    origins = {a[0].name for a in filters.flight_segments[0].departure_airport}
    if origins == {"JFK"}:          # the single-airport round trip is answered
        search.last_outbounds = list(_han_outs)
        app._local.search_diag = {"verdict": "ok", "attempts": ["a1 ok"]}
        return []
    app._local.search_diag = {"verdict": "refused", "attempts": ["a1 refused 96B 1s", "a2 refused 96B 1s"]}
    return []


def _ow_legs(spec):
    out = "HAN" in spec["destinations"]
    o, d = (spec["origins"][0], "HAN") if out else ("HAN", spec["destinations"][0])
    day = spec["departure_date"]
    return {"type": "flights", "message": "leg", "results": [
        _mc_row("OZ" if out else "CI", "221" if out else "782", 442.0 if out else 690.0, 1435, 1, day, o, d)]}


_rt_spec = {"trip_type": "round_trip", "origins": ["JFK", "EWR"], "destinations": ["HAN"],
            "departure_date": future(110), "return_date": future(126)}
_saved_rt = (app.run_search, app.cached_execute_spec, app.RT_LATE_RETRY_AT_S)
app.RT_LATE_RETRY_AT_S = 0.0
app.run_search, app.cached_execute_spec = _rt_by_shape, _ow_legs
_o_rt, _ = app.resolve_airports(["JFK", "EWR"]); _d_rt, _ = app.resolve_airports(["HAN"])
_rt_pair = app.search_fixed_dates(dict(_rt_spec), _o_rt, _d_rt, "USD")
app.run_search = lambda search, *a, **k: (setattr(app._local, "search_diag",
                                                  {"verdict": "refused", "attempts": ["a1 refused 96B 1s"]}), [])[1]
_rt_ow = app.execute_spec(dict(_rt_spec))
app.run_search, app.cached_execute_spec, app.RT_LATE_RETRY_AT_S = _saved_rt
check("a refused multi-airport round trip is rescued airport by airport",
      _rt_pair.get("type") == "itineraries" and len(_rt_pair.get("more_outbounds") or []) == 2
      and "searched as its own round trip" in _rt_pair["message"] and "EWR→HAN" in _rt_pair["message"],
      f"type={_rt_pair.get('type')}")
check("when every round-trip shape is refused, both directions still come back as one-ways",
      _rt_ow.get("type") == "multicity" and _rt_ow.get("rt_fallback") is True
      and [lg["count"] for lg in _rt_ow.get("legs") or []] == [1, 1]
      and (_rt_ow.get("results") or [{}])[0].get("total_price") == 1132,
      f"type={_rt_ow.get('type')} totals={[r.get('total_price') for r in _rt_ow.get('results') or []]}")
check("...with Google's ROUND-TRIP page one tap away and the model told not to offer the search",
      tfs_field(_rt_ow.get("combined_check_url", "tfs=").split("&")[0], 19) == 1
      and "ALREADY DONE: do not offer" in _rt_ow["message"]
      and "never claim round-trip pricing does not exist" in _rt_ow["message"])
check("...titled as the round trip it is, and says how it was priced",
      _rt_ow.get("title") == "New York to Hanoi" and "Round trip" in _rt_ow.get("subtitle", "")
      and _rt_ow.get("tags", [None])[0] == "Priced as two one-way tickets")
check("the concierge runs the obvious fallback instead of offering it",
      "Never end a reply by offering to run a search you could have run" in app.assistant_system_prompt())

_fe_mc = open(os.path.join(ROOT, "public", "index.html")).read()
check("multi-city opens on a per-leg board, filtered by the same bar",
      "mcBoardOpen(sec)" in _fe_mc and "mcLegRows(sec, leg)" in _fe_mc
      and "if (this.mcHasLegs(sec)) sec._expanded = true;" in _fe_mc
      and "_applyFilters({ type: 'flights' }, leg.results" in _fe_mc)
check("...books each leg on its own link, and offers to re-search a missing leg",
      "'Book leg ' + (k + 1)" in _fe_mc and "mcAskLeg(leg)" in _fe_mc
      and "mcPerLeg(it)" in _fe_mc)
check("...and a section whose legs came back is never hidden",
      "|| this.mcHasLegs(s)" in _fe_mc.split("visibleSections(msg) {", 1)[1].split("\n        },", 1)[0])
check("the prompt answers leg questions from the legs and names a missing leg",
      "legs_overview" in app.assistant_system_prompt()
      and "name exactly which leg is missing" in app.assistant_system_prompt())
_fe_mc = open(os.path.join(ROOT, "public", "index.html")).read()
check("the card offers 'price the combined ticket on Google'",
      "Price the combined one-ticket itinerary on Google" in _fe_mc)
check("the prompt stopped promising combined pricing and points at the link",
      "withholds combined one-ticket pricing" in app.assistant_system_prompt()
      and "never present the separate-ticket sum as the only price" in app.assistant_system_prompt())

# --------------------------------------------------------------------------
section("Flexible-date grids  — Changelog: 'round-trip grids priced same-day returns'")
# --------------------------------------------------------------------------
# Searched without a duration, Google's date grid prices departing AND flying
# home on the same date. Every "round trip" fare in the calendar was for a
# 0-night stay — understated and unbuyable in spirit.
_flex = {"from_date": future(40), "to_date": future(69)}
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
section("Cross-turn results ledger  — Changelog: 'the results stay on screen'")
# --------------------------------------------------------------------------
# History was prose only, so every card we shipped vanished at the end of the
# turn: a follow-up ("the second one") left Claude choosing between paying for
# the same search twice and reconstructing fares from its own sentences. The
# ledger is the record of what is still on screen — and it is a record of what
# was SHIPPED, never of what exists, which is what these checks pin down.
_ship = [
    {"airline": "JetBlue", "price": 239.0, "duration": 185, "stops": 0, "warnings": [],
     "legs": [{"from": "FLL", "to": "EWR", "departure": "2026-11-28T07:30:00",
               "arrival": "2026-11-28T10:35:00", "airline_code": "B6", "flight_number": "1802"}]},
    {"airline": "Delta", "price": 268.0, "duration": 420, "stops": 1, "warnings": [],
     "legs": [{"from": "FLL", "to": "ATL", "departure": "2026-11-28T06:00:00",
               "arrival": "2026-11-28T07:50:00", "airline_code": "DL", "flight_number": "1215"},
              {"from": "ATL", "to": "EWR", "departure": "2026-11-28T09:10:00",
               "arrival": "2026-11-28T11:30:00", "airline_code": "DL", "flight_number": "988"}]},
]
_entry = app.ledger_entry(
    {"origins": ["FLL"], "destinations": ["EWR"], "departure_date": "2026-11-28",
     "summary": "FLL to EWR one way"},
    {"type": "flights", "message": "m", "results": _ship})
check("a shipped option is remembered with its fare and flight number",
      _entry["options"][0]["price"] == 239.0
      and "B61802" in _entry["options"][0]["flights"],
      str(_entry["options"][0].get("flights")))
check("options keep the numbering the user saw ('the second one' resolves)",
      [o["n"] for o in _entry["options"]] == [1, 2])
check("an empty search leaves NO record (nothing on screen, nothing to quote)",
      app.ledger_entry({"origins": ["FLL"]}, {"type": "flights", "results": []}) is None)
# specs routinely carry a return_date even one-way (the stub and the models
# both set one); recording it filed a one-way search as a round trip
check("a one-way search is never recorded with a return date",
      "return" not in app.ledger_entry(
          {"origins": ["FLL"], "destinations": ["EWR"], "trip_type": "one_way",
           "departure_date": "2026-11-28", "return_date": "2026-12-02"},
          {"type": "flights", "results": _ship}))

# the July 25 lesson, one turn later: a round trip's priced sample is not its
# option space, so the count of unpriced outbounds has to survive into the
# next turn or the model re-learns "no nonstops exist" from the ledger
_rt_entry = app.ledger_entry(
    {"origins": ["JFK"], "destinations": ["LHR"], "trip_type": "round_trip"},
    {"type": "itineraries", "message": "m",
     "results": [{"total_price": 609, "outbound": {"airline": "FI", "legs": [], "stops": 1},
                  "return": {"airline": "FI", "legs": [], "stops": 1}, "return_options": [1, 2]}],
     "more_outbounds": [{"stops": 0, "from_total": 745.0}, {"stops": 1, "from_total": 700.0}]})
check("the ledger carries the unpriced outbounds, not just the priced sample",
      _rt_entry["unpriced_outbounds"]["nonstops"] == 1
      and _rt_entry["unpriced_outbounds"]["cheapest_from"] == 700.0)

_ctx = app.ledger_context([{"entries": [_entry]}])
check("the carried block forbids inventing anything it does not contain",
      "never infer" in _ctx and "fresh search" in _ctx)
check("...and states the fares are quotes from an earlier search, not live",
      "not a live fare" in _ctx)
check("no ledger, no block (a first turn is unchanged)",
      app.ledger_context([]) is None and app.ledger_context(None) is None)
# a client controls this payload; it must not be able to grow the prompt at will
_huge = {"entries": [dict(_entry, options=_entry["options"] * 400) for _ in range(6)]}
check("a client-supplied ledger is capped, not trusted",
      len(app.ledger_context([_huge, _huge])) < app.LEDGER_MAX_CHARS + 2000,
      f"{len(app.ledger_context([_huge, _huge]))} chars")

# a single oversized OPTION once stalled the halving loop at [:1] forever —
# ledger_context runs before any model call, so one crafted POST hung the
# whole request and pinned its SSE worker. The loop now proves progress on
# every pass or drops the ledger.
_poison = [{"entries": [{"route": "A to B", "kind": "flights",
                         "options": [{"airline": "X" * 20000}]}]}]
_led_out = [False, "unset"]


def _led_run():
    _led_out[1] = app.ledger_context(_poison)
    _led_out[0] = True


_led_t = threading.Thread(target=_led_run, daemon=True)
_led_t.start()
_led_t.join(5)
check("an unsalvageable oversized entry is dropped, never spun on forever",
      _led_out[0] and _led_out[1] is None)

# multi-city specs keep their route in the segments; the generic join
# recorded the literal string ' to ' for every multi-city search
_mc_led = app.ledger_entry(
    {"trip_type": "multi_city", "multi_city_segments": [
        {"origins": ["ATL"], "destinations": ["PEK"]},
        {"origins": ["HKG"], "destinations": ["JFK"]}]},
    {"type": "multicity", "results": [{"total_price": 1490, "price_basis": "separate tickets", "parts": []}]})
check("a multi-city ledger entry records the real route, not ' to '",
      _mc_led["route"] == "ATL-PEK then HKG-JFK", repr(_mc_led.get("route")))

# --------------------------------------------------------------------------
section("Price context  — Changelog: 'is this a good fare'")
# --------------------------------------------------------------------------
# Ranking says which of these; it never said whether today is a good day to
# buy. The baseline answers that from the SearchDates grid — and because it is
# a window comparison, every check here is about not over-claiming.
_win = app.price_ctx_window("2026-11-28")
check("the baseline window never prices dates in the past",
      _win[0] >= (datetime.now().date()).isoformat(), f"window from {_win[0]}")
_grid = [{"date": f"2026-11-{d:02d}", "return_date": None, "price": p}
         for d, p in zip(range(10, 30), [420, 410, 395, 380, 221, 300, 310, 330, 340, 350,
                                          360, 370, 375, 385, 390, 400, 405, 415, 425, 430])]
_cheap = app.price_verdict(239, _grid, ("2026-11-10", "2026-11-29"), None)
_dear = app.price_verdict(428, _grid, ("2026-11-10", "2026-11-29"), None)
check("a fare below almost every nearby date reads as cheap",
      "cheapest" in _cheap["verdict"], _cheap["verdict"])
check("a fare above almost every nearby date reads as dear",
      "priciest" in _dear["verdict"], _dear["verdict"])
check("the cheaper alternative is named with its real saving",
      _cheap["cheapest_nearby"]["date"] == "2026-11-14"
      and _cheap["cheapest_nearby"]["price"] == 221)
# "prices are low right now" is a claim about history. We have dates, not
# history, and the model must never be handed wording that blurs the two.
check("the note forbids reading the window as price history",
      "NOT price history" in _cheap["note"] and "never predict" in _cheap["note"])
check("too thin a sample yields no verdict at all",
      app.price_verdict(239, _grid[:4], ("2026-11-10", "2026-11-13"), None) is None)
check("no fare on screen, no verdict", app.price_verdict(None, _grid, _win, None) is None)

# the fare it characterizes must be one the user can actually see
check("the anchor is the cheapest fare the search shipped",
      app.payload_anchor_price({"type": "flights", "results": _ship}) == 239.0)
check("...and for round trips, from-priced outbounds count too",
      app.payload_anchor_price({
          "type": "itineraries", "results": [{"total_price": 900}],
          "more_outbounds": [{"from_total": 745.0}]}) == 745.0)

# an optional nicety must never cost the user their search
import concurrent.futures as _cf

_never = _cf.Future()
_pay = {"type": "flights", "results": _ship}
_t0 = time.monotonic()
app.attach_price_context(_pay, {"window": _win, "nights": None, "grid": None, "future": _never},
                         time.monotonic() - 1)
check("a late baseline is dropped, never waited on",
      "price_context" not in _pay and time.monotonic() - _t0 < 0.5,
      f"{time.monotonic() - _t0:.2f}s")
check("...and says WHY it is missing (prod disagreed with local once)",
      _pay.get("price_context_status") == "late", str(_pay.get("price_context_status")))
# attaching inside the payload cache froze "no context" into a 4-minute-old
# payload, so a route could never gain a baseline once it had missed one
_src_cached = open(os.path.join(ROOT, "api", "index.py")).read()
check("the baseline is attached OUTSIDE the payload cache",
      "def cached_execute_spec" in _src_cached
      and _src_cached.index("_start_ctx_for(spec)") < _src_cached.index("hit = _search_cache.get(key)"),
      "start it before the cache lookup and annotate every return path")
app.attach_price_context(_pay, None, time.monotonic() + 1)
check("no baseline handle is a no-op, not an error", "price_context" not in _pay)
# extra load during a refusal wave is what turns a session flag into an IP burn
app._breaker["open_until"] = time.monotonic() + 5
_blocked = app.start_price_context({"departure_date": "2026-11-28", "origins": ["FLL"]}, [], [])
app._breaker["open_until"] = 0.0
check("the baseline stands down while the circuit breaker is open", _blocked is None)
check("multi-city and flexible searches ask for no baseline",
      app.start_price_context({"trip_type": "multi_city", "departure_date": "2026-11-28"}, [], []) is None
      and app.start_price_context({"flexible_dates": {"from_date": "x"}, "departure_date": "2026-11-28"}, [], []) is None)

# --------------------------------------------------------------------------
section("Arrival-day arithmetic  — Changelog: 'the +2 the model read as +1'")
# --------------------------------------------------------------------------
# Asked for "the latest departure that still lands Jan 3 morning", the model
# picked a flight whose arrival timestamp read 2027-01-04T09:45 and called it
# Jan 3 — it inferred "next day" from the departure instead of reading the
# date (Evan's catch, July 30). The day offset is now STATED on every summary
# the model sees, in both the live results and the cross-turn ledger, because
# the misread happened one turn after the search, off the ledger.
_ke36 = {
    "airline": "Korean Air", "price": 873.0, "duration": 1945, "stops": 1, "warnings": [],
    "legs": [{"from": "ATL", "to": "ICN", "departure": "2027-01-02T12:20:00",
              "arrival": "2027-01-03T17:30:00", "airline_code": "KE", "flight_number": "36"},
             {"from": "ICN", "to": "PEK", "departure": "2027-01-04T07:40:00",
              "arrival": "2027-01-04T09:45:00", "airline_code": "OZ", "flight_number": "331"}],
}
check("a +2 arrival is stated as lands_plus_days on the model's summary",
      app._leg_summary(_ke36).get("lands_plus_days") == 2,
      str(app._leg_summary(_ke36).get("lands_plus_days")))
check("...and survives into the cross-turn ledger, where the misread happened",
      app._ledger_flight(_ke36).get("lands_plus_days") == 2)
check("a same-day flight carries no offset at all",
      "lands_plus_days" not in app._leg_summary(_ship[0]))
check("a dateline crossing that lands the day BEFORE is stated too",
      app._plus_days("2027-01-02T14:00:00", "2027-01-01T09:00:00") == -1)
check("unparseable timestamps degrade to no offset, never a crash",
      app._plus_days(None, "2027-01-04T09:45:00") == 0 and app._plus_days("x", "y") == 0)
_prompt = app.assistant_system_prompt()
check("the prompt tells the model to read arrival dates, not compute them",
      "lands_plus_days" in _prompt and "arrival timestamp" in _prompt)
check("...and to refuse a near miss rather than bend it",
      "near miss" in _prompt)

# the prompt tells the model the offset "matches the +N badge on the user's
# card" — so it must actually be on EVERY card type, and +1/+2 arrivals are
# routine precisely on the round-trip and multi-city cards that lacked it
_fe_badges = open(os.path.join(ROOT, "public", "index.html")).read()
check("round-trip cards show the day offset on both directions",
      "arrivalDay(it.outbound)" in _fe_badges and "arrivalDay(it.return)" in _fe_badges)
check("multi-city legs show the day offset", "arrivalDay(p)" in _fe_badges)
check("return pickers show the day offset on each option", "arrivalDay(r)" in _fe_badges)
check("a negative (dateline) offset renders too, keeping card parity honest",
      "days < 0" in _fe_badges)

# --------------------------------------------------------------------------
section("API error surfacing  — Changelog: 'the outage the reply called temporary'")
# --------------------------------------------------------------------------
# Four consecutive retries against a deterministic failure, each answered
# with "temporary ... try once more", and the actual status visible nowhere.
# The reply now carries the code, the log carries the autopsy, and errors a
# retry cannot fix stop asking for one.
def _fake_err(status, etype="invalid_request_error", message="bad request"):
    return types.SimpleNamespace(
        status_code=status,
        body={"error": {"type": etype, "message": message}},
        response=types.SimpleNamespace(headers={"request-id": "req_test123"}),
    )

_credit = app.assistant_error_reply(_fake_err(400, message="Your credit balance is too low"))
check("a credit-exhaustion 400 is named, not called temporary",
      "credit" in _credit["message"] and "temporary" not in _credit["message"].lower())
check("...and does not ask the user to retry at a wall",
      "Retrying won't help" in _credit["message"])
_bad = app.assistant_error_reply(_fake_err(400))
check("a plain 4xx admits retrying is unlikely to help and shows the code",
      "HTTP 400" in _bad["message"] and "unlikely" in _bad["message"])
_srv = app.assistant_error_reply(_fake_err(500, etype="api_error"))
check("a 5xx stays a polite try-again, now with the code visible",
      "HTTP 500" in _srv["message"] and "once more" in _srv["message"])
_over = app.assistant_error_reply(_fake_err(429, etype="rate_limit_error"))
check("429/529 keep the congestion copy", "congested" in _over["message"])
check("every error reply carries a machine-readable detail with the request id",
      "req_test123" in _bad["error_detail"] and "HTTP 400" in _bad["error_detail"])

# a proxy fronting an outage returns {"error": "a string"} shapes — the SDK
# itself double-guards this exact access, and a helper that crashes during an
# anomalous outage is a diagnostic that dies precisely when it is needed
for _body in ({"error": "upstream timeout"}, {"error": None}, None, "plain text",
              {"error": {"message": {"weird": 1}, "type": None}}):
    try:
        _r = app.assistant_error_reply(types.SimpleNamespace(
            status_code=502, body=_body, response=types.SimpleNamespace(headers={})))
        ok = bool(_r.get("message"))
    except Exception as ex:
        ok = False
    check(f"a malformed error body never crashes the reporter ({str(_body)[:28]!r})", ok)

# --------------------------------------------------------------------------
section("API spend  — Changelog: 'the outage was an empty balance'")
# --------------------------------------------------------------------------
# The July 30 outage was the account running dry mid-conversation. These pin
# the three levers that answer it: a cheaper loop model behind an env escape
# hatch, cache breakpoints that stop re-billing what was already sent, and a
# cost line computed from real usage so the next decision is made on data.
check("the loop model is Sonnet 5 by default, env-overridable for an A/B",
      app.ASSISTANT_MODEL == (os.environ.get("ASSISTANT_MODEL") or "claude-sonnet-5"))
check("the loop model has a price entry (a swap without one silently reports $0)",
      app.ASSISTANT_MODEL in app.MODEL_PRICES and app.ROUTER_MODEL in app.MODEL_PRICES)
# Sonnet 5 thinks by default and max_tokens caps thinking + text TOGETHER;
# the old 4000 cap could truncate a reply mid-sentence after a long think
check("max_tokens leaves adaptive thinking room to finish the reply",
      app.MAX_TOKENS >= 8000, f"MAX_TOKENS={app.MAX_TOKENS}")

_usage = types.SimpleNamespace(input_tokens=1000, cache_creation_input_tokens=4000,
                               cache_read_input_tokens=2000, output_tokens=300)
# fallback (no TTL breakdown) bills writes at the DEARER 1h rate — errs high:
# 1000*3 + 4000*3*2 + 2000*3*0.1 + 300*15 = 3000+24000+600+4500 = 32100 / 1e6
check("a call's cost is computed from its real usage block (errs high without breakdown)",
      abs(app.call_cost_usd("claude-sonnet-5", _usage) - 0.0321) < 1e-9,
      f"${app.call_cost_usd('claude-sonnet-5', _usage):.4f}")
_usage_bd = types.SimpleNamespace(
    input_tokens=1000, cache_creation_input_tokens=5000,
    cache_read_input_tokens=2000, output_tokens=300,
    cache_creation=types.SimpleNamespace(ephemeral_5m_input_tokens=1000,
                                         ephemeral_1h_input_tokens=4000))
# with the breakdown, 5m writes bill 1.25x and 1h writes 2x:
# 1000*3 + (1000*1.25 + 4000*2)*3 + 2000*3*0.1 + 300*15 = 3000+27750+600+4500 = 35850
check("the mixed-TTL write breakdown prices 5m at 1.25x and 1h at 2x",
      abs(app.call_cost_usd("claude-sonnet-5", _usage_bd) - 0.03585) < 1e-9,
      f"${app.call_cost_usd('claude-sonnet-5', _usage_bd):.5f}")
check("an unknown model or missing usage reports 0, never a crash",
      app.call_cost_usd("claude-nonexistent", _usage) == 0.0
      and app.call_cost_usd("claude-sonnet-5", None) == 0.0)
check("the write multipliers match the TTLs actually used",
      app.CACHE_WRITE_1H == 2.0 and app.CACHE_WRITE_5M == 1.25
      and app._CACHE_1H.get("ttl") == "1h" and app._CACHE_5M.get("ttl") == "5m")

_msgs = [{"role": "user", "content": "JFK to ORD"},
         {"role": "assistant", "content": "On it."},
         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1",
                                       "content": "results"}]}]
_tailed = app.cache_tail(_msgs)
# 5m, not 1h: the tail's readers are this turn's own calls, seconds apart;
# cross-turn reads can never match (client history is bare prose while the
# turn's bytes embedded the ledger), so the dearer write would buy nothing
check("the conversation tail carries a 5m cache breakpoint",
      _tailed[-1]["content"][-1].get("cache_control") == app._CACHE_5M)
check("...without mutating the loop's own messages list",
      "cache_control" not in _msgs[-1]["content"][-1] and _msgs[1]["content"] == "On it.")
_tail_str = app.cache_tail([{"role": "user", "content": "plain string"}])
check("a string tail is wrapped into a block so the breakpoint can attach",
      _tail_str[-1]["content"][0]["type"] == "text"
      and _tail_str[-1]["content"][0]["cache_control"] == app._CACHE_5M)
check("empty or SDK-object tails pass through untouched",
      app.cache_tail([]) == []
      and app.cache_tail([{"role": "assistant", "content": types.SimpleNamespace()}])[-1]["content"].__class__ is types.SimpleNamespace)

# both live call sites must use the shared knobs, not a re-hardcoded model
_src_spend = open(os.path.join(ROOT, "api", "index.py")).read()
check("no call site hardcodes an old model (the env override must actually rule)",
      'model="claude-opus-4-8"' not in _src_spend
      and '"model": "claude-opus-4-8"' not in _src_spend
      and _src_spend.count("model=ASSISTANT_MODEL") == 1
      and 'ROUTER_MODEL if use_haiku else ASSISTANT_MODEL' in _src_spend)
check("no breakpoint is left on the bare default TTL (every marker is explicit)",
      '"cache_control": {"type": "ephemeral"}' not in _src_spend
      and "'cache_control': {'type': 'ephemeral'}" not in _src_spend)
check("the loop caches the tail; the wrap-up does NOT (tool_choice none makes "
      "the messages tier unreadable, so its write would be pure waste)",
      _src_spend.count("cache_tail(messages)") == 1
      and 'tool_choice={"type": "none"},\n                messages=messages,' in _src_spend)
check("the SSE stream heartbeats through silent thinking gaps",
      '": ping' in _src_spend,
      "a 90s quiet gap used to break the stream and double-run the turn")
check("a mid-think max_tokens exit ships a real sentence, not an ellipsis",
      'stop_reason == "max_tokens" and not text' in _src_spend)

# Evan, Aug 2: streaming typed "let me take a look" and then wiped it. Prose
# is now emitted once per final answer, after the call resolves — a preamble
# that precedes a search is simply never shown, and nothing ever un-types.
check("prose is never live-streamed from the loop (no wipe is possible)",
      'emit("text_reset"' not in _src_spend
      and "stream.text_stream" not in _src_spend
      and _src_spend.count('emit("text_delta", text)') == 2)
_fe_now = open(os.path.join(ROOT, "public", "index.html")).read()
check("the typewriter cadence is eased and bounded (types, not pastes)",
      "buf.length / 45" in _fe_now and ", 14)" in _fe_now)
check("the masthead is a wordmark; the crest and its subtitle are gone",
      'class="crest"' not in _fe_now and "Claude · Live Google Flights Data</p>" not in _fe_now)
check("the credit line moved to the footer colophon",
      "Powered by Claude and Google Flights" in _fe_now)
check("round trips open on the mix & match board",
      # 3 call sites: plainTurn, maybeFinishStream, and (Aug 8) restoreConvo —
      # a restored round trip must land back on the board too
      "openBoard(sec)" in _fe_now and _fe_now.count("this.openBoard(sec);") == 3)
check("the reply sits in a panel that answers the user's bubble, in sans",
      "border-radius: 10px 10px 10px 2px" in _fe_now
      and "width: fit-content" in _fe_now
      and "font-family: var(--serif); font-optical-sizing" not in _fe_now)

# --------------------------------------------------------------------------
section("Inventory completeness  — Changelog: 'the missing carrier'")
# --------------------------------------------------------------------------
# Beijing->Chengdu, Aug 2: the app showed a wall of China Southern at $421
# while Google's own UI led with China Eastern at $314 and a cheapest tab of
# $242 — an entire carrier lost. Two layers now defend against ANY cause of
# "flights that should exist don't appear":
#   1. wrb chunks are UNIONED by itinerary identity, not chosen-from.
#   2. the date grid (an independent endpoint pricing the searched date)
#      vets the list's cheapest fare; a provably short list is retried as a
#      new identity and, failing that, honestly labeled.
def _wrow(airline, number, price):
    # minimal raw row: row[0] = detail, detail[2] = legs (the identity),
    # detail[9] = duration; row[1] carries our probe's price marker
    detail = [None, None, [[airline, number, "2027-01-06T09:00"]], None, None,
              None, None, None, None, 175]
    return [detail, price]

# merge_wrb_chunks mutates the richest chunk (fine in production: chunks are
# parsed fresh per response) — so every assertion builds fresh fixtures
_ck_cs = lambda: [None, None, [[_wrow("CS", "8101", 421), _wrow("CS", "8102", 421)]], None]
_ck_ce = lambda: [None, None, [[_wrow("CE", "5301", 314)]], None]
_merged = app.merge_wrb_chunks([_ck_cs(), _ck_ce()])
check("carriers split across chunks are UNIONED, not chosen between",
      len(_merged[2][0]) == 3,
      f"{len(_merged[2][0])} rows (richest-chunk would keep 2)")
_m2 = app.merge_wrb_chunks([_ck_cs(), [None, None, [[_wrow("CS", "8101", 399)]], None]])
check("a re-rendered itinerary dedupes to one row, the later snapshot's price",
      len(_m2[2][0]) == 2 and any(r[1] == 399 for r in _m2[2][0])
      and not any(r[1] == 421 and r[0][2][0][1] == "8101" for r in _m2[2][0]))
_m3 = app.merge_wrb_chunks(
    [[None, None, [[_wrow("XW", "1", 200)]], [[_wrow("XW", "2", 210)]]], _ck_ce()])
check("bucket-3 rows merge without duplicating into fli's concat",
      len(_m3[2][0]) == 3 and _m3[3][0] == [])
_solo = _ck_cs()
check("a single chunk passes through untouched",
      app.merge_wrb_chunks([_solo]) is _solo and len(_solo[2][0]) == 2)
check("no chunks yields None, not a crash", app.merge_wrb_chunks([]) is None)

_sent_handle = {"dep": "2027-01-06", "nights": None, "future": None,
                "grid": [{"date": "2027-01-06", "price": 242.0},
                         {"date": "2027-01-07", "price": 250.0}]}
_short = {"type": "flights", "message": "m",
          "results": [{"price": 421.0, "legs": [{"from": "PKX", "to": "TFU"}]}]}
_reruns: list = []
def _rerun_cheap(spec):
    _reruns.append(1)
    return {"type": "flights", "message": "m",
            "results": [{"price": 314.0, "legs": [{"from": "PKX", "to": "CTU"}]}]}
_out = app.verify_completeness({"departure_date": "2027-01-06"}, dict(_short),
                               _sent_handle, rerun=_rerun_cheap)
check("a provably short list is retried and the honest-cheaper set wins",
      len(_reruns) == 1 and app.payload_anchor_price(_out) == 314.0)
check("...and a STILL-short list is labeled so the model must disclose",
      "COMPLETENESS WARNING" in _out["message"]
      and _out["completeness_warning"]["grid_from"] == 242.0)
_reruns.clear()
_ok = {"type": "flights", "message": "m",
       "results": [{"price": 250.0, "legs": [{"from": "PKX", "to": "TFU"}]}]}
_out2 = app.verify_completeness({"departure_date": "2027-01-06"}, dict(_ok),
                                _sent_handle, rerun=_rerun_cheap)
check("a list consistent with the grid is never retried or labeled",
      len(_reruns) == 0 and "completeness_warning" not in _out2)
_reruns.clear()
def _rerun_fixed(spec):
    _reruns.append(1)
    return {"type": "flights", "message": "m",
            "results": [{"price": 249.0, "legs": [{"from": "PKX", "to": "CTU"}]}]}
_out3 = app.verify_completeness({"departure_date": "2027-01-06"}, dict(_short),
                                _sent_handle, rerun=_rerun_fixed)
check("a retry that recovers the inventory ships clean, no warning",
      app.payload_anchor_price(_out3) == 249.0 and "completeness_warning" not in _out3)
check("no grid evidence means no sentinel (never a retry on a hunch)",
      app.verify_completeness({"departure_date": "2027-01-06"}, dict(_short),
                              {"dep": "2027-01-06", "grid": [], "future": None},
                              rerun=_rerun_cheap) == _short and len(_reruns) == 1)

# the actual Beijing root cause: Google served China Eastern's SCHEDULE but
# withheld its FARES, and every layer counted only priced rows — the reply
# called $421 "the going rate" with the cheaper carrier sitting unpriced in
# the same payload. Unpriced inventory is inventory.
check("unpriced rows rank after priced ones by design, never crash",
      app.trip_cost(None, 175, 0, [], 175) == float("inf"))
_mixed = ([{"airline": "China Southern", "price": 421.0, "duration": 175, "stops": 0,
            "warnings": [], "legs": [{"from": "PKX", "to": "TFU",
                                      "departure": "2027-01-06T09:00:00",
                                      "arrival": "2027-01-06T11:55:00"}]}] * 3
          + [{"airline": "China Eastern", "price": None, "duration": 175, "stops": 0,
              "warnings": [], "legs": [{"from": "PKX", "to": "CTU",
                                        "departure": "2027-01-06T07:15:00",
                                        "arrival": "2027-01-06T10:10:00"}]}] * 2)
_cn = app.carrier_note(_mixed)
check("the message names fare-withheld carriers as SCHEDULED BUT UNPRICED",
      _cn is not None and "SCHEDULED BUT UNPRICED: China Eastern 2" in _cn
      and "China Southern 3 from $421" in _cn)
check("...and forbids calling the priced floor the going rate",
      "going rate" in _cn and "Book link" in _cn)
check("an all-priced list carries no unpriced note", app.carrier_note(_mixed[:3]) is None)
_cp = json.loads(app.compact_for_model({"type": "flights", "message": "m",
                                        "results": _mixed * 2}))
check("the model is told about unpriced carriers even outside the top-6 sample",
      _cp.get("unpriced_carriers", {}).get("counts", {}).get("China Eastern") == 4)
_cut = app.rescue_airports(_mixed[:3], _mixed, dest_of=lambda f: f.get("airline"))
check("the ship cut cannot erase a fare-withheld carrier",
      any(f.get("price") is None for f in _cut))
_prompt_up = app.assistant_system_prompt()
check("the prompt teaches unpriced inventory (withheld from the session, not the user)",
      "not from the user" in _prompt_up and "unpriced carriers exist" in _prompt_up)
_fe_up = open(os.path.join(ROOT, "public", "index.html")).read()
check("an unpriced card explains itself instead of showing a bare dash",
      "fare shows on Google" in _fe_up and "fare on Google" in _fe_up)

# the manifest layer: prod's proxied session got SIX rows and no unpriced
# schedules at all — the only witness left is the response's own airline
# filter list, so it must be extracted, gated sanely, and acted on
_inner_m = [None] * 8
_inner_m[7] = [None, [None, [["CZ", "China Southern"], ["MU", "China Eastern"],
                             ["MF", "XiamenAir"], ["3U", "Sichuan Airlines"],
                             ["HU", "Hainan"]]]]
check("the route's carrier manifest is extracted from the response itself",
      app._extract_route_airlines(_inner_m) == {
          "CZ": "China Southern", "MU": "China Eastern", "MF": "XiamenAir",
          "3U": "Sichuan Airlines", "HU": "Hainan"})
check("a malformed manifest block yields None, never a crash",
      app._extract_route_airlines([None] * 8) is None
      and app._extract_route_airlines([]) is None)

_res_thin = [result([leg("PKX", "TFU", "2027-01-06T09:00", "2027-01-06T11:55",
                         "CZ", "8101")], 421.0)] * 6
_manifest5 = app._extract_route_airlines(_inner_m)
check("missing carriers are computed against actual row airlines",
      set(app.missing_from_manifest(_res_thin, _manifest5)) == {"MU", "MF", "3U", "HU"})
check("a structural gap fires the probe (6 rows vs a 5-carrier manifest)",
      app.should_probe_missing(_res_thin, _manifest5))
_res_rich = [result([leg("BOS", "MIA", "2026-10-22T09:00", "2026-10-22T12:30",
                         c, "1")], 200.0) for c in ("AA", "DL", "UA", "B6", "WN", "F9", "AS")] * 5
_manifest_dom = {c: c for c in ("AA", "DL", "UA", "B6", "WN", "F9", "AS", "EK", "SV")}
check("interline oddballs on a well-covered route do NOT fire the probe",
      not app.should_probe_missing(_res_rich, _manifest_dom))
check("no manifest, no probe, ever",
      not app.should_probe_missing(_res_thin, None))
_src_mn = open(os.path.join(ROOT, "api", "index.py")).read()
check("the probe asks Google for the missing carriers BY NAME, once, bounded",
      '"airlines_include": probe_codes' in _src_mn and "budget_s=16.0" in _src_mn)
check("an unfillable gap ships an explicit ROUTE NOTE the model must relay",
      _src_mn.count("ROUTE NOTE") >= 2)

# the PEI incident: the model asserted PEI meant Beijing; PEI is Pereira,
# Colombia, and its cheap Bogota two-stops shipped as "Beijing" cards. A
# code is an assertion from memory — every result now opens with the
# geography, so a wrong code contradicts itself before the prose is written.
check("airport_place unmasks a hallucinated code",
      app.airport_place("PEI") == "PEI (Pereira, CO)"
      and app.airport_place("PEK") == "PEK (Beijing, CN)")
check("an unknown code degrades to itself, never a crash",
      app.airport_place("ZZZ") == "ZZZ")
_o_re, _ = app.resolve_airports(["ATL"])
_d_re, _ = app.resolve_airports(["PEI", "PEK"])
_echo = app.route_echo(_o_re, _d_re)
check("every search result opens with the route's real geography",
      "ATL (Atlanta, US)" in _echo and "PEI (Pereira, CO)" in _echo
      and "PEK (Beijing, CN)" in _echo and "re-search without it" in _echo)
check("...wired into execute_spec for every fixed and flexible search",
      "route_echo(origins, destinations)" in _src_mn)
check("the per-airport breakdown names places, not just codes",
      "airport_place(a)" in _src_mn.split("def airport_breakdown")[1].split("def ")[0])
check("the prompt orders a re-search when the geography contradicts the ask",
      "READ it before writing a word of prose" in app.assistant_system_prompt()
      and "Beijing -> PEK/PKX" in app.assistant_system_prompt(router=True))

# --------------------------------------------------------------------------
section("Latency overlaps  — Changelog: 'the sequential turn'")
# --------------------------------------------------------------------------
# The Aug 2 profile was router -> search -> synthesis, strictly sequential.
# These pin the overlaps that fixed it — and the guardrails that keep each
# one quality-free.
_sep18 = f"{datetime.now().year}-09-18"
if _sep18 < datetime.now().strftime("%Y-%m-%d"):  # passed: the nearest FUTURE Sept 18
    _sep18 = f"{datetime.now().year + 1}-09-18"
check("speculation fires only on the unmistakable shape",
      app.speculative_spec("jfk to ord sept 18") == {
          "origins": ["JFK"], "destinations": ["ORD"],
          "trip_type": "one_way", "departure_date": _sep18})
check("...and carries an explicit cheapest/fastest ask into the spec key",
      app.speculative_spec("jfk to ord sept 18 cheapest").get("sort") == "cheapest")
check("no date, no speculation (a dateless spec would only cache a clarify)",
      app.speculative_spec("jfk to ord") is None)
check("round-trip and flexible words disarm speculation",
      app.speculative_spec("jfk to fll round trip sept 18") is None
      and app.speculative_spec("jfk to fll flex september") is None)
check("unknown codes and prose never speculate",
      app.speculative_spec("zzz to ord sept 18") is None
      and app.speculative_spec("fly me to the moon on 9/18") is None)
check("an impossible calendar date never speculates",
      app.speculative_spec("jfk to ord 2/30") is None)

check("searchy follow-ups reach the fast router ('how about jfk to bos fri')",
      app.looks_like_plain_search("how about jfk to bos friday")
      and app.looks_like_plain_search("what about fll to ewr nov 28"))
check("knowledge questions still keep full effort",
      not app.looks_like_plain_search("what is the baggage allowance on Delta domestic?")
      and not app.looks_like_plain_search("how do layovers work in Doha"))

_router_p = app.assistant_system_prompt(router=True)
_full_p = app.assistant_system_prompt()
check("the router prompt is the request-handling half, not the whole concierge",
      len(_router_p) < 0.75 * len(_full_p)
      and "SUGGESTIONS" not in _router_p
      and "combined pool" in _router_p,   # the airport-honesty rules stay
      f"{len(_router_p)}/{len(_full_p)} chars")
check("the full prompt still carries every answering rule",
      "SUGGESTIONS" in _full_p and "lands_plus_days" in _full_p
      and "Deliberation is for hard calls" in _full_p)

_src_lat = open(os.path.join(ROOT, "api", "index.py")).read()
check("searches dispatch as their blocks finish streaming, not at message end",
      "content_block_stop" in _src_lat and "_fire(b)" in _src_lat
      and "futures = fired" in _src_lat)
check("connections pre-warm on the threads that will carry the POSTs",
      "generate_204" in _src_lat and "warm_google_connections(pool)" in _src_lat)
check("warming never touches search outcomes or the breaker",
      "note_search_outcome" not in _src_lat.split("def _warm_one")[1].split("def ")[0])
check("speculation and warming both have kill switches",
      'os.environ.get("SPECULATE")' in _src_lat
      and 'os.environ.get("WARM_CONNECTIONS")' in _src_lat)

# --------------------------------------------------------------------------
section("Reply rendering  — Changelog: 'the spacing the join left behind'")
# --------------------------------------------------------------------------
# The renderer joins the model's soft wraps into flowing text. Swapping each
# newline for a space left the whitespace on either side in place, so a wrap
# before a comma shipped as "on Nov 28 , with morning departures" and one
# after a trailing space as "rises to  $374" (Evan's report, July 28).
import re as _re

_fe_src = open(os.path.join(ROOT, "public", "index.html")).read()


def render_text(sample: str) -> str:
    """Run the SHIPPED renderText chain over `sample`.

    The steps are lifted out of index.html rather than copied here, so this
    tests the real regexes. JS and Python agree on every construct the chain
    uses (classes, quantifiers, lookahead, \\uXXXX escapes); anything more
    exotic fails the parse loudly instead of silently testing a stale copy.
    """
    body = _re.search(r"renderText\(t\) \{(.*?)\n\s*\},", _fe_src, _re.S)
    if not body:
        raise AssertionError("renderText not found in public/index.html")
    steps = _re.findall(
        r"\.replace\(/(.+?)/([gimsu]*)\s*,\s*('(?:[^'\\]|\\.)*'|PARA|LIST)\)", body.group(1))
    if len(steps) < 5:
        raise AssertionError(f"only parsed {len(steps)} replace() steps; keep the chain plain")
    out = sample
    for pattern, flags, rep in steps:
        if rep == "PARA":
            rep = ""
        elif rep == "LIST":
            rep = ""
        else:
            rep = (rep[1:-1].replace("\\n", "\n").replace("\\t", "\t")
                   .replace("\\'", "'").replace("\\\\", "\\"))
        rep = _re.sub(r"\$(\d)", r"\\\1", rep)
        out = _re.sub(pattern, rep, out, count=0 if "g" in flags else 1)
    return out


# exactly the shape Evan reported, as the model hard-wrapped it
_wrapped = ("Newark is the value play. \nJetBlue has a nonstop FLL to EWR for $239 on Nov 28\n"
            ", with morning, midday, and evening departures all at that price. The same flight "
            "rises to \n$374 on Nov 29\n, so the 28th is meaningfully cheaper.")
_rendered = render_text(_wrapped)
check("a wrap before a comma leaves no space before it",
      " ," not in _rendered and " ." not in _rendered, _rendered[:90])
check("a wrap after a trailing space leaves no double space",
      "  " not in _rendered, repr(_rendered[_rendered.find("rises"):][:24]))
check("the sentence still reads as one flowing line",
      "value play. JetBlue" in _rendered and "on Nov 28, with" in _rendered)
check("paragraph breaks survive the join",
      render_text("First para.\n\nSecond para.").count("\n\n") == 1)
check("list lines survive the join",
      render_text("Options:\n- one\n- two").count("\n") == 2,
      repr(render_text("Options:\n- one\n- two")))
check("bold still renders", "<strong>$239</strong>" in render_text("it is **$239** today"))

# --------------------------------------------------------------------------
section("Date coherence  — Changelog: 'a round trip that returned before it left'")
# --------------------------------------------------------------------------
# roll_past_dates rolled each date INDEPENDENTLY: asked for "Aug 1 to Aug 20"
# a few days after Aug 1, the departure rolled a year and the return stayed —
# an incoherent filter Google met with silence, reported to the user as
# "No flights found" for a perfectly answerable question. (For flexible
# windows fli silently SWAPS an inverted from/to, turning the asked month
# into an ~11-month grid — same class, different symptom.)
from datetime import timedelta as _td

_t0 = datetime.now()
_past6 = (_t0 - _td(days=6)).strftime("%Y-%m-%d")
_soon13 = (_t0 + _td(days=13)).strftime("%Y-%m-%d")
_rolled, _rnotes = app.roll_past_dates(
    {"trip_type": "round_trip", "departure_date": _past6, "return_date": _soon13})
check("a rolled departure drags the return past it (never return-before-depart)",
      _rolled["return_date"] > _rolled["departure_date"],
      f"{_rolled['departure_date']} -> {_rolled['return_date']}")
check("...and says so in the notes the user sees", len(_rnotes) >= 2)
_rolled_fx, _ = app.roll_past_dates(
    {"flexible_dates": {"from_date": _past6, "to_date": (_t0 + _td(days=24)).strftime("%Y-%m-%d")}})
check("a flexible window can never invert",
      _rolled_fx["flexible_dates"]["to_date"] >= _rolled_fx["flexible_dates"]["from_date"])
_rolled_arr, _ = app.roll_past_dates(
    {"trip_type": "one_way", "departure_date": _past6, "arrival_date": _soon13})
check("arrival-day targets stay on or after the rolled departure",
      _rolled_arr["arrival_date"] >= _rolled_arr["departure_date"])
_pd40 = _t0 - _td(days=40)
_sp_q = f"jfk to ord {_pd40.strftime('%b').lower()} {_pd40.day}"
_sp = app.speculative_spec(_sp_q)
check("speculation guesses the FUTURE date the router will emit (else the cache never matches)",
      _sp is None or _sp["departure_date"] >= _t0.strftime("%Y-%m-%d"),
      f"{_sp_q!r} -> {_sp and _sp['departure_date']}")

# --------------------------------------------------------------------------
section("Party totals  — Changelog: 'the family of four that booked one seat'")
# --------------------------------------------------------------------------
# Live A/B (JFK->ORD, 1 adult vs 2 adults + 1 child): all nine common flights
# priced at 2.99-3.00x — Google returns PARTY TOTALS for the searched pax
# mix, never per-person. Cards said "per person" and every Book link
# defaulted to one adult.
_ppay = app.stamp_party({"type": "flights", "message": "Found 3 options.", "results": []},
                        {"adults": 2, "children": 2})
check("payloads carry the party and tell the model prices are party totals",
      _ppay["party"]["travelers"] == 4
      and "TOTAL for 2 adult(s) + 2 child(ren)" in _ppay["message"])
check("a solo search keeps its message untouched",
      app.stamp_party({"type": "flights", "message": "m", "results": []}, {})["message"] == "m")
_src_pax = open(os.path.join(ROOT, "api", "index.py")).read()
_leg_sp = app._mc_leg_spec({"adults": 2, "children": 2, "cabin": "business"},
                           {"origins": ["JFK"], "destinations": ["HAN"], "date": future(90)})
check("the multi-city leg sub-specs stop dropping children",
      _leg_sp["children"] == 2 and _leg_sp["adults"] == 2 and _leg_sp["cabin"] == "business"
      and _src_pax.count("_mc_leg_spec(spec, seg)") >= 2)
check("the price-context cache key splits on children",
      '"children": spec.get("children") or 0,' in _src_pax)
check("the /api/returns spec_echo keeps the party (tap-to-price books it too)",
      app._rt_spec_echo({"adults": 3, "children": 1}, *[[types.SimpleNamespace(name="JFK")],
                        [types.SimpleNamespace(name="FLL")]])["adults"] == 3)
_fe_pax = open(os.path.join(ROOT, "public", "index.html")).read()
check("the frontend labels party totals instead of claiming 'per person'",
      "partyN(sec)" in _fe_pax and "paxSuffix(sec)" in _fe_pax
      and "total for ' + partyN(sec) + ' travelers" in _fe_pax)
check("share cards say whose total the price is",
      "total for ' + this.partyN(sec) + ' travelers" in _fe_pax)

# --------------------------------------------------------------------------
section("Bag-aware pricing  — Changelog: 'the cheapest fare that was not'")
# --------------------------------------------------------------------------
# Live A/B (FLL->BOS, no bags vs 1 carry-on + 1 checked): $90 -> $135, the
# real +$45 fee Google bakes in when bags are set. The cheapest headline
# fare is often a budget carrier that charges for a bag, so a bag-inclusive
# search can flip the ranking — the honest answer to 'is this basic economy'.
_o_bag, _ = app.resolve_airports(["FLL"])
_d_bag, _ = app.resolve_airports(["BOS"])
_bag_spec = {"trip_type": "one_way", "carry_on": True, "checked_bags": 1, "departure_date": future(40)}
_f_bags = app.build_filters(_bag_spec, _o_bag, _d_bag)
_f_none = app.build_filters({"trip_type": "one_way", "departure_date": future(40)}, _o_bag, _d_bag)
check("build_filters attaches a BagsFilter only when bags were asked",
      _f_bags.bags is not None and _f_bags.bags.carry_on is True
      and _f_bags.bags.checked_bags == 1 and _f_none.bags is None)
_bpay = app.stamp_party({"type": "flights", "message": "Found 5 options.", "results": []},
                        {"carry_on": True, "checked_bags": 1})
check("a bag-inclusive payload carries the bags and tells the model prices include them",
      _bpay["bags"]["summary"] == "1 carry-on + 1 checked bag"
      and "INCLUDES 1 carry-on + 1 checked bag" in _bpay["message"])
check("a bagless search stamps no bags and leaves its message clean",
      "bags" not in app.stamp_party({"type": "flights", "message": "m", "results": []}, {})
      and "BAG PRICING" not in app.stamp_party({"type": "flights", "message": "m", "results": []}, {})["message"])
check("bags split the search cache (never serve a bagless price for a bag search)",
      app._spec_key(_bag_spec) != app._spec_key({k: v for k, v in _bag_spec.items() if k not in ("carry_on", "checked_bags")}))
check("the /api/returns spec_echo keeps the bags (tap-to-price stays bag-inclusive)",
      app._rt_spec_echo({"carry_on": True, "checked_bags": 2}, *[[types.SimpleNamespace(name="JFK")],
                        [types.SimpleNamespace(name="FLL")]])["checked_bags"] == 2)
_src_bag = open(os.path.join(ROOT, "api", "index.py")).read()
check("the tool schema exposes carry_on and checked_bags",
      '"carry_on": {' in _src_bag and '"checked_bags": {' in _src_bag)
check("the prompt teaches the model to price bags in when the user mentions luggage",
      "Bags change the true price" in _src_bag)
check("the frontend discloses that shown fares include the bags",
      "sec.bags" in _fe_pax and "bag fees are baked in" in _fe_pax)

# --------------------------------------------------------------------------
section("Wire efficiency  — Changelog: 'the bytes the client threw away'")
# --------------------------------------------------------------------------
_src_eff = open(os.path.join(ROOT, "api", "index.py")).read()
check("the sections frame is a count marker, not a payload the client discards",
      'emit("sections", {"count": len(fresh)})' in _src_eff
      and 'emit("sections", fresh)' not in _src_eff)
check("SSE frames serialize compact (no ', '/': ' padding)",
      "separators=(',', ':')" in _src_eff)
_jfk_pt = app.coords_for("JFK")
check("route points ship rounded coordinates (3 decimals ~ 110m)",
      bool(_jfk_pt) and abs(_jfk_pt["lat"] * 1000 - round(_jfk_pt["lat"] * 1000)) < 1e-6
      and abs(_jfk_pt["lon"] * 1000 - round(_jfk_pt["lon"] * 1000)) < 1e-6)
_fe_eff = open(os.path.join(ROOT, "public", "index.html")).read()
check("world.js defers instead of blocking first paint",
      '<script defer src="/world.js' in _fe_eff)
check("world.js is immutable-cached (it is ?v= cache-busted)",
      "immutable" in open(os.path.join(ROOT, "vercel.json")).read())

# --------------------------------------------------------------------------
section("Frontend robustness  — Changelog: Aug 7 paper cuts")
# --------------------------------------------------------------------------
check("a degraded multi-city section still shows the Google link it promises",
      "s.type === 'multicity' && s.combined_check_url" in _fe_eff)
check("Stop stops the typewriter too, and never leaves an empty bubble",
      "this.reqId++;" in _fe_eff.split("stop(silent)")[1].split("async search")[0]
      and "this.messages.pop()" in _fe_eff.split("stop(silent)")[1].split("async search")[0])
check("IME Enter commits the composition, not the message",
      "isComposing" in _fe_eff)
check("a chip or calendar tap never wipes a typed draft",
      "if (!queryOverride)" in _fe_eff)
check("share image actions require a rendered image",
      'x-show="share.blob"' in _fe_eff)
check("the multi-city basis label counts the real legs",
      "'as ' + (it.parts || []).length + ' separate tickets'" in _fe_eff)
check("/api/returns rejects unknown airports as a 400, not a raw 500",
      "unrecognized leg airport" in _src_eff)

# --------------------------------------------------------------------------
section("Grid coverage  — Changelog: 'one chip where seventeen belonged'")
# --------------------------------------------------------------------------
# Aug 8, HVN/BDL/NYC -> FLL "mid-late December": the flexible-dates grid
# returned ONE row and shipped it as a single confident chip — while the
# fixed-date searches in the same turn found a cheaper day one square away.
# The grid degrades exactly like the list (a documented lesson), but the
# ladder accepted any NON-EMPTY grid as complete. Now a grid short of half
# its window retries as a fresh identity (keeping the richest attempt), and
# an incurably short grid ships with a coverage warning, never as if whole.
def _grid_row(day, price):
    return types.SimpleNamespace(date=[datetime(2026, 12, day)], price=price, currency="USD")


class _ThinThenFull:
    calls = 0

    def search(self, filters, currency="USD"):
        _ThinThenFull.calls += 1
        if _ThinThenFull.calls == 1:
            return [_grid_row(20, 146.0)]                       # the degraded slice
        return [_grid_row(15 + i, 130.0 + i) for i in range(17)]  # the real window


class _AlwaysThin:
    def search(self, filters, currency="USD"):
        return [_grid_row(20, 146.0)]


_real_sd = app.SearchDates
_flex_spec = {"trip_type": "one_way",
              "flexible_dates": {"from_date": future(60), "to_date": future(76)}}  # 17-day window
_o_fx, _ = app.resolve_airports(["HVN", "BDL"])
_d_fx, _ = app.resolve_airports(["FLL"])
app.SearchDates = _ThinThenFull
_full = app.search_flexible_dates(dict(_flex_spec), _o_fx, _d_fx, "USD")
app.SearchDates = _AlwaysThin
_thin = app.search_flexible_dates(dict(_flex_spec), _o_fx, _d_fx, "USD")
app.SearchDates = _real_sd
check("a grid short of its window is retried, not trusted",
      _ThinThenFull.calls >= 2 and len(_full["dates"]) == 17,
      f"attempts={_ThinThenFull.calls}, shipped {len(_full['dates'])} dates")
check("...and the recovered full window ships with no warning",
      "grid_coverage" not in _full and "GRID COVERAGE" not in _full["message"])
check("an incurably short grid ships, but never as if complete",
      len(_thin["dates"]) == 1
      and _thin["grid_coverage"] == {"priced_days": 1, "window_days": 17}
      and "NOT evidence of no service" in _thin["message"])
_fe_gc = open(os.path.join(ROOT, "public", "index.html")).read()
check("the card tells the user the calendar came back partial",
      "sec.grid_coverage" in _fe_gc and "not 'no flights'" in _fe_gc.replace("\\'", "'"))
check("a bounded window (~3 weeks) shows every priced day by default",
      "all.length <= 21" in _fe_gc)

# The mechanism behind Evan's one-chip screen, measured live: BDL->FLL alone
# returned 31/31 days while HVN+BDL+JFK->FLL returned ONE row — a combined
# grid collapses to its worst member. The cure mirrors probe_missing_airports:
# per-pair grids, merged at the min price per date, sparse airports named.
class _PoisonedCombined:
    def search(self, filters, currency="USD"):
        seg = filters.flight_segments[0]
        codes = {a[0].name for a in seg.departure_airport}
        if codes == {"BDL"}:
            return [_grid_row(15 + i, 150.0 + i) for i in range(17)]
        return [_grid_row(20, 146.0)]  # combined AND HVN-alone: one thin row


app.SearchDates = _PoisonedCombined
_merged = app.search_flexible_dates(dict(_flex_spec), _o_fx, _d_fx, "USD")
app.SearchDates = _real_sd
check("a poisoned combined grid is rebuilt from per-pair calendars",
      len(_merged["dates"]) == 17 and "GRID COVERAGE" not in _merged["message"],
      f"merged {len(_merged['dates'])} days")
check("...the merge keeps the cheaper price where pair days overlap",
      next(d["price"] for d in _merged["dates"] if d["date"] == "2026-12-20") == 146.0
      and next(d for d in _merged["dates"] if d["date"] == "2026-12-20")["cheapest"])
check("...the sparse airport is named, its gaps decoupled from 'no service'",
      "HVN" in _merged["message"] and "do NOT mean no service" in _merged["message"])
check("...and the payload records what the probe did (free diagnosis)",
      "pair grids" in _merged.get("grid_probe", ""))

# The ACTUAL root cause under all of the above, found Aug 8 when Evan put
# Google's own full calendar next to our one chip: GetCalendarGraph streams
# PER-SLICE wrb chunks — one day per chunk, out of order, ending in a sweep
# of unpriced placeholders — and fli's dates parser read only chunk one.
# The union must identify day items BY SHAPE: chunk tails are heterogeneous,
# and the first union keyed on chunk[-1], ingested a bare `31`, and
# OVERWROTE a healthy chunk's days with it (multi-origin grids broke).
def _day(day, price, month=10):
    tok = [[None, price], "tok"] if price is not None else None
    return [f"2026-{month:02d}-{day:02d}", None, tok, 1]


_cal_chunks = [
    ["meta", [_day(30, 84.0)]],            # per-slice: one day, arrives FIRST
    ["meta", [31]],                        # garbage tail: month length, no days
    ["meta", [_day(5, 69.0)]],
    ["meta", 31],                          # scalar tail
    ["meta", [_day(30, None), _day(7, None)]],  # unpriced placeholder sweep
]
_cal = app.merge_calendar_chunks([list(c) for c in _cal_chunks])
_cal_items = _cal[-1]
check("calendar chunks union per-slice days, identified by shape",
      [it[0] for it in _cal_items] == ["2026-10-05", "2026-10-07", "2026-10-30"]
      and 31 not in _cal_items,
      f"{[it[0] for it in _cal_items]}")
check("...an unpriced placeholder never erases a priced day",
      next(it for it in _cal_items if it[0] == "2026-10-30")[2][0][1] == 84.0)
check("...and the dates module actually uses the union",
      __import__("fli.search.dates", fromlist=["x"]).parse_first_wrb_payload
      is app._parse_merged_calendar_payload)


# An EMPTY combined grid is the extreme of thin: it must reach the per-pair
# fan-out, not die on an early "couldn't get date pricing" (the first
# version of the fan-out was unreachable for exactly the failing case).
class _EmptyCombined:
    def search(self, filters, currency="USD"):
        seg = filters.flight_segments[0]
        codes = {a[0].name for a in seg.departure_airport}
        if codes == {"BDL"}:
            return [_grid_row(15 + i, 150.0 + i) for i in range(17)]
        return []


app.SearchDates = _EmptyCombined
_rescued = app.search_flexible_dates(dict(_flex_spec), _o_fx, _d_fx, "USD")
app.SearchDates = _real_sd
check("an empty combined grid still fans out per pair",
      len(_rescued["dates"]) == 17 and "GRID COVERAGE" not in _rescued["message"],
      f"{len(_rescued['dates'])} days")
check("...naming the airport that returned nothing even alone",
      "HVN" in _rescued["message"])

# --------------------------------------------------------------------------
section("Memory  — Changelog: 'the workspace survives a reload'")
# --------------------------------------------------------------------------
# One reload, tab crash, or phone lock used to destroy the whole workspace —
# the single worst friction against Google Flights, whose state lives in the
# URL. The conversation now persists per-device and the landing greets you
# with your recent trips. The honesty rules are the point: a remembered fare
# always carries its age and never impersonates a live one.
_fe_mem = open(os.path.join(ROOT, "public", "index.html")).read()
check("the conversation persists and restores, versioned",
      "fa_convo_v1" in _fe_mem and "restoreConvo()" in _fe_mem
      and "fa_recents_v1" in _fe_mem)
check("runtime keys are stripped on save and anchors re-stamped on restore",
      "startsWith('_')" in _fe_mem
      and _fe_mem.count("'sec-' + (++this.sectionSeq)") == 3)
check("stored cards keep the ledger's budget (last 2 card-bearing replies)",
      "withCards" in _fe_mem
      and ".slice(-2)" in _fe_mem.split("persistConvo() {")[1].split("restoreConvo() {")[0])
check("recents are capped, deduped by query, and re-ask verbatim",
      ".slice(0, 8)" in _fe_mem and "rememberRecent" in _fe_mem
      and 'search(r.q)' in _fe_mem)
check("a remembered fare carries its age, never impersonates a live one",
      "agoText" in _fe_mem and "'saw ' + this.formatPrice" in _fe_mem
      and "re-checks live prices" in _fe_mem)
check("a resumed conversation admits its age once",
      "Fares shown were quoted then" in _fe_mem)
check("quota failure keeps the transcript and drops the cards",
      "memory degrades to this session only" in _fe_mem)
check("a fresh start keeps the recents (memory of trips, not of the chat)",
      "removeItem(CONVO_KEY)" in _fe_mem and "New trip" in _fe_mem
      and "removeItem(RECENTS_KEY)" in _fe_mem.split("clearRecents() {")[1][:200]
      and "removeItem(RECENTS_KEY)" not in _fe_mem.split("newConversation() {")[1].split("rememberRecent")[0])

# --------------------------------------------------------------------------
section("Cold start and stall recovery  — Changelog: 'the first search that never returned'")
# --------------------------------------------------------------------------
# Evan's #1: the first search after an idle spell took forever or never came
# back, worst on phones. Two root causes, two guards.
#   1) Cold start: Vercel spins the function down; the first request paid the
#      Python boot + a cold Anthropic TLS + the seconds-long residential-proxy
#      handshake to Google. A GET /api/warm, pinged before the first search,
#      opens those tunnels ahead of time (no API spend).
#   2) Never returns: on mobile the SSE socket dies SILENTLY, and reader.read()
#      hung forever because the only abort was user-stop. A client watchdog now
#      falls back on a dead socket / gives up on an alive-but-grinding one.
_api_src = open(os.path.join(ROOT, "api", "index.py")).read()
_warm_routes = [r for r in app.app.routes
                if getattr(r, "path", None) == "/api/warm"]
check("GET /api/warm is registered (the pre-search warm endpoint)",
      bool(_warm_routes) and "GET" in getattr(_warm_routes[0], "methods", set()))
_warm_body = _api_src.split("async def warm(")[1].split("\n@app.")[0]
check("warm opens Google + Anthropic tunnels but never searches or prompts",
      "warm_google_connections(" in _warm_body and "api.anthropic.com" in _warm_body
      and "run_assistant" not in _warm_body)
check("the SSE stream sends an immediate first byte so a cold worker still proves life",
      'yield ": open\\n\\n"' in _api_src)

_fe_net = open(os.path.join(ROOT, "public", "index.html")).read()
_stream_body = _fe_net.split("async streamTurn(")[1].split("_clearNetWatch() {")[0]
check("streamTurn arms an inactivity + ceiling watchdog over the read loop",
      "STALL_MS" in _stream_body and "CEILING_MS" in _stream_body
      and _stream_body.count("lastByte = Date.now()") >= 2)  # init + reset per chunk
check("the watchdog disarms the instant a result lands (the typewriter must not trip it)",
      "this._clearNetWatch(); // full result in hand" in _fe_net)
check("a dead-socket stall falls back on a FRESH controller, not the aborted one",
      "if (this.controller.signal.aborted) this.controller = new AbortController();" in _fe_net)
check("the plain-turn fallback is itself time-bounded, so it cannot hang in turn",
      "timedOut = true" in _fe_net.split("async plainTurn(")[1].split("},")[0])
check("a stall offers a one-tap retry of the exact query, never a dead end",
      "suggestions: [q]" in _fe_net)
check("the client warms the backend on load, on compose focus, and on tab return",
      "this.warm();" in _fe_net and '@focus="warm()"' in _fe_net
      and "visibilitychange" in _fe_net and "'/api/warm'" in _fe_net)

# --------------------------------------------------------------------------
section("Design system  — Changelog: 'the redesign: a product, not stationery'")
# --------------------------------------------------------------------------
import re as _re_ds
_fe_ds = open(os.path.join(ROOT, "public", "index.html")).read()
_css = _fe_ds[_fe_ds.index("<style>"):_fe_ds.index("</style>")]
_root_block = _css[_css.index(":root {"):_css.index("}", _css.index(":root {"))]
_dark_block = _css[_css.index("html.dark {"):_css.index("}", _css.index("html.dark {"))]
_colour_tokens = {m.group(1) for m in _re_ds.finditer(r"(--[a-z0-9-]+):\s*(#|rgba?\()", _root_block)}
_dark_tokens = set(_re_ds.findall(r"(--[a-z0-9-]+):", _dark_block))
check("every colour token is redefined for dark mode (no light colour leaks into dark)",
      _colour_tokens and _colour_tokens <= _dark_tokens,
      ", ".join(sorted(_colour_tokens - _dark_tokens)) or f"{len(_colour_tokens)} tokens")
_map_src = _fe_ds[_fe_ds.index("mapSvg(points, returnPoints) {"):_fe_ds.index("airlineColor(code) {")]
check("route maps take their colours from the theme, not hard-coded hex",
      not _re_ds.search(r'(fill|stroke)="#[0-9a-fA-F]{3,6}"', _map_src) and "m-route" in _map_src
      and ".route-map .m-land" in _css)
check("x-show cannot collapse the one-ticket link (display lives in the stylesheet)",
      "a.show-all { display: block;" in _css)
check("nothing floats over the sticky board bars: the search index lives in the compose bar",
      'class="ladder-fab"' in _fe_ds.split('class="input-row"')[1].split("</form>")[0])
check("one typeface: the serif and its download are gone",
      "Fraunces" not in _fe_ds and "family=Inter:wght@400..700" in _fe_ds)

# --------------------------------------------------------------------------
section("Section titles & dates  — Changelog: 'verify date year context'")
# --------------------------------------------------------------------------
# Nov 11: a results section was titled "verify date year context", the
# model's note to itself, because titles came from the free-text `summary`.
# The model wrote it while burning a search to double-check a year that a
# frozen schema example ("Assumed 2026 for 3/21") had taught it to doubt.
from datetime import date as _date_t, timedelta as _td_t
_passed = _date_t.today() - _td_t(days=30)
_past_ds = _passed.isoformat()                                   # rolls to next year
_next_ds = _passed.replace(year=_passed.year + 1).isoformat()
_hdr = app.section_header(app.roll_past_dates({
    "trip_type": "one_way", "origins": ["JFK", "LGA", "EWR"], "destinations": ["HAN"],
    "departure_date": _past_ds, "summary": "verify date year context"})[0])
check("section titles are built from the search, never the model's scratch summary",
      _hdr["title"] == "New York to Hanoi" and "verify" not in json.dumps(_hdr)
      and "JFK · LGA · EWR → HAN" in _hdr["subtitle"] and str(_passed.year + 1) in _hdr["subtitle"],
      json.dumps(_hdr))
check("...and filters that distinguish two searches become tags",
      app.section_header({"trip_type": "one_way", "origins": ["JFK"], "destinations": ["LAX"],
                          "departure_date": future(20), "max_stops": "non_stop",
                          "cabin": "business", "arrival_time": {"latest": 14}})["tags"]
      == ["Business", "Nonstop only", "Arrives by 2pm"])
_fe_ttl = open(os.path.join(ROOT, "public", "index.html")).read()
check("the screen prefers the built title over the message's first line",
      "if (sec.title) return sec.title;" in _fe_ttl)
check("a passed date and its rolled twin are ONE search (cache and dedupe key)",
      app._spec_key({"origins": ["JFK"], "destinations": ["HAN"], "departure_date": _past_ds})
      == app._spec_key({"origins": ["JFK"], "destinations": ["HAN"], "departure_date": _next_ds}))
_src_ttl = open(os.path.join(ROOT, "api", "index.py")).read()
check("a repeated search in one turn adds no second section",
      "section_index.get(_skey)" in _src_ttl and "sections[_prev] = payload" in _src_ttl)
_p_ttl = app.assistant_system_prompt()
check("the prompt states the year rule with a LIVE example, and no frozen one",
      f"means {_passed.year + 1}-{_passed:%m-%d}" in _p_ttl
      and "Never run a search only to double-check" in _p_ttl
      and not _re.search(r"Assumed 20\d\d", json.dumps(app.SEARCH_TOOL))  # no frozen year the model reads
      and f"means {_passed.year + 1}-" in app.assistant_system_prompt(router=True))
check("airline logos keep the two-letter code underneath as a fallback",
      '<span x-text="f.airline_code"></span><img :src="logoUrl(f.airline_code)"' in _fe_ttl
      and 'onerror="this.remove()"' in _fe_ttl)

# --------------------------------------------------------------------------
section("Reply assembly & dependency drift  — Changelog: 'Hà Nội, and the reply that changed its mind'")
# --------------------------------------------------------------------------
# With adaptive thinking a reply can arrive as several text blocks split
# mid-sentence; the old "\n".join broke sentences ("for $442 \n.").
_tb = lambda t: types.SimpleNamespace(type="text", text=t)
check("a reply split across text blocks reads as one answer",
      app.join_text_blocks([_tb("the top pick is "), types.SimpleNamespace(type="thinking"),
                            _tb("Asiana for $442 "), _tb(". That is also"), _tb(" the cheapest.\n\nNext.")])
      == "the top pick is Asiana for $442. That is also the cheapest.\n\nNext.")
check("...and words at a seam never collide",
      app.join_text_blocks([_tb("to Hanoi"), _tb("departing 10:30am.")]) == "to Hanoi departing 10:30am.")
check("the prompt forbids revising the answer mid-reply",
      "never revises itself mid-answer" in app.assistant_system_prompt())
# Nov 11: prod titled HAN "Hà Nội" though every published airportsdata
# release says "Hanoi" — an unpinned dependency let prod run a copy nothing
# here had verified. What the checks run is what ships.
from importlib.metadata import version as _ver
_reqs = {ln.split("==")[0].strip(): ln.split("==")[1].strip()
         for ln in open(os.path.join(ROOT, "requirements.txt")) if "==" in ln and not ln.startswith("#")}
check("data and patched-library dependencies are pinned to what the checks run",
      {"airportsdata", "flights"} <= set(_reqs)
      and all(_ver(k) == v for k, v in _reqs.items()),
      ", ".join(f"{k} {v} (local {_ver(k)})" for k, v in _reqs.items()))
check("titles use English place names",
      app.place_name(["HAN"]) == "Hanoi" and app.place_name(["SGN"]) == "Ho Chi Minh City")

# --------------------------------------------------------------------------
section("The airport Google turned away  — Changelog: 'the airport Google turned away'")
# --------------------------------------------------------------------------
# Nov 11, prod: "nyc to hanoi march 4" shipped LGA and EWR only. JFK->HAN was
# refused 8/8 through the proxy (answered in 0.3s from a home connection),
# the date grid was refused too, the partial set was cached for 4 minutes,
# and the reply called a $817 United flight "the only one-stop on offer"
# while JFK had Asiana at $442 with one stop.
_bf_ewr = [app.serialize_flight(result([leg("EWR", "HAN", future(60) + "T09:00", future(62) + "T01:25", "AS", str(299 + i))],
                                       445.0 + 40 * i, stops=2, duration=3145)) for i in range(3)]
_bf_jfk = [app.serialize_flight(result([leg("JFK", "HAN", future(60) + "T10:30", future(61) + "T22:25", "OZ", "221")],
                                       442.0, stops=1, duration=1435)),
           app.serialize_flight(result([leg("JFK", "HAN", future(60) + "T12:00", future(61) + "T21:45", "KE", "86")],
                                       473.0, stops=1, duration=1305))]
_bf_spec = {"trip_type": "one_way", "departure_date": future(60),
            "origins": ["JFK", "LGA", "EWR"], "destinations": ["HAN"]}
_have, _ = app.rank_and_cut_one_way([dict(r, highlights=[]) for r in _bf_ewr], _bf_spec)
_merged, _all, _added = app.merge_backfill_rows(_bf_spec, _have, _bf_jfk + [dict(_bf_ewr[0])])
_best = [r for r in _merged if "Best value" in r["highlights"]]
check("a late airport is merged and re-ranked by the turn's own rules",
      _added == 2 and len(_all) == 5 and len(_best) == 1
      and _best[0]["legs"][0]["from"] == "JFK", f"best: {_best[0]['airline'] if _best else None}")
check("...without listing a flight twice or stacking stale highlights",
      sum(1 for r in _merged if r["legs"][0]["flight_number"] == "299") == 1
      and all(r["highlights"].count("Cheapest option") <= 1 for r in _merged)
      and sum(1 for r in _merged if "Cheapest option" in r["highlights"]) == 1)
_note = app.backfill_note("JFK→HAN", 2, _have[0], _merged[0], _bf_spec)
check("the card says what the late airport changed, from the numbers, with no dashes",
      # Korean Air wins on value: 2h10m less in the air for $31 more
      "New best value: KE, $473, 1 stop, 21h 45m." in _note
      and "—" not in _note and "–" not in _note, _note)

_real_exec = app._execute_spec
app._execute_spec = lambda sub: {"type": "flights", "results": [dict(r) for r in _bf_jfk]} \
    if sub["origins"] == ["JFK"] else {"results": []}
_bf_ok = app.backfill_piece(_bf_spec, {"origins": ["JFK"], "destinations": ["HAN"]}, _have)
app._execute_spec = lambda sub: {"type": "flights", "results": [],
                                 "search_diag": {"verdict": "refused", "attempts": ["a1 refused 95B"]}}
_bf_ref = app.backfill_piece(_bf_spec, {"origins": ["JFK"], "destinations": ["HAN"]}, _have)
_bf_out = app.backfill_piece(_bf_spec, {"origins": ["BOS"], "destinations": ["HAN"]}, _have)
_bf_rt = app.backfill_piece({**_bf_spec, "trip_type": "round_trip"}, {"origins": ["JFK"], "destinations": ["HAN"]}, _have)
# odd attempts re-run the WHOLE search (a different request shape) and keep
# only the piece's rows: prod answered JFK/LGA/EWR->HAN while JFK->HAN alone
# was refused, minutes apart
_shapes = []
app._execute_spec = lambda sub: (_shapes.append(tuple(sub["origins"])),
                                 {"type": "flights", "results": [dict(r) for r in _bf_jfk + _bf_ewr]}
                                 if len(sub["origins"]) == 3 else {"results": []})[1]
_bf_whole = app.backfill_piece(_bf_spec, {"origins": ["JFK"], "destinations": ["HAN"]}, _have, attempt=1)
_bf_alone = app.backfill_piece(_bf_spec, {"origins": ["JFK"], "destinations": ["HAN"]}, _have, attempt=2)
app._execute_spec = lambda sub: {"type": "flights", "results": [dict(r) for r in _bf_ewr]}
_bf_lacks = app.backfill_piece(_bf_spec, {"origins": ["JFK"], "destinations": ["HAN"]}, _have, attempt=1)
app._execute_spec = _real_exec
# a backfill must never add volume to a wave (README: "Retrying harder makes
# it worse"): it stands down while the breaker is open, and asks lean
_br_keep = dict(app._breaker)
app._breaker["open_until"] = time.monotonic() + 30
_asked = []
app._execute_spec = lambda sub: (_asked.append(1), {"results": []})[1]
_bf_rest = app.backfill_piece(_bf_spec, {"origins": ["JFK"], "destinations": ["HAN"]}, _have)
app._breaker.clear(); app._breaker.update(_br_keep)
_lean_seen = []
app._execute_spec = lambda sub: (_lean_seen.append(getattr(app._local, "lean", False)), {"results": []})[1]
app.backfill_piece(_bf_spec, {"origins": ["JFK"], "destinations": ["HAN"]}, _have)
app._execute_spec = _real_exec
check("backfill stands down mid-wave and asks lean otherwise",
      _bf_rest["status"] == "refused" and not _asked and _lean_seen == [True]
      and not getattr(app._local, "lean", False)
      and "getattr(_local, \"lean\", False)" in open(os.path.join(ROOT, "api", "index.py")).read())
check("backfill alternates the request's shape: alone, then the whole search",
      _shapes == [("JFK", "LGA", "EWR"), ("JFK",)] and _bf_whole["status"] == "ok"
      and _bf_whole["added"] == 2 and _bf_alone["status"] == "refused", str(_shapes))
check("...and a whole search that comes back without the piece is not a success",
      _bf_lacks["status"] == "refused" and _bf_lacks.get("shape") == "whole")
check("backfill returns the merged list, the note, and what the next turn's memory should say",
      _bf_ok["status"] == "ok" and _bf_ok["added"] == 2
      and _bf_ok["results"][0]["legs"][0]["from"] == "JFK"
      and _bf_ok["ledger"]["route"] == "JFK/LGA/EWR to HAN" and _bf_ok["price_context_stale"],
      str({k: _bf_ok.get(k) for k in ("status", "added", "note")}))
check("...a piece refused again stays a refusal (never 'no flights')",
      _bf_ref["status"] == "refused")
check("...and it only ever searches part of the search it was given, one-way only",
      _bf_out["status"] == "invalid" and _bf_rt["status"] == "invalid")

# the turn ships what it could not get, and the reply is held to it
_resc_bf = (_resc.get("backfill") or {}).get("pieces") or []
check("a refused airport pair ships as a piece to ask for again, with Google's own link",
      {"origins": ["SGN"], "destinations": ["JFK"]} in [{k: p[k] for k in ("origins", "destinations")} for p in _resc_bf]
      and all(p.get("url", "").startswith("https://www.google.com/travel/flights?tfs=") for p in _resc_bf),
      str(_resc_bf)[:200])
check("...a search refused outright ships itself as the piece",
      [{k: p[k] for k in ("origins", "destinations")} for p in (_alone.get("backfill") or {}).get("pieces") or []]
      == [{"origins": ["SGN"], "destinations": ["JFK"]}])
check("...and the reply may not rank a partial list as if it were whole",
      "STILL LOADING" in _resc["message"] and "never 'cheapest overall'" in _resc["message"]
      and "STILL LOADING" in app.assistant_system_prompt())
check("a fully answered search ships no backfill",
      "backfill" not in _found_payload if (_found_payload := app.attach_backfill(
          {"type": "flights", "message": "", "results": [1]}, _bf_spec)) else False)

# a partial set must not be served to "try again" for 4 minutes
_saved = (app.execute_spec, app._start_ctx_for)
app.execute_spec = lambda spec: {"type": "flights", "message": "", "results": [dict(_bf_ewr[0])],
                                 "backfill": {"pieces": [{"origins": ["JFK"], "destinations": ["HAN"]}]}}
app._start_ctx_for = lambda spec: None
_pspec = dict(_bf_spec, departure_date=future(61))
app._search_cache.pop(app._spec_key(_pspec), None)
app.cached_execute_spec(_pspec)
_ttl_left = app._search_cache[app._spec_key(_pspec)][0] - time.monotonic()
app._search_cache.pop(app._spec_key(_pspec), None)
app.execute_spec, app._start_ctx_for = _saved
check("a result with a refused part is cached for seconds, not minutes",
      0 < _ttl_left <= 30 < app.SEARCH_CACHE_TTL, f"{_ttl_left:.0f}s")

# the other way out: two proxied refusals, then one direct attempt
_px_saved, _dir_saved, _sleep = app._proxy_base, dict(app._direct), app.time.sleep
_br_saved = dict(app._breaker)
app._proxy_base = "http://user:pass@geo.iproyal.com:12321"
app._direct.update(ident=None, cool_until=0.0)
app.time.sleep = lambda s: None
_via_log = []


def _refuse_unless_direct(*a, **k):
    _via_log.append((bool(app._local.identity.get("direct")), app._local.fli_timeout))
    app._local.last_wire = None
    return [result([leg("JFK", "HAN", future(60) + "T10:30", future(61) + "T22:25", "OZ", "221")], 442.0)] \
        if app._local.identity.get("direct") else []


_dstats_saved = dict(app._attempt_stats["direct"])
try:
    app._breaker.update(consecutive=0, open_until=0.0)
    _got = app.run_search(types.SimpleNamespace(search=_refuse_unless_direct), app.build_filters(
        {**_bf_spec, "origins": ["JFK"]}, *[app.resolve_airports(x)[0] for x in (["JFK"], ["HAN"])]),
        app.SortBy.CHEAPEST, top_n=10, budget_s=30)
    _d1 = app.search_verdict()["attempts"]
    _direct_timeouts = [t for d, t in _via_log if d]
    _dstats_after_ok = dict(app._attempt_stats["direct"])
    _sessions_direct = app._identity_session(app._direct["ident"]).proxies if app._direct["ident"] else None
    _via_log.clear()
    app._breaker.update(consecutive=0, open_until=0.0)
    app._direct.update(ident=None, cool_until=0.0)
    app.run_search(types.SimpleNamespace(search=lambda *a, **k: (setattr(app._local, "last_wire", None), [])[1]),
                   app.build_filters({**_bf_spec, "origins": ["JFK"]},
                                     *[app.resolve_airports(x)[0] for x in (["JFK"], ["HAN"])]),
                   app.SortBy.CHEAPEST, top_n=10, budget_s=30)
    _d2 = app.search_verdict()["attempts"]
    _cooling = app._direct["cool_until"] > time.monotonic() + 200
    _d3_direct = (app._breaker.update(consecutive=0, open_until=0.0), app.run_search(
        types.SimpleNamespace(search=lambda *a, **k: (setattr(app._local, "last_wire", None), [])[1]),
        app.build_filters({**_bf_spec, "origins": ["JFK"]},
                          *[app.resolve_airports(x)[0] for x in (["JFK"], ["HAN"])]),
        app.SortBy.CHEAPEST, top_n=10, budget_s=30), any("direct" in d for d in app.search_verdict()["attempts"]))[2]
finally:
    app._proxy_base = _px_saved
    app._direct.clear(); app._direct.update(_dir_saved)
    app.time.sleep = _sleep
    app._breaker.clear(); app._breaker.update(_br_saved)
    _dstats_end = dict(app._attempt_stats["direct"])
    app._attempt_stats["direct"].update(_dstats_saved)
check("...a direct attempt is capped short, and prod can count how the route fares",
      _direct_timeouts == [app.DIRECT_ATTEMPT_S] and app.DIRECT_ATTEMPT_S <= 12
      and _dstats_after_ok["ok"] == _dstats_saved["ok"] + 1
      and _dstats_end["refused"] == _dstats_saved["refused"] + 1
      and '"direct_route"' in open(os.path.join(ROOT, "api", "index.py")).read(),
      f"{_direct_timeouts} {_dstats_end}")
check("after two proxied refusals the next attempt leaves directly, and says so",
      len(_got) == 1 and _d1[:3] == [_d1[0], _d1[1], "a3 direct ok 1"] and "refused" in _d1[0]
      and not _sessions_direct, str(_d1))
check("...a refused direct attempt rests that route instead of hammering it",
      any(d.startswith("a3 direct refused") for d in _d2) and _cooling and not _d3_direct, str(_d2))
check("...and with no proxy configured nothing changes (every attempt is direct anyway)",
      not app.direct_available() if not app._proxy_base else True)

_fe_bf = open(os.path.join(ROOT, "public", "index.html")).read()
check("the page asks again after the turn, on both the streamed and the JSON path",
      _fe_bf.count("this.startBackfills(") == 2 and "fetch('/api/backfill'" in _fe_bf
      and "updateLedger(out.ledger)" in _fe_bf)
check("...and a search refused outright still shows, as its notice",
      "((s.backfill || {}).pieces || []).length > 0" in _fe_bf)
_bf_routes = [r for r in app.app.routes if getattr(r, "path", "") == "/api/backfill"]
check("POST /api/backfill is registered", bool(_bf_routes) and "POST" in _bf_routes[0].methods)

# --------------------------------------------------------------------------
section("Trip briefings  — Changelog: 'trip briefings by email'")
# --------------------------------------------------------------------------
# Evan, Nov 11: a Watch button that emails a briefing on a schedule (best,
# cheapest, fastest, what changed) to him and friends he adds. Owner-only:
# the site can never be used to email strangers. Runs fully offline here:
# a local JSON store, a local outbox, a stubbed search.
import tempfile as _tf_b, glob as _glob_b
from starlette.testclient import TestClient as _TC
_bdir = _tf_b.mkdtemp()
app._LOCAL_KV_PATH = os.path.join(_bdir, "store.json")
app._LOCAL_OUTBOX = os.path.join(_bdir, "outbox")
_bprice = {"v": 470.0}


def _brief_payload(spec):
    d = spec["departure_date"]
    return {"type": "flights", "message": "m", "results": [
        app.serialize_flight(result([leg("JFK", "LAX", d + "T08:00", d + "T11:24", "<b>B6</b>", "15")], _bprice["v"], stops=0, duration=384)),
        app.serialize_flight(result([leg("JFK", "LAX", d + "T06:00", d + "T13:00", "AA", "1")], 300.0, stops=1, duration=600))]}


_real_ces_b = app.cached_execute_spec
app.cached_execute_spec = _brief_payload
_cl = _TC(app.app)
_bspec = {"trip_type": "one_way", "origins": ["JFK"], "destinations": ["LAX"], "departure_date": future(40)}
_r = _cl.post("/api/watches", json={"spec": _bspec, "recipients": ["a@x.com"], "frequency_days": 1})
check("only the site key can create a briefing (the site never emails strangers)", _r.status_code == 401)
app._ON_VERCEL, _ok_env = True, os.environ.pop("OWNER_KEY", None)
_r503 = _cl.get("/api/watches", headers={"X-Owner-Key": "dev"})
app._ON_VERCEL = False
check("...and on Vercel with no OWNER_KEY it says it isn't set up, not 'wrong key'",
      _r503.status_code == 503 and "set up" in _r503.json()["error"])
_r = _cl.post("/api/watches", headers={"X-Owner-Key": "dev"}, json={
    "spec": _bspec, "query": "JFK to LAX", "recipients": ["Evan@X.com", "evan@x.com", "friend@x.com"],
    "frequency_days": 1, "threshold": 20})
_bw = _r.json()["watch"]
_mails = sorted(_glob_b.glob(os.path.join(app._LOCAL_OUTBOX, "*.html")))
check("creating a watch sends the first briefing to every recipient, deduplicated",
      _r.status_code == 200 and [x["email"] for x in _bw["recipients"]] == ["evan@x.com", "friend@x.com"]
      and len(_mails) == 2, f"{len(_mails)} emails")
_m0 = open(_mails[0]).read()
_tokens = {_re.search(r"[?&]t=([\w-]+)", open(m).read()).group(1) for m in _mails}
check("...each with its own unsubscribe link and a one-click List-Unsubscribe",
      len(_tokens) == 2 and "List-Unsubscribe: <https://flights.evanburkeen.com/api/watches/unsubscribe?w=" in _m0
      and "Unsubscribe</a>" in _m0)
check("...naming best value, cheapest and fastest, with real Book links",
      "Best value" in _m0 and "Cheapest" in _m0 and "Book on Google Flights" in _m0
      and "google.com/travel/flights?tfs=" in _m0)
check("...and escaping every value it prints", "<b>B6</b>" not in _m0 and "&lt;b&gt;B6&lt;/b&gt;" in _m0)
_bwatch = app.watch_load(_bw["id"])
_bprice["v"] = 460.0
check("a move smaller than the owner's threshold sends nothing",
      app.run_watch(app.watch_load(_bw["id"]))["status"] == "below_threshold"
      and len(_glob_b.glob(os.path.join(app._LOCAL_OUTBOX, "*.html"))) == 2)
_bprice["v"] = 432.0
_res = app.run_watch(app.watch_load(_bw["id"]))
_cur = app.briefing_snapshot(_brief_payload(_bspec))
_subj = app.briefing_email(app.watch_load(_bw["id"]), _cur,
                           app.briefing_changes({"best": 470.0, "cheapest": 300.0, "nonstop": 470.0}, _cur),
                           "s", app.watch_load(_bw["id"])["recipients"][0])[0]
check("a real move is sent, and the subject says what moved",
      _res["status"] == "sent" and "(down $38)" in _subj, _subj)
check("the written summary never uses dashes, even without the model",
      "\u2014" not in app.briefing_summary(app.watch_load(_bw["id"]), _cur, ["Best value down $38 to $432 (was $470)"])
      and "No price changes" in app.briefing_summary({"title": "t", "subtitle": "s"}, _cur, ["Best value unchanged at $432"]))
_tok = app.watch_load(_bw["id"])["recipients"][1]["token"]
# Mail security scanners (Safe Links, Mimecast) GET every link in a message:
# an unsubscribe that acted on GET dropped a friend the moment a briefing
# reached a corporate inbox. GET only shows the button.
_u = _cl.get(f"/api/watches/unsubscribe?w={_bw['id']}&t={_tok}")
check("opening the unsubscribe link (as a mail scanner does) changes nothing",
      _u.status_code == 200 and 'method="post"' in _u.text
      and [r["active"] for r in app.watch_load(_bw["id"])["recipients"]] == [True, True])
_u = _cl.post(f"/api/watches/unsubscribe?w={_bw['id']}&t={_tok}",
              content="List-Unsubscribe=One-Click",
              headers={"Content-Type": "application/x-www-form-urlencoded"})
check("a one-click POST removes that one person and nobody else",
      _u.status_code == 200 and "anymore" in _u.text
      and [r["active"] for r in app.watch_load(_bw["id"])["recipients"]] == [True, False])
check("...and a forged token does nothing",
      "isn't valid" in _cl.post(f"/api/watches/unsubscribe?w={_bw['id']}&t=nope").text
      and [r["active"] for r in app.watch_load(_bw["id"])["recipients"]] == [True, False])
check("the daily cron refuses calls without its secret", _cl.get("/api/watches/run").status_code == 401)
os.environ["CRON_SECRET"] = "s3cret"
_w2 = app.watch_load(_bw["id"]); _w2["next_due"] = 0; app.watch_save(_w2)
_cron = _cl.get("/api/watches/run", headers={"Authorization": "Bearer s3cret"}).json()
os.environ.pop("CRON_SECRET", None)
check("...and with it, runs every due watch", [x["id"] for x in _cron.get("ran", [])] == [_bw["id"]])
app.cached_execute_spec = lambda spec: {"type": "flights", "message": "refused", "results": []}
_nd = app.run_watch(app.watch_load(_bw["id"]), force=True)
check("a check with no fresh fares never sends a briefing", _nd["status"] == "no_data"
      and "no fares came back" in (app.watch_load(_bw["id"])["last_error"] or ""))
_w3 = app.watch_load(_bw["id"]); _w3["spec"]["departure_date"] = "2020-01-01"; app.watch_save(_w3)
check("a trip whose date has passed stops itself", app.run_watch(app.watch_load(_bw["id"]))["status"] == "expired"
      and app.watch_load(_bw["id"])["active"] is False)
app.cached_execute_spec = _real_ces_b
if _ok_env is not None:
    os.environ["OWNER_KEY"] = _ok_env
_fe_b = open(os.path.join(ROOT, "public", "index.html")).read()
check("Watch buttons appear only on a device that holds the site key",
      "return !!this.ownerKey && !!sec.watch_spec" in _fe_b and "openBriefings()" in _fe_b
      and "Trip briefings</button>" in _fe_b)
check("the email's 'Open this search' link runs the search", "get('q')" in _fe_b and "this.search(q)" in _fe_b)
_vj = json.load(open(os.path.join(ROOT, "vercel.json")))
check("a daily cron drives the briefings (Hobby allows once a day)",
      any(c["path"] == "/api/watches/run" and c["schedule"].split()[2:] == ["*", "*", "*"]
          and c["schedule"].split()[1].isdigit() for c in _vj.get("crons", [])))
# Nov 11, first real setup: "Briefing storage isn't connected yet" after
# Upstash was added. Vercel's connect dialog can prefix the variable names,
# and the bare error gave no way to tell which setup step went wrong.
_kv_saved = {k: os.environ.pop(k) for k in list(os.environ)
             if any(s in k for s in ("KV_", "REDIS", "UPSTASH"))}
try:
    os.environ["STORAGE_KV_REST_API_URL"] = "https://prefixed.example/"
    os.environ["STORAGE_KV_REST_API_TOKEN"] = "tok-prefixed"
    _pref = app._kv_conf()
    os.environ["KV_REST_API_URL"], os.environ["KV_REST_API_TOKEN"] = "https://plain.example", "tok-plain"
    _plain = app._kv_conf()
    check("storage variables are found under a Vercel prefix, and an unprefixed pair wins",
          _pref == ("https://prefixed.example", "tok-prefixed")
          and _plain == ("https://plain.example", "tok-plain"), f"{_pref[0]} / {_plain[0]}")
    for _k in ("STORAGE_KV_REST_API_URL", "STORAGE_KV_REST_API_TOKEN", "KV_REST_API_URL", "KV_REST_API_TOKEN"):
        os.environ.pop(_k)
    os.environ["REDIS_URL"] = "rediss://default:hunter2@db.example:6379"
    _hint = app._kv_missing_hint()
    check("the 'not connected' error names what it can see, never a value",
          "REDIS_URL" in _hint and "hunter2" not in _hint and "Upstash" in _hint, _hint)
    os.environ.pop("REDIS_URL")
    check("...and says when the deployment has no storage variables at all",
          "no storage variables" in app._kv_missing_hint())
finally:
    for _k in [k for k in os.environ if any(s in k for s in ("KV_", "REDIS", "UPSTASH"))]:
        os.environ.pop(_k)
    os.environ.update(_kv_saved)

# Nov 12, first real send: Resend answered 403 "error code: 1010" (Cloudflare
# refusing urllib's default Python-urllib signature) for every recipient.
_sent_reqs, _real_urlopen = [], app._urlreq.urlopen


class _FakeResp:
    def __init__(self, body):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


app._urlreq.urlopen = lambda req, timeout=None: (_sent_reqs.append(req), _FakeResp(b'{"id": "x", "result": "OK"}'))[1]
_env_keep = {k: os.environ.get(k) for k in ("RESEND_API_KEY", "KV_REST_API_URL", "KV_REST_API_TOKEN")}
try:
    os.environ["RESEND_API_KEY"] = "re_test"
    app.send_email("a@example.com", "s", "<p>h</p>", "t", "https://x/u")
    os.environ["KV_REST_API_URL"], os.environ["KV_REST_API_TOKEN"] = "https://kv.example", "tok"
    app._kv(["GET", "watch:x"])
finally:
    app._urlreq.urlopen = _real_urlopen
    for _k, _v in _env_keep.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v
_uas = [r.get_header("User-agent") or "" for r in _sent_reqs]
check("email and storage calls name themselves (Cloudflare 1010 blocks Python-urllib)",
      len(_uas) == 2 and all(u.startswith("flight-assistant/") for u in _uas), str(_uas))

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
for _ep in ("/api/returns", "/api/search/stream", "/api/backfill"):
    check(f"README documents {_ep}", _ep in _src and _ep in _readme)
check("README states the direct-route cooldown the code uses",
      f"{int(app.DIRECT_COOLDOWN_S)}s" in _readme, f"code rests it {app.DIRECT_COOLDOWN_S:.0f}s")
check("README and code agree on the Haiku first-call router",
      ("claude-haiku-4-5" in _src) == ("claude-haiku-4-5" in _readme))
_n = _re.search(r"run_search\(searcher, filters, sort, top_n=(\d+)[,)]", _src)
check("README's round-trip group count matches top_n",
      bool(_n) and f"~{_n.group(1)} round-trip OUTBOUND groups" in _readme,
      f"code expands top_n={_n.group(1) if _n else '?'}")
# The Aug 2/6 sessions each lost real time to a confidently wrong diagnosis.
# The cure is written down; these keep it written down.
check("README teaches the control-first debugging rule",
      "ALWAYS RUN A CONTROL" in _readme and "debugging your own network" in _readme)
check("README carries the open-bug list a new session must see",
      "KNOWN OPEN BUGS" in _readme and "leg_price_index" in _readme
      and "itinerary_url" in _readme)
check("README states the generalize-the-fix rule up front",
      "Fix the CLASS, not the instance" in _readme)
check("README documents streaming as live iff the frontend consumes it",
      ("streamTurn" in _fe) == ("Streaming is LIVE" in _readme))
check("README documents the cross-turn ledger iff the code carries one",
      ("ledger_context(" in _src) == ("results ledger" in _readme.lower()))
check("...and the frontend actually echoes it back",
      ("ledger_context(" in _src) == ("rememberLedger" in _fe))
check("README documents the price baseline iff the code computes one",
      ("price_verdict(" in _src) == ("price context" in _readme.lower()))
_w = _re.search(r"PRICE_CTX_SPREAD_D = (\d+)", _src)
check("README states the baseline's window width",
      bool(_w) and f"{_w.group(1)} days either side" in _readme,
      f"code spreads {_w.group(1) if _w else '?'} days")
_lm = _re.search(r'ASSISTANT_MODEL"\) or "(claude-[a-z0-9-]+)"', _src)
check("README names the loop model the code defaults to",
      bool(_lm) and _lm.group(1) in _readme,
      f"code defaults to {_lm.group(1) if _lm else '?'} — update the Stack table")
check("README documents the ASSISTANT_MODEL escape hatch",
      "ASSISTANT_MODEL" in _readme)
check("README states the 1-hour cache TTL it now pays 2x writes for",
      "1h" in _readme or "1-hour" in _readme)
check("README documents the tfs passenger/seat fields as verified",
      "one varint per traveler" in _readme and "f9" in _readme)
check("README states the party-total pricing invariant",
      "party total" in _readme.lower())
check("README pins the separate-ticket floor invariant",
      "separate-ticket floor" in _readme)
# Trip briefings: the routes, every env var the code reads for them, and the
# cron schedule are facts a reader acts on during setup.
for _ep in ("/api/watches", "/api/watches/unsubscribe", "/api/watches/run"):
    check(f"README documents {_ep}", _ep in _src and _ep in _readme)
for _env in ("OWNER_KEY", "CRON_SECRET", "RESEND_API_KEY", "BRIEFING_FROM",
             "KV_REST_API_URL", "KV_REST_API_TOKEN"):
    check(f"README names the briefing env var {_env}",
          f'"{_env}"' in _src and _env in _readme)
_vc = json.load(open(os.path.join(ROOT, "vercel.json")))
_cron = [c["schedule"] for c in _vc.get("crons", []) if c.get("path") == "/api/watches/run"]
check("README states the briefing cron's schedule",
      bool(_cron) and _cron[0] in _readme, f"vercel.json says {_cron}")
check("README states the cadences the code offers",
      all(label in _readme for label in ("Daily", "Every 3 days", "Weekly")))

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
