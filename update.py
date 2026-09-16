#!/usr/bin/env python3
"""ABS Challenge Tracker -- daily data generator for the GitHub Pages dashboard.

Standard library only (no pip installs) so it runs fast and reliably in GitHub
Actions. Each run:
  1. downloads the Savant team/player/league challenge CSVs,
  2. extends the per-game umpire challenge ledger for recent dates (incremental),
  3. pulls standings + ERA,
  4. writes docs/data.json for the static dashboard.

Usage:
  python3 update.py            # daily run (umpires: last 3 days)
  python3 update.py --days 80  # widen the umpire backfill window
"""
import csv
import json
import re
import os
import sys
import time
import argparse
import datetime as dt
import urllib.request
import urllib.error
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
LATEST = os.path.join(DATA, "latest")
DOCS = os.path.join(HERE, "docs")
for d in (DATA, LATEST, DOCS):
    os.makedirs(d, exist_ok=True)

SEASON = 2026
OPENING_DAY = "2026-03-26"
CHALLENGES_CSV = os.path.join(DATA, "challenges.csv")
PROCESSED_CSV = os.path.join(DATA, "processed_games.csv")
TEAM_TS_CSV = os.path.join(DATA, "team_timeseries.csv")

SAVANT = "https://baseballsavant.mlb.com/leaderboard/abs-challenges?challengeType={t}"
GF = "https://baseballsavant.mlb.com/gf?game_pk={pk}"
SCHED = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={d}"
BOX = "https://statsapi.mlb.com/api/v1/game/{pk}/boxscore"
STANDINGS = "https://statsapi.mlb.com/api/v1/standings?leagueId=103,104&season={s}&standingsTypes=regularSeason"
TEAMSTATS = "https://statsapi.mlb.com/api/v1/teams/stats?sportId=1&season={s}&group=pitching&stats=season"
TEAMS = "https://statsapi.mlb.com/api/v1/teams?sportId=1&season={s}"

UA = {"User-Agent": "Mozilla/5.0 (abs-tracker dashboard)"}
SAVANT_TYPES = ["team-summary", "batter", "catcher", "pitcher", "league"]
CHAL_HEADER = ["game_pk", "date", "game_type", "play_id", "hp_umpire", "challenge_team_id",
               "challenger_type", "challenging_player_id", "challenging_player_name",
               "is_batter", "is_overturned", "edge_distance",
               "inning", "half_inning", "pre_balls", "pre_strikes", "call_name"]
# regular season + the four postseason rounds; excludes spring (S), all-star (A), exhibition (E)
REGULAR = {"R"}
POSTSEASON = {"F", "D", "L", "W"}
TS_COLS = ["date", "entity_name", "team_abbr",
           "n_challenges_off", "n_overturns_off", "rate_overturns_off",
           "n_challenges_def", "n_overturns_def", "rate_overturns_def"]
ROLES = ("batter", "catcher", "pitcher")
MIN_TEAM_CHAL = 8
MIN_UMP_CHAL = 20
MIN_PLAYER_CHAL = 5


def today():
    return dt.date.today().isoformat()


def fetch(url, tries=3, timeout=45):
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"fetch failed: {url}\n  {last}")


def fetch_json(url, **kw):
    return json.loads(fetch(url, **kw).decode("utf-8"))


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
ABSDATA_RE = re.compile(r"const absData\s*=\s*(\[.*?\]);", re.S)


def absdata(t):
    """Savant's ?csv=true export for this leaderboard returns HTTP 500 (broken
    upstream as of 2026-09-16). The same rows are embedded in the page as a
    `const absData = [...]` literal, so read them from there instead."""
    html = fetch(SAVANT.format(t=t)).decode("utf-8", "replace")
    m = ABSDATA_RE.search(html)
    if not m:
        raise RuntimeError(f"absData payload not found on page: {SAVANT.format(t=t)}")
    rows = json.loads(m.group(1))
    for r in rows:
        # the CSV called this column entity_name; the payload calls it player_name
        r.setdefault("entity_name", r.get("player_name", ""))
    return rows


