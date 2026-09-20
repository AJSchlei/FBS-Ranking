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
  - Duplicate meetings between the same pair detected rather than overwritten
  - Overall records regressed toward .500 before the strength-0 fallback
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
from fbs_ranker import (FBSRoundRobinRanker, DuplicateGameError,
                        generate_demo_games, _is_ranked)


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


def assert_competition_ranking(case, results):
    """Ranks must follow standard competition ranking (1, 2, 2, 4, ...).

    Each rank equals one plus the number of teams ranked strictly above, so
    ranks start at 1, never decrease, and skip exactly as far as a tie is wide.
    """
    ranks = [row["rank"] for row in results]
    if not ranks:
        return
    case.assertEqual(ranks[0], 1, "best rank should be 1")
    case.assertEqual(ranks, sorted(ranks), "ranks must not decrease")
    for position, rank in enumerate(ranks):
        strictly_above = sum(1 for r in ranks if r < rank)
        case.assertEqual(rank, strictly_above + 1,
                         f"rank {rank} at position {position} skips wrongly")


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
        # Pinned to the record-only metric: this fixture is about the
        # fallback's ordering, not about opponent weighting.
        self.r.BLEND_WEIGHTS = (1.0, 0.0, 0.0)
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
        I1 (0.000 overall, −21 pd) — its only game was to undefeated X, so the
                                       opponent term of the blend lifts it
        Y  (0.333 overall)         — beats Z in trio; loses to I2 directly
        Z  (0.000 overall, −21 pd)

    No two of these teams share two opponents with differing results, so the
    common-opponent tier never speaks here and the blend decides every pair
    that no group settles.
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

    def test_y_above_i1_on_record_alone(self):
        """Weighted purely on record, Y (1-2) outranks I1 (0-1).

        This fixture is deliberately pinned to record-only weights.  I1 has
        played a single game, which is too little schedule for any opponent
        weighting to mean anything; what this class tests is that teams
        belonging to no shared group still get placed at all.  For what
        opponent weighting actually does, see
        TestOpponentWeightingOnAFullSchedule.
        """
        self.r.BLEND_WEIGHTS = (1.0, 0.0, 0.0)
        names = ranked_names(self.r.rank())
        self.assertLess(names.index("Y"), names.index("I1"))


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
            "rank", "team", "overall_wins", "overall_losses",
            "overall_win_pct", "group_size", "wins", "losses",
            "win_pct", "points_for", "points_against", "point_diff",
        }
        for row in self.results:
            self.assertEqual(set(row.keys()), required)

    def test_ranks_follow_competition_ranking(self):
        assert_competition_ranking(self, self.results)

    def test_no_ties_when_every_team_is_separated(self):
        """This fixture separates all three teams, so ranks run 1, 2, 3."""
        ranks = [row["rank"] for row in self.results]
        self.assertEqual(ranks, [1, 2, 3])

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
        # Record-only metric: this fixture exists to create a conflict between
        # a 4-group, a 3-group and the fallback, and the blend would resolve
        # A vs C the other way and dissolve the conflict being tested.
        self.ranker.BLEND_WEIGHTS = (1.0, 0.0, 0.0)
        cliques = [["A", "B", "X1", "X2"], ["B", "C", "Y1"],
                   ["C", "Z1"], ["C", "Z2"]]
        cmp_ab, str_ab = self.ranker._pairwise_compare("A", "B", cliques)
        cmp_bc, str_bc = self.ranker._pairwise_compare("B", "C", cliques)
        cmp_ac, str_ac = self.ranker._pairwise_compare("A", "C", cliques)
        self.assertEqual((cmp_ab, str_ab[0]), (-1, 4))   # A over B, 4-group
        self.assertEqual((cmp_bc, str_bc[0]), (-1, 3))   # B over C, 3-group
        self.assertEqual((cmp_ac, str_ac[0]), (1, 0))    # C over A, no group

    def test_ranking_has_no_cycles(self):
        """Ranked pairs must leave no cycles: ranks are a valid ordering."""
        results = self.ranker.rank()
        assert_competition_ranking(self, results)
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


class TestShrunkWinPct(unittest.TestCase):
    """The strength-0 fallback regresses overall records toward .500."""

    def setUp(self):
        self.r = FBSRoundRobinRanker()

    def test_arithmetic(self):
        """(wins + k/2) / (games + k), with the default k of 4."""
        self.assertAlmostEqual(self.r._shrunk_win_pct(2, 0), 4 / 6)    # .667
        self.assertAlmostEqual(self.r._shrunk_win_pct(7, 1), 9 / 12)   # .750
        self.assertAlmostEqual(self.r._shrunk_win_pct(8, 0), 10 / 12)  # .833
        self.assertAlmostEqual(self.r._shrunk_win_pct(0, 3), 2 / 7)    # .286

    def test_short_records_move_further(self):
        """A 2-0 record is pulled further from 1.000 than an 8-0 record."""
        short_drop = 1.0 - self.r._shrunk_win_pct(2, 0)
        long_drop = 1.0 - self.r._shrunk_win_pct(8, 0)
        self.assertGreater(short_drop, long_drop)

    def test_500_is_the_fixed_point(self):
        """An even record stays at .500 no matter how few games it covers."""
        for w in (1, 3, 20):
            self.assertAlmostEqual(self.r._shrunk_win_pct(w, w), 0.5)

    def test_prior_games_zero_restores_raw_win_pct(self):
        self.r.PRIOR_GAMES = 0
        for w, l in [(2, 0), (7, 1), (0, 3), (0, 0)]:
            self.assertAlmostEqual(self.r._shrunk_win_pct(w, l),
                                   self.r._win_pct(w, l))


class TestFallbackUsesShrunkRecord(unittest.TestCase):
    """Teams sharing no round-robin group are compared on shrunk records.

    P goes 7-1 against eight opponents who never play each other; Q goes 2-0
    against two more. P and Q never meet and share no group, so the strength-0
    fallback decides them.

      raw:     Q 1.000 beats P .875   -> Q would rank first
      shrunk:  P  .750 beats Q  .667  -> P ranks first
    """

    def setUp(self):
        games = [("P", f"P{i}", 30, 10) for i in range(1, 8)]   # P wins seven
        games.append(("P8", "P", 30, 10))                       # P loses one
        games += [("Q", "Q1", 30, 10), ("Q", "Q2", 30, 10)]     # Q wins two
        self.ranker = make_ranker(*games)
        self.names = ranked_names(self.ranker.rank())

    def test_records_are_as_designed(self):
        self.assertEqual(self.ranker._overall_record("P")[:2], (7, 1))
        self.assertEqual(self.ranker._overall_record("Q")[:2], (2, 0))

    def test_raw_win_pct_would_favour_the_shorter_record(self):
        self.assertGreater(self.ranker._win_pct(2, 0), self.ranker._win_pct(7, 1))

    def test_longer_record_wins_after_shrinking(self):
        self.assertLess(self.names.index("P"), self.names.index("Q"))

    def test_raw_comparison_flips_the_result(self):
        """With PRIOR_GAMES back to 0, the old 2-0-over-7-1 ordering returns."""
        self.ranker.BLEND_WEIGHTS = (1.0, 0.0, 0.0)
        self.ranker.PRIOR_GAMES = 0
        raw = ranked_names(self.ranker.rank())
        self.assertLess(raw.index("Q"), raw.index("P"))


class TestShrinkageIsFallbackOnly(unittest.TestCase):
    """Comparisons inside a round-robin group ignore the shrinkage entirely.

    Every member of a group played every other member, so their in-group
    records are already directly comparable and need no correction.
    """

    def setUp(self):
        # 3-team round-robin: G1 2-0, G2 1-1, G3 0-2.
        self.games = [("G1", "G2", 30, 10), ("G1", "G3", 30, 10),
                      ("G2", "G3", 30, 10)]

    def test_in_group_order_is_unchanged_by_an_extreme_prior(self):
        expected = ["G1", "G2", "G3"]
        for prior in (0, 4, 1000):
            r = make_ranker(*self.games)
            r.PRIOR_GAMES = prior
            self.assertEqual(ranked_names(r.rank()), expected,
                             f"in-group order changed at PRIOR_GAMES={prior}")


