#!/usr/bin/env python3
"""Build stations.js from the official Israeli public-transport GTFS feed.

Usage:
  python3 tools/stations_from_gtfs.py --download              # fetch the MOT feed
  python3 tools/stations_from_gtfs.py path/to/gtfs.zip        # use a local copy

Writes stations.js and tracks.js (unless --dry-run) and prints a report comparing the
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
        trips, shape_ids = set(), set()
        for t in rows(zf, "trips.txt"):
            if t["route_id"] in routes:
                trips.add(t["trip_id"])
                if t.get("shape_id"):
                    shape_ids.add(t["shape_id"])
        shapes = {}
        if shape_ids and "shapes.txt" in names:
            for sp in rows(zf, "shapes.txt"):  # large file: streamed
                if sp["shape_id"] in shape_ids:
                    shapes.setdefault(sp["shape_id"], []).append(
                        (int(sp["shape_pt_sequence"]), float(sp["shape_pt_lat"]), float(sp["shape_pt_lon"])))
        shapes = [[(lat, lon) for _, lat, lon in sorted(pts)] for pts in shapes.values()]
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
    return out, shapes, feed_date


# --- tracks: rail shapes, simplified and de-duplicated -----------------------

SIMPLIFY_M = 15      # Douglas-Peucker tolerance
CELL_M = 60          # grid used to drop shapes that run over already-kept track
STATION_ON_TRACK_M = 200


def to_xy(lat, lon, lat0):
    return lon * 111320 * math.cos(lat0 * math.pi / 180), lat * 110540


def simplify(pts, tol):
    lat0 = pts[0][0]
    xy = [to_xy(a, b, lat0) for a, b in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = xy[i], xy[j]
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy) or 1e-9
        best, bi = 0, None
        for k in range(i + 1, j):
            d = abs(dy * (xy[k][0] - x1) - dx * (xy[k][1] - y1)) / L
            if d > best:
                best, bi = d, k
        if bi is not None and best > tol:
            keep[bi] = True
            stack += [(i, bi), (bi, j)]
    return [p for p, k in zip(pts, keep) if k]


def dense(pts, step=20):
    """Points every ~step metres along the polyline (for coverage tests)."""
    out = [pts[0]]
    for (a1, b1), (a2, b2) in zip(pts, pts[1:]):
        n = max(1, int(dist(a1, b1, a2, b2) / step))
        out += [(a1 + (a2 - a1) * t / n, b1 + (b2 - b1) * t / n) for t in range(1, n + 1)]
    return out


def cell(lat, lon):
    x, y = to_xy(lat, lon, 31.5)
    return int(x // CELL_M), int(y // CELL_M)


def build_tracks(shapes, stations):
    covered, tracks = set(), []
    for pts in sorted(shapes, key=len, reverse=True):
        if len(pts) < 2:
            continue
        d = dense(pts)
        if sum(cell(*p) in covered for p in d) >= 0.98 * len(d):
            continue  # this route runs entirely over track we already have
        for p in d:
            cx, cy = cell(*p)
            covered.update((cx + i, cy + j) for i in (-1, 0, 1) for j in (-1, 0, 1))
        line = simplify(pts, SIMPLIFY_M)
        # stations lying on this track, attached to their nearest vertex
        stops = []
        for si, (_, slat, slon) in enumerate(stations):
            vi, vd = min(((i, dist(slat, slon, a, b)) for i, (a, b) in enumerate(line)), key=lambda t: t[1])
            if vd <= STATION_ON_TRACK_M:
                stops.append((vi, si))
        stops.sort()
        tracks.append((line, stops))
    return tracks


def write_tracks_js(path, tracks, feed_date):
    lines = [
        "// מסלולי המסילה מתוך shapes.txt של ה-GTFS הרשמי"
        + (f" (גרסה {feed_date})" if feed_date else "") + ", מפושטים לדיוק ~15 מ׳.",
        "// p: קו רוחב/אורך לסירוגין. s: [אינדקס נקודה במסלול, אינדקס תחנה ב-STATIONS].",
        "// נוצר אוטומטית: python3 tools/stations_from_gtfs.py --download",
        "const TRACKS = [",
    ]
    for line, stops in tracks:
        flat = ",".join(f"{a:.5f},{b:.5f}" for a, b in line)
        st = ",".join(f"[{v},{s}]" for v, s in stops)
        lines.append(f"{{p:[{flat}],s:[{st}]}},")
    lines.append("];")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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

    new, shapes, feed_date = rail_stations(zip_path)
    out = Path(a.out)
    report(read_current(out) if out.exists() else [], new)
    tracks = build_tracks(shapes, new)
    print(f"\nTracks: {len(shapes)} rail shapes in feed, {len(tracks)} kept after de-duplication, "
          f"{sum(len(t[0]) for t in tracks)} points")
    if not a.dry_run:
        write_js(out, new, feed_date)
        print(f"Wrote {len(new)} stations to {out}")
        if tracks:
            tracks_out = out.with_name("tracks.js")
            write_tracks_js(tracks_out, tracks, feed_date)
            print(f"Wrote tracks to {tracks_out}")


if __name__ == "__main__":
    main()