def write_csv(path, rows):
    cols = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})


def snapshot():
    saved = {}
    for t in SAVANT_TYPES:
        rows = absdata(t)
        if not rows:
            raise RuntimeError(f"empty payload for challengeType={t}")
        write_csv(os.path.join(LATEST, f"{t}.csv"), rows)
        saved[t] = rows
    # idempotent per-day append to the time series, on a fixed column set so
    # history written under the old CSV schema stays aligned
    prior = []
    if os.path.exists(TEAM_TS_CSV):
        prior = [r for r in csv.DictReader(open(TEAM_TS_CSV, encoding="utf-8-sig"))
                 if r.get("date") and r["date"] != today()]
    with open(TEAM_TS_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=TS_COLS, extrasaction="ignore")
        w.writeheader()
        for r in prior:
            w.writerow({c: r.get(c, "") for c in TS_COLS})
        for r in saved["team-summary"]:
            row = {c: ("" if r.get(c) is None else r.get(c)) for c in TS_COLS}
            row["date"] = today()
            w.writerow(row)
    print(f"  snapshot: team {len(saved['team-summary'])}, batters {len(saved['batter'])}, "
          f"catchers {len(saved['catcher'])}, pitchers {len(saved['pitcher'])}")
    return saved


def load_processed():
    if not os.path.exists(PROCESSED_CSV):
        return set()
    return {r["game_pk"] for r in csv.DictReader(open(PROCESSED_CSV))}


def hp_umpire(pk):
    for o in fetch_json(BOX.format(pk=pk)).get("officials", []):
        if o.get("officialType") == "Home Plate":
            return o.get("official", {}).get("fullName", "")
    return ""


def extract_challenges(pk):
    seen = {}

    def walk(o):
        if isinstance(o, dict):
            if o.get("is_abs_challenge") and o.get("abs_challenge"):
                seen[o.get("play_id")] = o
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(fetch_json(GF.format(pk=pk)))
    out = []
    for pid, p in seen.items():
        ac = p.get("abs_challenge", {}) or {}
        cpid = ac.get("challenging_player_id", "")
        ctype = ac.get("challenging_player_type", "")
        # resolve the challenger's name: match the id against the play's three
        # named participants, falling back to the role if the id is missing
        name = ""
        for role in ("batter", "catcher", "pitcher"):
            if cpid and str(p.get(role, "")) == str(cpid):
                name = p.get(f"{role}_name", "")
                break
        if not name and ctype in ("batter", "catcher", "pitcher"):
            name = p.get(f"{ctype}_name", "")
        out.append({"play_id": pid, "challenge_team_id": ac.get("challenge_team_id", ""),
                    "challenger_type": ctype, "challenging_player_id": cpid,
                    "challenging_player_name": name,
                    "is_batter": ac.get("is_batter", ""),
                    "is_overturned": ac.get("is_overturned", ""),
                    "edge_distance": ac.get("edge_distance", ""),
                    "inning": p.get("inning", ""), "half_inning": p.get("half_inning", ""),
                    "pre_balls": p.get("pre_balls", ""), "pre_strikes": p.get("pre_strikes", ""),
                    "call_name": p.get("call_name", "")})
    return out


def daterange(start, end):
    s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    while s <= e:
        yield s.isoformat()
        s += dt.timedelta(days=1)