class TestEqualSizedGroupsDisagree(unittest.TestCase):
    """Two groups of the SAME size that contradict each other decide nothing.

    A and B belong to two separate 4-team groups that reach opposite verdicts:

        {A, B, C, D}   A 1-2, B 2-1   ->  B over A
        {A, B, E, F}   A 3-0, B 0-3   ->  A over B

    Equally strong evidence pointing both ways is not evidence, so size 4
    decides nothing and the comparison drops to the next-smaller size — here
    there is none, so it lands on the strength-0 overall-record fallback.

    Regression: the previous implementation took whichever clique sorted first
    alphabetically, so renaming E and F flipped the result.
    """

    GAMES = [("A", "B", 30, 10),
             ("C", "A", 30, 10), ("D", "A", 30, 10), ("B", "C", 30, 10),
             ("B", "D", 30, 10), ("C", "D", 30, 10),
             ("A", "E", 30, 10), ("A", "F", 30, 10), ("E", "B", 30, 10),
             ("F", "B", 30, 10), ("E", "F", 30, 10)]

    def _cliques(self, ranker):
        import networkx as nx
        g = nx.Graph()
        g.add_nodes_from(ranker.teams)
        for x, y in ranker._game_map:
            g.add_edge(x, y)
        return list(nx.find_cliques(g))

    def setUp(self):
        self.ranker = make_ranker(*self.GAMES)
        self.cliques = self._cliques(self.ranker)

    def test_the_two_groups_really_do_disagree(self):
        shared = [c for c in self.cliques if "A" in c and "B" in c]
        self.assertEqual(sorted(len(c) for c in shared), [4, 4])
        verdicts = {self.ranker._verdict_in_group("A", "B", c)[0] for c in shared}
        self.assertEqual(verdicts, {-1, 1}, "fixture should contradict itself")

    def test_size_four_is_discarded(self):
        """The verdict must not carry strength 4 — that size decided nothing."""
        _, strength = self.ranker._pairwise_compare("A", "B", self.cliques)
        self.assertEqual(strength[0], 0,
                         "a contradicted size must not supply the verdict")

    def test_result_does_not_depend_on_the_other_members_names(self):
        """Renaming the non-shared members must not change A vs B."""
        renamed = []
        for home, away, hs, as_ in self.GAMES:
            swap = {"E": "AA", "F": "AB"}
            renamed.append((swap.get(home, home), swap.get(away, away), hs, as_))
        other = make_ranker(*renamed)
        first = ranked_names(self.ranker.rank())
        second = ranked_names(other.rank())
        self.assertEqual(first.index("A") < first.index("B"),
                         second.index("A") < second.index("B"))


class TestGenuineTiesShareARank(unittest.TestCase):
    """Teams nothing can separate get the same rank, not an alphabetical one."""

    def setUp(self):
        # Two teams with identical records against separate opponents, so no
        # shared group and identical overall records.
        self.ranker = make_ranker(("Xray", "X1", 30, 10),
                                  ("Yankee", "Y1", 30, 10))
        self.results = self.ranker.rank()
        self.by_team = {row["team"]: row for row in self.results}

    def test_pairwise_compare_reports_a_tie(self):
        import networkx as nx
        g = nx.Graph()
        g.add_nodes_from(self.ranker.teams)
        for x, y in self.ranker._game_map:
            g.add_edge(x, y)
        cmp, _ = self.ranker._pairwise_compare("Xray", "Yankee",
                                               list(nx.find_cliques(g)))
        self.assertEqual(cmp, 0, "identical teams should compare as tied")

    def test_tied_teams_share_a_rank(self):
        self.assertEqual(self.by_team["Xray"]["rank"],
                         self.by_team["Yankee"]["rank"])
        self.assertEqual(self.by_team["X1"]["rank"], self.by_team["Y1"]["rank"])

    def test_rank_skips_after_a_tie(self):
        """Two teams tied for 1st means the next rank is 3, not 2."""
        self.assertEqual([row["rank"] for row in self.results], [1, 1, 3, 3])

    def test_winners_still_outrank_losers(self):
        self.assertLess(self.by_team["Xray"]["rank"], self.by_team["X1"]["rank"])

    def test_alphabetical_order_carries_no_meaning(self):
        """Renaming the alphabetically-later team must not change its rank."""
        other = make_ranker(("Xray", "X1", 30, 10), ("Alpha", "Y1", 30, 10))
        by_team = {row["team"]: row for row in other.rank()}
        self.assertEqual(by_team["Alpha"]["rank"], by_team["Xray"]["rank"])

    def test_competition_ranking_shape(self):
        assert_competition_ranking(self, self.results)


class TestDuplicateDetection(unittest.TestCase):
    """on_duplicate="error" refuses any dataset containing a rematch.

    The default is "combine" (see TestSeasonSeries); this policy exists for
    callers who would rather be told than have a season series counted.
    """

    def test_same_pair_twice_raises(self):
        r = FBSRoundRobinRanker(on_duplicate="error")
        r.add_game("Alabama", "Georgia", 30, 10)
        with self.assertRaises(DuplicateGameError):
            r.add_game("Alabama", "Georgia", 21, 17)

    def test_reversed_order_is_still_the_same_pair(self):
        """Home and away swapped is a rematch, not a different fixture."""
        r = FBSRoundRobinRanker(on_duplicate="error")
        r.add_game("Alabama", "Georgia", 30, 10)
        with self.assertRaises(DuplicateGameError):
            r.add_game("Georgia", "Alabama", 21, 17)

    def test_identical_row_repeated_also_raises(self):
        r = FBSRoundRobinRanker(on_duplicate="error")
        r.add_game("Alabama", "Georgia", 30, 10)
        with self.assertRaises(DuplicateGameError):
            r.add_game("Alabama", "Georgia", 30, 10)

    def test_message_names_both_teams_and_both_results(self):
        r = FBSRoundRobinRanker(on_duplicate="error")
        r.add_game("Alabama", "Georgia", 30, 10)
        with self.assertRaises(DuplicateGameError) as ctx:
            r.add_game("Georgia", "Alabama", 21, 17)
        message = str(ctx.exception)
        for fragment in ("Alabama", "Georgia", "30-10", "17-21"):
            self.assertIn(fragment, message)

    def test_rejected_duplicate_leaves_the_data_untouched(self):
        r = FBSRoundRobinRanker(on_duplicate="error")
        r.add_game("Alabama", "Georgia", 30, 10)
        with self.assertRaises(DuplicateGameError):
            r.add_game("Georgia", "Alabama", 21, 17)
        self.assertEqual(r.get_result("Alabama", "Georgia"), (30, 10))
        self.assertEqual(len(r._game_map), 1)
        self.assertEqual(r.teams, {"Alabama", "Georgia"})

    def test_keep_first_ignores_the_rematch(self):
        r = FBSRoundRobinRanker(on_duplicate="keep_first")
        r.add_game("Alabama", "Georgia", 30, 10)
        r.add_game("Georgia", "Alabama", 21, 17)
        self.assertEqual(r.get_result("Alabama", "Georgia"), (30, 10))

    def test_keep_last_takes_the_rematch(self):
        r = FBSRoundRobinRanker(on_duplicate="keep_last")
        r.add_game("Alabama", "Georgia", 30, 10)
        r.add_game("Georgia", "Alabama", 21, 17)
        self.assertEqual(r.get_result("Alabama", "Georgia"), (17, 21))

    def test_unknown_policy_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            FBSRoundRobinRanker(on_duplicate="whatever")

    def test_distinct_pairs_are_unaffected(self):
        """Normal data with no rematches still loads."""
        r = make_ranker(("A", "B", 30, 10), ("B", "C", 30, 10), ("A", "C", 30, 10))
        self.assertEqual(len(r._game_map), 3)
        self.assertEqual(len(r.rank()), 3)


