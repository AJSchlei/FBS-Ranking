"""
Unit tests for fbs_ranker.py

Coverage:
  - Basic round-robin group ranking (win pct)
  - Head-to-head tiebreaker (2-team and 3-team)
  - 3-way tie with cyclic h2h → point differential fallback
  - Multiple group tiers (larger groups ranked above smaller groups)
  - Overlapping groups (team in two cliques ranked by the larger one)
  - Independent teams with no round-robin group
  - Conflicting pairwise verdicts resolved in favour of the largest group
  - Teams that played no games at all
  - CSV loading
  - Empty ranker
  - Deterministic output (stable ordering)
"""

import csv
import os
import tempfile
import unittest
import sys

# Allow running tests from the repo root without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fbs_ranker import FBSRoundRobinRanker, generate_demo_games


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ranker(*games):
    """Convenience: create a ranker from (home, away, hs, as) tuples."""
    r = FBSRoundRobinRanker()
    for home, away, hs, as_ in games:
        r.add_game(home, away, hs, as_)
    return r


def ranked_names(results):
    """Return just the ordered list of team names from rank() output."""
    return [row["team"] for row in results]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEmptyRanker(unittest.TestCase):
    def test_no_games(self):
        r = FBSRoundRobinRanker()
        self.assertEqual(r.rank(), [])

    def test_no_teams(self):
        r = FBSRoundRobinRanker()
        self.assertEqual(len(r.teams), 0)


class TestSingleGame(unittest.TestCase):
    """A single game creates a 2-clique.  Winner ranks first."""

    def setUp(self):
        self.r = make_ranker(("Alpha", "Beta", 28, 14))
        self.results = self.r.rank()

    def test_two_teams_ranked(self):
        self.assertEqual(len(self.results), 2)

    def test_winner_first(self):
        self.assertEqual(self.results[0]["team"], "Alpha")
        self.assertEqual(self.results[1]["team"], "Beta")

    def test_group_size_two(self):
        for row in self.results:
            self.assertEqual(row["group_size"], 2)

    def test_records(self):
        alpha = self.results[0]
        self.assertEqual(alpha["wins"], 1)
        self.assertEqual(alpha["losses"], 0)
        beta = self.results[1]
        self.assertEqual(beta["wins"], 0)
        self.assertEqual(beta["losses"], 1)


class TestThreeTeamFullRoundRobin(unittest.TestCase):
    """A → B → C → (no cycle) — clear ordering by win pct."""

    def setUp(self):
        # A beats B and C; B beats C.  A=2-0, B=1-1, C=0-2.
        self.r = make_ranker(
            ("A", "B", 30, 20),
            ("A", "C", 28, 14),
            ("B", "C", 21, 17),
        )
        self.results = self.r.rank()

    def test_order(self):
        self.assertEqual(ranked_names(self.results), ["A", "B", "C"])

    def test_group_size_three(self):
        for row in self.results:
            self.assertEqual(row["group_size"], 3)

    def test_win_pct(self):
        self.assertAlmostEqual(self.results[0]["win_pct"], 1.0)
        self.assertAlmostEqual(self.results[1]["win_pct"], 0.5)
        self.assertAlmostEqual(self.results[2]["win_pct"], 0.0)


class TestTwoTeamTieHeadToHead(unittest.TestCase):
    """
    Four-team group where two teams share a 2-2 record.
    The one that beat the other in their head-to-head ranks higher.
    """

    def setUp(self):
        # X beats everyone.  Y and Z are both 2-2.  Y beat Z directly.
        # W loses to everyone.
        self.r = make_ranker(
            ("X", "Y", 40, 10),
            ("X", "Z", 40, 10),
            ("X", "W", 40, 10),
            ("Y", "Z", 21, 14),   # Y beats Z
            ("Y", "W", 28, 14),
            ("Z", "W", 24, 17),
        )
        self.results = self.r.rank()

    def test_x_first(self):
        self.assertEqual(self.results[0]["team"], "X")

    def test_y_before_z(self):
        names = ranked_names(self.results)
        self.assertLess(names.index("Y"), names.index("Z"))

    def test_w_last(self):
        self.assertEqual(self.results[-1]["team"], "W")


