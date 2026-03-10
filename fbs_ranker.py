#!/usr/bin/env python3
"""
FBS Round-Robin Ranking Tool

Ranks FBS football teams by discovering round-robin groups (cliques where every
team played every other team) and ranking within those groups, processing from
the largest group down to the smallest.

Algorithm:
  1. Build an undirected game graph: nodes = teams, edges = games played.
  2. Find all maximal cliques (Bron-Kerbosch via NetworkX).  Each maximal clique
     is the largest round-robin group that cannot be extended further.
  3. Sort cliques by size, largest first.  Same-sized cliques are sorted by their
     collective winning percentage so stronger groups appear first.
  4. For each clique, rank only the teams that have not yet been ranked:
       a. Calculate each team's win-loss record against ALL members of the clique.
       b. Sort by win percentage (descending).
       c. Break ties with head-to-head record inside the tied sub-group.
       d. Further ties broken by cumulative point differential inside the clique.
  5. Teams ranked in a larger group always appear above teams ranked in a smaller
     group, regardless of performance differences.
  6. Any teams not placed by a clique (played no games or formed no complete
     round-robin with anyone) are ranked last by their overall win percentage,
     then point differential.

Usage:
    python fbs_ranker.py games.csv
    python fbs_ranker.py games.csv --output rankings.csv
    python fbs_ranker.py --demo
    python fbs_ranker.py --demo --output demo_rankings.csv

CSV format (input):
    Required columns: home_team, home_score, away_team, away_score
    Optional column:  date  (ignored by the ranker)
"""

import csv
import sys
import argparse
from collections import defaultdict
from itertools import combinations

import networkx as nx


# ---------------------------------------------------------------------------
# Core ranking engine
# ---------------------------------------------------------------------------

