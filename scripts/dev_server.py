"""Local dev server: real search backend + static frontend on :8123.

Run:  .venv/bin/python scripts/dev_server.py

If ANTHROPIC_API_KEY is absent, the Claude assistant is stubbed with a tiny
pattern parser so the UI and search plumbing can be exercised without the LLM:
  - "JFK to ORD"                -> one-way (codes required)
  - "... round ..."             -> round trip
  - "... flex/weekend ..."      -> flexible-dates grid
  - "compare ..."               -> two-option comparison
  - "multi ... AAA BBB CCC"     -> multi-city
"""
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))
import index as app_mod  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

if not os.environ.get("ANTHROPIC_API_KEY"):

    def stub_parse(query: str):
        q = query.lower()
        codes = [c for c in re.findall(r"\b([A-Z]{3})\b", query.upper())
                 if hasattr(app_mod.Airport, c)]
        if len(codes) < 2:
            return []
        d1 = (date.today() + timedelta(days=30)).isoformat()
        d2 = (date.today() + timedelta(days=34)).isoformat()
        spec = {
            "origins": [codes[0]], "destinations": [codes[1]],
            "trip_type": "round_trip" if "round" in q else "one_way",
            "departure_date": d1, "return_date": d2, "sort": "cheapest",
            "assumptions": ["STUB PARSER (no API key): dates set ~30 days out"],
            "summary": f"{codes[0]} to {codes[1]}" + (" round trip" if "round" in q else " one way"),
        }
        if "flex" in q or "weekend" in q:
            spec["flexible_dates"] = {
                "from_date": (date.today() + timedelta(days=20)).isoformat(),
                "to_date": (date.today() + timedelta(days=50)).isoformat(),
            }
            spec["departure_date"] = None
        if "multi" in q and len(codes) >= 3:
            return [{
                "trip_type": "multi_city", "sort": "cheapest",
                "summary": "STUB multi-city: " + " -> ".join(codes[:4]),
                "assumptions": ["STUB PARSER"],
                "multi_city_segments": [
                    {"origins": [codes[i]], "destinations": [codes[i + 1]],
                     "date": (date.today() + timedelta(days=30 + i * 4)).isoformat()}
                    for i in range(min(len(codes), 4) - 1)
                ],
            }]
        if "compare" in q:
            return [
                {**spec, "summary": "Option A: " + spec["summary"]},
                {**spec, "summary": "Option B: arrive by 2pm",
                 "arrival_time": {"earliest": None, "latest": 14}},
            ]
        return [spec]

    def stub_run_assistant(query: str, history=None, emit=None):
        emit = emit or (lambda *_: None)
        specs = stub_parse(query)
        if not specs:
            return {"message": "STUB: give me two airport codes, e.g. 'JFK to ORD'.", "sections": []}
        sections = [app_mod.cached_execute_spec(s) for s in specs]
        emit("sections", [s for s in sections if s.get("results") or s.get("dates")])
        # imitate token-by-token delivery so the streaming UI can be exercised
        for word in ("STUB ASSISTANT (no API key): results above arrived first, "
                     "then this prose streamed in word by word.").split(" "):
            emit("text_delta", word + " ")
            time.sleep(0.04)
        return {
            "message": f"STUB ASSISTANT (no API key): ran {len(sections)} search(es). "
                       "With a real key, Claude summarizes these conversationally.",
            "sections": sections,
            "suggestions": ["check Saturday instead", "only nonstops", "make it round trip"],
        }

    app_mod.run_assistant = stub_run_assistant
    print(">> run_assistant STUBBED (no ANTHROPIC_API_KEY)")

app = app_mod.app
app.mount("/", StaticFiles(directory=str(ROOT / "public"), html=True))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8123)
