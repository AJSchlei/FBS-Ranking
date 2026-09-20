"""
Unit tests for season_progression.py

The replay is a measurement tool, so these check that it measures what it
claims to: the season calendar it cuts weeks on, the evidence tiers it
attributes pairs to, and the stability statistics it reports.  The calendar
rule is checked against the real opening Saturdays of 2022-2025, and the
clique index is checked against the unindexed comparison it stands in for.
"""

import csv
import os
import sys
import tempfile
import unittest
from io import StringIO
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import season_progression as sp
from fbs_ranker import FBSRoundRobinRanker

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A 6-team round robin played over five Saturdays of the 2025 calendar.
# Earlier letter always wins, so the final order is A, B, C, D, E, F.
ROUNDS = [
    ("2025-08-23", [("A", "F"), ("B", "E"), ("C", "D")]),   # CFB week 0
    ("2025-08-30", [("B", "F"), ("A", "C"), ("D", "E")]),   # week 1
    ("2025-09-06", [("C", "F"), ("B", "D"), ("A", "E")]),   # week 2
    ("2025-09-13", [("D", "F"), ("C", "E"), ("A", "B")]),   # week 3
    ("2025-09-20", [("E", "F"), ("A", "D"), ("B", "C")]),   # week 4
]


def write_csv(rows, header=("date", "home_team", "home_score",
                            "away_team", "away_score",
                            "home_ranked", "away_ranked")):
    fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                     newline="", encoding="utf-8")
    writer = csv.writer(fh)
    writer.writerow(header)
    writer.writerows(rows)
    fh.close()
    return fh.name


def round_robin_rows():
    rows = []
    for date, pairs in ROUNDS:
        for home, away in pairs:
            # The earlier letter wins; margin keeps point diff unambiguous.
            if home < away:
                rows.append([date, home, 21, away, 7, "true", "true"])
            else:
                rows.append([date, home, 7, away, 21, "true", "true"])
    return rows


class TestSeasonCalendar(unittest.TestCase):
    """Week 0 is the Saturday nine days before Labor Day."""

    def test_labor_day_is_the_first_monday_of_september(self):
        for year, day in ((2022, 5), (2023, 4), (2024, 2),
                          (2025, 1), (2026, 7)):
            found = sp.labor_day(year)
            self.assertEqual((found.month, found.day), (9, day))
            self.assertEqual(found.weekday(), 0)

    def test_week_zero_matches_the_real_opening_saturdays(self):
        # The rule is only worth using because it reproduces the seasons in
        # the repo: if a future season moves, this test is what catches it.
        for year, opening in ((2022, "2022-08-27"), (2023, "2023-08-26"),
                              (2024, "2024-08-24"), (2025, "2025-08-23")):
            self.assertEqual(sp.week_zero_saturday(year).isoformat(), opening)

    def test_week_zero_matches_the_first_game_in_each_data_file(self):
        for year in (2022, 2023, 2024, 2025):
            path = os.path.join(REPO, f"games_{year}.csv")
            if not os.path.exists(path):
                continue
            with open(path, newline="", encoding="utf-8") as fh:
                dates = [r["date"] for r in csv.DictReader(fh) if r["date"]]
            self.assertEqual(min(dates), sp.week_zero_saturday(year).isoformat())

    def test_week_monday_follows_that_weeks_saturday(self):
        for week in range(0, 16):
            monday = sp.week_monday(2026, week)
            self.assertEqual(monday.weekday(), 0)
            self.assertEqual((monday - sp.week_zero_saturday(2026)).days,
                             7 * week + 2)

    def test_season_year_is_the_year_it_kicked_off(self):
        self.assertEqual(sp.season_year(["2025-08-23", "2026-01-09"]), 2025)


