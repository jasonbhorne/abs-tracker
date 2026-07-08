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

SAVANT = "https://baseballsavant.mlb.com/leaderboard/abs-challenges?challengeType={t}&csv=true"
GF = "https://baseballsavant.mlb.com/gf?game_pk={pk}"
SCHED = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={d}"
BOX = "https://statsapi.mlb.com/api/v1/game/{pk}/boxscore"
STANDINGS = "https://statsapi.mlb.com/api/v1/standings?leagueId=103,104&season={s}&standingsTypes=regularSeason"
TEAMSTATS = "https://statsapi.mlb.com/api/v1/teams/stats?sportId=1&season={s}&group=pitching&stats=season"
TEAMS = "https://statsapi.mlb.com/api/v1/teams?sportId=1&season={s}"

UA = {"User-Agent": "Mozilla/5.0 (abs-tracker dashboard)"}
SAVANT_TYPES = ["team-summary", "batter", "catcher", "pitcher", "league"]
CHAL_HEADER = ["game_pk", "date", "play_id", "hp_umpire", "challenge_team_id",
               "challenger_type", "is_batter", "is_overturned", "edge_distance",
               "inning", "half_inning", "pre_balls", "pre_strikes", "call_name"]
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
def snapshot():
    saved = {}
    for t in SAVANT_TYPES:
        raw = fetch(SAVANT.format(t=t))
        with open(os.path.join(LATEST, f"{t}.csv"), "wb") as fh:
            fh.write(raw)
        saved[t] = list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
    # idempotent per-day append to the time series
    team = saved["team-summary"]
    cols = ["date"] + list(team[0].keys())
    prior = list(csv.reader(open(TEAM_TS_CSV))) if os.path.exists(TEAM_TS_CSV) else []
    body = [r for r in prior[1:] if r and r[0] != today()]
    with open(TEAM_TS_CSV, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerows(body)
        for r in team:
            w.writerow([today()] + list(r.values()))
    print(f"  snapshot: team {len(team)}, batters {len(saved['batter'])}, "
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
        out.append([pid, ac.get("challenge_team_id", ""), ac.get("challenging_player_type", ""),
                    ac.get("is_batter", ""), ac.get("is_overturned", ""), ac.get("edge_distance", ""),
                    p.get("inning", ""), p.get("half_inning", ""), p.get("pre_balls", ""),
                    p.get("pre_strikes", ""), p.get("call_name", "")])
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
        games = [str(g["gamePk"]) for dd in sched.get("dates", []) for g in dd.get("games", [])
                 if "Final" in g.get("status", {}).get("detailedState", "")]
        for pk in games:
            if pk in done:
                continue
            try:
                ump, chs = hp_umpire(pk), extract_challenges(pk)
            except RuntimeError as e:
                print(f"   ! game {pk}: {e}")
                continue
            with open(CHALLENGES_CSV, "a", newline="") as fh:
                w = csv.writer(fh)
                for c in chs:
                    w.writerow([pk, d, c[0], ump] + c[1:])
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


def team_rows():
    path = os.path.join(LATEST, "team-summary.csv")
    rows = []
    for r in csv.DictReader(open(path, encoding="utf-8-sig")):
        co, oo = f(r["n_challenges_off"]), f(r["n_overturns_off"])
        cd, od = f(r["n_challenges_def"]), f(r["n_overturns_def"])
        C, O = co + cd, oo + od
        rows.append({"team": r["entity_name"], "abbr": r["team_abbr"],
                     "chal_off": int(co), "chal_def": int(cd), "chal": int(C),
                     "overturned": int(O), "rate": (O / C if C else 0.0),
                     "rate_off": f(r["rate_overturns_off"]), "rate_def": f(r["rate_overturns_def"])})
    return rows


def umpire_rows():
    agg = defaultdict(lambda: {"n": 0, "ovr": 0, "games": set()})
    for r in csv.DictReader(open(CHALLENGES_CSV)):
        u = r["hp_umpire"] or "(unknown)"
        agg[u]["n"] += 1
        agg[u]["games"].add(r["game_pk"])
        if str(r["is_overturned"]).lower() == "true":
            agg[u]["ovr"] += 1
    out = [{"umpire": u, "challenges": a["n"], "overturned": a["ovr"], "games": len(a["games"]),
            "rate": (a["ovr"] / a["n"] if a["n"] else 0.0)} for u, a in agg.items()]
    return sorted(out, key=lambda x: -x["rate"])


def player_rows(kind):
    path = os.path.join(LATEST, f"{kind}.csv")
    rows = []
    for r in csv.DictReader(open(path, encoding="utf-8-sig")):
        c = f(r.get("n_challenges", 0))
        if c >= MIN_PLAYER_CHAL:
            rows.append({"name": r["entity_name"], "team": r["team_abbr"], "challenges": int(c),
                         "overturned": int(f(r.get("n_overturns", 0))), "rate": f(r.get("rate_overturns", 0))})
    return sorted(rows, key=lambda x: -x["rate"])[:25]


ROLES = ("batter", "catcher", "pitcher")


def challenger_profiles(id2abbr):
    """Per-team role split (batter/catcher/pitcher) + success rate, from the ledger,
    plus the single most-active named challenger per team, from the player CSVs."""
    # role split per team from the challenge ledger
    agg = defaultdict(lambda: {r: [0, 0] for r in ROLES})  # abbr -> role -> [n, overturned]
    league = {r: [0, 0] for r in ROLES}
    for row in csv.DictReader(open(CHALLENGES_CSV)):
        ab = id2abbr.get(int(row["challenge_team_id"])) if row["challenge_team_id"] else None
        role = row["challenger_type"]
        if not ab or role not in ROLES:
            continue
        ov = 1 if str(row["is_overturned"]).lower() == "true" else 0
        agg[ab][role][0] += 1
        agg[ab][role][1] += ov
        league[role][0] += 1
        league[role][1] += ov

    # most-active named challenger per team across the three player CSVs
    top = {}  # abbr -> {name, role, challenges, rate}
    for role in ROLES:
        path = os.path.join(LATEST, f"{role}.csv")
        if not os.path.exists(path):
            continue
        for r in csv.DictReader(open(path, encoding="utf-8-sig")):
            ab = r["team_abbr"]
            n = int(f(r.get("n_challenges", 0)))
            cur = top.get(ab)
            if not cur or n > cur["challenges"]:
                top[ab] = {"name": r["entity_name"], "role": role, "challenges": n,
                           "rate": f(r.get("rate_overturns", 0))}

    profiles = []
    for ab, roles in agg.items():
        total = sum(roles[r][0] for r in ROLES)
        prof = {"abbr": ab, "total": total,
                "roles": {r: {"n": roles[r][0], "overturned": roles[r][1],
                              "rate": (roles[r][1] / roles[r][0] if roles[r][0] else 0.0)}
                          for r in ROLES},
                "top": top.get(ab)}
        profiles.append(prof)
    profiles.sort(key=lambda x: -x["total"])
    league_summary = {r: {"n": league[r][0], "overturned": league[r][1],
                          "rate": (league[r][1] / league[r][0] if league[r][0] else 0.0)}
                      for r in ROLES}
    return profiles, league_summary


def inning_rows():
    """Challenge volume and success by inning; innings 10+ bucket as extras."""
    agg = defaultdict(lambda: {"n": 0, "ovr": 0, "batter_n": 0, "batter_ovr": 0,
                               "def_n": 0, "def_ovr": 0})
    for r in csv.DictReader(open(CHALLENGES_CSV)):
        try:
            inn = int(float(r["inning"]))
        except (TypeError, ValueError):
            continue
        a = agg[min(inn, 10)]
        ov = 1 if str(r["is_overturned"]).lower() == "true" else 0
        a["n"] += 1
        a["ovr"] += ov
        side = "batter" if r["challenger_type"] == "batter" else "def"
        a[side + "_n"] += 1
        a[side + "_ovr"] += ov
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


def league_trend():
    if not os.path.exists(TEAM_TS_CSV):
        return []
    by = {}
    for r in csv.DictReader(open(TEAM_TS_CSV)):
        d = r["date"]
        c = f(r.get("n_challenges_off")) + f(r.get("n_challenges_def"))
        o = f(r.get("n_overturns_off")) + f(r.get("n_overturns_def"))
        by.setdefault(d, [0, 0])
        by[d][0] += c
        by[d][1] += o
    return [{"date": d, "challenges": int(by[d][0]), "overturned": int(by[d][1]),
             "rate": (by[d][1] / by[d][0] if by[d][0] else 0)} for d in sorted(by)]


def build_json():
    teams = team_rows()
    umps = umpire_rows()
    id2abbr = id_to_abbr()
    standings = get_standings()
    profiles, role_league = challenger_profiles(id2abbr)
    tot_c = sum(t["chal"] for t in teams)
    tot_o = sum(t["overturned"] for t in teams)

    # merge standings into team rows + correlation
    xs, win, era, rd = [], [], [], []
    for t in teams:
        s = standings.get(t["abbr"], {})
        t["win_pct"] = s.get("win_pct")
        t["era"] = s.get("era")
        t["run_diff"] = s.get("run_diff")
        xs.append(t["rate"])
        win.append(s.get("win_pct"))
        era.append(s.get("era"))
        rd.append(s.get("run_diff"))
    corr = {}
    for lab, ys in [("win_pct", win), ("era", era), ("run_diff", rd)]:
        r, n = pearson(xs, ys)
        corr[lab] = {"r": (round(r, 3) if r is not None else None), "n": n}

    data = {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "season": SEASON,
        "league": {"challenges": tot_c, "overturned": tot_o,
                   "rate": (tot_o / tot_c if tot_c else 0), "per_team": round(tot_c / 30, 1),
                   "games_logged": len(load_processed())},
        "teams": sorted(teams, key=lambda x: -x["rate"]),
        "umpires": umps,
        "min_ump_chal": MIN_UMP_CHAL,
        "min_team_chal": MIN_TEAM_CHAL,
        "players": {k: player_rows(k) for k in ("batter", "catcher", "pitcher")},
        "correlation": corr,
        "trend": league_trend(),
        "challengers": profiles,
        "role_league": role_league,
        "innings": inning_rows(),
    }
    out = os.path.join(DOCS, "data.json")
    with open(out, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    print(f"  wrote {out}  (teams {len(teams)}, umpires {len(umps)}, "
          f"league {data['league']['rate']:.1%})")


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