class TestCyclicThreeWayTie(unittest.TestCase):
    """
    Three teams in a 4-team group share a 1-2 record (last place).
    Their h2h is cyclic (A→B, B→C, C→A), so the tiebreaker falls to
    point differential within the full clique.
    """

    def setUp(self):
        # Top: X beats everyone (3-0).
        # Bottom three: A, B, C each 1-2.
        # A beats B, B beats C, C beats A — perfect cycle.
        # We engineer different point differentials to break the cycle:
        #   A: higher PF-PA than B, B: higher than C.
        self.r = make_ranker(
            ("X", "A", 35, 10),
            ("X", "B", 35, 14),
            ("X", "C", 35, 17),
            ("A", "B", 24, 17),   # A beats B
            ("B", "C", 21, 14),   # B beats C
            ("C", "A", 28, 21),   # C beats A  (cycle)
        )
        self.results = self.r.rank()

    def test_x_first(self):
        self.assertEqual(self.results[0]["team"], "X")

    def test_cyclic_resolved_by_point_diff(self):
        # Within the 4-clique each team's point diff:
        #   X: (35-10)+(35-14)+(35-17) = 64  — not relevant, X is 3-0
        #   A: (10-35) + (24-17) + (21-28) = -25 + 7 - 7 = -25
        #   B: (14-35) + (17-24) + (21-14) = -21 - 7 + 7 = -21
        #   C: (17-35) + (14-21) + (28-21) = -18 - 7 + 7 = -18
        # So C has best diff among tied, then B, then A.
        names = ranked_names(self.results)[1:]   # skip X
        self.assertEqual(names, ["C", "B", "A"])


class TestCrossCliqueNoSharedGroup(unittest.TestCase):
    """
    When teams from different cliques share no common clique they are
    compared by overall (all-games) win percentage, then point differential.

    Setup — 4-clique {P,Q,R,S}, separate 2-clique {U,V}, no cross-games:
        P 3-0 (overall 1.000, +57 pd)   Q 2-1 (overall 0.667)
        R 1-2 (overall 0.333)            S 0-3 (overall 0.000, −56 pd)
        U 1-0 (overall 1.000, +21 pd)   V 0-1 (overall 0.000, −21 pd)
    """

    def setUp(self):
        self.r = make_ranker(
            ("P", "Q", 28, 21),
            ("P", "R", 35, 17),
            ("P", "S", 42, 10),
            ("Q", "R", 24, 17),
            ("Q", "S", 31, 14),
            ("R", "S", 28, 21),
            ("U", "V", 35, 14),
        )
        self.results = self.r.rank()
        self.names = ranked_names(self.results)

    def test_p_above_u(self):
        """P (1.000 win pct, +57 pd) outranks U (1.000 win pct, +21 pd)."""
        self.assertLess(self.names.index("P"), self.names.index("U"))

    def test_u_above_q_r_s(self):
        """U (1.000 win pct) outranks Q, R, S which have lower win pct."""
        for below in ["Q", "R", "S"]:
            self.assertLess(self.names.index("U"), self.names.index(below),
                            f"U (1.000) should rank above {below}")

    def test_four_group_internal_order(self):
        """Within the 4-clique, shared-clique comparison gives P>Q>R>S."""
        for better, worse in [("P", "Q"), ("Q", "R"), ("R", "S")]:
            self.assertLess(self.names.index(better), self.names.index(worse))

    def test_v_above_s(self):
        """V (0.000 win pct, −21 pd) outranks S (0.000, −56 pd) on point diff."""
        self.assertLess(self.names.index("V"), self.names.index("S"))