class TestCsvDuplicateReporting(unittest.TestCase):
    """A duplicate found in a CSV names the file and line."""

    def _write(self, rows):
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         newline="", encoding="utf-8")
        writer = csv.writer(fh)
        writer.writerow(["date", "home_team", "home_score", "away_team", "away_score"])
        writer.writerows(rows)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_duplicate_row_reports_file_and_line(self):
        path = self._write([
            ["2025-10-11", "Texas", 31, "Oklahoma", 24],
            ["2025-11-01", "Ohio State", 28, "Michigan", 20],
            ["2025-12-06", "Oklahoma", 27, "Texas", 21],   # rematch, line 4
        ])
        r = FBSRoundRobinRanker(on_duplicate="error")
        with self.assertRaises(DuplicateGameError) as ctx:
            r.load_csv(path)
        message = str(ctx.exception)
        self.assertIn("line 4", message)
        self.assertIn(path, message)
        self.assertIn("Oklahoma", message)
        self.assertIn("Texas", message)

    def test_clean_csv_still_loads(self):
        path = self._write([
            ["2025-10-11", "Texas", 31, "Oklahoma", 24],
            ["2025-11-01", "Ohio State", 28, "Michigan", 20],
        ])
        r = FBSRoundRobinRanker()
        r.load_csv(path)
        self.assertEqual(len(r._game_map), 2)
        self.assertEqual(len(r.rank()), 4)

    def test_keep_last_loads_a_csv_with_a_rematch(self):
        path = self._write([
            ["2025-10-11", "Texas", 31, "Oklahoma", 24],
            ["2025-12-06", "Oklahoma", 27, "Texas", 21],
        ])
        r = FBSRoundRobinRanker(on_duplicate="keep_last")
        r.load_csv(path)
        self.assertEqual(r.get_result("Oklahoma", "Texas"), (27, 21))


class TestSeasonSeries(unittest.TestCase):
    """The default policy counts every meeting, so a split series is 1-1.

    Alabama and Georgia met twice in 2025 and split.  Picking either game
    alone declares a winner the season did not; counting both leaves them
    even head to head and lets the rest of the evidence decide.
    """

    SPLIT = [("Georgia", "Alabama", 21, 24),     # Alabama won the first
             ("Alabama", "Georgia", 7, 28)]      # Georgia won the rematch

    def test_combine_is_the_default(self):
        self.assertEqual(FBSRoundRobinRanker().on_duplicate, "combine")

    def test_both_meetings_are_kept(self):
        r = make_ranker(*self.SPLIT)
        self.assertEqual(len(r.get_results("Alabama", "Georgia")), 2)
        self.assertEqual(r.total_games(), 2)
        self.assertEqual(len(r._game_map), 1, "still one pair")

    def test_results_are_oriented_to_the_team_asked_about(self):
        r = make_ranker(*self.SPLIT)
        bama = r.get_results("Alabama", "Georgia")
        dawgs = r.get_results("Georgia", "Alabama")
        self.assertEqual(bama, [(24, 21), (7, 28)])
        self.assertEqual(dawgs, [(21, 24), (28, 7)])

    def test_a_split_series_is_one_win_and_one_loss(self):
        r = make_ranker(*self.SPLIT)
        self.assertEqual(r._overall_record("Alabama")[:2], (1, 1))
        self.assertEqual(r._overall_record("Georgia")[:2], (1, 1))

    def test_points_come_from_both_games(self):
        r = make_ranker(*self.SPLIT)
        _, _, pf, pa = r._overall_record("Alabama")
        self.assertEqual((pf, pa), (24 + 7, 21 + 28))

    def test_a_sweep_is_two_wins(self):
        r = make_ranker(("Texas Tech", "BYU", 29, 7),
                        ("Texas Tech", "BYU", 34, 7))
        self.assertEqual(r._overall_record("Texas Tech")[:2], (2, 0))
        self.assertEqual(r._overall_record("BYU")[:2], (0, 2))

    def test_in_group_record_counts_both_meetings(self):
        """A 3-team group where two of them played twice gives one team 3 games."""
        r = make_ranker(("A", "B", 30, 10), ("B", "A", 30, 10),   # split
                        ("A", "C", 30, 10), ("B", "C", 30, 10))
        group = ["A", "B", "C"]
        self.assertEqual(r._record_in_group("A", group)[:2], (2, 1))
        self.assertEqual(r._record_in_group("B", group)[:2], (2, 1))

    def test_a_split_leaves_the_pair_even_rather_than_picking_a_winner(self):
        """keep_first and keep_last disagree here; combine refuses to guess."""
        first = FBSRoundRobinRanker(on_duplicate="keep_first")
        last = FBSRoundRobinRanker(on_duplicate="keep_last")
        for ranker in (first, last):
            for home, away, hs, as_ in self.SPLIT:
                ranker.add_game(home, away, hs, as_)
        self.assertEqual(first._overall_record("Alabama")[:2], (1, 0))
        self.assertEqual(last._overall_record("Alabama")[:2], (0, 1))
        # combine gives neither team the series
        combined = make_ranker(*self.SPLIT)
        self.assertEqual(combined._overall_record("Alabama")[:2],
                         combined._overall_record("Georgia")[:2])

    def test_get_result_returns_the_first_meeting(self):
        r = make_ranker(*self.SPLIT)
        self.assertEqual(r.get_result("Alabama", "Georgia"), (24, 21))

    def test_get_results_is_empty_for_teams_that_never_played(self):
        r = make_ranker(("A", "B", 30, 10), ("C", "D", 30, 10))
        self.assertEqual(r.get_results("A", "C"), [])
        self.assertIsNone(r.get_result("A", "C"))


class TestOverallRecordInOutput(unittest.TestCase):
    """rank() reports the full-season record next to the in-group one."""

    def setUp(self):
        # A, B, C form a 3-team group.  D plays only A, so D is outside it —
        # A's season therefore covers one more game than its group does.
        self.results = make_ranker(
            ("A", "B", 30, 10), ("A", "C", 30, 10), ("B", "C", 30, 10),
            ("A", "D", 30, 10),
        ).rank()
        self.by_team = {r["team"]: r for r in self.results}

    def test_overall_record_covers_every_game(self):
        """A went 3-0 overall but only 2-0 inside its 3-team group."""
        a = self.by_team["A"]
        self.assertEqual((a["overall_wins"], a["overall_losses"]), (3, 0))
        self.assertEqual((a["wins"], a["losses"]), (2, 0))

    def test_overall_win_pct_matches_the_overall_record(self):
        for row in self.results:
            games = row["overall_wins"] + row["overall_losses"]
            expected = round(row["overall_wins"] / games, 3) if games else 0.0
            self.assertAlmostEqual(row["overall_win_pct"], expected)

    def test_every_row_has_played_at_least_one_game(self):
        for row in self.results:
            self.assertGreater(row["overall_wins"] + row["overall_losses"], 0)


class TestUnrankedOpponents(unittest.TestCase):
    """A game against an unranked opponent counts in the record only.

    This is how a loss to an FCS team becomes visible: it hurts the FBS team's
    record, while the FCS team stays out of the graph and the standings.
    """

    def setUp(self):
        self.r = make_ranker(("Army", "Navy", 30, 10))
        # Tarleton State beat Army and is not itself ranked.
        self.r.add_game("Army", "Tarleton State", 20, 27, away_ranked=False)

    def test_the_unranked_team_is_not_ranked(self):
        self.assertNotIn("Tarleton State", self.r.teams)
        self.assertNotIn("Tarleton State", ranked_names(self.r.rank()))

    def test_it_is_remembered_as_an_opponent(self):
        self.assertIn("Tarleton State", self.r.unranked_opponents)
        self.assertEqual(self.r.get_unranked_results("Army"),
                         [("Tarleton State", 20, 27)])

    def test_the_loss_counts_in_the_record(self):
        self.assertEqual(self.r._overall_record("Army")[:2], (1, 1))

    def test_points_count_too(self):
        _, _, pf, pa = self.r._overall_record("Army")
        self.assertEqual((pf, pa), (30 + 20, 10 + 27))

    def test_it_never_enters_a_round_robin_group(self):
        for row in self.r.rank():
            self.assertNotIn("Tarleton State", str(row))
        self.assertEqual(self.r.get_results("Army", "Tarleton State"), [])

    def test_total_games_counts_it(self):
        self.assertEqual(self.r.total_games(), 2)

    def test_a_game_between_two_unranked_teams_is_ignored(self):
        r = make_ranker(("A", "B", 30, 10))
        r.add_game("Mercer", "Furman", 20, 17,
                   home_ranked=False, away_ranked=False)
        self.assertEqual(r.teams, {"A", "B"})
        self.assertEqual(r.total_games(), 1)

    def test_an_unranked_home_side_works_the_same_way(self):
        r = make_ranker(("A", "B", 30, 10))
        r.add_game("Tarleton State", "A", 27, 20, home_ranked=False)
        self.assertEqual(r._overall_record("A")[:2], (1, 1))
        self.assertEqual(r.get_unranked_results("A"),
                         [("Tarleton State", 20, 27)])

    def test_beating_an_unranked_team_is_still_a_win(self):
        r = make_ranker(("A", "B", 30, 10))
        r.add_game("A", "Mercer", 55, 3, away_ranked=False)
        self.assertEqual(r._overall_record("A")[:2], (2, 0))