def umpires(start, end):
    done = load_processed()
    if not os.path.exists(CHALLENGES_CSV):
        with open(CHALLENGES_CSV, "w", newline="") as fh:
            csv.writer(fh).writerow(CHAL_HEADER)
    if not os.path.exists(PROCESSED_CSV):
        with open(PROCESSED_CSV, "w", newline="") as fh:
            csv.writer(fh).writerow(["game_pk", "date", "hp_umpire", "n_challenges"])
    ng = nc = 0
    for d in daterange(start, end):
        try:
            sched = fetch_json(SCHED.format(d=d))
        except RuntimeError as e:
            print(f"   ! schedule {d}: {e}")
            continue
        games = [(str(g["gamePk"]), g.get("gameType", ""))
                 for dd in sched.get("dates", []) for g in dd.get("games", [])
                 if "Final" in g.get("status", {}).get("detailedState", "")]
        for pk, gtype in games:
            if pk in done:
                continue
            try:
                ump, chs = hp_umpire(pk), extract_challenges(pk)
            except RuntimeError as e:
                print(f"   ! game {pk}: {e}")
                continue
            with open(CHALLENGES_CSV, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=CHAL_HEADER, extrasaction="ignore")
                for c in chs:
                    w.writerow({**c, "game_pk": pk, "date": d, "game_type": gtype, "hp_umpire": ump})
            with open(PROCESSED_CSV, "a", newline="") as fh:
                csv.writer(fh).writerow([pk, d, ump, len(chs)])
            done.add(pk)
            ng += 1
            nc += len(chs)
            time.sleep(0.25)
    print(f"  umpires: +{ng} games, +{nc} challenges (window {start}..{end})")


# --------------------------------------------------------------------------- #
def id_to_abbr():
    return {t["id"]: t.get("abbreviation") for t in fetch_json(TEAMS.format(s=SEASON))["teams"]}


def get_standings():
    id2abbr = id_to_abbr()
    teams = {}
    for rec in fetch_json(STANDINGS.format(s=SEASON)).get("records", []):
        for tr in rec.get("teamRecords", []):
            ab = id2abbr.get(tr["team"]["id"])
            teams[ab] = {"abbr": ab, "team": tr["team"]["name"], "wins": tr.get("wins"),
                         "losses": tr.get("losses"), "win_pct": f(tr.get("winningPercentage")),
                         "run_diff": tr.get("runDifferential")}
    for sp in fetch_json(TEAMSTATS.format(s=SEASON))["stats"][0]["splits"]:
        ab = id2abbr.get(sp["team"]["id"])
        if ab in teams:
            teams[ab]["era"] = f(sp["stat"].get("era"))
            teams[ab]["whip"] = f(sp["stat"].get("whip"))
    return teams


def pearson(xs, ys):
    pts = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pts)
    if n < 3:
        return None, n
    sx, sy = sum(p[0] for p in pts), sum(p[1] for p in pts)
    sxx = sum(p[0] ** 2 for p in pts)
    syy = sum(p[1] ** 2 for p in pts)
    sxy = sum(p[0] * p[1] for p in pts)
    den = ((n * sxx - sx ** 2) * (n * syy - sy ** 2)) ** 0.5
    return ((n * sxy - sx * sy) / den, n) if den else (None, n)


def load_ledger():
    if not os.path.exists(CHALLENGES_CSV):
        return []
    return list(csv.DictReader(open(CHALLENGES_CSV, encoding="utf-8-sig")))


def ovr(r):
    return 1 if str(r.get("is_overturned", "")).lower() == "true" else 0


SCOPES = {
    # postseason samples are far smaller, so the "enough data to rank" floors drop
    "regular":    {"types": REGULAR,    "label": "Regular season",
                   "min_ump": MIN_UMP_CHAL, "min_team": MIN_TEAM_CHAL,
                   "min_player": MIN_PLAYER_CHAL},
    "postseason": {"types": POSTSEASON, "label": "Postseason",
                   "min_ump": 3, "min_team": 2, "min_player": 2},
}


def scope_rows(rows, scope):
    ok = SCOPES[scope]["types"]
    return [r for r in rows if r.get("game_type") in ok]


