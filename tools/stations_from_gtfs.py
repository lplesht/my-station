#!/usr/bin/env python3
"""Build stations.js from the official Israeli public-transport GTFS feed.

Usage:
  python3 tools/stations_from_gtfs.py --download --osm        # MOT feed + OpenStreetMap tracks
  python3 tools/stations_from_gtfs.py path/to/gtfs.zip        # use a local GTFS copy, chord tracks
  python3 tools/stations_from_gtfs.py gtfs.zip --osm-file osm.json   # OSM data from a file

Writes stations.js and tracks.js (unless --dry-run) and prints a report comparing the
new list with the current one: stations that moved, were added or removed.

Track geometry: the MOT feed has no shapes, so tracks come from OpenStreetMap
(railway=rail, (c) OpenStreetMap contributors, ODbL). Which stations are adjacent comes from
the official stop_times; each adjacent pair is routed along the OSM rail network. Without
--osm, a straight chord between the two stations is used instead.

Rail stops are found by following routes with route_type 2 (or the Israel
Railways agency) through trips and stop_times, so bus stops that happen to
be named after a station are not included.
"""
import argparse
import csv
import heapq
import io
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
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
        trips, shape_ids, trip_cols = set(), set(), []
        for t in rows(zf, "trips.txt"):
            trip_cols = list(t)
            if t["route_id"] in routes:
                trips.add(t["trip_id"])
                if t.get("shape_id"):
                    shape_ids.add(t["shape_id"])
        shapes, shape_rows, shape_cols, shape_sample = {}, 0, [], []
        if shape_ids and "shapes.txt" in names:
            for sp in rows(zf, "shapes.txt"):  # large file: streamed
                shape_rows += 1
                if not shape_cols:
                    shape_cols = list(sp)
                if len(shape_sample) < 3:
                    shape_sample.append(sp["shape_id"])
                if sp["shape_id"] in shape_ids:
                    shapes.setdefault(sp["shape_id"], []).append(
                        (int(float(sp["shape_pt_sequence"])), float(sp["shape_pt_lat"]), float(sp["shape_pt_lon"])))
        shapes = [[(lat, lon) for _, lat, lon in sorted(pts)] for pts in shapes.values()]
        print("[diag] files in feed:", sorted(names))
        print(f"[diag] rail agencies: {sorted(rail_agencies)}  rail routes: {len(routes)}  rail trips: {len(trips)}")
        print(f"[diag] trips.txt columns: {trip_cols}")
        print(f"[diag] rail trips with a shape_id: {len(shape_ids)}  e.g. {sorted(shape_ids)[:3]}")
        print(f"[diag] shapes.txt rows: {shape_rows}  columns: {shape_cols}  first ids: {shape_sample}")
        print(f"[diag] rail shapes matched: {len(shapes)}")
        stop_ids, trip_stops = set(), {}
        for st in rows(zf, "stop_times.txt"):  # large file: streamed, not loaded
            if st["trip_id"] in trips:
                stop_ids.add(st["stop_id"])
                trip_stops.setdefault(st["trip_id"], []).append((int(float(st["stop_sequence"])), st["stop_id"]))
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

    # which stations follow each other on real train services (official data, no geometry needed)
    name_of = {}
    for sid in stop_ids:
        s = stops.get(sid)
        if s:
            parent = s.get("parent_station") or ""
            name_of[sid] = clean_name((stops[parent] if parent in stops else s)["stop_name"])
    pairs = set()
    for seq in trip_stops.values():
        names_in_order = []
        for _, sid in sorted(seq):
            n = name_of.get(sid)
            if n and (not names_in_order or names_in_order[-1] != n):
                names_in_order.append(n)
        pairs.update(tuple(sorted(p)) for p in zip(names_in_order, names_in_order[1:]))
    return out, shapes, pairs, feed_date


# --- tracks ------------------------------------------------------------------

SIMPLIFY_M = 10      # Douglas-Peucker tolerance for OSM geometry


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


CHORD_TOL_M = 500    # straight chords between stations only approximate the real track
SKIP_NEAR_M = 400    # drop A-C when a station B lies on the chord A-C (an express service skipping B)


DETOUR_RATIO = 1.5   # drop A-C when the rest of the network connects A to C within 1.5x of the chord