class TestRankedColumnParsing(unittest.TestCase):
    """The optional home_ranked / away_ranked CSV columns."""

    def _write(self, header, rows):
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         newline="", encoding="utf-8")
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_missing_columns_mean_everyone_is_ranked(self):
        path = self._write(["home_team", "home_score", "away_team", "away_score"],
                           [["A", 30, "B", 10]])
        r = FBSRoundRobinRanker()
        r.load_csv(path)
        self.assertEqual(r.teams, {"A", "B"})
        self.assertEqual(r.unranked_opponents, set())

    def test_false_marks_a_team_unranked(self):
        path = self._write(
            ["home_team", "home_score", "away_team", "away_score",
             "home_ranked", "away_ranked"],
            [["A", 20, "Tarleton State", 27, "true", "false"]])
        r = FBSRoundRobinRanker()
        r.load_csv(path)
        self.assertEqual(r.teams, {"A"})
        self.assertEqual(r._overall_record("A")[:2], (0, 1))

    def test_accepted_spellings(self):
        for value in ["false", "False", "FALSE", "0", "no", "n", "unranked"]:
            self.assertFalse(_is_ranked(value), value)
        for value in ["true", "True", "1", "yes", "", None, "anything"]:
            self.assertTrue(_is_ranked(value), repr(value))


# ---------------------------------------------------------------------------
# Strength 1: common opponents
# ---------------------------------------------------------------------------

class TestCommonOpponentCounting(unittest.TestCase):
    """common_opponents() separates shared opponents from informative ones.

    A and B both play C, D and E.  They split on C and E (one won, one lost)
    but both beat D, so D is shared without telling us anything.
    """

    def setUp(self):
        self.r = make_ranker(
            ("A", "C", 30, 10),   # A beats C
            ("B", "C", 10, 30),   # B loses to C   -> differs
            ("A", "D", 30, 10),   # both beat D    -> does not differ
            ("B", "D", 28, 14),
            ("A", "E", 10, 30),   # A loses to E
            ("B", "E", 30, 10),   # B beats E      -> differs
        )

    def test_shared_counts_every_common_opponent(self):
        shared, _ = self.r.common_opponents("A", "B")
        self.assertEqual(shared, {"C", "D", "E"})

    def test_differing_drops_the_agreeing_opponent(self):
        _, differing = self.r.common_opponents("A", "B")
        self.assertEqual(differing, {"C", "E"})

    def test_symmetric(self):
        self.assertEqual(self.r.common_opponents("A", "B"),
                         self.r.common_opponents("B", "A"))


class TestCommonOpponentThreshold(unittest.TestCase):
    """The tier speaks only once MIN_DIFFERING_RESULTS results actually differ."""

    def _pair_with(self, differing):
        """A and B share three opponents, `differing` of which split them."""
        games = []
        for i, name in enumerate(("C", "D", "E")):
            a_wins = True
            b_wins = i >= differing      # first `differing` opponents split
            games.append(("A", name, 30, 10) if a_wins else (name, "A", 30, 10))
            games.append(("B", name, 30, 10) if b_wins else (name, "B", 30, 10))
        return make_ranker(*games)

    def test_one_differing_result_stays_silent(self):
        r = self._pair_with(1)
        _, differing = r.common_opponents("A", "B")
        self.assertEqual(len(differing), 1)
        self.assertIsNone(r._common_opponent_verdict("A", "B"))

    def test_two_differing_results_issue_a_verdict(self):
        r = self._pair_with(2)
        _, differing = r.common_opponents("A", "B")
        self.assertEqual(len(differing), 2)
        self.assertIsNotNone(r._common_opponent_verdict("A", "B"))

    def test_no_shared_opponents_stays_silent(self):
        r = make_ranker(("A", "C", 30, 10), ("B", "D", 30, 10))
        self.assertIsNone(r._common_opponent_verdict("A", "B"))

    def test_zero_disables_the_tier_entirely(self):
        r = self._pair_with(3)
        r.MIN_DIFFERING_RESULTS = 0
        self.assertIsNone(r._common_opponent_verdict("A", "B"))

    def test_better_shared_record_ranks_higher(self):
        r = self._pair_with(3)          # A beat all three, B lost to all three
        cmp, _, _ = r._common_opponent_verdict("A", "B")
        self.assertEqual(cmp, -1)
        self.assertLess(ranked_names(r.rank()).index("A"),
                        ranked_names(r.rank()).index("B"))

    def test_level_shared_records_decide_nothing(self):
        # A and B split their two shared opponents in opposite directions:
        # both finish 1-1, so the tier has nothing to say.
        r = make_ranker(
            ("A", "C", 30, 10), (
             "C", "B", 30, 10),
            ("D", "A", 30, 10), ("B", "D", 30, 10),
        )
        _, differing = r.common_opponents("A", "B")
        self.assertEqual(len(differing), 2)
        self.assertIsNone(r._common_opponent_verdict("A", "B"))