def team_rows(rows, id2abbr, id2name):
    agg = defaultdict(lambda: {"off": [0, 0], "def": [0, 0]})
    for r in rows:
        tid = r.get("challenge_team_id")
        if not tid:
            continue
        try:
            ab = id2abbr.get(int(tid))
        except (TypeError, ValueError):
            ab = None
        if not ab:
            continue
        side = "off" if r.get("challenger_type") == "batter" else "def"
        a = agg[ab][side]
        a[0] += 1
        a[1] += ovr(r)
    out = []
    for ab, a in agg.items():
        co, oo = a["off"]
        cd, od = a["def"]
        C, O = co + cd, oo + od
        out.append({"team": id2name.get(ab, ab), "abbr": ab,
                    "chal_off": co, "chal_def": cd, "chal": C, "overturned": O,
                    "rate": (O / C if C else 0.0),
                    "rate_off": (oo / co if co else 0.0),
                    "rate_def": (od / cd if cd else 0.0)})
    return out


def umpire_rows(rows):
    agg = defaultdict(lambda: {"n": 0, "ovr": 0, "games": set()})
    for r in rows:
        u = r.get("hp_umpire") or "(unknown)"
        a = agg[u]
        a["n"] += 1
        a["games"].add(r["game_pk"])
        a["ovr"] += ovr(r)
    out = [{"umpire": u, "challenges": a["n"], "overturned": a["ovr"], "games": len(a["games"]),
            "rate": (a["ovr"] / a["n"] if a["n"] else 0.0)} for u, a in agg.items()]
    return sorted(out, key=lambda x: -x["rate"])


def player_rows(rows, role, id2abbr, min_chal):
    agg = defaultdict(lambda: {"n": 0, "ovr": 0, "name": "", "abbr": ""})
    for r in rows:
        if r.get("challenger_type") != role:
            continue
        pid = r.get("challenging_player_id") or r.get("challenging_player_name")
        if not pid:
            continue
        a = agg[pid]
        a["n"] += 1
        a["ovr"] += ovr(r)
        a["name"] = r.get("challenging_player_name") or a["name"]
        try:
            a["abbr"] = id2abbr.get(int(r["challenge_team_id"])) or a["abbr"]
        except (TypeError, ValueError):
            pass
    out = [{"name": a["name"], "team": a["abbr"], "challenges": a["n"],
            "overturned": a["ovr"], "rate": (a["ovr"] / a["n"] if a["n"] else 0.0)}
           for a in agg.values() if a["n"] >= min_chal]
    return sorted(out, key=lambda x: -x["rate"])[:25]


def challenger_profiles(rows, id2abbr):
    """Per-team role split (batter/catcher/pitcher) + success rate, and the single
    most-active named challenger per team. All from the ledger."""
    agg = defaultdict(lambda: {r: [0, 0] for r in ROLES})
    league = {r: [0, 0] for r in ROLES}
    named = defaultdict(lambda: {"n": 0, "ovr": 0, "name": "", "role": ""})
    for row in rows:
        tid = row.get("challenge_team_id")
        try:
            ab = id2abbr.get(int(tid)) if tid else None
        except (TypeError, ValueError):
            ab = None
        role = row.get("challenger_type")
        if not ab or role not in ROLES:
            continue
        o = ovr(row)
        agg[ab][role][0] += 1
        agg[ab][role][1] += o
        league[role][0] += 1
        league[role][1] += o
        pid = row.get("challenging_player_id")
        if pid:
            k = (ab, pid)
            named[k]["n"] += 1
            named[k]["ovr"] += o
            named[k]["name"] = row.get("challenging_player_name") or named[k]["name"]
            named[k]["role"] = role
    top = {}
    for (ab, _pid), a in named.items():
        cur = top.get(ab)
        if not cur or a["n"] > cur["challenges"]:
            top[ab] = {"name": a["name"], "role": a["role"], "challenges": a["n"],
                       "rate": (a["ovr"] / a["n"] if a["n"] else 0.0)}
    profiles = []
    for ab, roles in agg.items():
        total = sum(roles[r][0] for r in ROLES)
        profiles.append({"abbr": ab, "total": total,
                         "roles": {r: {"n": roles[r][0], "overturned": roles[r][1],
                                       "rate": (roles[r][1] / roles[r][0] if roles[r][0] else 0.0)}
                                   for r in ROLES},
                         "top": top.get(ab)})
    profiles.sort(key=lambda x: -x["total"])
    league_summary = {r: {"n": league[r][0], "overturned": league[r][1],
                          "rate": (league[r][1] / league[r][0] if league[r][0] else 0.0)}
                      for r in ROLES}
    return profiles, league_summary


