#!/usr/bin/env python3
"""
Check a ranking against the games it was built from.

The ranker is built to respect results, but "respects results" is a claim that
has to be measured rather than assumed.  It is not true unconditionally, and
this reports how often it fails and why.

Two distinct things can put the loser of a game above the winner:

  1. THE GROUP RANKED THEM THE OTHER WAY.  Inside a shared round-robin group,
     _verdict_in_group compares in-group win percentage and then in-group
     point differential -- never the head-to-head result.  Three teams that
     beat each other in a cycle are all 1-1, so point differential decides,
     and it can favour the team that lost the meeting.  This is deliberate:
     raw head-to-head is not transitive, and a cycle has to break somewhere.
  2. THE VERDICT WAS OVERRIDDEN.  The comparison did favour the winner, but
     ranked pairs discarded it because a chain of larger-group verdicts
     already implied the opposite.  A game between two teams with no common
     opponent yields a group of size 2 -- the weakest group strength there
     is -- so interconference games are the most fragile verdicts in the
     system.

Both are by design.  Neither is visible unless you count them:

    python head_to_head.py games_2025_both.csv
    python head_to_head.py games_20*.csv --list 15
    python head_to_head.py games_2025_both.csv --since 2025-12-14
    python head_to_head.py games_2025.csv --max-pct 15    # regression guard

A "decided" pair is one where a team won the season series.  Pairs that split
a series are reported separately and never counted as contradictions: there is
no single result to contradict.
"""

import argparse
import csv
import sys

import networkx as nx

from fbs_ranker import FBSRoundRobinRanker

GROUP = "group ranked them the other way"
OVERRIDDEN = "verdict favoured the winner but was overridden"
UNDECIDED = "comparison could not separate them"


def maximal_cliques(ranker):
    """Every maximal round-robin group in the ranker's game graph."""
    graph = nx.Graph()
    graph.add_nodes_from(ranker.teams)
    for team_a, team_b in ranker._game_map:
        graph.add_edge(team_a, team_b)
    return list(nx.find_cliques(graph))


def series_winner(results):
    """(winner_index, 'W-L') for a season series, or None if it split.

    *results* is the ranker's list of (score_a, score_b) for the pair, so the
    winner is reported as 0 for the first team and 1 for the second.
    """
    wins_a = sum(1 for first, second in results if first > second)
    wins_b = sum(1 for first, second in results if second > first)
    if wins_a == wins_b:
        return None
    if wins_a > wins_b:
        return 0, f"{wins_a}-{wins_b}"
    return 1, f"{wins_b}-{wins_a}"


def classify(ranker, cliques, team_a, team_b, winner):
    """Why the ranking placed *winner* below the team it beat."""
    first, second = sorted((team_a, team_b))
    cmp, strength = ranker._pairwise_compare(first, second, cliques)
    preferred = first if cmp < 0 else (second if cmp > 0 else None)
    if preferred is None:
        return UNDECIDED, strength[0]
    return (OVERRIDDEN if preferred == winner else GROUP), strength[0]


def pair_dates(path):
    """{(team_a, team_b): [dates]} keyed the way the ranker keys a pair.

    The ranker stores only scores, so --since reads the dates back from the
    CSV rather than from the loaded games.
    """
    dates = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if not row.get("date"):
                continue
            key = tuple(sorted((row["home_team"].strip(),
                                row["away_team"].strip())))
            dates.setdefault(key, []).append(row["date"])
    return dates


def audit(ranker, since=None, dates=None):
    """Compare every decided head-to-head series against the final ranking.

    With *since*, only pairs that met on or after that date are audited; the
    ranking is still built from every game loaded.
    """
    cliques = maximal_cliques(ranker)
    rank = {row["team"]: row["rank"] for row in ranker.rank()}

    respected, split, found = 0, 0, []
    for (team_a, team_b), results in ranker._game_map.items():
        if since is not None:
            met = (dates or {}).get((team_a, team_b), [])
            if not any(day >= since for day in met):
                continue
        decided = series_winner(results)
        if decided is None:
            split += 1
            continue
        index, record = decided
        winner = (team_a, team_b)[index]
        loser = (team_b, team_a)[index]
        if rank[winner] < rank[loser]:
            respected += 1
            continue
        mechanism, strength = classify(ranker, cliques, team_a, team_b, winner)
        found.append(dict(
            winner=winner, winner_rank=rank[winner],
            loser=loser, loser_rank=rank[loser],
            gap=rank[winner] - rank[loser],
            series=record, meetings=len(results),
            mechanism=mechanism, group_size=strength,
        ))

    decided_total = respected + len(found)
    return dict(
        decided=decided_total, split=split,
        respected=respected, contradicted=len(found),
        pct=len(found) / decided_total * 100 if decided_total else 0.0,
        contradictions=sorted(found, key=lambda c: -c["gap"]),
    )


def print_audit(report, title, limit):
    print(f"\n===== {title} =====")
    print(f"decided head-to-head series : {report['decided']}")
    print(f"  ranking respects them     : {report['respected']} "
          f"({100 - report['pct']:.1f}%)")
    print(f"  ranking contradicts them  : {report['contradicted']} "
          f"({report['pct']:.1f}%)")
    for mechanism in (GROUP, OVERRIDDEN, UNDECIDED):
        count = sum(1 for c in report["contradictions"]
                    if c["mechanism"] == mechanism)
        if count:
            print(f"      {count:>4}  {mechanism}")
    if report["split"]:
        print(f"  split series (no winner)  : {report['split']}  "
              f"(not counted either way)")

    if limit and report["contradictions"]:
        print(f"\n  largest contradictions:")
        print(f"  {'winner':<24} {'':>5}  {'loser':<24} {'':>5} {'gap':>5} "
              f"{'grp':>4}  why")
        for item in report["contradictions"][:limit]:
            why = {GROUP: "group", OVERRIDDEN: "overridden",
                   UNDECIDED: "undecided"}[item["mechanism"]]
            print(f"  {item['winner']:<24} #{item['winner_rank']:<4} "
                  f"{item['loser']:<24} #{item['loser_rank']:<4} "
                  f"{item['gap']:>5} {item['group_size']:>4}  {why}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report how often the ranking places the loser of a game "
                    "above the winner, and which mechanism did it.")
    parser.add_argument("csv_files", nargs="+", help="season CSV(s)")
    parser.add_argument("--list", type=int, default=10, metavar="N",
                        help="show the N largest contradictions (default 10, "
                             "0 for none)")
    parser.add_argument("--since", metavar="DATE",
                        help="only audit pairs that met on or after DATE "
                             "(ISO), e.g. the first postseason game.  The "
                             "ranking is still built from the whole file.")
    parser.add_argument("--max-pct", type=float, metavar="PCT",
                        help="exit non-zero if contradictions exceed PCT, so "
                             "this can guard against regressions")
    parser.add_argument("--on-duplicate", default="combine",
                        choices=FBSRoundRobinRanker.DUPLICATE_POLICIES,
                        help="how to treat a repeated meeting (default combine)")
    args = parser.parse_args()

    worst = 0.0
    for path in args.csv_files:
        ranker = FBSRoundRobinRanker(on_duplicate=args.on_duplicate)
        try:
            ranker.load_csv(path)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(1)
        report = audit(ranker, since=args.since,
                       dates=pair_dates(path) if args.since else None)
        print_audit(report, path, args.list)
        worst = max(worst, report["pct"])

    if args.max_pct is not None and worst > args.max_pct:
        print(f"\nFAIL: {worst:.1f}% exceeds the {args.max_pct:.1f}% limit",
              file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