class TestCommonOpponentStrength(unittest.TestCase):
    """Strength 1 sits below every group and above the blend."""

    def _cliques(self, ranker):
        import networkx as nx
        g = nx.Graph()
        g.add_nodes_from(ranker.teams)
        for ta, tb in ranker._game_map:
            g.add_edge(ta, tb)
        return list(nx.find_cliques(g))

    def test_verdict_strength_is_one(self):
        r = make_ranker(
            ("A", "C", 30, 10), ("C", "B", 30, 10),
            ("A", "D", 30, 10), ("D", "B", 30, 10),
        )
        _, strength = r._pairwise_compare("A", "B", self._cliques(r))
        self.assertEqual(strength[0], 1)

    def test_a_shared_opponent_puts_two_teams_that_met_in_a_group(self):
        """Playing each other plus any shared opponent IS a round-robin group.

        A shared opponent C completes the triangle A-B-C, so two teams that
        met are in a group together whenever they have a common opponent at
        all, and that group speaks before the common-opponent tier does.
        """
        r = make_ranker(
            ("B", "A", 21, 17),                       # B beats A directly
            ("A", "C", 30, 10), ("C", "B", 30, 10),
            ("A", "D", 30, 10), ("D", "B", 30, 10),
        )
        self.assertTrue(any({"A", "B"} <= set(c) for c in self._cliques(r)))
        _, strength = r._pairwise_compare("A", "B", self._cliques(r))
        self.assertGreaterEqual(strength[0], 3)

    def test_group_tiebreak_defaults_to_point_differential(self):
        self.assertEqual(FBSRoundRobinRanker.GROUP_TIEBREAK, "point_diff")

    def test_head_to_head_tiebreak_overrides_point_differential(self):
        """Tied on in-group win pct, the direct result can decide instead.

        A three-way cycle leaves all three teams 1-1.  Point differential
        ranks B first (+20) and A last (-29), which contradicts A beating B.
        With GROUP_TIEBREAK="head_to_head" the meeting decides that pair.
        """
        games = (("A", "B", 21, 20), ("B", "C", 21, 0), ("C", "A", 30, 0))
        base = make_ranker(*games)
        self.assertEqual(base._verdict_in_group("A", "B", ["A", "B", "C"])[0],
                         1, "point diff should favour B")

        tie = make_ranker(*games)
        tie.GROUP_TIEBREAK = "head_to_head"
        self.assertEqual(tie._verdict_in_group("A", "B", ["A", "B", "C"])[0],
                         -1, "the meeting should favour A")

    def test_head_to_head_tiebreak_ignores_a_split_series(self):
        """A split series settles nothing, so point differential still rules."""
        r = make_ranker(("A", "B", 21, 20), ("B", "A", 30, 0),
                        ("B", "C", 21, 0), ("C", "A", 30, 0))
        r.GROUP_TIEBREAK = "head_to_head"
        self.assertIsNone(r._head_to_head_verdict("A", "B"))

    def test_head_to_head_verdict_reads_the_season_series(self):
        r = make_ranker(("A", "B", 21, 0), ("B", "A", 3, 0), ("A", "B", 7, 0))
        self.assertEqual(r._head_to_head_verdict("A", "B"), -1)   # A wins 2-1
        self.assertEqual(r._head_to_head_verdict("B", "A"), 1)
        self.assertIsNone(r._head_to_head_verdict("A", "Nobody"))

    def test_group_tie_min_diff_default(self):
        self.assertEqual(FBSRoundRobinRanker.GROUP_TIE_MIN_DIFF, 20)

    def test_withhold_thin_declines_a_tie_it_cannot_settle_convincingly(self):
        """A margin of a couple of points is not evidence a group can supply.

        B and C are both 1-1 inside the cycle and separated by 11 points of
        in-group differential.  Above that threshold the verdict stands;
        raise the bar past it and the group says nothing instead.
        """
        games = (("A", "B", 21, 20), ("B", "C", 21, 0), ("C", "A", 30, 0))
        group = ["A", "B", "C"]

        speaks = make_ranker(*games)
        speaks.GROUP_TIEBREAK = "withhold_thin"
        speaks.GROUP_TIE_MIN_DIFF = 10
        self.assertIsNotNone(speaks._verdict_in_group("B", "C", group))

        withholds = make_ranker(*games)
        withholds.GROUP_TIEBREAK = "withhold_thin"
        withholds.GROUP_TIE_MIN_DIFF = 15
        self.assertIsNone(withholds._verdict_in_group("B", "C", group))

    def test_a_withheld_verdict_drops_the_pair_out_of_the_group_tier(self):
        """Withholding is not a tie -- the pair falls through to weaker tiers.

        That is the whole point: a pair the group cannot settle should be
        decided by common opponents or the blend, not inherit a position from
        a margin of a point or two.
        """
        r = make_ranker(("A", "B", 21, 20), ("B", "C", 21, 0), ("C", "A", 30, 0))
        r.GROUP_TIEBREAK = "withhold_thin"
        r.GROUP_TIE_MIN_DIFF = 100          # withhold every tie
        _, strength = r._pairwise_compare("B", "C", self._cliques(r))
        self.assertLess(strength[0], 3, "should not still be a group verdict")

    def test_withhold_thin_at_zero_reproduces_point_diff(self):
        games = (("A", "B", 21, 20), ("B", "C", 21, 0), ("C", "A", 30, 0))
        zero = make_ranker(*games)
        zero.GROUP_TIEBREAK = "withhold_thin"
        zero.GROUP_TIE_MIN_DIFF = 0
        self.assertEqual([x["team"] for x in zero.rank()],
                         [x["team"] for x in make_ranker(*games).rank()])

    def test_withhold_thin_never_touches_a_pair_separated_on_win_pct(self):
        """It withholds TIES; a decided in-group record is untouched."""
        r = make_ranker(("A", "B", 21, 20), ("A", "C", 30, 0), ("B", "C", 30, 0))
        r.GROUP_TIEBREAK = "withhold_thin"
        r.GROUP_TIE_MIN_DIFF = 10 ** 6      # withhold every tie
        cmp, gap, _ = r._verdict_in_group("A", "B", ["A", "B", "C"])
        self.assertEqual(cmp, -1)           # A is 2-0, B is 1-1
        self.assertGreater(gap, 0.0)

    def test_acyclic_tiebreak_uses_the_meeting_when_the_tied_set_is_an_order(self):
        """Two teams tied at 2-1 in a four-team group; their meeting decides.

        The tied set is just {A, B}, which cannot hold a cycle, so the
        head-to-head result is safe to honour.  Point differential would say
        the opposite (B is +59 inside the group, A is -18).
        """
        games = (("A", "B", 21, 20), ("A", "C", 21, 0), ("D", "A", 40, 0),
                 ("B", "C", 30, 0), ("B", "D", 30, 0), ("C", "D", 30, 0))
        group = ["A", "B", "C", "D"]
        base = make_ranker(*games)
        self.assertEqual(base._verdict_in_group("A", "B", group)[0], 1)

        acyclic = make_ranker(*games)
        acyclic.GROUP_TIEBREAK = "head_to_head_acyclic"
        self.assertTrue(acyclic._tied_set_is_acyclic(group, 2 / 3))
        self.assertEqual(acyclic._verdict_in_group("A", "B", group)[0], -1)

    def test_acyclic_tiebreak_stands_aside_for_a_cycle(self):
        """In a 3-way cycle no ordering honours every meeting, so it defers.

        This is the whole point of the variant: it matches plain
        head_to_head where the meetings agree, and plain point_diff where
        they cannot.
        """
        games = (("A", "B", 21, 20), ("B", "C", 21, 0), ("C", "A", 30, 0))
        group = ["A", "B", "C"]
        acyclic = make_ranker(*games)
        acyclic.GROUP_TIEBREAK = "head_to_head_acyclic"
        self.assertFalse(acyclic._tied_set_is_acyclic(group, 0.5))

        base = make_ranker(*games)
        self.assertEqual(acyclic._verdict_in_group("A", "B", group)[0],
                         base._verdict_in_group("A", "B", group)[0])

    def test_a_tied_set_too_small_to_cycle_is_acyclic(self):
        r = make_ranker(("A", "B", 21, 20))
        self.assertTrue(r._tied_set_is_acyclic(["A", "B"], 1.0))

    def test_a_split_series_contributes_no_arc_to_the_cycle_check(self):
        """A and B split, so nothing points either way and no cycle closes."""
        r = make_ranker(("A", "B", 21, 0), ("B", "A", 21, 0),
                        ("B", "C", 21, 0), ("C", "A", 21, 0))
        self.assertIsNone(r._head_to_head_verdict("A", "B"))
        self.assertTrue(r._tied_set_is_acyclic(["A", "B", "C"], 0.5))

    def test_the_acyclic_cache_is_dropped_when_a_game_arrives(self):
        # Records change when a game is added, so a stale answer would be
        # read against a different tied set -- the bug the metrics cache had.
        r = make_ranker(("A", "B", 21, 20), ("B", "C", 21, 0), ("C", "A", 30, 0))
        r.GROUP_TIEBREAK = "head_to_head_acyclic"
        r._tied_set_is_acyclic(["A", "B", "C"], 0.5)
        self.assertTrue(r._acyclic_cache)
        r.add_game("A", "D", 21, 0)
        self.assertFalse(r._acyclic_cache)

    def test_the_tiebreak_cannot_reach_a_pair_separated_on_win_pct(self):
        """It is a TIEbreak: a decided in-group record is never overruled."""
        r = make_ranker(("A", "B", 21, 20), ("A", "C", 30, 0), ("B", "C", 30, 0))
        r.GROUP_TIEBREAK = "head_to_head"
        # A is 2-0 and B is 1-1, so win pct decides before the tiebreak runs.
        cmp, gap, _ = r._verdict_in_group("A", "B", ["A", "B", "C"])
        self.assertEqual(cmp, -1)
        self.assertGreater(gap, 0.0)

    def test_a_two_team_group_is_unaffected_by_the_tiebreak(self):
        """Win pct already encodes head-to-head when the group IS the game."""
        for mode in ("point_diff", "head_to_head"):
            r = make_ranker(("A", "B", 21, 20))
            r.GROUP_TIEBREAK = mode
            self.assertEqual(r._verdict_in_group("A", "B", ["A", "B"])[0], -1)

    def test_disagreeing_groups_let_a_pair_that_met_fall_through(self):
        """Sharing a group is not the same as that group deciding anything.

        This test exists because the one above was read as proving more than
        it does.  Equally sized groups that contradict each other settle
        nothing, so that size is skipped -- and a pair that PLAYED can fall
        all the way past the common-opponent tier to the blend.

        A beat B.  {A, B, C} says A is better (A is 2-0 inside it); {A, B, D}
        says B is (both 1-1, and B has the point differential).  Both are size
        3, so neither wins and the comparison drops out of the group tier
        entirely.  Teams that met are NOT confined to strength >= 2.
        """
        r = make_ranker(
            ("A", "B", 21, 20),
            ("A", "C", 30, 0), ("C", "B", 30, 0),     # {A,B,C} favours A
            ("D", "A", 30, 0), ("B", "D", 30, 0),     # {A,B,D} favours B
        )
        cliques = self._cliques(r)
        shared = [c for c in cliques if {"A", "B"} <= set(c)]
        self.assertEqual(sorted(len(c) for c in shared), [3, 3])
        verdicts = {r._verdict_in_group("A", "B", c)[0] for c in shared}
        self.assertEqual(verdicts, {-1, 1}, "the two groups must disagree")

        _, strength = r._pairwise_compare("A", "B", cliques)
        self.assertEqual(strength[0], 0,
                         "a pair that met fell past groups AND common opponents")

    def test_common_opponents_outrank_the_blend(self):
        """A has the worse blended score but the better shared record.

        A and B never meet.  Both played C and D: A beat each, B lost to
        each.  A then loses three games to teams outside, leaving it with the
        worse blend — but the shared results are strength 1 and decide first.
        """
        r = make_ranker(
            ("A", "C", 30, 10), ("C", "B", 30, 10),
            ("A", "D", 30, 10), ("D", "B", 30, 10),
            ("E", "A", 30, 10), ("F", "A", 30, 10), ("G", "A", 30, 10),
            ("B", "H", 30, 10), ("B", "I", 30, 10), ("B", "J", 30, 10),
        )
        self.assertEqual(r._common_opponent_verdict("A", "B")[0], -1)
        self.assertLess(r.blended_score("A"), r.blended_score("B"))
        names = ranked_names(r.rank())
        self.assertLess(names.index("A"), names.index("B"))


