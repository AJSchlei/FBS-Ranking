#!/usr/bin/env python3
"""
Replay a season week by week and report when the ranking becomes meaningful.

The ranker never contradicts a result it has seen, in Week 3 or Week 14, so
"ordering integrity" holds at every point in a season.  That is not the same
as the ranking being worth publishing.  Two things have to be true first:

  1. Teams have played enough games that their record outweighs the prior.
     Shrinkage regresses a record toward .500 by PRIOR_GAMES phantom games,
     so a team with g games shows only g/(g+4) of its true spread.
  2. The round-robin groups actually exist.  Early in a season the game graph
     has no cliques bigger than a triangle, so nearly every pair falls through
     to the blend and the ranker is a shrunken win-percentage list wearing a
     round-robin hat.

This replays each Monday of a season, re-ranks from scratch on the games
played to that point, and reports both:

    python season_progression.py games_2025.csv
    python season_progression.py games_*.csv --average --calendar 2026
    python season_progression.py games_2024.csv --bands

Column notes:
  * "evidence%" is the share of all pairs decided at strength >= 1 (a shared
    round-robin group, or common opponents) rather than by the blend.  It is
    the honest measure of how much of the ranking is coming from results.
  * "largest" is the biggest round-robin group in the graph.  Below 4 this
    ranker has nothing to distinguish it from a record sort.
  * "tau" is Kendall tau-b against the FINAL ordering in the file.  It
    flatters late weeks: Week 14 scores well partly because it nearly is the
    final data.  "churn" -- mean positions moved since the previous week --
    is the only column here you could actually observe in real time.
  * With --bands, "tau25"/"shift25" restrict the comparison to the teams that
    finish in the final top N, and "+-3" counts how many of them are already
    within three positions of where they end up.
"""

import argparse
import csv
import datetime
import itertools
import statistics
import sys
from collections import defaultdict

import networkx as nx

from fbs_ranker import FBSRoundRobinRanker, _is_ranked


# ---------------------------------------------------------------------------
# Season calendar
# ---------------------------------------------------------------------------

def labor_day(year: int) -> datetime.date:
    """First Monday of September, which anchors the college football calendar."""
    d = datetime.date(year, 9, 1)
    return d + datetime.timedelta(days=(0 - d.weekday()) % 7)


def week_zero_saturday(year: int) -> datetime.date:
    """Saturday of Week 0.

    Week 1 is the weekend ending on Labor Day, so Week 0 is the Saturday nine
    days before it.  This reproduces the opening Saturday of 2022-2025 exactly
    (2022-08-27, 2023-08-26, 2024-08-24, 2025-08-23).
    """
    return labor_day(year) - datetime.timedelta(days=9)


def week_monday(year: int, week: int) -> datetime.date:
    """The Monday that closes a given CFB week, which is our cutoff."""
    return week_zero_saturday(year) + datetime.timedelta(days=7 * week + 2)


def season_year(dates) -> int:
    """The season a set of ISO dates belongs to (the year it kicked off)."""
    return int(min(dates)[:4])


def week_cutoffs(dates):
    """[(cfb_week, cutoff_date)] covering every game date in *dates*.

    Cutoffs land on the Monday closing each CFB week, so a Thursday-through-
    Monday slate always falls whole inside one week.
    """
    year = season_year(dates)
    last = max(dates)
    out, week = [], 0
    while True:
        cut = week_monday(year, week)
        out.append((week, cut.isoformat()))
        if cut.isoformat() >= last:
            return out
        week += 1


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_rows(path: str):
    """Return (rows, unranked_teams) for a season CSV.

    Division is metadata rather than a result, so the unranked set is taken
    from the whole file even when we later truncate to a cutoff: in live use
    the API would tell you a team's classification in Week 3 too.  As in
    load_csv, a team marked unranked in ANY row is unranked throughout.
    """
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if rows and "date" not in rows[0]:
        raise ValueError(f"{path}: needs a 'date' column to replay a season")
    unranked = set()
    for row in rows:
        for side in ("home", "away"):
            if not _is_ranked(row.get(f"{side}_ranked")):
                unranked.add(row[f"{side}_team"].strip())
    return rows, unranked


def ranker_through(rows, unranked, cutoff, on_duplicate="combine"):
    """Build a ranker from the games played on or before *cutoff*."""
    ranker = FBSRoundRobinRanker(on_duplicate=on_duplicate)
    played = 0
    for row in rows:
        if row["date"] > cutoff:
            continue
        home = row["home_team"].strip()
        away = row["away_team"].strip()
        ranker.add_game(home, away, int(row["home_score"]), int(row["away_score"]),
                        home_ranked=home not in unranked,
                        away_ranked=away not in unranked)
        played += 1
    return ranker, played


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------

def shared_clique_index(cliques):
    """Map each ranked pair to the cliques containing both teams.

    _pairwise_compare filters the clique list it is handed down to the pairs
    that share a clique, so handing it exactly that list gives an identical
    answer without rescanning every clique for every pair.
    """
    index = defaultdict(list)
    for clique in cliques:
        for pair in itertools.combinations(sorted(clique), 2):
            index[pair].append(clique)
    return index


