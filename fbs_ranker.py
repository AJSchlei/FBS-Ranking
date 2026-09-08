#!/usr/bin/env python3
"""
FBS Round-Robin Ranking Tool

Ranks FBS football teams by discovering round-robin groups (cliques where every
team played every other team) and using those groups as the authoritative basis
for comparison — with larger groups taking priority over smaller ones.

Algorithm:
  1. Build an undirected game graph: nodes = teams, edges = games played.
  2. Find all maximal cliques (Bron-Kerbosch via NetworkX).  Each maximal clique
     is the largest round-robin group that cannot be extended further.
  3. For every pair of teams, determine their relative order using the LARGEST
     clique that contains both of them ("shared clique"):
       a. Compare win percentage within that shared clique (descending).
       b. Still tied → compare cumulative point differential within that clique.
       c. Still tied → repeat steps a-b with the next-smaller shared clique.
          A smaller shared clique can only break an existing tie; it can never
          reverse an order established by a larger clique.
       d. No shared clique → compare by overall (all-games) win percentage,
          then overall point differential, then alphabetical name.
     Each comparison records its STRENGTH: the size of the group that decided
     it (0 for the overall-record fallback in step d).
  4. Combine the pairwise results into one global order using Tideman's
     ranked-pairs procedure: sort every comparison strongest-first, then lock
     each one in unless it contradicts what the already-locked (stronger)
     comparisons imply.  When pairwise results conflict, the ordering made
     inside the largest group therefore wins, and the result is acyclic by
     construction.
  5. Each team's displayed record is their win-loss-PF-PA within their
     largest (primary) clique.

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

    def _overall_record(self, team: str) -> tuple:
        """Return (wins, losses, points_for, points_against) across all games."""
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
        return w, l, pf, pa

    def _pairwise_compare(self, team_a: str, team_b: str,
                          all_cliques: list) -> tuple:
        """Compare two teams and report how authoritative the comparison is.

        Returns ``(cmp, strength)``.  *cmp* is -1 if team_a ranks higher, 1 if
        team_b ranks higher, 0 if the two are genuinely indistinguishable.

        *strength* is ``(group_size, win_pct_gap, point_diff_gap)`` describing
        the round-robin group that actually decided the comparison.  ``rank()``
        uses it to settle conflicts between pairwise results: a comparison made
        inside a larger group beats one made inside a smaller group.

        Strategy:
          1. Find every clique containing BOTH teams (shared cliques).
          2. Process from largest shared clique to smallest:
               a. Compare by win pct within that clique.
               b. If tied, compare by point differential within that clique.
               c. If still tied, move to the next-smaller shared clique.
             A smaller clique can only break an existing tie — it can never
             reverse an ordering established by a larger clique.
          3. If no shared clique (or all shared cliques are completely tied),
             fall back to overall win pct, then overall point diff, then name.
             This fallback reports group_size 0 — the weakest strength there
             is — so any chain of group-based orderings overrides it.
        """
        # All cliques containing both teams, largest first.
        # Use sorted team list as a stable secondary key.
        shared = sorted(
            [c for c in all_cliques if team_a in c and team_b in c],
            key=lambda c: (-len(c), sorted(c)),
        )

        for clique in shared:
            wa, la, pfa, paa = self._record_in_group(team_a, clique)
            wb, lb, pfb, pab = self._record_in_group(team_b, clique)

            wpc_a = self._win_pct(wa, la)
            wpc_b = self._win_pct(wb, lb)
            diff_a = pfa - paa
            diff_b = pfb - pab

            if abs(wpc_a - wpc_b) > 1e-9:
                strength = (len(clique), abs(wpc_a - wpc_b), abs(diff_a - diff_b))
                return (-1 if wpc_a > wpc_b else 1), strength

            # Tied on win pct: compare point differential within this clique.
            # (Using point diff avoids the non-transitivity that raw h2h creates
            # in 3-way cycles; for 2-team cliques win pct already encodes h2h.)
            if diff_a != diff_b:
                strength = (len(clique), 0.0, abs(diff_a - diff_b))
                return (-1 if diff_a > diff_b else 1), strength

            # Completely tied in this clique → try the next-smaller shared clique.

        # No shared clique (or tied across all of them): fall back to overall
        # record.  group_size 0 marks this as the weakest possible evidence, so
        # ranked pairs discards it first whenever it conflicts with a group.
        ow_a, ol_a, opf_a, opa_a = self._overall_record(team_a)
        ow_b, ol_b, opf_b, opa_b = self._overall_record(team_b)

        wpc_a = self._win_pct(ow_a, ol_a)
        wpc_b = self._win_pct(ow_b, ol_b)
        diff_a = opf_a - opa_a
        diff_b = opf_b - opa_b

        if abs(wpc_a - wpc_b) > 1e-9:
            strength = (0, abs(wpc_a - wpc_b), abs(diff_a - diff_b))
            return (-1 if wpc_a > wpc_b else 1), strength

        if diff_a != diff_b:
            return (-1 if diff_a > diff_b else 1), (0, 0.0, abs(diff_a - diff_b))

        # Alphabetical as deterministic final fallback.
        if team_a < team_b:
            return -1, (0, 0.0, 0)
        if team_a > team_b:
            return 1, (0, 0.0, 0)
        return 0, (0, 0.0, 0)

    # ------------------------------------------------------------------
    # Public ranking API
    # ------------------------------------------------------------------

    def rank(self) -> list:
        """Perform the full round-robin ranking.

        For each pair of teams, the comparison uses the largest shared clique
        (round-robin group they both belong to).  Smaller shared cliques serve
        only as tiebreakers and never reverse a larger clique's ordering.
        Teams with no shared clique are compared by overall record.

        Conflicts between pairwise results (e.g. A over B, B over C, but C
        over A) are resolved by Tideman's ranked-pairs procedure: every
        comparison is sorted by the size of the group that decided it and
        locked in strongest-first, and any comparison that would contradict
        the already-locked ones is discarded.  The ordering established inside
        the largest group therefore always wins a conflict, and the resulting
        order is acyclic by construction.

        Returns:
            A list of dicts (one per team), ordered best-to-worst, with keys:
                rank          - integer position (1 = best)
                team          - team name string
                group_size    - size of the team's largest (primary) clique
                wins          - wins inside that primary clique
                losses        - losses inside that primary clique
                win_pct       - wins / (wins + losses), rounded to 3 dp
                points_for    - cumulative PF vs. primary-clique opponents
                points_against- cumulative PA vs. primary-clique opponents
                point_diff    - points_for − points_against
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

        # ---- For display: each team's PRIMARY clique (largest they belong to) ----
        def primary_clique(team: str) -> list:
            team_cliques = [c for c in all_cliques if team in c]
            if not team_cliques:
                return [team]
            # Priority: largest clique, then best win pct within that clique,
            # then best point differential, then alphabetical (deterministic).
            def key(c):
                w, l, pf, pa = self._record_in_group(team, c)
                return (len(c), self._win_pct(w, l), pf - pa, sorted(c))
            return max(team_cliques, key=key)

        team_primary = {t: primary_clique(t) for t in self.teams}

        # ---- Collect every pairwise verdict, with its strength ----
        # We evaluate all O(n²) pairs; each yields one candidate ordering plus
        # the size of the round-robin group that decided it.
        teams_list = sorted(self.teams)   # deterministic iteration order
        candidates = []
        for i in range(len(teams_list)):
            for j in range(i + 1, len(teams_list)):
                a, b = teams_list[i], teams_list[j]
                cmp, strength = self._pairwise_compare(a, b, all_cliques)
                if cmp < 0:
                    candidates.append((strength, a, b))
                elif cmp > 0:
                    candidates.append((strength, b, a))
                # cmp == 0: teams are indistinguishable; no candidate added.

        # Strongest first: biggest deciding group, then the most decisive
        # margin inside it.  Team names break remaining ties deterministically.
        candidates.sort(key=lambda c: (-c[0][0], -c[0][1], -c[0][2], c[1], c[2]))

        # ---- Lock verdicts in strongest-first (Tideman's ranked pairs) ----
        # A verdict is discarded if the verdicts already locked in imply the
        # opposite order.  Since stronger verdicts are locked first, the
        # largest shared group always wins a conflict — and the resulting
        # graph is acyclic by construction, so no cycle handling is needed.
        #
        # Reachability is kept as integer bitmasks, which makes the
        # "would this create a cycle?" test a single bitwise AND.
        bit = {t: 1 << i for i, t in enumerate(teams_list)}
        # desc[t]: teams t ranks above (transitively), including t itself.
        # anc[t] : teams that rank above t (transitively), including t itself.
        desc = {t: bit[t] for t in teams_list}
        anc = {t: bit[t] for t in teams_list}

        def set_bits(mask: int):
            """Yield the team names whose bits are set in *mask*."""
            while mask:
                low = mask & -mask
                yield teams_list[low.bit_length() - 1]
                mask ^= low

        order = nx.DiGraph()
        order.add_nodes_from(teams_list)
        for _strength, winner, loser in candidates:
            if desc[loser] & bit[winner]:
                # Stronger verdicts already put loser above winner.  The larger
                # group's ordering wins, so this weaker verdict is discarded.
                continue
            if desc[winner] & bit[loser]:
                continue                  # already implied transitively
            order.add_edge(winner, loser)
            above, below = anc[winner], desc[loser]
            for t in set_bits(above):
                desc[t] |= below
            for t in set_bits(below):
                anc[t] |= above

        # Acyclic by construction.  Teams left mutually unordered (only
        # possible when _pairwise_compare called them exactly equal) fall back
        # to alphabetical order.
        sorted_teams = list(nx.lexicographical_topological_sort(order))

        # ---- Build output records ----
        results = []
        for rank_idx, team in enumerate(sorted_teams, start=1):
            primary = team_primary[team]
            w, l, pf, pa = self._record_in_group(team, primary)
            results.append({
                "rank": rank_idx,
                "team": team,
                "group_size": len(primary),
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

    The dataset includes a 3-way tie in the Power Conference (at 2-5) to
    exercise the point-differential tiebreaker (cyclic h2h makes h2h record
    among the three teams all 1-1, so point diff decides).

    Cross-conference games let independents be placed relative to conference
    teams via shared 2-cliques, demonstrating the largest-shared-clique rule.
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
    # Independents – 3 teams that each play 2 cross-conference games so we
    # can compare them to conference teams via shared 2-cliques.
    # Each independent beats the last-place team in one conference and loses
    # to a strong team in another.  This creates meaningful cross-group
    # bridges without generating cyclic preferences.
    #
    # Lone Wolf:   beats Zephyrs (last in Mid-Major), loses to Aces (Power 1st)
    # Road Runner: beats Sharks   (last in Small),    loses to Bears (Power 2nd)
    # Trailblazer: beats Cougars  (last in Trio),     loses to Lions (Small 1st)
    # -----------------------------------------------------------------------
    g("Lone Wolf",    35, "Zephyrs",     14)   # Lone Wolf beats last Mid-Major
    g("Aces",         45, "Lone Wolf",   17)   # Aces beats Lone Wolf
    g("Road Runner",  35, "Sharks",      17)   # Road Runner beats last Small
    g("Bears",        31, "Road Runner", 14)   # Bears beats Road Runner
    g("Trailblazer",  28, "Cougars",     14)   # Trailblazer beats last Trio
    g("Lions",        21, "Trailblazer", 14)   # Lions beats Trailblazer

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