class TestUnrankedCommonOpponents(unittest.TestCase):
    """An FCS opponent both teams played still counts as a common opponent."""

    def setUp(self):
        self.r = FBSRoundRobinRanker()
        self.r.add_game("A", "Tarleton State", 30, 10, away_ranked=False)
        self.r.add_game("Tarleton State", "B", 30, 10, home_ranked=False)
        self.r.add_game("A", "Austin Peay", 30, 10, away_ranked=False)
        self.r.add_game("Austin Peay", "B", 30, 10, home_ranked=False)

    def test_unranked_opponents_are_shared(self):
        shared, differing = self.r.common_opponents("A", "B")
        self.assertEqual(shared, {"Tarleton State", "Austin Peay"})
        self.assertEqual(differing, shared)

    def test_they_produce_a_verdict(self):
        cmp, _, _ = self.r._common_opponent_verdict("A", "B")
        self.assertEqual(cmp, -1)

    def test_they_are_still_not_ranked(self):
        self.assertEqual(ranked_names(self.r.rank()), ["A", "B"])


class TestCommonOpponentsCountEveryMeeting(unittest.TestCase):
    """A shared opponent played twice contributes both games."""

    def test_season_series_against_a_shared_opponent(self):
        r = make_ranker(
            ("A", "C", 30, 10), ("C", "A", 30, 10),   # A splits with C
            ("C", "B", 30, 10), ("C", "B", 28, 14),   # B loses to C twice
            ("A", "D", 30, 10), ("D", "B", 30, 10),
        )
        self.assertEqual(r._record_against("A", {"C"})[:2], (1, 1))
        self.assertEqual(r._record_against("B", {"C"})[:2], (0, 2))


# ---------------------------------------------------------------------------
# Strength 0: the opponent-weighted blend
# ---------------------------------------------------------------------------

class TestBlendedScore(unittest.TestCase):
    """blended_score() combines a team's record with its opponents'."""

    def setUp(self):
        # A beats B; B beats C and D.  A's one opponent is 2-1 excluding A.
        self.r = make_ranker(
            ("A", "B", 30, 10),
            ("B", "C", 30, 10),
            ("B", "D", 30, 10),
        )

    def test_record_only_weights_reproduce_the_shrunk_record(self):
        self.r.BLEND_WEIGHTS = (1.0, 0.0, 0.0)
        for team in self.r.teams:
            w, l = self.r._overall_record(team)[:2]
            self.assertAlmostEqual(self.r.blended_score(team),
                                   self.r._shrunk_win_pct(w, l))

    def test_opponent_record_excludes_the_team_being_scored(self):
        """A's OWP is B's record with the A game removed: 2-0, not 2-1."""
        m = self.r._metrics()
        self.assertAlmostEqual(m["owp"]["A"], self.r._shrunk_win_pct(2, 0))

    def test_weights_interpolate(self):
        m = self.r._metrics()
        self.r.BLEND_WEIGHTS = (0.5, 0.5, 0.0)
        self.assertAlmostEqual(self.r.blended_score("A"),
                               0.5 * m["wp"]["A"] + 0.5 * m["owp"]["A"])

    def test_third_weight_uses_opponents_opponents(self):
        m = self.r._metrics()
        self.r.BLEND_WEIGHTS = (0.0, 0.0, 1.0)
        self.assertAlmostEqual(self.r.blended_score("A"), m["oowp"]["A"])

    def test_unranked_opponents_stay_out_of_the_averages(self):
        r = FBSRoundRobinRanker()
        r.add_game("A", "B", 30, 10)
        r.add_game("A", "Bryant", 10, 30, away_ranked=False)
        m = r._metrics()
        # OWP averages over B alone; Bryant has no record to contribute.
        self.assertAlmostEqual(m["owp"]["A"], r._shrunk_win_pct(0, 0))
        # The loss still counts against A's own record.
        self.assertEqual(r._overall_record("A")[:2], (1, 1))


class TestMetricsCacheInvalidation(unittest.TestCase):
    """Cached metrics must not outlive the inputs they were computed from."""

    def test_adding_a_game_recomputes(self):
        r = make_ranker(("A", "B", 30, 10))
        before = r.blended_score("A")
        r.add_game("A", "C", 10, 30)
        self.assertNotAlmostEqual(before, r.blended_score("A"))

    def test_adding_an_unranked_game_recomputes(self):
        r = make_ranker(("A", "B", 30, 10))
        before = r.blended_score("A")
        r.add_game("A", "Bryant", 10, 30, away_ranked=False)
        self.assertNotAlmostEqual(before, r.blended_score("A"))

    def test_changing_the_prior_recomputes(self):
        r = make_ranker(("A", "B", 30, 10))
        before = r.blended_score("A")
        r.PRIOR_GAMES = 0
        self.assertNotAlmostEqual(before, r.blended_score("A"))


class TestOpponentWeightingOnAFullSchedule(unittest.TestCase):
    """What the blend does once every team has a real schedule.

    Two teams that never meet, each with six games, sharing no opponent:

      A goes 3-3 against a strong pool, each of whom beat six neutral teams.
      B goes 4-2 against a weak pool, each of whom lost to those same six.

    B has the better record; A played far better opposition.  Every team in
    the fixture plays at least six games, and the strong and weak pools reach
    the rest of the field through the same neutral teams, so the two pods are
    structurally alike and differ only in opponent quality.
    """

    @staticmethod
    def _games():
        games = []
        strong = [f"S{i}" for i in range(1, 7)]
        weak = [f"W{i}" for i in range(1, 7)]
        neutral = [f"N{i}" for i in range(1, 7)]
        for n in neutral:
            for s in strong:
                games.append((s, n, 31, 10))      # strong beat the neutrals
            for w in weak:
                games.append((n, w, 31, 10))      # neutrals beat the weak
        for i, s in enumerate(strong):            # A finishes 3-3
            games.append(("A", s, 24, 17) if i < 3 else (s, "A", 24, 17))
        for i, w in enumerate(weak):              # B finishes 4-2
            games.append(("B", w, 24, 17) if i < 4 else (w, "B", 24, 17))
        return games

    def _ranker(self, weights):
        r = make_ranker(*self._games())
        r.BLEND_WEIGHTS = weights
        return r

    def _cliques(self, ranker):
        import networkx as nx
        g = nx.Graph()
        g.add_nodes_from(ranker.teams)
        for ta, tb in ranker._game_map:
            g.add_edge(ta, tb)
        return list(nx.find_cliques(g))

    def test_every_team_has_a_real_schedule(self):
        r = self._ranker((0.5, 0.5, 0.0))
        for team in r.teams:
            played = sum(r._overall_record(team)[:2])
            self.assertGreaterEqual(played, 6, team)

    def test_the_pair_is_decided_at_strength_zero(self):
        """A and B share no opponent and no group, so nothing else applies."""
        r = self._ranker((0.5, 0.5, 0.0))
        shared, _ = r.common_opponents("A", "B")
        self.assertEqual(shared, set())
        _, strength = r._pairwise_compare("A", "B", self._cliques(r))
        self.assertEqual(strength[0], 0)

    def test_b_has_the_better_record(self):
        r = self._ranker((1.0, 0.0, 0.0))
        self.assertEqual(r._overall_record("A")[:2], (3, 3))
        self.assertEqual(r._overall_record("B")[:2], (4, 2))

    def test_record_only_favours_b(self):
        r = self._ranker((1.0, 0.0, 0.0))
        self.assertLess(r.blended_score("A"), r.blended_score("B"))
        cmp, _ = r._pairwise_compare("A", "B", self._cliques(r))
        self.assertEqual(cmp, 1)                  # B over A

    def test_opponent_weighting_flips_the_verdict_to_a(self):
        r = self._ranker((0.5, 0.5, 0.0))
        self.assertGreater(r.blended_score("A"), r.blended_score("B"))
        cmp, _ = r._pairwise_compare("A", "B", self._cliques(r))
        self.assertEqual(cmp, -1)                 # A over B

    def test_stronger_evidence_still_decides_the_final_order(self):
        """The flipped verdict changes no rank, because chains outrank it.

        B lost to two weak teams that six neutrals beat, and those neutrals
        lost to the strong teams A split with.  Those 2-clique verdicts chain
        A above B at strength 2, so the strength-0 verdict is locked only when
        it agrees and discarded when it does not.  Schedule strength expresses
        itself here without ever getting a vote.
        """
        ranks = {}
        for weights in [(1.0, 0.0, 0.0), (0.5, 0.5, 0.0)]:
            r = self._ranker(weights)
            ranks[weights] = {x["team"]: x["rank"] for x in r.rank()}
            self.assertLess(ranks[weights]["A"], ranks[weights]["B"], weights)
        self.assertEqual(ranks[(1.0, 0.0, 0.0)], ranks[(0.5, 0.5, 0.0)])