def tier_counts(ranker):
    """How many pairs each evidence tier decides.

    Returns a dict with group/common/blend/tied/total counts plus the size of
    the largest round-robin group in the graph.
    """
    graph = nx.Graph()
    graph.add_nodes_from(ranker.teams)
    for team_a, team_b in ranker._game_map:
        graph.add_edge(team_a, team_b)
    cliques = list(nx.find_cliques(graph))
    index = shared_clique_index(cliques)

    counts = dict(group=0, common=0, blend=0, tied=0, total=0)
    for pair in itertools.combinations(sorted(ranker.teams), 2):
        cmp, strength = ranker._pairwise_compare(pair[0], pair[1],
                                                 index.get(pair, []))
        counts["total"] += 1
        size = strength[0]
        counts["group" if size >= 2 else ("common" if size == 1 else "blend")] += 1
        if cmp == 0:
            counts["tied"] += 1
    counts["largest_group"] = max((len(c) for c in cliques), default=0)
    return counts


def games_played(ranker):
    """Games each team has played, counting unranked opponents."""
    played = defaultdict(int)
    for (team_a, team_b), results in ranker._game_map.items():
        played[team_a] += len(results)
        played[team_b] += len(results)
    for team in ranker.teams:
        played[team] += len(ranker.get_unranked_results(team))
    return {team: played[team] for team in ranker.teams}


def kendall_tau_b(rank_a, rank_b, keys):
    """Kendall tau-b between two rankings over *keys*, tolerating ties.

    Both arguments map a team to a rank number where smaller is better.  Ties
    are common here -- the ranker gives teams nothing separates the same rank
    -- so tau-b's tie correction is the right variant.
    """
    concordant = discordant = tied_a = tied_b = total = 0
    for first, second in itertools.combinations(keys, 2):
        delta_a = rank_a[first] - rank_a[second]
        delta_b = rank_b[first] - rank_b[second]
        total += 1
        if delta_a == 0:
            tied_a += 1
        if delta_b == 0:
            tied_b += 1
        if delta_a and delta_b:
            if (delta_a > 0) == (delta_b > 0):
                concordant += 1
            else:
                discordant += 1
    # tau-b divides by sqrt((n0 - n1)(n0 - n2)): the pairs each side could
    # have ordered.  A pair tied on BOTH sides is excluded from both factors,
    # so it must be subtracted, not added -- adding it silently shrinks tau
    # toward zero wherever the ranker declares teams equal.
    denominator = ((total - tied_a) * (total - tied_b)) ** 0.5
    return (concordant - discordant) / denominator if denominator else float("nan")


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def progression(path, on_duplicate="combine", band=25, quick=False):
    """Replay a season, returning one dict per CFB week."""
    rows, unranked = load_rows(path)
    cutoffs = week_cutoffs([row["date"] for row in rows if row["date"]])

    final_ranker, _ = ranker_through(rows, unranked, cutoffs[-1][1], on_duplicate)
    final_rows = sorted(final_ranker.rank(), key=lambda r: (r["rank"], r["team"]))
    final_order = [r["team"] for r in final_rows]
    final_rank = {r["team"]: r["rank"] for r in final_rows}
    final_pos = {team: i for i, team in enumerate(final_order)}
    final_band = final_order[:band]

    weeks, previous = [], None
    for week, cutoff in cutoffs:
        ranker, played = ranker_through(rows, unranked, cutoff, on_duplicate)
        if not ranker.teams:
            continue
        results = sorted(ranker.rank(), key=lambda r: (r["rank"], r["team"]))
        order = [r["team"] for r in results]
        position = {team: i for i, team in enumerate(order)}
        current = {r["team"]: r["rank"] for r in results}

        counts = tier_counts(ranker) if not quick else None
        played_counts = sorted(games_played(ranker).values())
        shared = [team for team in current if team in final_rank]
        shifts = [abs(position[t] - final_pos[t]) for t in shared]

        churn = float("nan")
        if previous is not None:
            common = [t for t in position if t in previous]
            if common:
                churn = statistics.mean(abs(position[t] - previous[t])
                                        for t in common)

        entry = dict(
            week=week, cutoff=cutoff, games=played, teams=len(ranker.teams),
            min_games=played_counts[0],
            median_games=statistics.median(played_counts),
            tau=kendall_tau_b(current, final_rank, shared) if len(shared) > 2
            else float("nan"),
            mean_shift=statistics.mean(shifts) if shifts else float("nan"),
            churn=churn,
            band_hits=len(set(order[:band]) & set(final_band)),
        )
        if counts:
            total = counts["total"] or 1
            entry.update(
                group_pct=counts["group"] / total * 100,
                common_pct=counts["common"] / total * 100,
                evidence_pct=(counts["group"] + counts["common"]) / total * 100,
                largest_group=counts["largest_group"],
            )

        present = [t for t in final_band if t in position]
        if len(present) > 2:
            entry.update(
                band_tau=kendall_tau_b(current, final_rank, present),
                band_shift=statistics.mean(abs(position[t] - final_pos[t])
                                           for t in present),
                band_within3=sum(1 for t in present
                                 if abs(position[t] - final_pos[t]) <= 3),
            )
        weeks.append(entry)
        previous = position
    return weeks


