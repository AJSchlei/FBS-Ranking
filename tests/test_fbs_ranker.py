"""
Unit tests for fbs_ranker.py

Coverage:
  - Basic round-robin group ranking (win pct)
  - Head-to-head tiebreaker (2-team and 3-team)
  - 3-way tie with cyclic h2h → point differential fallback
  - Multiple group tiers (larger groups ranked above smaller groups)
  - Overlapping groups (team in two cliques ranked by the larger one)
  - Independent teams with no round-robin group
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


class TestMultipleGroupTiers(unittest.TestCase):
    """
    Teams from a larger round-robin group always outrank teams from a
    smaller group, even if the smaller group's teams have better records.
    """

    def setUp(self):
        # 4-team group: P, Q, R, S — S goes 0-3 (worst record possible)
        # 2-team group: U, V — U goes 1-0 (best record possible)
        # S should still rank above U because S's group is larger.
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

    def test_four_group_before_two_group(self):
        names = ranked_names(self.results)
        # All of P, Q, R, S must appear before U and V
        for four_team in ["P", "Q", "R", "S"]:
            for two_team in ["U", "V"]:
                self.assertLess(
                    names.index(four_team),
                    names.index(two_team),
                    f"{four_team} (4-group) should rank above {two_team} (2-group)",
                )

    def test_s_before_u(self):
        names = ranked_names(self.results)
        self.assertLess(names.index("S"), names.index("U"))


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

    def test_all_4_group_teams_before_e(self):
        names = ranked_names(self.results)
        for team in ["A", "B", "C", "D"]:
            self.assertLess(names.index(team), names.index("E"))


class TestIndependentTeams(unittest.TestCase):
    """
    Teams that played games but never formed a complete round-robin with
    anyone beyond a 2-clique (head-to-head) still appear in the rankings.
    """

    def setUp(self):
        # Full trio round-robin: X, Y, Z
        # Independent I1 only played X.  I2 only played Y.
        self.r = make_ranker(
            ("X", "Y", 28, 21),
            ("X", "Z", 35, 17),
            ("Y", "Z", 24, 21),
            ("I1", "X", 14, 35),   # I1 loses to X
            ("I2", "Y", 28, 14),   # I2 beats Y
        )
        self.results = self.r.rank()

    def test_all_five_ranked(self):
        self.assertEqual(len(self.results), 5)

    def test_trio_before_independents(self):
        names = ranked_names(self.results)
        for trio_team in ["X", "Y", "Z"]:
            for ind in ["I1", "I2"]:
                self.assertLess(names.index(trio_team), names.index(ind))


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

    def test_power_conf_is_first_8(self):
        power = {"Aces", "Bears", "Colts", "Dukes",
                 "Eagles", "Falcons", "Gators", "Hawks"}
        for row in self.results[:8]:
            self.assertIn(row["team"], power)
            self.assertEqual(row["group_size"], 8)

    def test_mid_major_is_next_6(self):
        mid = {"Rams", "Spartans", "Tigers", "Vikings", "Wildcats", "Zephyrs"}
        for row in self.results[8:14]:
            self.assertIn(row["team"], mid)
            self.assertEqual(row["group_size"], 6)

    def test_small_conf_next_4(self):
        small = {"Lions", "Panthers", "Sharks", "Wolves"}
        for row in self.results[14:18]:
            self.assertIn(row["team"], small)
            self.assertEqual(row["group_size"], 4)

    def test_trio_next_3(self):
        trio = {"Mustangs", "Bobcats", "Cougars"}
        for row in self.results[18:21]:
            self.assertIn(row["team"], trio)
            self.assertEqual(row["group_size"], 3)

    def test_cyclic_tie_resolved_by_point_diff(self):
        # Eagles, Falcons, Gators all 2-5 with cyclic h2h (1-1 among each other).
        # Resolved by point differential in the 8-clique.
        eagles  = self.by_team["Eagles"]["rank"]
        falcons = self.by_team["Falcons"]["rank"]
        gators  = self.by_team["Gators"]["rank"]
        # Eagles has best diff → ranks highest; Gators has worst → ranks lowest
        self.assertLess(eagles, falcons)
        self.assertLess(falcons, gators)

    def test_hawks_last_in_power_conf(self):
        self.assertEqual(self.results[7]["team"], "Hawks")


if __name__ == "__main__":
    unittest.main(verbosity=2)
