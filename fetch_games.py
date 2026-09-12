#!/usr/bin/env python3
"""
Fetch a season of FBS results from CollegeFootballData.com into ranker CSV.

The output is exactly what fbs_ranker.py reads:

    date,home_team,home_score,away_team,away_score

Completed games are written with a home_ranked / away_ranked column marking
whether each side belongs in the rankings.  A game against an FCS or other
non-FBS opponent is kept, with that side marked unranked: it counts toward the
FBS team's record — which is how a loss to an FCS team shows up at all — while
the FCS team stays out of the game graph and out of the standings.

Games between two non-FBS teams are dropped; neither side would be ranked.

An API key is required (free, from https://collegefootballdata.com/key).  Pass
it with --api-key or, better, put it in the CFBD_API_KEY environment variable
so it stays out of your shell history:

    export CFBD_API_KEY=...
    python fetch_games.py --year 2025 --out games_2025.csv
    python fbs_ranker.py games_2025.csv

Rematches:
    A conference championship game is often a rematch of a regular-season
    meeting.  Both meetings are written by default, because the ranker counts
    every meeting: a split series is 1-1 with points from both games.  The
    repeated pairs are listed so you can see them.  --on-duplicate can drop
    one of the meetings, or refuse the season outright, if you want that.

This script only reads from the API; it never writes anything back.
"""

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://api.collegefootballdata.com/games"
TIMEOUT = 60


def fetch_games(year, season_type, api_key, url=API_URL, timeout=TIMEOUT):
    """Return the raw list of game dicts for a season from the CFBD API."""
    query = urllib.parse.urlencode({
        "year": year,
        "seasonType": season_type,
        "division": "fbs",
    })
    request = urllib.request.Request(
        f"{url}?{query}",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "fbs-ranking/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise SystemExit(
                f"API rejected the key ({exc.code}). Check CFBD_API_KEY, or get "
                f"a free key at https://collegefootballdata.com/key"
            ) from None
        raise SystemExit(f"API request failed: HTTP {exc.code} {exc.reason}") from None
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach {url}: {exc.reason}") from None


def _first(record, *names):
    """Return the first present, non-None key among *names*.

    CFBD has served both camelCase and snake_case over the years, so accept
    either rather than breaking on a field rename.
    """
    for name in names:
        if record.get(name) is not None:
            return record[name]
    return None


def extract_fbs_games(raw_games):
    """Turn raw API records into ranker rows.

    Keeps every completed game with at least one FBS side, marking any non-FBS
    opponent unranked.  Returns (rows, skipped) where skipped counts why
    records were dropped.
    """
    rows = []
    skipped = {"not_final": 0, "no_fbs_side": 0, "missing_fields": 0}

    for game in raw_games:
        home = _first(game, "homeTeam", "home_team")
        away = _first(game, "awayTeam", "away_team")
        home_score = _first(game, "homePoints", "home_points")
        away_score = _first(game, "awayPoints", "away_points")

        if not home or not away:
            skipped["missing_fields"] += 1
            continue
        if home_score is None or away_score is None:
            # Not played yet, or cancelled.
            skipped["not_final"] += 1
            continue

        home_div = _first(game, "homeClassification", "home_classification")
        away_div = _first(game, "awayClassification", "away_classification")
        # A missing classification is not evidence the team is non-FBS, so
        # treat only an explicit non-fbs value as unranked.
        home_ranked = not (home_div and home_div != "fbs")
        away_ranked = not (away_div and away_div != "fbs")
        if not home_ranked and not away_ranked:
            skipped["no_fbs_side"] += 1
            continue

        start = _first(game, "startDate", "start_date") or ""
        rows.append({
            "date": str(start)[:10],
            "home_team": str(home).strip(),
            "home_score": int(home_score),
            "away_team": str(away).strip(),
            "away_score": int(away_score),
            "home_ranked": str(home_ranked).lower(),
            "away_ranked": str(away_ranked).lower(),
        })

    return rows, skipped


def find_duplicate_pairs(rows):
    """Return {(team_a, team_b): [row, ...]} for pairs appearing more than once."""
    seen = {}
    for row in rows:
        pair = tuple(sorted((row["home_team"], row["away_team"])))
        seen.setdefault(pair, []).append(row)
    return {pair: games for pair, games in seen.items() if len(games) > 1}


def apply_duplicate_policy(rows, policy):
    """Drop repeated meetings according to *policy*.

    "combine" keeps every meeting and is a no-op; "keep_first" and
    "keep_last" reduce each repeated pair to a single game.
    """
    if policy == "combine":
        return list(rows)
    if policy == "keep_last":
        rows = list(reversed(rows))

    kept, seen = [], set()
    for row in rows:
        pair = tuple(sorted((row["home_team"], row["away_team"])))
        if pair in seen:
            continue
        seen.add(pair)
        kept.append(row)

    if policy == "keep_last":
        kept.reverse()
    return kept