def prune_detours(edges, stations):
    """Drop segments that skip stations: fast trains link far-apart stations directly, but the
    track really passes through the stations in between. Longest segments are tested first."""
    def length(a, b):
        return dist(stations[a][1], stations[a][2], stations[b][1], stations[b][2])

    def shortest(es, a, b):
        adj = {}
        for x, y in es:
            w = length(x, y)
            adj.setdefault(x, []).append((y, w))
            adj.setdefault(y, []).append((x, w))
        best, heap = {a: 0}, [(0, a)]
        while heap:
            d, u = heapq.heappop(heap)
            if u == b:
                return d
            if d > best[u]:
                continue
            for v, w in adj.get(u, []):
                if d + w < best.get(v, float("inf")):
                    best[v] = d + w
                    heapq.heappush(heap, (d + w, v))
        return None

    for a, b in sorted(edges, key=lambda e: -length(*e)):
        rest = edges - {(a, b)}
        d = shortest(rest, a, b)
        if d is not None and d <= DETOUR_RATIO * length(a, b):
            edges = rest
    return sorted(edges)


def adjacent_pairs(pairs, stations):
    """Station pairs that follow each other on a train service, minus express-skip pairs."""
    index = {n: i for i, (n, _, _) in enumerate(stations)}
    segs = [tuple(sorted((index[a], index[b]))) for a, b in pairs if a in index and b in index and a != b]  # north first

    def on_chord(a, b, c):
        la, lo, lb, lob = stations[a][1], stations[a][2], stations[b][1], stations[b][2]
        lat0 = (la + lb) / 2
        ax, ay = to_xy(la, lo, lat0)
        bx, by = to_xy(lb, lob, lat0)
        cx, cy = to_xy(stations[c][1], stations[c][2], lat0)
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy or 1e-9
        t = ((cx - ax) * dx + (cy - ay) * dy) / L2
        return 0.05 < t < 0.95 and math.hypot(ax + dx * t - cx, ay + dy * t - cy) < SKIP_NEAR_M

    kept = [(a, b) for a, b in segs
            if not any(c not in (a, b) and on_chord(a, b, c) for c in range(len(stations)))]
    return prune_detours(set(kept), stations), len(segs)


def chord_track(a, b, stations):
    line = [(stations[a][1], stations[a][2]), (stations[b][1], stations[b][2])]
    return line, [(0, a), (1, b)], CHORD_TOL_M


# --- OpenStreetMap rail network ----------------------------------------------

OVERPASS_URLS = ["https://overpass-api.de/api/interpreter",
                 "https://overpass.kumi.systems/api/interpreter",
                 "https://overpass.private.coffee/api/interpreter"]
# main rail lines and the crossovers that join parallel tracks; no yards/sidings/spurs,
# no industrial or military branches
OVERPASS_QUERY = ('[out:json][timeout:240];'
                  'way["railway"="rail"]["service"!~"^(yard|siding|spur)$"]["usage"!~"industrial|military"]'
                  '(29.3,34.2,33.5,35.95);out geom;')
EXCLUDED_SERVICE = ("yard", "siding", "spur")
OSM_TOL_M = 200          # distance from an OSM track that counts as "on the railway"
SNAP_MAX_M = 300         # track segments this close to a station can start/end its routes
MAX_DETOUR = 1.8         # a routed path longer than this x the straight line is not trusted
MIN_COMPONENT = 200      # ignore disconnected scraps of track smaller than this (nodes)


def fetch_osm():
    data = ("data=" + urllib.parse.quote(OVERPASS_QUERY)).encode()
    last_err = None
    for url in OVERPASS_URLS:
        for attempt in range(2):
            try:
                print(f"[osm] POST {url} (attempt {attempt + 1})", file=sys.stderr)
                req = urllib.request.Request(url, data=data, headers={"User-Agent": "my-station-pwa/1.0"})
                with urllib.request.urlopen(req, timeout=300) as r:
                    return json.loads(r.read().decode("utf-8"))
            except (urllib.error.URLError, OSError, ValueError) as e:
                last_err = e
                print(f"[osm] failed: {e}", file=sys.stderr)
                time.sleep(5)
    sys.exit(f"OpenStreetMap download failed on every server: {last_err}")


