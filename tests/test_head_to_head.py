"""
Unit tests for head_to_head.py

This is the check that catches an unverified claim about the ranker, so the
tests pin both mechanisms that put a loser above a winner, and characterise
the rate on the repository's own data.  A change that moves those numbers
should have to say so out loud.
"""

import csv
import os
import sys
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import head_to_head as h2h
from fbs_ranker import FBSRoundRobinRanker

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_ranker(*games):
    ranker = FBSRoundRobinRanker(on_duplicate="combine")
    for home, away, home_score, away_score in games:
        ranker.add_game(home, away, home_score, away_score)
    return ranker


def cycle_ranker():
    """Three teams beating each other in a cycle: all 1-1 inside the group.

    Point differential breaks it, and it breaks against A -- who beat B but
    finishes below B.  This is the 'group ranked them the other way' path.
    """
    return make_ranker(("A", "B", 21, 20), ("B", "C", 21, 0), ("C", "A", 30, 0))


def override_ranker():
    """W beats L, shares no opponent with L, and still finishes below it.

    L is 3-0 in a four-team group, W is 0-3 in another, and a chain of those
    larger groups links L above W.  The W-over-L verdict is real but sits at
    strength 2, so ranked pairs discards it.
    """
    games = []
    for pair in (("L", "a"), ("L", "b"), ("L", "c"), ("a", "b"), ("a", "c"),
                 ("b", "c"), ("x", "W"), ("y", "W"), ("z", "W"), ("x", "y"),
                 ("x", "z"), ("y", "z"), ("a", "x"), ("a", "m"), ("x", "m")):
        games.append((pair[0], pair[1], 30, 0))
    games.append(("W", "L", 21, 0))
    return make_ranker(*games)


class TestSeriesWinner(unittest.TestCase):

    def test_a_single_game_names_its_winner(self):
        self.assertEqual(h2h.series_winner([(21, 7)]), (0, "1-0"))
        self.assertEqual(h2h.series_winner([(7, 21)]), (1, "1-0"))

    def test_a_swept_series_names_the_sweeper(self):
        self.assertEqual(h2h.series_winner([(21, 7), (14, 10)]), (0, "2-0"))

    def test_a_split_series_has_no_winner(self):
        self.assertIsNone(h2h.series_winner([(21, 7), (7, 21)]))

    def test_a_series_won_two_of_three(self):
        self.assertEqual(h2h.series_winner([(21, 7), (7, 21), (3, 0)]),
                         (0, "2-1"))


class TestMechanisms(unittest.TestCase):
    """The two ways a winner ends up below the team it beat."""

    def test_a_cycle_is_broken_by_the_group_not_the_head_to_head(self):
        ranker = cycle_ranker()
        report = h2h.audit(ranker)
        self.assertEqual(report["contradicted"], 1)
        only = report["contradictions"][0]
        self.assertEqual((only["winner"], only["loser"]), ("A", "B"))
        self.assertEqual(only["mechanism"], h2h.GROUP)

    def test_a_strength_two_verdict_can_be_overridden(self):
        ranker = override_ranker()
        report = h2h.audit(ranker)
        found = [c for c in report["contradictions"] if c["winner"] == "W"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["loser"], "L")
        self.assertEqual(found[0]["mechanism"], h2h.OVERRIDDEN)
        # A game between teams with no common opponent is its own whole group,
        # which is the weakest group strength there is.
        self.assertEqual(found[0]["group_size"], 2)

    def test_the_overridden_verdict_really_did_favour_the_winner(self):
        # Otherwise this would be the group mechanism wearing the wrong label.
        ranker = override_ranker()
        cliques = h2h.maximal_cliques(ranker)
        cmp, _ = ranker._pairwise_compare("L", "W", cliques)
        self.assertGreater(cmp, 0, "the pairwise verdict should favour W")


class TestAudit(unittest.TestCase):

    def test_a_ranking_that_respects_every_result_reports_none(self):
        ranker = make_ranker(("A", "B", 21, 0), ("B", "C", 21, 0),
                             ("A", "C", 21, 0))
        report = h2h.audit(ranker)
        self.assertEqual(report["contradicted"], 0)
        self.assertEqual(report["decided"], 3)
        self.assertEqual(report["pct"], 0.0)

    def test_a_split_series_is_neither_respected_nor_contradicted(self):
        ranker = make_ranker(("A", "B", 21, 0), ("A", "B", 0, 21))
        report = h2h.audit(ranker)
        self.assertEqual(report["split"], 1)
        self.assertEqual(report["decided"], 0)
        self.assertEqual(report["contradicted"], 0)

    def test_contradictions_are_ordered_by_how_far_apart_they_are(self):
        report = h2h.audit(override_ranker())
        gaps = [c["gap"] for c in report["contradictions"]]
        self.assertEqual(gaps, sorted(gaps, reverse=True))

    def test_the_percentage_is_of_decided_series(self):
        report = h2h.audit(cycle_ranker())
        self.assertAlmostEqual(report["pct"], 100 / 3)