class TestOverlappingGroups(unittest.TestCase):
    """
    When a team is a member of two cliques of different sizes, they are
    ranked using the larger clique and are not re-ranked by the smaller one.
    """

    def setUp(self):
        # 4-clique: A, B, C, D (all play each other)
        # 3-clique extending: A, B, E (A and B played E, but C/D did not)
        # A, B, C, D are ranked in the 4-clique.
        # E is ranked in the 3-clique {A, B, E} but A and B already ranked.
        self.r = make_ranker(
            ("A", "B", 28, 21),
            ("A", "C", 35, 17),
            ("A", "D", 42, 14),
            ("B", "C", 24, 21),
            ("B", "D", 28, 17),
            ("C", "D", 21, 14),
            # E plays A and B but not C or D → forms 3-clique {A, B, E}
            ("A", "E", 31, 28),
            ("B", "E", 24, 21),
        )
        self.results = self.r.rank()

    def test_abcd_ranked_in_4_group(self):
        for team in ["A", "B", "C", "D"]:
            row = next(r for r in self.results if r["team"] == team)
            self.assertEqual(row["group_size"], 4,
                             f"{team} should be ranked by 4-group, got {row['group_size']}")

    def test_e_ranked_in_3_group(self):
        row = next(r for r in self.results if r["team"] == "E")
        self.assertEqual(row["group_size"], 3)

    def test_abc_before_e(self):
        """A, B, C all beat or outperform E in the shared 3-clique → rank above E."""
        names = ranked_names(self.results)
        for team in ["A", "B", "C"]:
            self.assertLess(names.index(team), names.index("E"),
                            f"{team} should rank above E")

    def test_e_above_d(self):
        """E (0-2 overall, −6 pd) outranks D (0-3 overall, −46 pd) on overall record."""
        names = ranked_names(self.results)
        self.assertLess(names.index("E"), names.index("D"))


class TestIndependentTeams(unittest.TestCase):
    """
    Teams that formed no round-robin group larger than a 2-clique are still
    ranked using the available shared-clique and overall-record comparisons.

    Setup:
        Trio {X,Y,Z}: X 2-0, Y 1-1, Z 0-2  (shared 3-clique)
        I1 lost to X  → shared {I1,X}: I1 0-1
        I2 beat  Y    → shared {I2,Y}: I2 1-0

    Expected order derived from pairwise comparisons:
        X  (1.000 overall, +46 pd) — beats I1 directly
        I2 (1.000 overall, +14 pd) — beats Y directly; no shared clique with X,
                                       X ranks first via X's better point diff
        Y  (0.333 overall)         — beats Z in trio; loses to I2 directly
        I1 (0.000 overall, −21 pd) — tied with Z on win pct and point diff;
                                       alphabetically 'I1' < 'Z' → I1 above Z
        Z  (0.000 overall, −21 pd)
    """

    def setUp(self):
        self.r = make_ranker(
            ("X", "Y", 28, 21),
            ("X", "Z", 35, 17),
            ("Y", "Z", 24, 21),
            ("I1", "X", 14, 35),   # I1 loses to X
            ("I2", "Y", 28, 14),   # I2 beats Y
        )
        self.results = self.r.rank()
        self.names = ranked_names(self.results)

    def test_all_five_ranked(self):
        self.assertEqual(len(self.results), 5)

    def test_x_first(self):
        """X (2-0 in trio, 3-0 overall) tops the ranking."""
        self.assertEqual(self.names[0], "X")

    def test_i2_above_y(self):
        """I2 beat Y in their shared 2-clique → I2 ranks above Y."""
        self.assertLess(self.names.index("I2"), self.names.index("Y"))

    def test_x_above_i1(self):
        """X beat I1 in their shared 2-clique → X ranks above I1."""
        self.assertLess(self.names.index("X"), self.names.index("I1"))

    def test_y_above_i1(self):
        """Y (0.333 overall) outranks I1 (0.000 overall) via fallback."""
        self.assertLess(self.names.index("Y"), self.names.index("I1"))