def osm_graph(osm):
    nodes, adj, ways, edges = {}, {}, 0, []
    for el in osm.get("elements", []):
        if el.get("type") != "way" or not el.get("geometry") or any(g is None for g in el["geometry"]):
            continue
        tags = el.get("tags", {})
        if tags.get("railway") != "rail" or tags.get("service") in EXCLUDED_SERVICE \
                or tags.get("usage") in ("industrial", "military"):
            continue   # same filter as the query, in case the file came from elsewhere
        ways += 1
        ids = el["nodes"]
        for nid, g in zip(ids, el["geometry"]):
            nodes[nid] = (g["lat"], g["lon"])
        for u, v in zip(ids, ids[1:]):
            w = dist(*nodes[u], *nodes[v])
            adj.setdefault(u, []).append((v, w))
            adj.setdefault(v, []).append((u, w))
            edges.append((u, v))
    # keep only the big connected components: stray fragments would trap a station snap
    seen, keep = set(), set()
    for start in adj:
        if start in seen:
            continue
        comp, stack = [], [start]
        seen.add(start)
        while stack:
            u = stack.pop()
            comp.append(u)
            for v, _ in adj[u]:
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        if len(comp) >= MIN_COMPONENT:
            keep.update(comp)
    nodes = {n: c for n, c in nodes.items() if n in keep}
    adj = {u: [(v, w) for v, w in es if v in keep] for u, es in adj.items() if u in keep}
    edges = [(u, v) for u, v in edges if u in keep and v in keep]
    print(f"[osm] {ways} ways, {len(nodes)} track nodes, {len(edges)} segments in main network")
    return nodes, adj, edges


def near_segments(lat, lon, nodes, edges):
    """Where a station projects onto nearby track segments. OSM ways have sparse vertices on straight
    track, so snapping to the nearest *node* can be hundreds of metres off; snapping to the nearest
    point on a *segment* is exact. A station may sit between parallel tracks that only join further
    away, so every segment within SNAP_MAX_M is a candidate.
    Returns {graph node: (cost m, point)}: entering the graph at that node costs the walk station ->
    projection -> node, and the route polyline starts at the projection point."""
    k = math.cos(lat * math.pi / 180)
    kx, ky = 111320 * k, 110540
    out = {}
    for u, v in edges:
        (ua, ub), (va, vb) = nodes[u], nodes[v]
        if not (min(ua, va) - 0.003 <= lat <= max(ua, va) + 0.003 and min(ub, vb) - 0.004 <= lon <= max(ub, vb) + 0.004):
            continue
        ax, ay, bx, by = (ub - lon) * kx, (ua - lat) * ky, (vb - lon) * kx, (va - lat) * ky
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = max(0.0, min(1.0, -(ax * dx + ay * dy) / L2)) if L2 else 0.0
        px, py = ax + dx * t, ay + dy * t
        off = math.hypot(px, py)
        if off > SNAP_MAX_M:
            continue
        point = (lat + py / ky, lon + px / kx)
        for n, nx, ny in ((u, ax, ay), (v, bx, by)):
            cost = off + math.hypot(nx - px, ny - py)
            if n not in out or cost < out[n][0]:
                out[n] = (cost, point)
    return out


def route(adj, nodes, srcs, dsts):
    """Cheapest path from the graph entry nodes of one station to those of another.
    srcs/dsts: {node: (entry cost, projection point)}. Returns the polyline and its length."""
    best, prev, heap = {n: c for n, (c, _) in srcs.items()}, {}, [(c, n) for n, (c, _) in srcs.items()]
    heapq.heapify(heap)
    total, end = float("inf"), None
    while heap:
        d, u = heapq.heappop(heap)
        if d > total:
            break
        if d > best[u]:
            continue
        if u in dsts and d + dsts[u][0] < total:
            total, end = d + dsts[u][0], u
        for v, w in adj[u]:
            if d + w < best.get(v, float("inf")):
                best[v] = d + w
                prev[v] = u
                heapq.heappush(heap, (d + w, v))
    if end is None:
        return None, None
    path = [end]
    while path[-1] in prev:
        path.append(prev[path[-1]])
    path.reverse()
    line = [srcs[path[0]][1]] + [nodes[n] for n in path] + [dsts[end][1]]
    return line, sum(dist(*p, *q) for p, q in zip(line, line[1:]))


