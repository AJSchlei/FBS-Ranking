"""
Unit tests for compare_weights.py

The comparison is a reporting tool, so these check that it measures what it
claims to: movement between orderings, record inversions, and the influence
figures that make nominal weights misleading.
"""

import csv
import os
import sys
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import compare_weights
from fbs_ranker import FBSRoundRobinRanker


def write_csv(rows):
    fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                     newline="", encoding="utf-8")
    writer = csv.writer(fh)
    writer.writerow(["home_team", "home_score", "away_team", "away_score"])
    writer.writerows(rows)
    fh.close()
    return fh.name


class TestLabel(unittest.TestCase):
    def test_trailing_zeros_are_trimmed(self):
        self.assertEqual(compare_weights.label((0.75, 0.25, 0.0)),
                         "(0.75, 0.25, 0)")

    def test_whole_numbers_stay_short(self):
        self.assertEqual(compare_weights.label((1.0, 0.0, 0.0)), "(1, 0, 0)")


class TestMovement(unittest.TestCase):
    def test_identical_orderings_move_nothing(self):
        order = ["A", "B", "C", "D"]
        self.assertEqual(compare_weights.movement(order, list(order)),
                         (0, 0, 0))

    def test_a_swap_moves_two_teams_one_place(self):
        moved, median, biggest = compare_weights.movement(
            ["A", "B", "C"], ["B", "A", "C"])
        self.assertEqual((moved, median, biggest), (2, 1.0, 1))

    def test_the_max_is_the_furthest_single_move(self):
        moved, _, biggest = compare_weights.movement(
            ["A", "B", "C", "D"], ["D", "A", "B", "C"])
        self.assertEqual(moved, 4)
        self.assertEqual(biggest, 3)


class TestCountInversions(unittest.TestCase):
    def rows(self, *records):
        return [{"team": name, "overall_wins": w, "overall_losses": l}
                for name, w, l in records]

    def test_a_correctly_ordered_field_has_none(self):
        rows = self.rows(("A", 9, 1), ("B", 5, 5), ("C", 1, 9))
        self.assertEqual(compare_weights.count_inversions(rows, 0.15), 0)

    def test_a_clear_inversion_is_counted(self):
        rows = self.rows(("A", 1, 9), ("B", 9, 1))
        self.assertEqual(compare_weights.count_inversions(rows, 0.15), 1)

    def test_a_gap_below_the_margin_is_not_counted(self):
        # .600 vs .500 is a .100 gap, under the .15 margin.
        rows = self.rows(("A", 5, 5), ("B", 6, 4))
        self.assertEqual(compare_weights.count_inversions(rows, 0.15), 0)
        self.assertEqual(compare_weights.count_inversions(rows, 0.05), 1)

    def test_a_winless_team_does_not_divide_by_zero(self):
        rows = self.rows(("A", 0, 0), ("B", 9, 1))
        self.assertEqual(compare_weights.count_inversions(rows, 0.15), 1)


class TestInfluenceTable(unittest.TestCase):
    """Weight times spread, the figure that makes nominal weights misleading."""

    def setUp(self):
        self.ranker = FBSRoundRobinRanker()
        for home, away in [("A", "B"), ("B", "C"), ("C", "D"), ("D", "A"),
                           ("A", "C"), ("B", "D")]:
            self.ranker.add_game(home, away, 30, 10)

    def test_record_only_weights_give_opponents_no_influence(self):
        (weights, own, opp, oopp, ratio), = compare_weights.influence_table(
            self.ranker, [(1.0, 0.0, 0.0)])
        self.assertGreater(own, 0)
        self.assertEqual(opp, 0)
        self.assertIsNone(ratio, "a zero opponent term has no finite ratio")

    def test_lowering_the_own_weight_lowers_the_ratio(self):
        # Needs a field whose opponent records actually differ, or there is
        # no opponent signal to weigh against.
        r = FBSRoundRobinRanker()
        for i in range(1, 5):
            r.add_game("Strong", f"Weak{i}", 30, 10)
        r.add_game("Weak1", "Weak2", 30, 10)
        r.add_game("Weak3", "Weak4", 30, 10)
        table = compare_weights.influence_table(
            r, [(0.75, 0.25, 0.0), (0.5, 0.5, 0.0)])
        self.assertIsNotNone(table[0][4])
        self.assertGreater(table[0][4], table[1][4])

    def test_an_undifferentiated_field_yields_no_ratio(self):
        """Identical opponent records leave nothing to weigh, not a huge ratio."""
        (_, _, _, _, ratio), = compare_weights.influence_table(
            self.ranker, [(0.75, 0.25, 0.0)])
        self.assertIsNone(ratio)

    def test_a_single_team_yields_no_table(self):
        r = FBSRoundRobinRanker()
        r.add_game("A", "Tarleton State", 30, 10, away_ranked=False)
        self.assertEqual(compare_weights.influence_table(r, [(1.0, 0.0, 0.0)]),
                         [])


class TestCli(unittest.TestCase):
    GAMES = [("A", 30, "B", 10), ("B", 30, "C", 10), ("C", 30, "D", 10),
             ("D", 30, "A", 10), ("A", 30, "C", 10), ("B", 30, "D", 10),
             ("E", 30, "F", 10), ("F", 30, "G", 10), ("G", 30, "E", 10)]

    def setUp(self):
        self.path = write_csv(self.GAMES)
        self.addCleanup(os.unlink, self.path)

    def run_cli(self, *argv):
        out = StringIO()
        with patch("sys.stdout", out):
            code = compare_weights.main([self.path, *argv])
        return code, out.getvalue()

    def test_it_runs_and_reports_the_season(self):
        code, text = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("7 teams", text)
        self.assertIn("9 games", text)

    def test_every_default_weighting_appears(self):
        _, text = self.run_cli()
        for weights in compare_weights.DEFAULT_WEIGHTS:
            self.assertIn(compare_weights.label(weights), text)

    def test_custom_weights_replace_the_defaults(self):
        _, text = self.run_cli("--weights", "1,0,0", "0.9,0.1,0")
        self.assertIn("(0.9, 0.1, 0)", text)
        self.assertNotIn("(0.5, 0.5, 0)", text)

    def test_top_limits_the_table(self):
        _, text = self.run_cli("--top", "3")
        self.assertIn("Top 3", text)

    def test_malformed_weights_are_rejected(self):
        with self.assertRaises(SystemExit):
            with patch("sys.stderr", StringIO()):
                compare_weights.main([self.path, "--weights", "1,0"])

    def test_non_numeric_weights_are_rejected(self):
        with self.assertRaises(SystemExit):
            with patch("sys.stderr", StringIO()):
                compare_weights.main([self.path, "--weights", "a,b,c"])

    def test_a_stranded_team_is_warned_about(self):
        """A team whose only opponent is unranked is connected to nothing."""
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         newline="", encoding="utf-8")
        writer = csv.writer(fh)
        writer.writerow(["home_team", "home_score", "away_team", "away_score",
                         "home_ranked", "away_ranked"])
        writer.writerows([[h, hs, a, a_s, "true", "true"]
                          for h, hs, a, a_s in self.GAMES])
        writer.writerow(["Dakota State", 30, "North Dakota State", 10,
                         "true", "false"])
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        out = StringIO()
        with patch("sys.stdout", out):
            compare_weights.main([fh.name, "--top", "2"])
        text = out.getvalue()
        self.assertIn("played no ranked opponent", text)
        self.assertIn("Dakota State", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