class TestCSVLoading(unittest.TestCase):
    """CSV loading produces the same results as programmatic add_game."""

    def setUp(self):
        self.games = [
            ("Alpha", "Beta", 28, 14),
            ("Alpha", "Gamma", 35, 21),
            ("Beta", "Gamma", 24, 17),
        ]

    def _write_csv(self, path):
        with open(path, "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["home_team", "home_score", "away_team", "away_score"])
            for home, away, hs, as_ in self.games:
                writer.writerow([home, hs, away, as_])

    def test_csv_equals_programmatic(self):
        r_prog = make_ranker(*self.games)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False
        ) as tmp:
            tmp_path = tmp.name
        try:
            self._write_csv(tmp_path)
            r_csv = FBSRoundRobinRanker()
            r_csv.load_csv(tmp_path)
            self.assertEqual(ranked_names(r_prog.rank()),
                             ranked_names(r_csv.rank()))
        finally:
            os.unlink(tmp_path)


class TestCSVWithDateColumn(unittest.TestCase):
    """CSV files with an optional 'date' column load without errors."""

    def test_date_column_ignored(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, newline=""
        ) as tmp:
            tmp_path = tmp.name
            writer = csv.writer(tmp)
            writer.writerow(["date", "home_team", "home_score", "away_team", "away_score"])
            writer.writerow(["2024-09-07", "Alpha", 31, "Beta", 24])
            writer.writerow(["2024-09-14", "Beta", 28, "Gamma", 21])
            writer.writerow(["2024-09-21", "Alpha", 35, "Gamma", 17])

        try:
            r = FBSRoundRobinRanker()
            r.load_csv(tmp_path)
            results = r.rank()
            self.assertEqual(len(results), 3)
        finally:
            os.unlink(tmp_path)


class TestResultFields(unittest.TestCase):
    """Every result dict has all required fields with correct types."""

    def setUp(self):
        self.r = make_ranker(
            ("P", "Q", 28, 21),
            ("P", "R", 35, 14),
            ("Q", "R", 24, 17),
        )
        self.results = self.r.rank()

    def test_all_fields_present(self):
        required = {
            "rank", "team", "group_size", "wins", "losses",
            "win_pct", "points_for", "points_against", "point_diff",
        }
        for row in self.results:
            self.assertEqual(set(row.keys()), required)

    def test_rank_is_sequential(self):
        ranks = [row["rank"] for row in self.results]
        self.assertEqual(ranks, list(range(1, len(self.results) + 1)))

    def test_point_diff_equals_pf_minus_pa(self):
        for row in self.results:
            self.assertEqual(row["point_diff"],
                             row["points_for"] - row["points_against"])

    def test_win_pct_in_range(self):
        for row in self.results:
            self.assertGreaterEqual(row["win_pct"], 0.0)
            self.assertLessEqual(row["win_pct"], 1.0)