def find_unranked_upsets(rows):
    """Games an unranked opponent won: the results most worth surfacing."""
    upsets = []
    for row in rows:
        home_ranked = row.get("home_ranked", "true") == "true"
        away_ranked = row.get("away_ranked", "true") == "true"
        if home_ranked == away_ranked:
            continue                       # both ranked, or neither
        if home_ranked and row["away_score"] > row["home_score"]:
            upsets.append((row, row["away_team"], row["home_team"]))
        elif away_ranked and row["home_score"] > row["away_score"]:
            upsets.append((row, row["home_team"], row["away_team"]))
    return upsets


def write_csv(rows, path):
    fields = ["date", "home_team", "home_score", "away_team", "away_score",
              "home_ranked", "away_ranked"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="fetch_games",
        description="Fetch an FBS season from CollegeFootballData.com as ranker CSV.",
    )
    parser.add_argument("--year", type=int, required=True,
                        help="Season year, e.g. 2025")
    parser.add_argument("--out", "-o", required=True,
                        help="CSV file to write")
    parser.add_argument("--season-type", default="regular",
                        choices=["regular", "postseason", "both"],
                        help="Which games to fetch (default: regular)")
    parser.add_argument("--api-key",
                        help="CFBD API key (default: CFBD_API_KEY env var)")
    parser.add_argument("--on-duplicate", default="combine",
                        choices=["combine", "error", "keep_first", "keep_last"],
                        help="What to do when a pair of teams meets twice: "
                             "combine keeps both meetings (default, matching "
                             "the ranker), error refuses the season, "
                             "keep_first / keep_last drop one")
    args = parser.parse_args(argv)

    api_key = args.api_key or os.environ.get("CFBD_API_KEY")
    if not api_key:
        parser.error(
            "no API key: set CFBD_API_KEY or pass --api-key. "
            "Free keys: https://collegefootballdata.com/key"
        )

    season_types = ["regular", "postseason"] if args.season_type == "both" \
        else [args.season_type]

    raw = []
    for season_type in season_types:
        print(f"Fetching {args.year} {season_type} games …")
        raw.extend(fetch_games(args.year, season_type, api_key))

    rows, skipped = extract_fbs_games(raw)
    unranked = sum(1 for r in rows
                   if "false" in (r["home_ranked"], r["away_ranked"]))
    print(f"  {len(raw)} records returned")
    print(f"  {len(rows)} completed games kept "
          f"({len(rows) - unranked} FBS-vs-FBS, {unranked} vs an unranked opponent)")
    print(f"  skipped: {skipped['not_final']} not final, "
          f"{skipped['no_fbs_side']} with no FBS side, "
          f"{skipped['missing_fields']} missing fields")

    upsets = find_unranked_upsets(rows)
    if upsets:
        print(f"\n{len(upsets)} loss(es) to an unranked opponent — "
              f"these count against the FBS team:")
        for row, winner, loser in sorted(upsets, key=lambda u: u[0]["date"]):
            ws = max(row["home_score"], row["away_score"])
            ls = min(row["home_score"], row["away_score"])
            print(f"  {row['date']}  {winner} {ws}-{ls} {loser}")

    duplicates = find_duplicate_pairs(rows)
    if duplicates:
        print(f"\n{len(duplicates)} pair(s) met more than once:")
        for (team_a, team_b), games in sorted(duplicates.items()):
            print(f"  {team_a} vs {team_b}")
            for game in games:
                print(f"    {game['date']}  {game['home_team']} "
                      f"{game['home_score']}-{game['away_score']} "
                      f"{game['away_team']}")

        if args.on_duplicate == "error":
            print(
                "\n--on-duplicate error was requested, so nothing was written."
                "\nDrop the flag to keep both meetings as a season series, or "
                "use\nkeep_first / keep_last to count only one of them.",
                file=sys.stderr,
            )
            return 1

        if args.on_duplicate == "combine":
            print("\nBoth meetings are kept: the ranker counts a split series "
                  "as 1-1\nwith points from both games.")
        else:
            before = len(rows)
            rows = apply_duplicate_policy(rows, args.on_duplicate)
            print(f"\nApplied --on-duplicate {args.on_duplicate}: "
                  f"dropped {before - len(rows)} game(s).")

    write_csv(rows, args.out)
    teams = {t for row in rows for t in (row["home_team"], row["away_team"])}
    print(f"\nWrote {len(rows)} games ({len(teams)} teams) to {args.out}")
    print(f"Next: python fbs_ranker.py {args.out} --output rankings_{args.year}.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
