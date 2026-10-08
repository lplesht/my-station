#!/usr/bin/env python3
"""Build stations.js from the official Israeli public-transport GTFS feed.

Usage:
  python3 tools/stations_from_gtfs.py --download              # fetch the MOT feed
  python3 tools/stations_from_gtfs.py path/to/gtfs.zip        # use a local copy

Writes stations.js (unless --dry-run) and prints a report comparing the
new list with the current one: stations that moved, were added or removed.

Rail stops are found by following routes with route_type 2 (or the Israel
Railways agency) through trips and stop_times, so bus stops that happen to
be named after a station are not included.
"""
import argparse
import csv
import io
import json
import math
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

GTFS_URL = "https://gtfs.mot.gov.il/gtfsfiles/israel-public-transportation.zip"
RAIL_AGENCY_NAMES = ("רכבת ישראל", "israel railways")
ROOT = Path(__file__).resolve().parent.parent


def rows(zf, name):
    with zf.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))


def dist(lat1, lon1, lat2, lon2):
    r = math.pi / 180
    a = (math.sin((lat2 - lat1) * r / 2) ** 2 +
         math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin((lon2 - lon1) * r / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(a))


def clean_name(name):
    name = re.sub(r"^\s*(תחנת רכבת|ת\.\s*רכבת|רכבת)\s+", "", name.strip())
    return re.sub(r"\s+", " ", name.replace("-", " ").replace("–", " ")).strip()


def norm(name):
    return re.sub(r"[\s\"'״׳/\-–.]", "", clean_name(name))


def rail_stations(zip_path):
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        rail_agencies = {a["agency_id"] for a in rows(zf, "agency.txt")
                         if a.get("agency_name", "").strip().lower() in RAIL_AGENCY_NAMES}
        routes = {r["route_id"] for r in rows(zf, "routes.txt")
                  if r.get("route_type", "").strip() == "2" or r.get("agency_id") in rail_agencies}
        if not routes:
            sys.exit("No rail routes found in feed (route_type 2 / Israel Railways agency).")
        trips = {t["trip_id"] for t in rows(zf, "trips.txt") if t["route_id"] in routes}
        stop_ids = set()
        for st in rows(zf, "stop_times.txt"):  # large file: streamed, not loaded
            if st["trip_id"] in trips:
                stop_ids.add(st["stop_id"])
        stops = {s["stop_id"]: s for s in rows(zf, "stops.txt")}
        feed_date = ""
        if "feed_info.txt" in names:
            info = next(rows(zf, "feed_info.txt"), {})
            feed_date = info.get("feed_version") or info.get("feed_start_date", "")

    # platforms → their parent station when the feed has one
    stations = {}
    for sid in stop_ids:
        s = stops.get(sid)
        if not s:
            continue
        parent = s.get("parent_station") or ""
        if parent in stops:
            s = stops[parent]
        stations[s["stop_id"]] = s

    # one entry per station name (some stations have several stop_ids); average them
    by_name = {}
    for s in stations.values():
        by_name.setdefault(clean_name(s["stop_name"]), []).append(s)
    out = []
    for name, group in by_name.items():
        lat = sum(float(s["stop_lat"]) for s in group) / len(group)
        lon = sum(float(s["stop_lon"]) for s in group) / len(group)
        out.append((name, round(lat, 5), round(lon, 5)))
    out.sort(key=lambda s: -s[1])  # north to south
    return out, feed_date


def read_current(path):
    text = path.read_text(encoding="utf-8")
    return [(n, float(a), float(b)) for n, a, b in
            re.findall(r'\[\s*"([^"]+)"\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\]', text)]


def report(current, new):
    cur = {norm(n): (n, lat, lon) for n, lat, lon in current}
    matched = set()
    moved, added = [], []
    for name, lat, lon in new:
        key = norm(name)
        if key in cur:
            matched.add(key)
            on, olat, olon = cur[key]
            d = dist(olat, olon, lat, lon)
            moved.append((d, name, on))
        else:
            added.append(name)
    removed = [n for k, (n, _, _) in cur.items() if k not in matched]
    moved.sort(reverse=True)
    print(f"GTFS rail stations: {len(new)}   current list: {len(current)}")
    print(f"\nMatched by name ({len(moved)}), largest correction first:")
    for d, name, old in moved:
        flag = "  <-- far off" if d > 300 else ""
        label = name if name == old else f"{name}  (was: {old})"
        print(f"  {d:7.0f} m  {label}{flag}")
    print(f"\nIn GTFS but missing from current list ({len(added)}):")
    for n in added:
        print("  +", n)
    print(f"\nIn current list but not matched in GTFS ({len(removed)}) - check spelling or closure:")
    for n in removed:
        print("  -", n)


def write_js(path, stations, feed_date):
    lines = [
        "// רשימת תחנות רכבת ישראל: [שם, קו רוחב, קו אורך]",
        "// נוצר אוטומטית מקובץ ה-GTFS הרשמי של משרד התחבורה (gtfs.mot.gov.il)"
        + (f", גרסה {feed_date}" if feed_date else "") + ".",
        "// לעדכון: python3 tools/stations_from_gtfs.py --download",
        "const STATIONS = [",
    ]
    lines += [f"  [{json.dumps(n, ensure_ascii=False)}, {lat}, {lon}]," for n, lat, lon in stations]
    lines.append("];")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("zip", nargs="?", help="path to a GTFS zip")
    p.add_argument("--download", action="store_true", help=f"download {GTFS_URL}")
    p.add_argument("--out", default=str(ROOT / "stations.js"))
    p.add_argument("--dry-run", action="store_true", help="only print the report")
    a = p.parse_args()

    zip_path = a.zip
    if a.download:
        zip_path = str(ROOT / "gtfs.zip")
        print("Downloading", GTFS_URL, "...", file=sys.stderr)
        urllib.request.urlretrieve(GTFS_URL, zip_path)
    if not zip_path:
        p.error("give a GTFS zip path or --download")

    new, feed_date = rail_stations(zip_path)
    out = Path(a.out)
    report(read_current(out) if out.exists() else [], new)
    if not a.dry_run:
        write_js(out, new, feed_date)
        print(f"\nWrote {len(new)} stations to {out}")


if __name__ == "__main__":
    main()
