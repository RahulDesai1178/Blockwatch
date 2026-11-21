#!/usr/bin/env python3
"""BLOCKWATCH collector.

Runs on a schedule (GitHub Actions). Each run:
  1. discovers trending / top games from Roblox's explore charts,
  2. fetches live stats (players, visits, favorites, votes) for every tracked game,
  3. appends a row per game to history/YYYY-MM-DD.csv,
  4. rebuilds latest.json, series.json, events.json and status.json for the dashboard.

Only the Python standard library is used. Usage: python3 collect.py <data_dir>
"""
import csv, datetime as dt, glob, gzip, json, os, re, sys, time, traceback, uuid
import urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "data")
UA = "BLOCKWATCH-collector/1.0 (+https://github.com/RahulDesai1178/Blockwatch)"
RAW_KEEP_DAYS = 15          # full-resolution history kept this long; hourly rollups are kept forever
MAX_TRACKED = 900           # cap on games tracked per run
KEEP_SEEN_HOURS = 72        # keep tracking a game this long after it leaves the charts
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
TS = int(NOW.timestamp())
log_lines, errors = [], []


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    log_lines.append(s)


def err(where, e):
    msg = f"{where}: {type(e).__name__}: {e}"
    print("ERROR", msg, flush=True)
    errors.append(msg)


def get(url, params=None, tries=6):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return json.loads(body)
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504):
                wait = e.headers.get("Retry-After") if e.headers else None
                time.sleep(min(30, float(wait)) if wait and wait.isdigit() else 3 * 2 ** i)
                continue
            raise
        except Exception as e:  # network hiccup
            last = e
            time.sleep(2 ** i)
    raise last