class TestWeekCutoffs(unittest.TestCase):

    def test_cutoffs_start_at_week_zero_and_cover_the_last_game(self):
        cutoffs = sp.week_cutoffs([d for d, _ in ROUNDS])
        self.assertEqual(cutoffs[0][0], 0)
        self.assertGreaterEqual(cutoffs[-1][1], max(d for d, _ in ROUNDS))

    def test_cutoffs_are_consecutive_weeks(self):
        cutoffs = sp.week_cutoffs([d for d, _ in ROUNDS])
        self.assertEqual([w for w, _ in cutoffs], list(range(len(cutoffs))))

    def test_a_saturday_slate_lands_whole_inside_one_week(self):
        # Every game of a round falls at or before that round's cutoff, and
        # after the previous one -- no slate is split across two weeks.
        cutoffs = dict(sp.week_cutoffs([d for d, _ in ROUNDS]))
        for week, (date, _) in enumerate(ROUNDS):
            self.assertLessEqual(date, cutoffs[week])
            if week:
                self.assertGreater(date, cutoffs[week - 1])


class TestLoadRows(unittest.TestCase):

    def test_a_date_column_is_required(self):
        path = write_csv([["A", 21, "B", 7]],
                         header=("home_team", "home_score",
                                 "away_team", "away_score"))
        try:
            with self.assertRaises(ValueError) as caught:
                sp.load_rows(path)
            self.assertIn("date", str(caught.exception))
        finally:
            os.unlink(path)

    def test_unranked_marking_applies_to_the_whole_season(self):
        # Marked unranked in one row and ranked in another: the unranked
        # marking wins, as it does in load_csv, and for the same reason.
        path = write_csv([
            ["2025-08-23", "A", 21, "Tiny College", 7, "true", "false"],
            ["2025-08-30", "B", 14, "Tiny College", 10, "true", "true"],
        ])
        try:
            _, unranked = sp.load_rows(path)
            self.assertIn("Tiny College", unranked)
        finally:
            os.unlink(path)