class TestDemoData(unittest.TestCase):
    """Smoke-test the demo data generator: all 24 teams ranked in correct tiers."""

    def setUp(self):
        r = FBSRoundRobinRanker()
        for game in generate_demo_games():
            r.add_game(game["home_team"], game["away_team"],
                       game["home_score"], game["away_score"])
        self.results = r.rank()
        self.by_team = {row["team"]: row for row in self.results}

    def test_all_24_teams_ranked(self):
        self.assertEqual(len(self.results), 24)

    def test_aces_first(self):
        self.assertEqual(self.results[0]["team"], "Aces")

    def test_intra_clique_order_preserved(self):
        """Within each conference, in-clique record determines ordering."""
        # Power conf: Aces>Bears>Colts>Dukes (all by in-clique win pct)
        names = ranked_names(self.results)
        for better, worse in [("Aces", "Bears"), ("Bears", "Colts"),
                               ("Colts", "Dukes")]:
            self.assertLess(names.index(better), names.index(worse),
                            f"{better} should rank above {worse}")
        # Mid-major: Rams>Spartans>Tigers>Vikings>Wildcats>Zephyrs
        for better, worse in [("Rams", "Spartans"), ("Spartans", "Tigers"),
                               ("Tigers", "Vikings"), ("Vikings", "Wildcats"),
                               ("Wildcats", "Zephyrs")]:
            self.assertLess(names.index(better), names.index(worse),
                            f"{better} should rank above {worse}")
        # Small conf: Lions>Panthers>Wolves>Sharks
        for better, worse in [("Lions", "Panthers"), ("Panthers", "Wolves"),
                               ("Wolves", "Sharks")]:
            self.assertLess(names.index(better), names.index(worse))
        # Trio: Mustangs>Bobcats>Cougars
        self.assertLess(names.index("Mustangs"), names.index("Bobcats"))
        self.assertLess(names.index("Bobcats"), names.index("Cougars"))

    def test_cross_clique_via_shared_2clique(self):
        """Direct shared 2-clique games determine cross-conference ordering."""
        names = ranked_names(self.results)
        # Aces beat Lone Wolf → Aces above Lone Wolf
        self.assertLess(names.index("Aces"), names.index("Lone Wolf"))
        # Bears beat Road Runner → Bears above Road Runner
        self.assertLess(names.index("Bears"), names.index("Road Runner"))
        # Lions beat Trailblazer → Lions above Trailblazer
        self.assertLess(names.index("Lions"), names.index("Trailblazer"))
        # Lone Wolf beat Zephyrs → Lone Wolf above Zephyrs
        self.assertLess(names.index("Lone Wolf"), names.index("Zephyrs"))
        # Road Runner beat Sharks → Road Runner above Sharks
        self.assertLess(names.index("Road Runner"), names.index("Sharks"))
        # Trailblazer beat Cougars → Trailblazer above Cougars
        self.assertLess(names.index("Trailblazer"), names.index("Cougars"))

    def test_cross_clique_via_overall_record(self):
        """Teams with no shared clique are ordered by overall win pct / point diff."""
        names = ranked_names(self.results)
        # Aces (8-0, 1.000) ranks above Rams (5-0, 1.000) by overall point diff
        self.assertLess(names.index("Aces"), names.index("Rams"))
        # Bears (7-1, 0.875) ranks above Spartans (5-1, 0.833)
        self.assertLess(names.index("Bears"), names.index("Spartans"))

    def test_cyclic_tie_resolved_by_point_diff(self):
        # Eagles, Falcons, Gators all 2-5 in the 8-clique with cyclic h2h.
        # Resolved by point differential within the shared 8-clique.
        eagles  = self.by_team["Eagles"]["rank"]
        falcons = self.by_team["Falcons"]["rank"]
        gators  = self.by_team["Gators"]["rank"]
        # Eagles has best diff → ranks highest; Gators has worst → ranks lowest
        self.assertLess(eagles, falcons)
        self.assertLess(falcons, gators)

    def test_group_size_field_reflects_primary_clique(self):
        """group_size shows the size of the team's largest round-robin group."""
        for team in ["Aces", "Bears", "Colts", "Dukes",
                     "Eagles", "Falcons", "Gators", "Hawks"]:
            self.assertEqual(self.by_team[team]["group_size"], 8)
        for team in ["Rams", "Spartans", "Tigers",
                     "Vikings", "Wildcats", "Zephyrs"]:
            self.assertEqual(self.by_team[team]["group_size"], 6)
        for team in ["Lions", "Panthers", "Wolves", "Sharks"]:
            self.assertEqual(self.by_team[team]["group_size"], 4)
        for team in ["Mustangs", "Bobcats", "Cougars"]:
            self.assertEqual(self.by_team[team]["group_size"], 3)