def average_weeks(runs):
    """Average per-week metrics across several seasons.

    A week is only reported where every season has one, so the average is
    never a different set of seasons from one row to the next.
    """
    by_week = defaultdict(list)
    for weeks in runs:
        for entry in weeks:
            by_week[entry["week"]].append(entry)
    out = []
    for week in sorted(by_week):
        entries = by_week[week]
        if len(entries) < len(runs):
            continue
        merged = dict(week=week, cutoff="", seasons=len(entries))
        for key in entries[0]:
            if key in ("week", "cutoff"):
                continue
            values = [e[key] for e in entries if key in e]
            if len(values) == len(entries):
                merged[key] = statistics.mean(values)
        out.append(merged)
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_progression(weeks, title, calendar_year=None, quick=False):
    print(f"\n===== {title} =====")
    head = f"{'wk':>3} {'through':>11}"
    if calendar_year:
        head += f" {str(calendar_year):>11}"
    head += f" {'games':>6} {'min':>4} {'med':>5}"
    if not quick:
        head += f" {'group%':>7} {'com%':>6} {'evid%':>6} {'largest':>8}"
    head += f" {'tau':>6} {'shift':>6} {'churn':>6}"
    print(head)
    print("-" * len(head))
    for entry in weeks:
        line = f"{entry['week']:>3} {entry['cutoff']:>11}"
        if calendar_year:
            line += f" {week_monday(calendar_year, entry['week']).isoformat():>11}"
        line += (f" {entry['games']:>6.0f} {entry['min_games']:>4.1f} "
                 f"{entry['median_games']:>5.1f}")
        if not quick:
            line += (f" {entry['group_pct']:>7.1f} {entry['common_pct']:>6.1f} "
                     f"{entry['evidence_pct']:>6.1f} {entry['largest_group']:>8.1f}")
        line += (f" {entry['tau']:>6.3f} {entry['mean_shift']:>6.1f} "
                 f"{entry['churn']:>6.1f}")
        print(line)


def print_bands(weeks, title, band, calendar_year=None):
    print(f"\n----- {title}: teams finishing in the top {band} -----")
    head = f"{'wk':>3} {'through':>11}"
    if calendar_year:
        head += f" {str(calendar_year):>11}"
    head += f" {'tau' + str(band):>7} {'shift':>7} {'within3':>8}"
    print(head)
    print("-" * len(head))
    for entry in weeks:
        if "band_tau" not in entry:
            continue
        line = f"{entry['week']:>3} {entry['cutoff']:>11}"
        if calendar_year:
            line += f" {week_monday(calendar_year, entry['week']).isoformat():>11}"
        line += (f" {entry['band_tau']:>7.3f} {entry['band_shift']:>7.1f} "
                 f"{entry['band_within3']:>8.1f}")
        print(line)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay a season week by week to see when the ranking "
                    "stops being mostly prior and starts being mostly results.")
    parser.add_argument("csv_files", nargs="+",
                        help="season CSV(s); each needs a date column")
    parser.add_argument("--average", action="store_true",
                        help="average the weekly metrics across the files "
                             "instead of printing each season separately")
    parser.add_argument("--bands", action="store_true",
                        help="also report stability among the teams that "
                             "finish in the top N (see --band)")
    parser.add_argument("--band", type=int, default=25,
                        help="size of the band --bands reports on (default 25)")
    parser.add_argument("--calendar", type=int, metavar="YEAR",
                        help="add a column mapping each week onto YEAR's "
                             "calendar, e.g. --calendar 2026")
    parser.add_argument("--quick", action="store_true",
                        help="skip the evidence-tier columns, which are the "
                             "slow part, and report stability only")
    parser.add_argument("--on-duplicate", default="combine",
                        choices=FBSRoundRobinRanker.DUPLICATE_POLICIES,
                        help="how to treat a repeated meeting (default combine)")
    args = parser.parse_args()

    runs = []
    for path in args.csv_files:
        try:
            weeks = progression(path, on_duplicate=args.on_duplicate,
                                band=args.band, quick=args.quick)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        runs.append((path, weeks))

    if args.average and len(runs) > 1:
        merged = average_weeks([weeks for _, weeks in runs])
        title = f"{len(runs)}-season average"
        print_progression(merged, title, args.calendar, args.quick)
        if args.bands:
            print_bands(merged, title, args.band, args.calendar)
    else:
        for path, weeks in runs:
            print_progression(weeks, path, args.calendar, args.quick)
            if args.bands:
                print_bands(weeks, path, args.band, args.calendar)

    print("\nReminder: tau compares against the final ordering in the file, so "
          "it rewards\nlate weeks for being nearly the final data.  Churn is "
          "the column you could\nhave watched live.")


if __name__ == "__main__":
    main()
