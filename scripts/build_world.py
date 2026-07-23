"""Regenerate public/world.js (map coastlines + lakes) from Natural Earth data.

Run:  .venv/bin/python scripts/build_world.py
Then bump the cache-buster in index.html (<script src="/world.js?v=N">).

Projection must match the frontend: x = lon + 180, y = 90 - lat, 1 decimal.
"""
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"


def to_path(geojson: dict) -> str:
    parts = []
    for feat in geojson["features"]:
        geom = feat["geometry"]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        for poly in polys:
            for ring in poly:
                pts = [(round(lon + 180, 1), round(90 - lat, 1)) for lon, lat in ring]
                dedup = [pts[0]]
                for p in pts[1:]:
                    if p != dedup[-1]:
                        dedup.append(p)
                if len(dedup) < 4:
                    continue
                parts.append(
                    "M" + f"{dedup[0][0]} {dedup[0][1]}"
                    + "L" + " ".join(f"{x} {y}" for x, y in dedup[1:]) + "Z"
                )
    return "".join(parts)


def fetch(name: str) -> dict:
    with urllib.request.urlopen(BASE + name) as r:
        return json.load(r)


if __name__ == "__main__":
    land = to_path(fetch("ne_110m_land.geojson"))
    lakes = to_path(fetch("ne_110m_lakes.geojson"))
    out = f'const WORLD_PATH = "{land}";\nconst LAKES_PATH = "{lakes}";\n'
    (ROOT / "public" / "world.js").write_text(out)
    print(f"world.js written: land {len(land)}B, lakes {len(lakes)}B")