class TestInconsistentRankedMarking(unittest.TestCase):
    """A team marked unranked anywhere is unranked everywhere.

    Real CFBD data leaves the classification field blank on some games, and
    a blank reads as ranked.  Taken row by row that promotes an FCS team into
    the rankings on the fraction of its schedule that was left blank -- and
    since the rows naming it correctly are the ones it played FBS teams in,
    the games it lost are exactly the ones that get dropped.
    """

    def _write(self, rows):
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         newline="", encoding="utf-8")
        writer = csv.writer(fh)
        writer.writerow(["home_team", "home_score", "away_team", "away_score",
                         "home_ranked", "away_ranked"])
        writer.writerows(rows)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def setUp(self):
        # Tarleton State beat Army (correctly marked) and beat UTEP (marked
        # ranked, as a blank classification field would be).
        path = self._write([
            ["Army", 10, "Tarleton State", 20, "true", "false"],
            ["UTEP", 14, "Tarleton State", 21, "true", "true"],
            ["Army", 31, "Navy", 14, "true", "true"],
        ])
        self.r = FBSRoundRobinRanker()
        self.r.load_csv(path)

    def test_the_team_stays_out_of_the_rankings(self):
        self.assertNotIn("Tarleton State", self.r.teams)
        self.assertNotIn("Tarleton State", ranked_names(self.r.rank()))

    def test_it_is_reported_as_demoted(self):
        self.assertEqual(self.r.demoted_opponents, {"Tarleton State"})

    def test_both_losses_still_count(self):
        """The whole point: the blank-marked game is not silently dropped."""
        self.assertEqual(self.r._overall_record("Army")[:2], (1, 1))
        self.assertEqual(self.r._overall_record("UTEP")[:2], (0, 1))

    def test_consistent_data_reports_nothing(self):
        path = self._write([
            ["Army", 10, "Tarleton State", 20, "true", "false"],
            ["Army", 31, "Navy", 14, "true", "true"],
        ])
        r = FBSRoundRobinRanker()
        r.load_csv(path)
        self.assertEqual(r.demoted_opponents, set())
        self.assertEqual(r.unranked_opponents, {"Tarleton State"})

    def test_a_fully_ranked_file_is_untouched(self):
        path = self._write([["Army", 31, "Navy", 14, "true", "true"]])
        r = FBSRoundRobinRanker()
        r.load_csv(path)
        self.assertEqual(r.demoted_opponents, set())
        self.assertEqual(r.teams, {"Army", "Navy"})

    def test_duplicate_errors_still_name_the_line(self):
        """The two-pass load must not lose the line number in the message."""
        path = self._write([
            ["Army", 31, "Navy", 14, "true", "true"],
            ["Army", 20, "Navy", 17, "true", "true"],
        ])
        r = FBSRoundRobinRanker(on_duplicate="error")
        with self.assertRaises(DuplicateGameError) as ctx:
            r.load_csv(path)
        self.assertIn("line 3", str(ctx.exception))


class TestTeamsWithoutRankedOpponents(unittest.TestCase):
    """A ranked team connected to nothing in the rankings is reported."""

    def test_a_team_with_only_unranked_opponents_is_flagged(self):
        r = FBSRoundRobinRanker()
        r.add_game("Army", "Navy", 31, 14)
        r.add_game("Dakota State", "North Dakota State", 30, 10,
                   away_ranked=False)
        self.assertEqual(r.teams_without_ranked_opponents(), {"Dakota State"})

    def test_a_fully_connected_field_flags_nobody(self):
        r = make_ranker(("Army", "Navy", 31, 14), ("Navy", "Air Force", 28, 21))
        self.assertEqual(r.teams_without_ranked_opponents(), set())

    def test_a_team_with_one_ranked_opponent_is_not_flagged(self):
        r = FBSRoundRobinRanker()
        r.add_game("Army", "Navy", 31, 14)
        r.add_game("Army", "Tarleton State", 20, 27, away_ranked=False)
        self.assertEqual(r.teams_without_ranked_opponents(), set())

    def test_flagged_teams_are_still_ranked(self):
        """This reports a data problem; it does not silently drop anyone."""
        r = FBSRoundRobinRanker()
        r.add_game("Army", "Navy", 31, 14)
        r.add_game("Dakota State", "North Dakota State", 30, 10,
                   away_ranked=False)
        self.assertIn("Dakota State", ranked_names(r.rank()))


class TestRepeatMeetings(unittest.TestCase):
    """The season-series count spans ranked and unranked games alike.

    It used to be derived as total_games() minus the number of ranked pairs,
    but those do not cover the same games: the total includes games against
    unranked opponents, which never become pairs.  On the 2024 season that
    reported 125 repeat meetings where there were 4.
    """

    def test_no_rematches_counts_zero(self):
        r = make_ranker(("A", "B", 30, 10), ("B", "C", 30, 10))
        self.assertEqual(r.repeat_meetings(), 0)

    def test_a_pair_meeting_twice_counts_one(self):
        r = make_ranker(("A", "B", 30, 10), ("B", "A", 20, 17))
        self.assertEqual(r.repeat_meetings(), 1)

    def test_a_pair_meeting_three_times_counts_two(self):
        r = make_ranker(("A", "B", 30, 10), ("B", "A", 20, 17),
                        ("A", "B", 24, 21))
        self.assertEqual(r.repeat_meetings(), 2)

    def test_unranked_opponents_alone_are_not_rematches(self):
        """The bug: these inflate total_games() without creating pairs."""
        r = FBSRoundRobinRanker()
        r.add_game("A", "B", 30, 10)
        r.add_game("A", "Tarleton State", 20, 27, away_ranked=False)
        r.add_game("B", "Austin Peay", 30, 10, away_ranked=False)
        self.assertEqual(r.total_games(), 3)
        self.assertEqual(r.repeat_meetings(), 0)

    def test_a_repeated_unranked_opponent_does_count(self):
        r = FBSRoundRobinRanker()
        r.add_game("A", "B", 30, 10)
        r.add_game("A", "Tarleton State", 20, 27, away_ranked=False)
        r.add_game("A", "Tarleton State", 30, 10, away_ranked=False)
        self.assertEqual(r.repeat_meetings(), 1)

    def test_ranked_and_unranked_rematches_add_up(self):
        r = FBSRoundRobinRanker()
        r.add_game("A", "B", 30, 10)
        r.add_game("B", "A", 20, 17)
        r.add_game("A", "Tarleton State", 20, 27, away_ranked=False)
        r.add_game("A", "Tarleton State", 30, 10, away_ranked=False)
        self.assertEqual(r.repeat_meetings(), 2)