def osm_tracks(pairs, stations, osm):
    nodes, adj, edges = osm_graph(osm)
    near = {i: near_segments(lat, lon, nodes, edges) for i, (_, lat, lon) in enumerate(stations)}
    lonely = [stations[i][0] for i, nn in near.items() if not nn]
    closest = sorted((min(dist(stations[i][1], stations[i][2], *pt) for _, pt in nn.values()), stations[i][0])
                     for i, nn in near.items() if nn)[-5:]
    print(f"[osm] stations with NO track segment within {SNAP_MAX_M} m: {lonely}")
    print(f"[osm] stations furthest from any track segment (m): {[(n, round(d)) for d, n in closest]}")
    tracks, fallbacks, ratios = [], [], []
    for a, b in pairs:
        chord = dist(stations[a][1], stations[a][2], stations[b][1], stations[b][2])
        line, length = route(adj, nodes, near[a], near[b]) if near[a] and near[b] else (None, None)
        if line is None or length > MAX_DETOUR * chord:
            fallbacks.append((stations[a][0], stations[b][0],
                              "no path" if line is None else f"path {length / 1000:.1f} km vs chord {chord / 1000:.1f} km",
                              f"{len(near[a])}/{len(near[b])} start/end entries"))
            tracks.append(chord_track(a, b, stations))
            continue
        ratios.append((length / chord, stations[a][0], stations[b][0]))
        line = simplify(line, SIMPLIFY_M)
        tracks.append((line, [(0, a), (len(line) - 1, b)], OSM_TOL_M))
    ratios.sort(reverse=True)
    print(f"[osm] routed {len(ratios)} of {len(pairs)} segments along real track, {len(fallbacks)} fell back to a straight chord")
    for f in fallbacks:
        print("[osm]   chord fallback:", *f)
    print("[osm] most winding segments (path / straight line):",
          [f"{a}-{b} x{r:.2f}" for r, a, b in ratios[:6]])
    return tracks


def write_tracks_js(path, tracks, feed_date, source):
    lines = [
        f"// {source}" + (f" (גרסה {feed_date})" if feed_date else "") + ".",
        "// p: קו רוחב/אורך לסירוגין. s: [אינדקס נקודה, אינדקס תחנה ב-STATIONS] לשתי קצוות הקטע.",
        "// t: עד כמה מטרים מהקטע נחשבים \"על המסילה\".",
        "// נוצר אוטומטית: python3 tools/stations_from_gtfs.py --download --osm",
        "const TRACKS = [",
    ]
    for line, stops, tol in tracks:
        flat = ",".join(f"{a:.5f},{b:.5f}" for a, b in line)
        st = ",".join(f"[{v},{s}]" for v, s in stops)
        lines.append(f"{{p:[{flat}],s:[{st}],t:{tol}}},")
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
    p.add_argument("--osm", action="store_true", help="download rail tracks from OpenStreetMap (Overpass)")
    p.add_argument("--osm-file", help="use OSM data from this Overpass JSON file instead of downloading")
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

    new, shapes, pairs, feed_date = rail_stations(zip_path)
    out = Path(a.out)
    report(read_current(out) if out.exists() else [], new)
    if shapes:
        print(f"\nNote: the feed contains {len(shapes)} rail shapes, but they are not used (OSM tracks are).")

    kept, total = adjacent_pairs(pairs, new)
    print(f"\nAdjacent stations: {total} pairs from stop_times, {total - len(kept)} express/skip pairs dropped, {len(kept)} kept")
    if a.osm_file or a.osm:
        osm = json.loads(Path(a.osm_file).read_text(encoding="utf-8")) if a.osm_file else fetch_osm()
        tracks = osm_tracks(kept, new, osm)
        source = ("מסילות: © תורמי OpenStreetMap (ODbL), מנותבות בין כל שתי תחנות עוקבות; "
                  "התחנות והסדר ביניהן מה-GTFS הרשמי")
    else:
        tracks = [chord_track(a_, b_, new) for a_, b_ in kept]
        source = "קו ישר בין כל שתי תחנות עוקבות בשירות רכבת אמיתי (stop_times ב-GTFS הרשמי); אין נתוני מסילה"
    print(f"Tracks: {len(tracks)} segments, {sum(len(t[0]) for t in tracks)} points")
    if not a.dry_run:
        write_js(out, new, feed_date)
        print(f"Wrote {len(new)} stations to {out}")
        tracks_out = out.with_name("tracks.js")
        write_tracks_js(tracks_out, tracks, feed_date, source)
        print(f"Wrote tracks to {tracks_out}")


if __name__ == "__main__":
    main()