def jload(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def jsave(path, obj, compact=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- discovery
def walk_games(node, out):
    """Collect every dict that looks like a game (has a universeId) anywhere in a JSON tree."""
    if isinstance(node, dict):
        uid = node.get("universeId") or node.get("UniverseId")
        if isinstance(uid, int) and uid > 0:
            out.append(node)
        for v in node.values():
            if isinstance(v, (dict, list)):
                walk_games(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_games(v, out)


EXPLORE = "https://apis.roblox.com/explore-api/v1"
DEVICE = {"device": "computer", "country": "all", "cpuCores": 8, "maxResolution": "1920x1080",
          "maxMemory": 8192, "networkType": "Wifi"}
WANT_SORTS = re.compile(r"top-playing|trending|up-and-coming|top-earning|revisit|popular|rising|top-rated|fun-with|hot", re.I)


def discover():
    """Returns {universeId: set(sortIds)} in chart order, plus chart ranks."""
    found, ranks, sorts_seen = {}, {}, []
    session = str(uuid.uuid4())

    def add(sort_id, games):
        for i, g in enumerate(games):
            uid = g.get("universeId")
            found.setdefault(uid, set()).add(sort_id)
            ranks.setdefault(sort_id, {}).setdefault(uid, len(ranks.get(sort_id, {})) + 1)

    token, pages = "", 0
    while pages < 6:
        params = dict(DEVICE, sessionId=session)
        if token:
            params["sortsPageToken"] = token
        try:
            data = get(EXPLORE + "/get-sorts", params)
        except Exception as e:
            err("explore get-sorts", e)
            break
        for s in data.get("sorts", []) or []:
            sid = s.get("sortId") or s.get("topicId") or s.get("sortDisplayName") or "unknown"
            sid = str(sid)
            sorts_seen.append(sid)
            gs = []
            walk_games(s, gs)
            add(sid, gs)
        token = data.get("nextSortsPageToken") or ""
        pages += 1
        if not token:
            break

    # page deeper into the most useful charts
    for sid in [s for s in dict.fromkeys(sorts_seen) if WANT_SORTS.search(s)]:
        ptoken, n = "", 0
        while n < 6:
            params = dict(DEVICE, sessionId=session, sortId=sid)
            if ptoken:
                params["pageToken"] = ptoken
            try:
                data = get(EXPLORE + "/get-sort-content", params)
            except Exception as e:
                err(f"explore get-sort-content {sid}", e)
                break
            gs = []
            walk_games(data.get("games", data), gs)
            if not gs:
                break
            add(sid, gs)
            ptoken = data.get("nextPageToken") or ""
            n += 1
            if not ptoken:
                break
    log(f"discovered {len(found)} games across {len(set(sorts_seen))} charts")
    return found, ranks, list(dict.fromkeys(sorts_seen))


# ---------------------------------------------------------------- details
def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_details(uids):
    games, votes = {}, {}
    _fetch(uids, games, votes)
    missing = [u for u in uids if u not in games or u not in votes]
    if missing:  # second pass for batches that were rate-limited
        time.sleep(5)
        _fetch(missing, games, votes)
    log(f"details for {len(games)} games, votes for {len(votes)}")
    return games, votes


def _fetch(uids, games, votes):
    for part in chunks(uids, 50):
        ids = ",".join(map(str, part))
        try:
            for g in get("https://games.roblox.com/v1/games", {"universeIds": ids}).get("data", []):
                games[g["id"]] = g
        except Exception as e:
            err("games.roblox.com/v1/games", e)
        try:
            for v in get("https://games.roblox.com/v1/games/votes", {"universeIds": ids}).get("data", []):
                votes[v["id"]] = v
        except Exception as e:
            err("games.roblox.com/v1/games/votes", e)
        time.sleep(0.8)


# ---------------------------------------------------------------- tagging
TAGS = jload(os.path.join(HERE, "tags.json"), {})
RULES = [(re.compile(p, re.I), t) for p, t in TAGS.get("rules", [])]
EMOJI = re.compile("[\U0001F000-\U0001FFFF☀-➿️‍⬀-⯿]")


def clean(name):
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)|【[^】]*】", " ", name or "")
    s = EMOJI.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or (name or "").strip()


def tag(name, g):
    """Categories for a game: manual override > Roblox subgenre > name keywords > Roblox genre."""
    key = clean(name).lower()
    if key in TAGS.get("overrides", {}):
        return TAGS["overrides"][key]
    out = []
    def add(t):
        if t and t not in out:
            out.append(t)
    add(TAGS.get("genre2", {}).get(g.get("genre_l2") or ""))
    for rx, t in RULES:
        if rx.search(key):
            add(t)
    if not out or out == ["anime"]:
        add(TAGS.get("genre1", {}).get(g.get("genre_l1") or ""))
    # an "anime" tag alone is a theme, not a game type
    if len(out) > 1 and out[0] == "anime":
        out = out[1:] + ["anime"]
    return out[:3] or ["other"]


UPD_RX = re.compile(r"\b(UPD\w*|UPDATE|EVENT|RELEASE|PATCH|PART|WEEK|SEASON|HALLOWEEN|CHRISTMAS|XMAS|WINTER|LIMITED|NEW)\b", re.I)


# ---------------------------------------------------------------- history
def day_file(d):
    return os.path.join(DATA, "history", f"{d:%Y-%m-%d}.csv")


def append_history(rows):
    path = day_file(NOW.date())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ts", "uid", "playing"])
        for uid, playing in rows:
            w.writerow([TS, uid, playing])


def load_history(days):
    """{uid: [(ts, playing), ...]} sorted by ts, for the last `days` days."""
    hist = {}
    for i in range(days, -1, -1):
        p = day_file((NOW - dt.timedelta(days=i)).date())
        if not os.path.exists(p):
            continue
        with open(p, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    hist.setdefault(int(r["uid"]), []).append((int(r["ts"]), int(r["playing"])))
                except Exception:
                    pass
    for v in hist.values():
        v.sort()
    return hist


def prune_and_rollup():
    """Roll finished days into hourly/YYYY-MM.csv (avg + max per hour), then delete old raw days."""
    state = jload(os.path.join(DATA, "state.json"), {})
    rolled = set(state.get("rolled_days", []))
    for p in sorted(glob.glob(os.path.join(DATA, "history", "*.csv"))):
        day = os.path.basename(p)[:-4]
        if day >= f"{NOW:%Y-%m-%d}":
            continue
        if day not in rolled:
            buckets = {}
            with open(p, newline="") as f:
                for r in csv.DictReader(f):
                    try:
                        h = int(r["ts"]) // 3600 * 3600
                        b = buckets.setdefault((h, int(r["uid"])), [0, 0, 0])
                        v = int(r["playing"]); b[0] += v; b[1] += 1; b[2] = max(b[2], v)
                    except Exception:
                        pass
            out = os.path.join(DATA, "hourly", day[:7] + ".csv")
            os.makedirs(os.path.dirname(out), exist_ok=True)
            new = not os.path.exists(out)
            with open(out, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["hour", "uid", "avg", "max"])
                for (h, uid), (s, n, mx) in sorted(buckets.items()):
                    w.writerow([h, uid, round(s / n), mx])
            rolled.add(day)
        age = (NOW.date() - dt.date.fromisoformat(day)).days
        if age > RAW_KEEP_DAYS:
            os.remove(p)
    cutoff = f"{NOW - dt.timedelta(days=RAW_KEEP_DAYS + 5):%Y-%m-%d}"
    state["rolled_days"] = sorted(d for d in rolled if d >= cutoff)
    jsave(os.path.join(DATA, "state.json"), state, compact=False)


def daily_peaks(days=60):
    """{uid: {YYYY-MM-DD: max}} from hourly rollups."""
    out = {}
    start = (NOW - dt.timedelta(days=days)).timestamp()
    for p in sorted(glob.glob(os.path.join(DATA, "hourly", "*.csv")))[-3:]:
        with open(p, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    h = int(r["hour"])
                    if h < start:
                        continue
                    d = dt.datetime.fromtimestamp(h, dt.timezone.utc).strftime("%Y-%m-%d")
                    u = out.setdefault(int(r["uid"]), {})
                    u[d] = max(u.get(d, 0), int(r["max"]))
                except Exception:
                    pass
    return out


def nearest(series, target, tol):
    best = None
    for ts, v in series:
        d = abs(ts - target)
        if d <= tol and (best is None or d < best[0]):
            best = (d, v)
    return None if best is None else best[1]


def pct(now, then):
    if then is None or then <= 0:
        return None
    return round((now - then) / then * 100, 1)


# ---------------------------------------------------------------- main
def main():
    registry = jload(os.path.join(DATA, "registry.json"), {})        # uid -> info that persists
    prev = jload(os.path.join(DATA, "latest.json"), {})
    prev_games = {g["id"]: g for g in prev.get("games", [])}

    found, chart_ranks, sorts = discover()

    # who to track: everything on the charts now + anything seen recently
    track = list(found.keys())
    cutoff = TS - KEEP_SEEN_HOURS * 3600
    recent = sorted(((int(u), r) for u, r in registry.items() if r.get("lastChart", 0) >= cutoff and int(u) not in found),
                    key=lambda x: -x[1].get("peak", 0))
    track += [u for u, _ in recent]
    track = track[:MAX_TRACKED]
    if not track:
        raise SystemExit("no games to track: discovery failed and registry is empty")

    details, votes = fetch_details(track)
    if not details:
        raise SystemExit("games API returned nothing")

    append_history([(uid, g.get("playing") or 0) for uid, g in details.items()])
    prune_and_rollup()
    hist = load_history(8)
    peaks = daily_peaks(60)

    games, series = [], {"start": None, "step": 3600, "games": {}}
    h_end = TS // 3600 * 3600
    h_start = h_end - 7 * 24 * 3600
    series["start"] = h_start
    nh = (h_end - h_start) // 3600 + 1

    for uid, g in details.items():
        name = g.get("name") or ""
        playing = g.get("playing") or 0
        v = votes.get(uid, {})
        up, down = v.get("upVotes") or 0, v.get("downVotes") or 0
        s = hist.get(uid, [])
        r = registry.setdefault(str(uid), {"firstSeen": TS})
        r["name"] = clean(name)
        if uid in found:
            r["lastChart"] = TS
        if playing > r.get("peak", 0):
            r["peak"], r["peakTs"] = playing, TS

        # hourly series for the last 7 days (average of samples in each hour)
        buckets = [None] * nh
        acc = {}
        for ts, val in s:
            if ts >= h_start:
                i = (ts - h_start) // 3600
                a = acc.setdefault(i, [0, 0]); a[0] += val; a[1] += 1
        for i, (tot, n) in acc.items():
            if 0 <= i < nh:
                buckets[i] = round(tot / n)
        series["games"][str(uid)] = buckets

        last24 = [val for ts, val in s if ts >= TS - 86400]
        last7 = [val for ts, val in s if ts >= TS - 7 * 86400]
        # time-of-day profile: average players per UTC hour over last 7 days
        prof = {}
        for ts, val in s:
            if ts >= TS - 7 * 86400:
                hr = (ts // 3600) % 24
                a = prof.setdefault(hr, [0, 0]); a[0] += val; a[1] += 1
        peak_hour = max(prof, key=lambda h: prof[h][0] / prof[h][1]) if len(prof) >= 12 else None
        low_hour = min(prof, key=lambda h: prof[h][0] / prof[h][1]) if len(prof) >= 12 else None

        updated = g.get("updated")
        pg = prev_games.get(uid)
        tags = tag(name, g)
        games.append({
            "id": uid, "placeId": g.get("rootPlaceId"), "name": clean(name), "raw": name,
            "creator": (g.get("creator") or {}).get("name") or "", "creatorType": (g.get("creator") or {}).get("type"),
            "verified": bool((g.get("creator") or {}).get("hasVerifiedBadge")),
            "playing": playing, "visits": g.get("visits") or 0, "favs": g.get("favoritedCount") or 0,
            "up": up, "down": down, "like": round(up / (up + down) * 100, 1) if up + down else None,
            "maxPlayers": g.get("maxPlayers"), "genre": g.get("genre"), "genre1": g.get("genre_l1"), "genre2": g.get("genre_l2"),
            "created": g.get("created"), "updated": updated, "cats": tags,
            "event": bool(UPD_RX.search(" ".join(re.findall(r"\[[^\]]*\]|\([^)]*\)|【[^】]*】", name)))),
            "charts": sorted(found.get(uid, [])),
            "chartRank": {k: chart_ranks[k][uid] for k in chart_ranks if uid in chart_ranks[k]},
            "d1h": pct(playing, nearest(s, TS - 3600, 20 * 60)),
            "d24h": pct(playing, nearest(s, TS - 86400, 75 * 60)),
            "d7d": pct(playing, nearest(s, TS - 7 * 86400, 4 * 3600)),
            "peak24h": max(last24) if last24 else playing, "low24h": min(last24) if last24 else playing,
            "peak7d": max(last7) if last7 else playing,
            "peakHourUTC": peak_hour, "lowHourUTC": low_hour,
            "allTimePeak": r.get("peak"), "allTimePeakTs": r.get("peakTs"), "firstSeen": r.get("firstSeen"),
            "samples": len(s),
            "daily": [[d, m] for d, m in sorted(peaks.get(uid, {}).items())][-60:],
            "_prevUpdated": pg.get("updated") if pg else None, "_prevPlaying": pg.get("playing") if pg else None,
            "_prevRank": pg.get("rank") if pg else None,
        })

    games.sort(key=lambda x: -x["playing"])
    for i, g in enumerate(games):
        g["rank"] = i + 1

    # ---------------- events
    events = jload(os.path.join(DATA, "events.json"), [])
    tracked_for = TS - min((r.get("firstSeen", TS) for r in registry.values()), default=TS)
    new_ev = []

    def ev(kind, g, text, sev):
        # don't repeat the same kind of alert for the same game within 6 hours
        if any(e["id"] == g["id"] and e["kind"] == kind and TS - e["ts"] < 6 * 3600 for e in new_ev + events[:400]):
            return
        new_ev.append({"ts": TS, "kind": kind, "id": g["id"], "name": g["name"], "text": text, "sev": sev,
                       "playing": g["playing"]})

    for g in games:
        p, pp = g["playing"], g["_prevPlaying"]
        if g["_prevUpdated"] and g["updated"] and g["updated"] != g["_prevUpdated"] and p >= 1000:
            ev("UPDATE", g, f"{g['name']} just published an update", "in")
        if pg_rank := g["_prevRank"]:
            for cut in (10, 25, 50):
                if g["rank"] <= cut < pg_rank:
                    ev("RANK", g, f"{g['name']} entered the top {cut} (now #{g['rank']})", "up"); break
        elif tracked_for > 3 * 3600 and g["charts"] and p >= 2000:
            ev("NEW", g, f"{g['name']} appeared on the charts with {p:,} players", "hi")
        if g["d1h"] is not None and p >= 3000 and g["d1h"] >= 30:
            ev("SURGE", g, f"{g['name']} up {g['d1h']:.0f}% in the last hour", "hi")
        if g["d1h"] is not None and p >= 5000 and g["d1h"] <= -30:
            ev("DROP", g, f"{g['name']} down {abs(g['d1h']):.0f}% in the last hour", "dn")
        if pp:
            for m in (1_000_000, 500_000, 250_000, 100_000, 50_000):
                if pp < m <= p:
                    ev("MILESTONE", g, f"{g['name']} passed {m:,} players", "up"); break
        if tracked_for > 2 * 86400 and pp and p == g["allTimePeak"] and pp < p and p >= 10000 and g["samples"] > 200:
            ev("PEAK", g, f"{g['name']} hit a new tracked peak: {p:,}", "hi")
    events = (new_ev + events)[:600]
    for g in games:
        for k in ("_prevUpdated", "_prevPlaying", "_prevRank"):
            g.pop(k, None)

    total = sum(g["playing"] for g in games)
    plat = jload(os.path.join(DATA, "platform.json"), [])
    plat.append([TS, total, len(games)])
    plat = [x for x in plat if x[0] >= TS - 30 * 86400]

    jsave(os.path.join(DATA, "latest.json"), {
        "ts": TS, "iso": NOW.isoformat(), "total": total, "count": len(games), "charts": sorts,
        "trackingSince": min((r.get("firstSeen", TS) for r in registry.values()), default=TS),
        "games": games})
    jsave(os.path.join(DATA, "series.json"), series)
    jsave(os.path.join(DATA, "events.json"), events)
    jsave(os.path.join(DATA, "platform.json"), plat)
    jsave(os.path.join(DATA, "registry.json"), registry)
    log(f"saved {len(games)} games, total {total:,} players, {len(new_ev)} new events")


if __name__ == "__main__":
    t0 = time.time()
    ok = True
    try:
        main()
    except SystemExit as e:
        ok = False
        err("fatal", e)
    except Exception as e:
        ok = False
        err("crash", e)
        traceback.print_exc()
    os.makedirs(DATA, exist_ok=True)
    jsave(os.path.join(DATA, "status.json"), {"ts": TS, "iso": NOW.isoformat(), "ok": ok,
          "seconds": round(time.time() - t0, 1), "errors": errors[-30:], "log": log_lines[-30:]}, compact=False)
    sys.exit(0 if ok else 1)