class TestSinceFilter(unittest.TestCase):

    def setUp(self):
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         newline="", encoding="utf-8")
        writer = csv.writer(fh)
        writer.writerow(["date", "home_team", "home_score",
                         "away_team", "away_score"])
        writer.writerows([
            ["2025-09-06", "A", 21, "B", 20],
            ["2025-09-13", "B", 21, "C", 0],
            ["2025-12-20", "C", 30, "A", 0],      # the only late meeting
        ])
        fh.close()
        self.path = fh.name

    def tearDown(self):
        os.unlink(self.path)

    def test_pair_dates_are_keyed_the_way_the_ranker_keys_a_pair(self):
        dates = h2h.pair_dates(self.path)
        self.assertIn(("A", "C"), dates)          # sorted, not home/away
        self.assertEqual(dates[("A", "C")], ["2025-12-20"])

    def test_since_audits_only_pairs_that_met_on_or_after_it(self):
        ranker = FBSRoundRobinRanker(on_duplicate="combine")
        ranker.load_csv(self.path)
        dates = h2h.pair_dates(self.path)
        full = h2h.audit(ranker)
        late = h2h.audit(ranker, since="2025-12-01", dates=dates)
        self.assertEqual(full["decided"], 3)
        self.assertEqual(late["decided"], 1)

    def test_since_still_ranks_on_every_game(self):
        # The filter narrows what is audited, never what the ranking saw.
        ranker = FBSRoundRobinRanker(on_duplicate="combine")
        ranker.load_csv(self.path)
        late = h2h.audit(ranker, since="2025-12-01",
                         dates=h2h.pair_dates(self.path))
        self.assertEqual(len(ranker.teams), 3)
        self.assertEqual(late["contradicted"] + late["respected"], 1)


class TestAgainstRealSeasons(unittest.TestCase):
    """Characterisation: these numbers were measured, not assumed.

    The ranker contradicts a double-digit share of head-to-head results in
    every season in the repository.  That is a consequence of the tier
    hierarchy, not a bug -- but it was documented as impossible until this
    check was written, so the counts are pinned here.  A change that moves
    them should have to update this test deliberately.
    """

    def _audit(self, name):
        path = os.path.join(REPO, name)
        if not os.path.exists(path):
            self.skipTest(f"{name} not present")
        ranker = FBSRoundRobinRanker(on_duplicate="combine")
        ranker.load_csv(path)
        return h2h.audit(ranker)

    def test_every_season_contradicts_some_results(self):
        for name in ("games_2022.csv", "games_2023.csv",
                     "games_2024.csv", "games_2025.csv"):
            report = self._audit(name)
            self.assertGreater(report["contradicted"], 0, name)
            self.assertLess(report["pct"], 20.0, name)

    def test_the_2025_counts_are_what_they_were_measured_to_be(self):
        report = self._audit("games_2025.csv")
        self.assertEqual(report["decided"], 750)
        self.assertEqual(report["contradicted"], 92)
        by = {}
        for item in report["contradictions"]:
            by[item["mechanism"]] = by.get(item["mechanism"], 0) + 1
        self.assertEqual(by[h2h.GROUP], 65)
        self.assertEqual(by[h2h.OVERRIDDEN], 27)

    def test_the_group_mechanism_is_the_larger_share(self):
        for name in ("games_2022.csv", "games_2023.csv",
                     "games_2024.csv", "games_2025.csv"):
            report = self._audit(name)
            counts = {}
            for item in report["contradictions"]:
                counts[item["mechanism"]] = counts.get(item["mechanism"], 0) + 1
            self.assertGreater(counts.get(h2h.GROUP, 0),
                               counts.get(h2h.OVERRIDDEN, 0), name)


class TestCommandLine(unittest.TestCase):

    def setUp(self):
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         newline="", encoding="utf-8")
        writer = csv.writer(fh)
        writer.writerow(["home_team", "home_score", "away_team", "away_score"])
        writer.writerows([["A", 21, "B", 20], ["B", 21, "C", 0],
                          ["C", 30, "A", 0]])
        fh.close()
        self.path = fh.name

    def tearDown(self):
        os.unlink(self.path)

    def _run(self, *args):
        out = StringIO()
        with patch.object(sys, "argv", ["head_to_head.py", self.path, *args]), \
                patch.object(sys, "stdout", out):
            h2h.main()
        return out.getvalue()

    def test_it_reports_both_totals(self):
        text = self._run()
        self.assertIn("decided head-to-head series", text)
        self.assertIn("contradicts them", text)

    def test_it_names_the_mechanism(self):
        self.assertIn("group ranked them the other way", self._run())

    def test_the_list_flag_can_be_switched_off(self):
        self.assertNotIn("largest contradictions", self._run("--list", "0"))

    def test_max_pct_passes_when_under_the_limit(self):
        self._run("--max-pct", "50")          # 33.3% here: no SystemExit

    def test_max_pct_fails_when_over_the_limit(self):
        err = StringIO()
        with patch.object(sys, "argv",
                          ["head_to_head.py", self.path, "--max-pct", "10"]), \
                patch.object(sys, "stdout", StringIO()), \
                patch.object(sys, "stderr", err):
            with self.assertRaises(SystemExit) as caught:
                h2h.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("exceeds", err.getvalue())

    def test_a_missing_file_fails_cleanly(self):
        err = StringIO()
        with patch.object(sys, "argv", ["head_to_head.py", "nope.csv"]), \
                patch.object(sys, "stderr", err):
            with self.assertRaises(SystemExit):
                h2h.main()
        self.assertIn("error", err.getvalue())


if __name__ == "__main__":
    unittest.main()