def inning_rows(rows):
    """Challenge volume and success by inning; innings 10+ bucket as extras."""
    agg = defaultdict(lambda: {"n": 0, "ovr": 0, "batter_n": 0, "batter_ovr": 0,
                               "def_n": 0, "def_ovr": 0})
    for r in rows:
        try:
            inn = int(float(r["inning"]))
        except (TypeError, ValueError, KeyError):
            continue
        a = agg[min(inn, 10)]
        o = ovr(r)
        a["n"] += 1
        a["ovr"] += o
        side = "batter" if r.get("challenger_type") == "batter" else "def"
        a[side + "_n"] += 1
        a[side + "_ovr"] += o
    total = sum(a["n"] for a in agg.values())
    out = []
    for k in sorted(agg):
        a = agg[k]
        out.append({"inning": ("10+" if k == 10 else k),
                    "challenges": a["n"], "overturned": a["ovr"],
                    "rate": (a["ovr"] / a["n"] if a["n"] else 0.0),
                    "share": (a["n"] / total if total else 0.0),
                    "batter_n": a["batter_n"],
                    "batter_rate": (a["batter_ovr"] / a["batter_n"]) if a["batter_n"] else None,
                    "def_n": a["def_n"],
                    "def_rate": (a["def_ovr"] / a["def_n"]) if a["def_n"] else None})
    return out


def league_trend(rows):
    """Cumulative league overturn rate by date, straight from the ledger so it
    covers the whole season rather than only the days we happened to snapshot."""
    by = defaultdict(lambda: [0, 0])
    for r in rows:
        d = r.get("date")
        if not d:
            continue
        by[d][0] += 1
        by[d][1] += ovr(r)
    out, cc, co = [], 0, 0
    for d in sorted(by):
        c, o = by[d]
        cc += c
        co += o
        out.append({"date": d, "challenges": c, "overturned": o,
                    "rate": (o / c if c else 0.0),
                    "cum_challenges": cc, "cum_rate": (co / cc if cc else 0.0)})
    return out


def records(rows):
    """Single-game superlatives: the extremes a season wrap-up actually wants."""
    g = defaultdict(lambda: {"n": 0, "ovr": 0, "date": "", "ump": ""})
    tg = defaultdict(lambda: {"n": 0, "ovr": 0})
    pg = defaultdict(lambda: {"n": 0, "ovr": 0, "name": "", "role": ""})
    for r in rows:
        pk = r["game_pk"]
        a = g[pk]
        a["n"] += 1
        a["ovr"] += ovr(r)
        a["date"] = r.get("date", "")
        a["ump"] = r.get("hp_umpire", "")
        tid = r.get("challenge_team_id")
        if tid:
            b = tg[(pk, tid)]
            b["n"] += 1
            b["ovr"] += ovr(r)
        pid = r.get("challenging_player_id")
        if pid:
            c = pg[(pk, pid)]
            c["n"] += 1
            c["ovr"] += ovr(r)
            c["name"] = r.get("challenging_player_name") or c["name"]
            c["role"] = r.get("challenger_type", "")
    games = [dict(v, game_pk=k) for k, v in g.items()]
    if not games:
        return {}
    worst = sorted([x for x in games if x["n"] >= 5],
                   key=lambda a: (-a["ovr"] / a["n"], -a["n"]))[:5]
    most = sorted(games, key=lambda a: -a["ovr"])[:5]
    perfect = [x for x in games if x["ovr"] == 0]
    team_best = sorted([dict(v, game_pk=k[0], team_id=k[1]) for k, v in tg.items()],
                       key=lambda a: (-a["ovr"], -a["n"]))[:5]
    play_best = sorted([dict(v, game_pk=k[0]) for k, v in pg.items()],
                       key=lambda a: (-a["ovr"], -a["n"]))[:5]
    return {
        "worst_ump_games": worst,
        "most_overturns_games": most,
        "perfect_games_count": len(perfect),
        "total_games": len(games),
        "perfect_games_top": sorted(perfect, key=lambda a: -a["n"])[:5],
        "team_best_games": team_best,
        "player_best_games": play_best,
        "busiest_game": max(games, key=lambda a: a["n"]),
    }


