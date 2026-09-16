#!/usr/bin/env python3
"""One-time backfill: re-read every game in the ledger from the Savant game feed
to add game_type and challenger identity (challenging_player_id / _name), which
the original collector never stored.

Resumable: appends to data/challenges_enriched.csv and skips games already done.
Validates against the existing ledger before anything is swapped in.

  python3 backfill.py --limit 20   # smoke test
  python3 backfill.py              # full run
"""
import csv, json, os, sys, time, argparse
import update as U

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "challenges_enriched.csv")
TYPEMAP = os.path.join(HERE, "data", "game_types.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    old = list(csv.DictReader(open(U.CHALLENGES_CSV, encoding="utf-8-sig")))
    # game_pk -> (date, hp_umpire) straight from the existing ledger
    meta = {}
    for r in old:
        meta.setdefault(r["game_pk"], (r["date"], r["hp_umpire"]))
    gtypes = json.load(open(TYPEMAP)) if os.path.exists(TYPEMAP) else {}

    done = set()
    if os.path.exists(OUT):
        done = {r["game_pk"] for r in csv.DictReader(open(OUT, encoding="utf-8-sig"))}
    else:
        with open(OUT, "w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=U.CHAL_HEADER).writeheader()

    todo = [pk for pk in meta if pk not in done]
    todo.sort(key=lambda pk: meta[pk][0])
    if a.limit:
        todo = todo[:a.limit]
    print(f"games: {len(meta)} total, {len(done)} done, {len(todo)} to pull")

    t0, nc, fails = time.time(), 0, []
    for i, pk in enumerate(todo, 1):
        d, ump = meta[pk]
        try:
            chs = U.extract_challenges(pk)
        except RuntimeError as e:
            fails.append((pk, str(e)))
            print(f"  ! {pk} {d}: {e}")
            continue
        with open(OUT, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=U.CHAL_HEADER, extrasaction="ignore")
            for c in chs:
                w.writerow({**c, "game_pk": pk, "date": d,
                            "game_type": gtypes.get(pk, ""), "hp_umpire": ump})
        nc += len(chs)
        if i % 100 == 0 or i == len(todo):
            el = time.time() - t0
            print(f"  [{i}/{len(todo)}] {nc} challenges | {el/i:.2f}s/game | "
                  f"eta {(len(todo)-i)*el/i/60:.0f}m", flush=True)
        time.sleep(0.2)
    print(f"done: {nc} challenges, {len(fails)} failed games")
    if fails:
        print("  failures:", fails[:10])


if __name__ == "__main__":
    main()
