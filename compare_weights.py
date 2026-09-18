#!/usr/bin/env python3
"""
Compare BLEND_WEIGHTS settings on a season of game data.

The blend is the weakest tier in the ranker: it decides only pairs that no
round-robin group and no set of common opponents could settle.  Changing it
therefore cannot reorder teams that played each other — but it does change how
much a team's schedule counts against its record, and that is a judgement call
rather than something the data settles on its own.

This prints what each weighting actually does to a season, so the call can be
made on evidence:

    python compare_weights.py games_2025.csv
    python compare_weights.py games_2024.csv --top 40
    python compare_weights.py games_2024.csv --weights 0.8,0.2,0 0.6,0.4,0

Usage notes:
  * "influence" is weight times how much a metric varies across the field.
    Nominal weight overstates the opponent terms, because each averaging layer
    pulls harder toward .500 — so a quarter weight on opponents is nothing like
    a quarter of the decision.
  * "record inversions" counts pairs where the LOWER-ranked team has the better
    win percentage by at least the given margin.  Some are legitimate (a group
    verdict outranking a record), so what matters is how the count MOVES.
"""

import argparse
import itertools
import statistics
import sys

from fbs_ranker import FBSRoundRobinRanker

DEFAULT_WEIGHTS = [(1.0, 0.0, 0.0), (0.75, 0.25, 0.0),
                   (0.60, 0.30, 0.10), (0.50, 0.50, 0.0)]


def label(weights):
    a, b, c = weights
    return f"({a:g}, {b:g}, {c:g})"


def build(path, weights, on_duplicate):
    ranker = FBSRoundRobinRanker(on_duplicate=on_duplicate)
    ranker.BLEND_WEIGHTS = weights
    ranker.load_csv(path)
    return ranker


def win_pct(row):
    played = row["overall_wins"] + row["overall_losses"]
    return row["overall_wins"] / played if played else 0.0


def count_inversions(rows, margin):
    """Pairs where the lower-ranked team has the better record by `margin`."""
    pcts = [win_pct(r) for r in rows]
    return sum(1 for i, j in itertools.combinations(range(len(rows)), 2)
               if pcts[j] - pcts[i] > margin)


def movement(baseline, other):
    """(moved, median shift, max shift) between two orderings."""
    where = {team: i for i, team in enumerate(other)}
    shifts = [abs(i - where[team]) for i, team in enumerate(baseline)
              if i != where[team]]
    if not shifts:
        return 0, 0, 0
    return len(shifts), float(statistics.median(shifts)), max(shifts)