def build_scope(rows, scope, id2abbr, id2name, standings):
    cfg = SCOPES[scope]
    teams = team_rows(rows, id2abbr, id2name)
    tot_c = sum(t["chal"] for t in teams)
    tot_o = sum(t["overturned"] for t in teams)
    profiles, role_league = challenger_profiles(rows, id2abbr)
    corr = None
    if scope == "regular":
        xs, win, era, rd = [], [], [], []
        for t in teams:
            st = standings.get(t["abbr"], {})
            t["win_pct"] = st.get("win_pct")
            t["era"] = st.get("era")
            t["run_diff"] = st.get("run_diff")
            xs.append(t["rate"])
            win.append(st.get("win_pct"))
            era.append(st.get("era"))
            rd.append(st.get("run_diff"))
        corr = {}
        for lab, ys in [("win_pct", win), ("era", era), ("run_diff", rd)]:
            r, n = pearson(xs, ys)
            corr[lab] = {"r": (round(r, 3) if r is not None else None), "n": n}
    return {
        "label": cfg["label"],
        "league": {"challenges": tot_c, "overturned": tot_o,
                   "rate": (tot_o / tot_c if tot_c else 0),
                   "per_team": round(tot_c / 30, 1) if tot_c else 0,
                   "games_logged": len({r["game_pk"] for r in rows})},
        "teams": sorted(teams, key=lambda x: -x["rate"]),
        "umpires": umpire_rows(rows),
        "players": {k: player_rows(rows, k, id2abbr, cfg["min_player"]) for k in ROLES},
        "correlation": corr,
        "trend": league_trend(rows),
        "challengers": profiles,
        "role_league": role_league,
        "innings": inning_rows(rows),
        "records": records(rows),
        "min_ump_chal": cfg["min_ump"],
        "min_team_chal": cfg["min_team"],
        "min_player_chal": cfg["min_player"],
    }


def build_json():
    ledger = load_ledger()
    id2abbr = id_to_abbr()
    standings = get_standings()
    id2name = {ab: v.get("team", ab) for ab, v in standings.items()}
    scopes = {k: build_scope(scope_rows(ledger, k), k, id2abbr, id2name, standings)
              for k in SCOPES}
    data = {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "season": SEASON,
        "scopes": scopes,
        "available": [k for k, v in scopes.items() if v["league"]["challenges"] > 0],
        "default_scope": "regular",
    }
    out = os.path.join(DOCS, "data.json")
    with open(out, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    reg = scopes["regular"]["league"]
    post = scopes["postseason"]["league"]
    print(f"  wrote {out}  (regular {reg['challenges']} chal @ {reg['rate']:.1%}, "
          f"postseason {post['challenges']} chal)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="umpire backfill window (days back)")
    ap.add_argument("--skip-umpires", action="store_true")
    args = ap.parse_args()
    print("ABS dashboard update", today())
    snapshot()
    if not args.skip_umpires:
        end = today()
        start = (dt.date.fromisoformat(end) - dt.timedelta(days=args.days)).isoformat()
        umpires(start, end)
    build_json()
    print("done")