class TestBlendEpsilon(unittest.TestCase):
    """How far apart two blended scores must be to count as different.

    The blend is built from shrunk averages, so two teams can differ in the
    fourth decimal for no reason a game could account for.  Measured on the
    2023-2025 seasons, one of a team's OWN games is worth about .047 of blend
    and one game played by ONE of its opponents about .0045, so a gap below
    .001 is finer than any evidence the data can express.

    Until this was set deliberately the comparison used a bare 1e-9 literal —
    float noise, meaning any difference at all decided.  In 2023 that put
    Washington above Michigan on a gap of .00012, overriding a 171-point
    differential.

    No test pins the behaviour of a gap exactly equal to the threshold: the
    comparison is a strict >, and floating point makes "exactly equal"
    unreachable in practice — 0.5 + 0.001 - 0.5 is 0.0010000000000000009.
    Nothing should depend on which side of the boundary such a gap lands.
    """

    class ScriptedBlend(FBSRoundRobinRanker):
        """A ranker whose blended scores are dictated, not computed."""

        scores: dict = {}

        def blended_score(self, team):
            return self.scores.get(team, 0.5)

    def _ranker(self, epsilon, gap):
        # Two teams that never meet and share nothing: strength 0 decides.
        # X has the marginally better blend; Y has far the better point diff.
        r = self.ScriptedBlend()
        r.BLEND_EPSILON = epsilon
        r.scores = {"X": 0.500 + gap, "Y": 0.500}
        r.add_game("X", "Xo", 20, 17)      # X wins by 3
        r.add_game("Y", "Yo", 60, 0)       # Y wins by 60
        return r

    def _cliques(self, ranker):
        import networkx as nx
        g = nx.Graph()
        g.add_nodes_from(ranker.teams)
        for ta, tb in ranker._game_map:
            g.add_edge(ta, tb)
        return list(nx.find_cliques(g))

    def test_default_is_a_fifth_of_one_opponent_game(self):
        self.assertEqual(FBSRoundRobinRanker.BLEND_EPSILON, 0.001)

    def test_a_gap_finer_than_the_default_no_longer_decides(self):
        """The 2023 Washington / Michigan shape: .00012 is not a difference."""
        r = self._ranker(epsilon=FBSRoundRobinRanker.BLEND_EPSILON,
                         gap=0.00012)
        cmp, _ = r._pairwise_compare("X", "Y", self._cliques(r))
        self.assertEqual(cmp, 1, "point differential should decide")

    def test_a_gap_above_the_threshold_decides(self):
        r = self._ranker(epsilon=0.0001, gap=0.001)
        cmp, strength = r._pairwise_compare("X", "Y", self._cliques(r))
        self.assertEqual(strength[0], 0)
        self.assertEqual(cmp, -1, "X's better blend should win")

    def test_a_gap_below_the_threshold_hands_over_to_point_differential(self):
        r = self._ranker(epsilon=0.001, gap=0.0001)
        cmp, strength = r._pairwise_compare("X", "Y", self._cliques(r))
        self.assertEqual(cmp, 1, "Y's better point differential should win")
        self.assertEqual(strength[1], 0.0, "the blend gap is not the reason")

    def test_equal_blends_and_equal_point_diffs_are_a_genuine_tie(self):
        r = self.ScriptedBlend()
        r.scores = {"X": 0.5, "Y": 0.5}
        r.add_game("X", "Xo", 20, 17)
        r.add_game("Y", "Yo", 20, 17)
        cmp, strength = r._pairwise_compare("X", "Y", self._cliques(r))
        self.assertEqual(cmp, 0)
        self.assertEqual(strength, (0, 0.0, 0))

    def test_the_threshold_cannot_reach_above_strength_zero(self):
        """A group verdict is unaffected however wide the threshold is."""
        for epsilon in (1e-9, 0.001, 1.0):
            r = make_ranker(("G1", "G2", 17, 14), ("G1", "G3", 20, 0),
                            ("G2", "G3", 30, 10))
            r.BLEND_EPSILON = epsilon
            self.assertEqual(ranked_names(r.rank()), ["G1", "G2", "G3"],
                             f"group order changed at epsilon={epsilon}")


class TestStrengthZeroOrder(unittest.TestCase):
    """Which of the two strength-0 signals decides first.

    The default compares the blended score and falls through to overall point
    differential.  "point_diff" reverses that.  Measured on 2022-2025 the
    reversal costs 5% to 20% more record inversions and predicts held-out
    postseason games exactly as well (identically, in fact: only 8 of 176
    pairs change direction and they cancel), so the default stays.
    """

    def _pair(self, order):
        """X has the better blend; Y has far the better point differential."""
        r = FBSRoundRobinRanker()
        r.STRENGTH_ZERO_ORDER = order
        r.add_game("X", "Xo", 20, 17)      # X wins by 3
        r.add_game("X", "Xp", 20, 17)
        r.add_game("Y", "Yo", 70, 0)       # Y wins by 70
        r.add_game("Yo", "Y", 24, 21)      # and loses one, to keep records level
        return r

    def _cliques(self, ranker):
        import networkx as nx
        g = nx.Graph()
        g.add_nodes_from(ranker.teams)
        for ta, tb in ranker._game_map:
            g.add_edge(ta, tb)
        return list(nx.find_cliques(g))

    def test_default_is_blend_first(self):
        self.assertEqual(FBSRoundRobinRanker.STRENGTH_ZERO_ORDER, "blend")

    def test_blend_first_reports_the_blend_gap_as_the_margin(self):
        r = self._pair("blend")
        cmp, strength = r._pairwise_compare("X", "Y", self._cliques(r))
        self.assertEqual(strength[0], 0)
        self.assertGreater(strength[1], 0.0,
                           "the blend gap should be the deciding margin")

    def test_point_diff_first_reports_the_point_gap_as_the_margin(self):
        r = self._pair("point_diff")
        cmp, strength = r._pairwise_compare("X", "Y", self._cliques(r))
        self.assertEqual(strength[0], 0)
        self.assertGreater(strength[1], 1.0,
                           "the point-differential gap should be the margin")

    def test_the_two_orders_can_disagree(self):
        a = self._pair("blend")
        b = self._pair("point_diff")
        ca, _ = a._pairwise_compare("X", "Y", self._cliques(a))
        cb, _ = b._pairwise_compare("X", "Y", self._cliques(b))
        self.assertNotEqual(ca, cb, "the fixture should separate the two orders")

    def test_neither_order_reaches_above_strength_zero(self):
        for order in ("blend", "point_diff"):
            r = make_ranker(("G1", "G2", 17, 14), ("G1", "G3", 20, 0),
                            ("G2", "G3", 30, 10))
            r.STRENGTH_ZERO_ORDER = order
            self.assertEqual(ranked_names(r.rank()), ["G1", "G2", "G3"],
                             f"group order changed under {order}")


class TestDefaultWeights(unittest.TestCase):
    """The shipped defaults are a deliberate choice, so pin them."""

    def test_blend_weights(self):
        self.assertEqual(FBSRoundRobinRanker.BLEND_WEIGHTS, (0.75, 0.25, 0.0))

    def test_the_third_term_is_off_by_default(self):
        """Opponents' opponents were measured, found redundant, and dropped.

        The machinery stays — blended_score honours a non-zero third weight
        and TestBlendedScore covers it — but nothing in four seasons showed it
        deciding anything the second term did not already decide.
        """
        self.assertEqual(FBSRoundRobinRanker.BLEND_WEIGHTS[2], 0.0)

    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(FBSRoundRobinRanker.BLEND_WEIGHTS), 1.0)

    def test_min_differing_results(self):
        self.assertEqual(FBSRoundRobinRanker.MIN_DIFFERING_RESULTS, 2)

    def test_prior_games(self):
        self.assertEqual(FBSRoundRobinRanker.PRIOR_GAMES, 4.0)


class TestTierHierarchyHolds(unittest.TestCase):
    """The blend can never reorder teams a group or shared opponents settled."""

    def setUp(self):
        self.games = [
            # 3-team group: G1 beats G2 beats G3.
            ("G1", "G2", 30, 10), ("G1", "G3", 30, 10), ("G2", "G3", 30, 10),
            # G3 then feasts on weak teams outside the group.
            ("G3", "W1", 50, 0), ("G3", "W2", 50, 0), ("G3", "W3", 50, 0),
        ]

    def test_group_order_survives_every_weighting(self):
        for weights in [(1.0, 0.0, 0.0), (0.5, 0.5, 0.0), (0.0, 1.0, 0.0),
                        (0.0, 0.0, 1.0), (0.34, 0.33, 0.33)]:
            r = make_ranker(*self.games)
            r.BLEND_WEIGHTS = weights
            names = ranked_names(r.rank())
            self.assertLess(names.index("G1"), names.index("G2"), weights)
            self.assertLess(names.index("G2"), names.index("G3"), weights)


if __name__ == "__main__":
    unittest.main(verbosity=2)
