# MLB ABS Challenge Tracker

A daily-updating dashboard for MLB's 2026 Automated Ball-Strike (ABS) challenge system.

Live site: https://jasonbhorne.github.io/abs-tracker/

It answers three questions and keeps re-answering them as the season accumulates:

1. Which umpires get overturned the most and least?
2. Which teams are best and worst at winning their challenges?
3. Does challenge success correlate with team success (win%, ERA, run differential)?

## How it works

- `update.py` (standard library only) pulls the data and writes `docs/data.json`:
  - Team and player challenge aggregates come from the
    [Baseball Savant ABS leaderboard](https://baseballsavant.mlb.com/leaderboard/abs-challenges) (CSV).
  - Per-umpire overturn rates are built here, because Savant does not publish a
    per-umpire leaderboard. Each challenge is read from the Savant game feed
    (`gf?game_pk=`) and matched to that game's home-plate umpire from the
    [MLB Stats API](https://statsapi.mlb.com). Results accumulate in `data/challenges.csv`.
  - Standings, ERA, and run differential come from the MLB Stats API, joined to
    Savant on team abbreviation.
- `docs/` is a static site (vanilla JS + Chart.js) served by GitHub Pages.
- `.github/workflows/update.yml` runs daily at 11:00 UTC, regenerates the data,
  commits the changes, and Pages redeploys.

## Reading the numbers

- Umpire overturn rate mixes the umpire's accuracy with how selectively teams chose to
  challenge that crew. Raw counts are shown alongside, and umpires under 20 challenges
  are flagged as low sample (toggle to show them).
- The success correlation is across 30 teams on a partial season. Treat the r-values as
  directional, not conclusive.

## Run locally

```
python3 update.py --days 3        # daily incremental
python3 -m http.server 8842 -d docs   # then open http://localhost:8842
```

Not affiliated with MLB. Data belongs to MLBAM.