class FBSRoundRobinRanker:
    """
    Discovers round-robin groups among FBS teams and produces an ordered ranking.

    Attributes:
        teams (set): All team names seen in the loaded game data.
    """

    def __init__(self):
        self.teams: set = set()
        # Canonical key: (team_a, team_b) with team_a < team_b (lexicographic).
        # Value: (score_for_team_a, score_for_team_b)
        # One game per pair is supported per season.  If a duplicate pair is
        # loaded the second result silently overwrites the first.
        self._game_map: dict = {}

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def load_csv(self, filepath: str) -> None:
        """Load game results from a CSV file.

        Required columns: home_team, home_score, away_team, away_score
        """
        with open(filepath, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                home = row["home_team"].strip()
                away = row["away_team"].strip()
                home_score = int(row["home_score"])
                away_score = int(row["away_score"])
                self._add_game(home, away, home_score, away_score)

    def add_game(self, home: str, away: str, home_score: int, away_score: int) -> None:
        """Add a single game result programmatically."""
        self._add_game(home, away, home_score, away_score)

    def _add_game(self, team_a: str, team_b: str, score_a: int, score_b: int) -> None:
        self.teams.add(team_a)
        self.teams.add(team_b)
        if team_a < team_b:
            self._game_map[(team_a, team_b)] = (score_a, score_b)
        else:
            self._game_map[(team_b, team_a)] = (score_b, score_a)

    # ------------------------------------------------------------------
    # Game result helpers
    # ------------------------------------------------------------------

    def get_result(self, team_a: str, team_b: str):
        """Return (score_a, score_b) for the game between the two teams, or None."""
        if team_a < team_b:
            return self._game_map.get((team_a, team_b))
        result = self._game_map.get((team_b, team_a))
        if result is None:
            return None
        return (result[1], result[0])

    def _record_in_group(self, team: str, group) -> tuple:
        """Return (wins, losses, points_for, points_against) for *team* vs every
        other member of *group*."""
        wins = losses = pf = pa = 0
        for opp in group:
            if opp == team:
                continue
            result = self.get_result(team, opp)
            if result is None:
                continue
            st, so = result
            pf += st
            pa += so
            if st > so:
                wins += 1
            else:
                losses += 1
        return wins, losses, pf, pa

    # ------------------------------------------------------------------
    # Ranking helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _win_pct(wins: int, losses: int) -> float:
        total = wins + losses
        return wins / total if total > 0 else 0.0

    def _rank_group(self, unranked_teams: list, full_clique: list) -> list:
        """Return *unranked_teams* sorted best-to-worst using their record
        inside *full_clique*.

        Tie-breaking order:
          1. Win percentage within the clique (descending).
          2. Head-to-head record among tied teams only.
          3. Point differential within the clique (descending).
          4. Alphabetical name (deterministic fallback).
        """
        if len(unranked_teams) <= 1:
            return list(unranked_teams)

        # Compute records for each team in the full clique context.
        records: dict = {}
        for t in unranked_teams:
            w, l, pf, pa = self._record_in_group(t, full_clique)
            records[t] = {"wins": w, "losses": l, "pf": pf, "pa": pa,
                          "win_pct": self._win_pct(w, l)}

        # Initial sort: win pct desc, then point diff desc, then name asc.
        sorted_teams = sorted(
            unranked_teams,
            key=lambda t: (
                -records[t]["win_pct"],
                -(records[t]["pf"] - records[t]["pa"]),
                t,
            ),
        )

        # Scan for ties at the win-pct level and resolve with head-to-head.
        result: list = []
        i = 0
        while i < len(sorted_teams):
            wpc_i = records[sorted_teams[i]]["win_pct"]
            j = i + 1
            while j < len(sorted_teams) and abs(records[sorted_teams[j]]["win_pct"] - wpc_i) < 1e-9:
                j += 1
            tied = sorted_teams[i:j]
            if len(tied) > 1:
                tied = self._resolve_ties(tied, records)
            result.extend(tied)
            i = j

        return result

    def _resolve_ties(self, tied_teams: list, outer_records: dict) -> list:
        """Break ties among teams that share a win percentage.

        Strategy:
          1. Compute head-to-head win percentage among ONLY the tied teams.
          2. Sort by that h2h win pct descending.
          3. Any remaining ties within h2h are broken by overall point diff
             (from outer_records), then alphabetical.
        """
        # Two-team shortcut
        if len(tied_teams) == 2:
            a, b = tied_teams
            result = self.get_result(a, b)
            if result:
                sa, sb = result
                if sa != sb:
                    return [a, b] if sa > sb else [b, a]
            # Fall through to point differential
            diff_a = outer_records[a]["pf"] - outer_records[a]["pa"]
            diff_b = outer_records[b]["pf"] - outer_records[b]["pa"]
            if diff_a != diff_b:
                return [a, b] if diff_a > diff_b else [b, a]
            return sorted([a, b])  # alphabetical

        # Compute h2h win pct within the tied group.
        h2h_wins: dict = defaultdict(int)
        h2h_losses: dict = defaultdict(int)
        for a, b in combinations(tied_teams, 2):
            result = self.get_result(a, b)
            if result is None:
                continue
            sa, sb = result
            if sa > sb:
                h2h_wins[a] += 1
                h2h_losses[b] += 1
            elif sb > sa:
                h2h_wins[b] += 1
                h2h_losses[a] += 1
            # Ties in score are ignored for record purposes (shouldn't happen in FBS)

        def h2h_pct(t: str) -> float:
            total = h2h_wins[t] + h2h_losses[t]
            return h2h_wins[t] / total if total > 0 else 0.0

        # Sort by h2h win pct, then outer point diff, then name.
        sorted_tied = sorted(
            tied_teams,
            key=lambda t: (
                -h2h_pct(t),
                -(outer_records[t]["pf"] - outer_records[t]["pa"]),
                t,
            ),
        )

        # Recursively resolve any remaining ties at the h2h level.
        result_list: list = []
        i = 0
        while i < len(sorted_tied):
            pct_i = h2h_pct(sorted_tied[i])
            j = i + 1
            while j < len(sorted_tied) and abs(h2h_pct(sorted_tied[j]) - pct_i) < 1e-9:
                j += 1
            sub = sorted_tied[i:j]
            if len(sub) > 1:
                # Final level: sort by point differential, then alphabetically.
                sub.sort(key=lambda t: (-(outer_records[t]["pf"] - outer_records[t]["pa"]), t))
            result_list.extend(sub)
            i = j

        return result_list

    # ------------------------------------------------------------------
    # Public ranking API
    # ------------------------------------------------------------------

    def rank(self) -> list:
        """Perform the full round-robin ranking.

        Returns:
            A list of dicts (one per team), ordered best-to-worst, with keys:
                rank          - integer position (1 = best)
                team          - team name string
                group_size    - number of teams in the round-robin group used
                wins          - wins against group opponents
                losses        - losses against group opponents
                win_pct       - wins / (wins + losses), rounded to 3 dp
                points_for    - cumulative points scored against group opponents
                points_against- cumulative points allowed against group opponents
                point_diff    - points_for - points_against
        """
        if not self.teams:
            return []

        # ---- Build undirected game graph ----
        G = nx.Graph()
        G.add_nodes_from(self.teams)
        for ta, tb in self._game_map:
            G.add_edge(ta, tb)

        # ---- Discover all maximal cliques (round-robin groups) ----
        all_cliques = list(nx.find_cliques(G))

        # ---- Sort cliques: largest first; ties broken by group quality ----
        # Group quality = average win-pct of members within the clique, so
        # stronger groups appear before weaker groups of the same size.
        def clique_sort_key(clique):
            avg_wins = sum(
                self._record_in_group(t, clique)[0] for t in clique
            ) / max(len(clique), 1)
            # Negate both: largest size first, then highest avg wins first.
            return (-len(clique), -avg_wins)

        all_cliques.sort(key=clique_sort_key)

        # ---- Assign ranks ----
        ranked_teams: list = []
        ranked_set: set = set()
        team_group: dict = {}   # team -> clique list used for ranking

        for clique in all_cliques:
            unranked = [t for t in clique if t not in ranked_set]
            if not unranked:
                continue

            ordered = self._rank_group(unranked, clique)
            for team in ordered:
                ranked_teams.append(team)
                ranked_set.add(team)
                team_group[team] = clique

        # ---- Handle teams with no round-robin group (independents, etc.) ----
        remaining = [t for t in self.teams if t not in ranked_set]
        if remaining:
            overall: dict = {}
            for team in remaining:
                w = l = pf = pa = 0
                for (ta, tb), (sa, sb) in self._game_map.items():
                    if ta == team:
                        pf += sa; pa += sb
                        if sa > sb: w += 1
                        else: l += 1
                    elif tb == team:
                        pf += sb; pa += sa
                        if sb > sa: w += 1
                        else: l += 1
                overall[team] = {"wins": w, "losses": l, "pf": pf, "pa": pa,
                                 "win_pct": self._win_pct(w, l)}

            remaining.sort(key=lambda t: (
                -overall[t]["win_pct"],
                -(overall[t]["pf"] - overall[t]["pa"]),
                t,
            ))
            for team in remaining:
                ranked_teams.append(team)
                team_group[team] = [team]   # solo pseudo-group

        # ---- Build output records ----
        results = []
        for rank_idx, team in enumerate(ranked_teams, start=1):
            group = team_group[team]
            w, l, pf, pa = self._record_in_group(team, group)
            total = w + l
            results.append({
                "rank": rank_idx,
                "team": team,
                "group_size": len(group),
                "wins": w,
                "losses": l,
                "win_pct": round(self._win_pct(w, l), 3),
                "points_for": pf,
                "points_against": pa,
                "point_diff": pf - pa,
            })

        return results


# ---------------------------------------------------------------------------
# Demo data generator
# ---------------------------------------------------------------------------

def generate_demo_games() -> list:
    """Return a list of game-result dicts demonstrating multiple group tiers.

    Groups:
      * Power Conference – 8 teams, full round-robin (28 games)
      * Mid-Major Conference – 6 teams, full round-robin (15 games)
      * Small Conference – 4 teams, full round-robin (6 games)
      * Trio Group – 3 teams, full round-robin (3 games)
      * Independents – 3 teams that played some games but form no
                       complete round-robin among themselves

    The dataset includes a 3-way tie in the Power Conference (at 3-4) to
    exercise the head-to-head tiebreaker, and a 2-way tie in the Mid-Major
    (at 2-3) resolved by point differential.
    """
    games = []

    def g(home, hs, away, as_):
        games.append({"home_team": home, "home_score": hs,
                      "away_team": away, "away_score": as_})

    # -----------------------------------------------------------------------
    # Power Conference (8 teams)  –  Aces, Bears, Colts, Dukes,
    #                                Eagles, Falcons, Gators, Hawks
    # Final record inside the group:
    #   Aces   7-0   Bears  6-1   Colts  5-2   Dukes  4-3
    #   Eagles 3-4   Falcons 3-4  Gators 3-4   Hawks  0-7
    # Eagles / Falcons / Gators are 3-4 — will be sorted by h2h among them.
    #   Eagles beat Falcons, Falcons beat Gators, Gators beat Eagles (cycle).
    #   → fallback to point differential within the clique.
    # -----------------------------------------------------------------------
    g("Aces",    48, "Bears",   24)
    g("Aces",    45, "Colts",   17)
    g("Aces",    38, "Dukes",   21)
    g("Aces",    27, "Eagles",  10)
    g("Aces",    42, "Falcons",  7)
    g("Aces",    55, "Gators",  14)
    g("Aces",    48, "Hawks",   13)
    g("Bears",   35, "Colts",   28)
    g("Bears",   21, "Dukes",   17)
    g("Bears",   31, "Eagles",  14)
    g("Bears",   28, "Falcons", 10)
    g("Bears",   45, "Gators",  21)
    g("Bears",   38, "Hawks",   17)
    g("Colts",   24, "Dukes",   21)
    g("Colts",   35, "Eagles",  17)
    g("Colts",   28, "Falcons", 14)
    g("Colts",   42, "Gators",  10)
    g("Colts",   31, "Hawks",   21)
    g("Dukes",   17, "Eagles",  14)   # Dukes wins
    g("Dukes",   24, "Falcons", 17)   # Dukes wins
    g("Dukes",   35, "Gators",  28)   # Dukes wins
    g("Dukes",   28, "Hawks",   21)   # Dukes wins
    g("Eagles",  24, "Falcons", 17)   # Eagles beats Falcons
    g("Eagles",  17, "Gators",  24)   # Gators beats Eagles
    g("Eagles",  28, "Hawks",   21)   # Eagles beats Hawks
    g("Falcons", 21, "Gators",  17)   # Falcons beats Gators
    g("Falcons", 24, "Hawks",   14)   # Falcons beats Hawks
    g("Gators",  17, "Hawks",   14)   # Gators beats Hawks

    # -----------------------------------------------------------------------
    # Mid-Major Conference (6 teams)  –  Rams, Spartans, Tigers, Vikings,
    #                                    Wildcats, Zephyrs
    # Final records inside group:
    #   Rams     5-0   Spartans  4-1   Tigers  3-2
    #   Vikings  2-3   Wildcats  2-3   Zephyrs 0-5
    # Vikings / Wildcats tied 2-3: Vikings has better point diff → ranks higher.
    # -----------------------------------------------------------------------
    g("Rams",     35, "Spartans", 14)
    g("Rams",     28, "Tigers",   21)
    g("Rams",     42, "Vikings",  17)
    g("Rams",     31, "Wildcats", 10)
    g("Rams",     38, "Zephyrs",  14)
    g("Spartans", 24, "Tigers",   21)
    g("Spartans", 35, "Vikings",  17)
    g("Spartans", 28, "Wildcats", 14)
    g("Spartans", 45, "Zephyrs",  21)
    g("Tigers",   31, "Vikings",  24)
    g("Tigers",   24, "Wildcats", 17)
    g("Tigers",   28, "Zephyrs",  14)
    g("Vikings",  28, "Wildcats", 21)   # Vikings beats Wildcats
    g("Vikings",  35, "Zephyrs",  14)   # Vikings beats Zephyrs
    g("Wildcats", 21, "Zephyrs",  14)   # Wildcats beats Zephyrs

    # -----------------------------------------------------------------------
    # Small Conference (4 teams)  –  Lions, Panthers, Sharks, Wolves
    # Final records:
    #   Lions 3-0, Panthers 2-1, Wolves 1-2, Sharks 0-3
    # -----------------------------------------------------------------------
    g("Lions",   21, "Panthers", 14)
    g("Lions",   28, "Sharks",   17)
    g("Lions",   17, "Wolves",   14)   # Lions wins tight one
    g("Panthers", 24, "Sharks",  21)
    g("Panthers", 21, "Wolves",  17)   # Panthers beats Wolves
    g("Wolves",  21, "Sharks",   17)   # Wolves beats Sharks

    # -----------------------------------------------------------------------
    # Trio Group (3 teams)  –  Bobcats, Cougars, Mustangs
    # Final records:
    #   Mustangs 2-0, Bobcats 1-1, Cougars 0-2
    # -----------------------------------------------------------------------
    g("Bobcats",  35, "Cougars",  21)   # Bobcats beats Cougars
    g("Mustangs", 28, "Bobcats",  14)   # Mustangs beats Bobcats
    g("Mustangs", 24, "Cougars",  17)   # Mustangs beats Cougars

    # -----------------------------------------------------------------------
    # Independents (3 teams, no complete round-robin among themselves)
    # Each plays 2–3 games but the trio never all played each other.
    # Lone Wolf played Aces (from Power) and Rams (from Mid-Major).
    # -----------------------------------------------------------------------
    g("Lone Wolf",   35, "Rams",      28)   # Lone Wolf wins  (2 total games: 1-1)
    g("Aces",        45, "Lone Wolf", 17)   # Aces wins
    g("Road Runner", 14, "Bears",     31)   # Bears wins      (1 game: 0-1)
    g("Road Runner", 21, "Spartans",  28)   # Spartans wins   (1 game: 0-2)
    g("Trailblazer", 31, "Lions",     24)   # Trailblazer wins (1 game: 1-0)

    return games


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def print_rankings(results: list) -> None:
    """Print rankings as a formatted table to stdout."""
    print()
    print("=" * 90)
    print("  FBS ROUND-ROBIN RANKINGS")
    print("=" * 90)
    hdr = f"  {'Rank':<5} {'Team':<22} {'Grp':<5} {'W-L':<8} {'Win%':<7} {'PF':<6} {'PA':<6} {'Diff'}"
    print(hdr)
    sep = "-" * 90

    prev_size = None
    for r in results:
        if prev_size is not None and r["group_size"] != prev_size:
            print()   # blank line between tiers
        print(sep)
        wl = f"{r['wins']}-{r['losses']}"
        diff = f"{r['point_diff']:+d}"
        print(
            f"  {r['rank']:<5} {r['team']:<22} {r['group_size']:<5} "
            f"{wl:<8} {r['win_pct']:<7.3f} {r['points_for']:<6} "
            f"{r['points_against']:<6} {diff}"
        )
        prev_size = r["group_size"]

    print("=" * 90)
    print()


def save_csv(results: list, filepath: str) -> None:
    """Save rankings to a CSV file."""
    fieldnames = [
        "rank", "team", "group_size", "wins", "losses",
        "win_pct", "points_for", "points_against", "point_diff",
    ]
    with open(filepath, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)
    print(f"Rankings saved to: {filepath}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fbs_ranker",
        description="FBS Round-Robin Ranking Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python fbs_ranker.py games.csv
  python fbs_ranker.py games.csv --output rankings.csv
  python fbs_ranker.py --demo
  python fbs_ranker.py --demo --output demo_rankings.csv

CSV input format:
  Required columns: home_team, home_score, away_team, away_score
  Optional column:  date (ignored)
        """,
    )
    parser.add_argument(
        "input", nargs="?", help="CSV file with game results"
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="Run with built-in demo data and save demo_games.csv"
    )
    parser.add_argument(
        "--output", "-o", metavar="FILE",
        help="Also save rankings to this CSV file"
    )

    args = parser.parse_args()

    if not args.demo and not args.input:
        parser.print_help()
        sys.exit(1)

    ranker = FBSRoundRobinRanker()

    if args.demo:
        print("Generating demo game data …")
        games = generate_demo_games()
        for row in games:
            ranker.add_game(row["home_team"], row["away_team"],
                            row["home_score"], row["away_score"])
        demo_csv = "demo_games.csv"
        with open(demo_csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=["home_team", "home_score", "away_team", "away_score"]
            )
            writer.writeheader()
            writer.writerows(games)
        print(f"Demo games written to: {demo_csv}")
    else:
        print(f"Loading games from: {args.input}")
        ranker.load_csv(args.input)

    print(f"Teams: {len(ranker.teams)}  |  Games: {len(ranker._game_map)}")

    results = ranker.rank()
    print_rankings(results)

    if args.output:
        save_csv(results, args.output)


if __name__ == "__main__":
    main()