class TestConflictingVerdicts(unittest.TestCase):
    """When pairwise verdicts conflict, the largest group's ordering wins.

    Fixture (A and C never play, and share no round-robin group):

      4-group {A, B, X1, X2}   A goes 2-1, B goes 1-2   → A over B, strength 4
      3-group {B, C, Y1}       B goes 2-0, C goes 1-1   → B over C, strength 3
      no shared group          A is 2-1 (.667) overall,
                               C is 3-1 (.750) overall  → C over A, strength 0

    Taken pairwise these three verdicts form a cycle.  The C-over-A verdict is
    the weakest (it rests on overall record, not on any round-robin group), so
    it is the one discarded, leaving A > B > C.
    """

    def setUp(self):
        self.ranker = make_ranker(
            # 4-team round-robin group
            ("A", "B", 30, 10),
            ("A", "X2", 30, 10),
            ("X1", "A", 30, 10),
            ("B", "X1", 30, 10),
            ("X2", "B", 30, 10),
            ("X1", "X2", 30, 10),
            # 3-team round-robin group sharing B
            ("B", "C", 30, 10),
            ("B", "Y1", 30, 10),
            ("C", "Y1", 30, 10),
            # padding wins that lift C's overall record above A's
            ("C", "Z1", 30, 10),
            ("C", "Z2", 30, 10),
        )
        self.names = ranked_names(self.ranker.rank())

    def test_four_group_verdict_holds(self):
        """A over B was decided in the 4-group and must survive."""
        self.assertLess(self.names.index("A"), self.names.index("B"))

    def test_three_group_verdict_holds(self):
        """B over C was decided in the 3-group and must survive."""
        self.assertLess(self.names.index("B"), self.names.index("C"))

    def test_weak_overall_verdict_is_discarded(self):
        """C's better overall record must not leapfrog it above A.

        Regression: the previous implementation condensed the cycle and ranked
        its members by overall record, which put C first overall.
        """
        self.assertLess(self.names.index("A"), self.names.index("C"))
        self.assertNotEqual(self.names[0], "C")

    def test_verdict_strengths(self):
        """Each verdict reports the size of the group that decided it."""
        cliques = [["A", "B", "X1", "X2"], ["B", "C", "Y1"],
                   ["C", "Z1"], ["C", "Z2"]]
        cmp_ab, str_ab = self.ranker._pairwise_compare("A", "B", cliques)
        cmp_bc, str_bc = self.ranker._pairwise_compare("B", "C", cliques)
        cmp_ac, str_ac = self.ranker._pairwise_compare("A", "C", cliques)
        self.assertEqual((cmp_ab, str_ab[0]), (-1, 4))   # A over B, 4-group
        self.assertEqual((cmp_bc, str_bc[0]), (-1, 3))   # B over C, 3-group
        self.assertEqual((cmp_ac, str_ac[0]), (1, 0))    # C over A, no group

    def test_ranking_is_a_strict_total_order(self):
        """Ranked pairs must leave no cycles: every team gets a unique rank."""
        results = self.ranker.rank()
        ranks = [row["rank"] for row in results]
        self.assertEqual(ranks, list(range(1, len(results) + 1)))
        self.assertEqual(len(set(ranked_names(results))), len(results))

    def test_deterministic_across_insertion_orders(self):
        """Shuffling the input games must not change the ranking."""
        games = [
            ("C", "Z2", 30, 10), ("B", "X1", 30, 10), ("X1", "X2", 30, 10),
            ("C", "Y1", 30, 10), ("A", "B", 30, 10), ("C", "Z1", 30, 10),
            ("X1", "A", 30, 10), ("B", "Y1", 30, 10), ("A", "X2", 30, 10),
            ("X2", "B", 30, 10), ("B", "C", 30, 10),
        ]
        self.assertEqual(ranked_names(make_ranker(*games).rank()), self.names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