class TestRankerThrough(unittest.TestCase):

    def setUp(self):
        self.path = write_csv(round_robin_rows())
        self.rows, self.unranked = sp.load_rows(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def test_games_after_the_cutoff_are_excluded(self):
        ranker, played = sp.ranker_through(self.rows, self.unranked,
                                           "2025-08-30")
        self.assertEqual(played, 6)          # two rounds of three
        self.assertEqual(ranker.total_games(), 6)

    def test_the_full_season_loads_every_game(self):
        ranker, played = sp.ranker_through(self.rows, self.unranked,
                                           "2025-09-20")
        self.assertEqual(played, 15)         # a complete 6-team round robin
        self.assertEqual(len(ranker.teams), 6)

    def test_a_cutoff_before_kickoff_loads_nothing(self):
        ranker, played = sp.ranker_through(self.rows, self.unranked,
                                           "2025-08-01")
        self.assertEqual(played, 0)
        self.assertEqual(ranker.teams, set())


class TestTierCounts(unittest.TestCase):

    def setUp(self):
        self.path = write_csv(round_robin_rows())
        self.rows, self.unranked = sp.load_rows(self.path)

    def tearDown(self):
        os.unlink(self.path)

    def _ranker(self, cutoff):
        return sp.ranker_through(self.rows, self.unranked, cutoff)[0]

    def test_counts_cover_every_pair_exactly_once(self):
        ranker = self._ranker("2025-09-06")
        counts = sp.tier_counts(ranker)
        teams = len(ranker.teams)
        self.assertEqual(counts["total"], teams * (teams - 1) // 2)
        self.assertEqual(counts["group"] + counts["common"] + counts["blend"],
                         counts["total"])

    def test_a_completed_round_robin_decides_everything_by_group(self):
        counts = sp.tier_counts(self._ranker("2025-09-20"))
        self.assertEqual(counts["largest_group"], 6)
        self.assertEqual(counts["blend"], 0)
        self.assertEqual(counts["group"], counts["total"])

    def test_the_first_week_has_no_group_bigger_than_a_pair(self):
        counts = sp.tier_counts(self._ranker("2025-08-25"))
        self.assertEqual(counts["largest_group"], 2)

    def test_the_clique_index_matches_an_unindexed_comparison(self):
        # tier_counts hands _pairwise_compare only the cliques containing both
        # teams, which is what it would filter down to anyway.  If that ever
        # stops being true the counts would silently drift, so check it.
        import itertools
        import networkx as nx
        for cutoff in ("2025-08-30", "2025-09-06", "2025-09-20"):
            ranker = self._ranker(cutoff)
            graph = nx.Graph()
            graph.add_nodes_from(ranker.teams)
            for team_a, team_b in ranker._game_map:
                graph.add_edge(team_a, team_b)
            cliques = list(nx.find_cliques(graph))
            index = sp.shared_clique_index(cliques)
            for pair in itertools.combinations(sorted(ranker.teams), 2):
                self.assertEqual(
                    ranker._pairwise_compare(pair[0], pair[1],
                                             index.get(pair, [])),
                    ranker._pairwise_compare(pair[0], pair[1], cliques),
                    f"{pair} diverged at {cutoff}")


class TestGamesPlayed(unittest.TestCase):

    def test_every_team_is_counted_once_per_game(self):
        path = write_csv(round_robin_rows())
        try:
            rows, unranked = sp.load_rows(path)
            ranker, _ = sp.ranker_through(rows, unranked, "2025-09-20")
            played = sp.games_played(ranker)
            self.assertEqual(set(played.values()), {5})
        finally:
            os.unlink(path)

    def test_unranked_opponents_still_count_as_games(self):
        path = write_csv([
            ["2025-08-23", "A", 21, "B", 7, "true", "true"],
            ["2025-08-30", "A", 35, "Tiny College", 3, "true", "false"],
        ])
        try:
            rows, unranked = sp.load_rows(path)
            ranker, _ = sp.ranker_through(rows, unranked, "2025-09-20")
            played = sp.games_played(ranker)
            self.assertEqual(played["A"], 2)
            self.assertEqual(played["B"], 1)
            self.assertNotIn("Tiny College", played)
        finally:
            os.unlink(path)


class TestKendallTauB(unittest.TestCase):

    def test_an_identical_ordering_scores_one(self):
        order = {"A": 1, "B": 2, "C": 3, "D": 4}
        self.assertAlmostEqual(sp.kendall_tau_b(order, order, list(order)), 1.0)

    def test_a_reversed_ordering_scores_minus_one(self):
        forward = {"A": 1, "B": 2, "C": 3, "D": 4}
        backward = {"A": 4, "B": 3, "C": 2, "D": 1}
        self.assertAlmostEqual(
            sp.kendall_tau_b(forward, backward, list(forward)), -1.0)

    def test_one_swapped_pair_of_four(self):
        forward = {"A": 1, "B": 2, "C": 3, "D": 4}
        swapped = {"A": 2, "B": 1, "C": 3, "D": 4}
        # Five of six pairs agree, one disagrees: (5 - 1) / 6.
        self.assertAlmostEqual(
            sp.kendall_tau_b(forward, swapped, list(forward)), 4 / 6)

    def test_it_matches_the_standard_tau_b_on_known_values(self):
        # Published tau-b values; the tie-heavy cases are the ones that catch
        # a denominator that adds tied pairs instead of subtracting them.
        def tau(first, second):
            left = {i: v for i, v in enumerate(first)}
            right = {i: v for i, v in enumerate(second)}
            return sp.kendall_tau_b(left, right, list(range(len(first))))

        self.assertAlmostEqual(tau([1, 2, 2, 3], [1, 2, 3, 4]),
                               0.9128709291752769, places=12)
        self.assertAlmostEqual(tau([1, 1, 2, 2], [1, 2, 1, 2]), 0.0, places=12)
        self.assertAlmostEqual(tau([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]), -1.0)

    def test_a_pair_tied_on_both_sides_leaves_the_rest_undiluted(self):
        # A and B are tied in both rankings; C and D agree.  The tied pair is
        # excluded from the denominator rather than counted against agreement.
        left = {"A": 1, "B": 1, "C": 3, "D": 4}
        right = {"A": 1, "B": 1, "C": 3, "D": 4}
        self.assertAlmostEqual(sp.kendall_tau_b(left, right, list(left)), 1.0)

    def test_a_ranking_with_no_ordering_at_all_is_not_a_number(self):
        flat = {"A": 1, "B": 1, "C": 1}
        result = sp.kendall_tau_b(flat, flat, list(flat))
        self.assertNotEqual(result, result)  # nan: nothing to correlate


class TestProgression(unittest.TestCase):

    def setUp(self):
        self.path = write_csv(round_robin_rows())
        self.weeks = sp.progression(self.path, band=3)

    def tearDown(self):
        os.unlink(self.path)

    def test_one_entry_per_week_with_games_played(self):
        self.assertEqual([w["week"] for w in self.weeks], list(range(5)))

    def test_games_and_evidence_accumulate(self):
        games = [w["games"] for w in self.weeks]
        self.assertEqual(games, sorted(games))
        self.assertEqual(games[-1], 15)

    def test_the_final_week_matches_itself_exactly(self):
        last = self.weeks[-1]
        self.assertAlmostEqual(last["tau"], 1.0)
        self.assertAlmostEqual(last["mean_shift"], 0.0)

    def test_the_first_week_has_no_churn_to_report(self):
        self.assertNotEqual(self.weeks[0]["churn"], self.weeks[0]["churn"])

    def test_evidence_share_is_reported_as_a_percentage(self):
        for week in self.weeks:
            self.assertGreaterEqual(week["evidence_pct"], 0.0)
            self.assertLessEqual(week["evidence_pct"], 100.0)
            self.assertAlmostEqual(week["evidence_pct"],
                                   week["group_pct"] + week["common_pct"])

    def test_quick_mode_omits_the_tier_columns(self):
        quick = sp.progression(self.path, band=3, quick=True)
        self.assertNotIn("evidence_pct", quick[0])
        self.assertIn("tau", quick[0])


class TestAverageWeeks(unittest.TestCase):

    def test_only_weeks_present_in_every_season_are_averaged(self):
        short = [dict(week=0, cutoff="", tau=0.2)]
        long = [dict(week=0, cutoff="", tau=0.4),
                dict(week=1, cutoff="", tau=0.8)]
        merged = sp.average_weeks([short, long])
        self.assertEqual([m["week"] for m in merged], [0])
        self.assertAlmostEqual(merged[0]["tau"], 0.3)

    def test_a_metric_missing_from_one_season_is_dropped(self):
        first = [dict(week=0, cutoff="", tau=0.2, band_tau=0.1)]
        second = [dict(week=0, cutoff="", tau=0.4)]
        merged = sp.average_weeks([first, second])
        self.assertIn("tau", merged[0])
        self.assertNotIn("band_tau", merged[0])

    def test_the_season_count_is_reported(self):
        runs = [[dict(week=0, cutoff="", tau=0.2)],
                [dict(week=0, cutoff="", tau=0.4)]]
        self.assertEqual(sp.average_weeks(runs)[0]["seasons"], 2)


class TestCommandLine(unittest.TestCase):

    def setUp(self):
        self.path = write_csv(round_robin_rows())

    def tearDown(self):
        os.unlink(self.path)

    def _run(self, *args):
        out = StringIO()
        with patch.object(sys, "argv", ["season_progression.py", self.path,
                                        *args]), \
                patch.object(sys, "stdout", out):
            sp.main()
        return out.getvalue()

    def test_it_prints_a_week_table(self):
        text = self._run()
        self.assertIn("evid%", text)
        self.assertIn("churn", text)

    def test_the_calendar_flag_adds_the_target_years_dates(self):
        text = self._run("--calendar", "2026")
        self.assertIn("2026", text)
        self.assertIn(sp.week_monday(2026, 0).isoformat(), text)

    def test_the_bands_flag_adds_a_band_table(self):
        text = self._run("--bands", "--band", "3")
        self.assertIn("top 3", text)

    def test_quick_mode_drops_the_tier_columns(self):
        text = self._run("--quick")
        self.assertNotIn("evid%", text)
        self.assertIn("churn", text)

    def test_the_tau_caveat_is_always_printed(self):
        # The measure is biased toward late weeks; saying so is not optional.
        self.assertIn("churn", self._run().lower())
        self.assertIn("final ordering", self._run())

    def test_a_file_without_dates_fails_cleanly(self):
        path = write_csv([["A", 21, "B", 7]],
                         header=("home_team", "home_score",
                                 "away_team", "away_score"))
        try:
            err = StringIO()
            with patch.object(sys, "argv", ["season_progression.py", path]), \
                    patch.object(sys, "stderr", err):
                with self.assertRaises(SystemExit):
                    sp.main()
            self.assertIn("date", err.getvalue())
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