def influence_table(ranker, weight_sets):
    """Weight times spread, which is what actually separates teams."""
    metrics = ranker._metrics()
    teams = sorted(ranker.teams)
    if len(teams) < 2:
        return []
    spread = {key: statistics.stdev(metrics[key][t] for t in teams)
              for key in ("wp", "owp", "oowp")}
    out = []
    for weights in weight_sets:
        a, b, c = weights
        own = a * spread["wp"]
        opp = b * spread["owp"] + c * spread["oowp"]
        # A field where every opponent record is identical gives the
        # opponent terms nothing to say; guard against dividing by the
        # floating-point crumbs that leaves behind.
        ratio = own / opp if opp > 1e-9 else None
        out.append((weights, own, b * spread["owp"], c * spread["oowp"], ratio))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="compare_weights",
        description="Compare BLEND_WEIGHTS settings on one season.")
    parser.add_argument("input", help="CSV of game results")
    parser.add_argument("--weights", nargs="+", metavar="A,B,C",
                        help="weightings to compare (default: four common ones)")
    parser.add_argument("--top", type=int, default=25,
                        help="how many ranking places to print (default 25)")
    parser.add_argument("--margin", type=float, default=0.15,
                        help="win-pct gap counted as a record inversion "
                             "(default 0.15)")
    parser.add_argument("--movers", type=int, default=10,
                        help="how many biggest movers to list (default 10)")
    parser.add_argument("--on-duplicate", default="combine",
                        choices=FBSRoundRobinRanker.DUPLICATE_POLICIES,
                        help="passed through to the ranker (default: combine)")
    args = parser.parse_args(argv)

    if args.weights:
        weight_sets = []
        for text in args.weights:
            parts = text.replace("(", "").replace(")", "").split(",")
            if len(parts) != 3:
                parser.error(f"--weights takes A,B,C triples; got {text!r}")
            try:
                weight_sets.append(tuple(float(p) for p in parts))
            except ValueError:
                parser.error(f"--weights values must be numbers; got {text!r}")
    else:
        weight_sets = list(DEFAULT_WEIGHTS)

    runs = {}
    for weights in weight_sets:
        ranker = build(args.input, weights, args.on_duplicate)
        rows = ranker.rank()
        runs[weights] = {"rows": rows, "order": [r["team"] for r in rows],
                         "ranker": ranker}

    first = runs[weight_sets[0]]["ranker"]
    print(f"{args.input}: {len(first.teams)} teams, {first.total_games()} games")
    if first.unranked_opponents:
        print(f"  {len(first.unranked_opponents)} unranked opponent(s) counted "
              f"but not ranked")
    stranded = first.teams_without_ranked_opponents()
    if stranded:
        print(f"  WARNING: {len(stranded)} ranked team(s) played no ranked "
              f"opponent: {', '.join(sorted(stranded))}")

    print("\nInfluence on the strength-0 score (weight x how much it varies)")
    print(f"  {'weights':18} {'own':>9} {'opponents':>10} {'opps opps':>10} "
          f"{'own:opp':>10}")
    for weights, own, opp, oopp, ratio in influence_table(first, weight_sets):
        shown = f"{ratio:.1f}:1" if ratio is not None else "no contest"
        print(f"  {label(weights):18} {own:9.4f} {opp:10.4f} {oopp:10.4f} "
              f"{shown:>10}")

    baseline = weight_sets[0]
    print(f"\nAgainst {label(baseline)}")
    print(f"  {'weights':18} {'moved':>7} {'median':>7} {'max':>5} "
          f"{'inversions':>11}")
    for weights in weight_sets:
        moved, med, mx = movement(runs[baseline]["order"], runs[weights]["order"])
        inv = count_inversions(runs[weights]["rows"], args.margin)
        if weights == baseline:
            shown = ("--", "--", "--")
        else:
            shown = (str(moved), f"{med:g}", str(mx))
        print(f"  {label(weights):18} {shown[0]:>7} {shown[1]:>7} "
              f"{shown[2]:>5} {inv:11}")
    print(f"  (inversions: pairs where the lower-ranked team's win pct is "
          f"more than {args.margin:g} better)")

    width = max(26, max(len(label(w)) for w in weight_sets) + 2)
    print(f"\nTop {args.top}")
    print("  " + "".join(label(w).ljust(width) for w in weight_sets))
    depth = min(args.top, min(len(r["order"]) for r in runs.values()))
    for i in range(depth):
        cells = []
        for weights in weight_sets:
            row = runs[weights]["rows"][i]
            cells.append(f"{row['team']} "
                         f"({row['overall_wins']}-{row['overall_losses']})")
        print(f"{i + 1:3} " + "".join(c.ljust(width) for c in cells))

    for weights in weight_sets[1:]:
        where = {t: i for i, t in enumerate(runs[weights]["order"])}
        moved = [(t, i + 1, where[t] + 1)
                 for i, t in enumerate(runs[baseline]["order"])
                 if i != where[t]]
        if not moved:
            print(f"\nBiggest movers, {label(weights)}: none")
            continue
        print(f"\nBiggest movers, {label(weights)} vs {label(baseline)}")
        for team, was, now in sorted(moved,
                                     key=lambda m: -abs(m[1] - m[2])
                                     )[:args.movers]:
            row = runs[baseline]["rows"][was - 1]
            rec = f"{row['overall_wins']}-{row['overall_losses']}"
            print(f"   {team:24} {rec:>6}  {was:3} -> {now:3} ({now - was:+d})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
