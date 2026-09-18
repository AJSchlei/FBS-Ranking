"""
Unit tests for fetch_games.py

The network is never touched: the API layer is exercised with a stubbed
urlopen, and everything else runs on fixture records shaped like real
CollegeFootballData responses.

Coverage:
  - camelCase and snake_case field names both understood
  - unplayed, cancelled, non-FBS and unclassified games dropped
  - rematches detected regardless of home/away order
  - keep_first / keep_last policies
  - the CSV it writes loads into FBSRoundRobinRanker
"""

import csv
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fetch_games
from fbs_ranker import FBSRoundRobinRanker, DuplicateGameError


def fbs_game(home, home_pts, away, away_pts, date="2025-09-06", snake=False):
    """One completed FBS-vs-FBS record, in either of CFBD's field styles."""
    if snake:
        return {"home_team": home, "home_points": home_pts,
                "away_team": away, "away_points": away_pts,
                "home_classification": "fbs", "away_classification": "fbs",
                "start_date": f"{date}T16:00:00.000Z"}
    return {"homeTeam": home, "homePoints": home_pts,
            "awayTeam": away, "awayPoints": away_pts,
            "homeClassification": "fbs", "awayClassification": "fbs",
            "startDate": f"{date}T16:00:00.000Z"}


class TestExtractFbsGames(unittest.TestCase):

    def test_keeps_a_completed_fbs_matchup(self):
        rows, _ = fetch_games.extract_fbs_games([fbs_game("Texas", 31, "Oklahoma", 24)])
        self.assertEqual(rows, [{"date": "2025-09-06", "home_team": "Texas",
                                 "home_score": 31, "away_team": "Oklahoma",
                                 "away_score": 24, "home_ranked": "true",
                                 "away_ranked": "true"}])

    def test_understands_snake_case_fields(self):
        camel, _ = fetch_games.extract_fbs_games([fbs_game("Texas", 31, "Oklahoma", 24)])
        snake, _ = fetch_games.extract_fbs_games(
            [fbs_game("Texas", 31, "Oklahoma", 24, snake=True)])
        self.assertEqual(camel, snake)

    def test_drops_games_with_no_score(self):
        """Unplayed or cancelled games carry null points."""
        raw = [fbs_game("Texas", 31, "Oklahoma", 24),
               {"homeTeam": "Utah", "homePoints": None,
                "awayTeam": "BYU", "awayPoints": None,
                "homeClassification": "fbs", "awayClassification": "fbs"}]
        rows, skipped = fetch_games.extract_fbs_games(raw)
        self.assertEqual(len(rows), 1)
        self.assertEqual(skipped["not_final"], 1)

    def test_marks_an_fcs_opponent_unranked_rather_than_dropping_it(self):
        raw = [fbs_game("Texas", 31, "Oklahoma", 24),
               {"homeTeam": "Alabama", "homePoints": 55,
                "awayTeam": "Mercer", "awayPoints": 3,
                "homeClassification": "fbs", "awayClassification": "fcs"}]
        rows, _ = fetch_games.extract_fbs_games(raw)
        self.assertEqual(len(rows), 2, "the FCS game must be kept")
        mercer = next(r for r in rows if r["away_team"] == "Mercer")
        self.assertEqual(mercer["home_ranked"], "true")
        self.assertEqual(mercer["away_ranked"], "false")

    def test_drops_games_with_no_fbs_side(self):
        raw = [{"homeTeam": "Mercer", "homePoints": 20,
                "awayTeam": "Furman", "awayPoints": 17,
                "homeClassification": "fcs", "awayClassification": "fcs"}]
        rows, skipped = fetch_games.extract_fbs_games(raw)
        self.assertEqual(rows, [])
        self.assertEqual(skipped["no_fbs_side"], 1)

    def test_drops_games_with_no_classification(self):
        """A missing classification is not evidence the team IS FBS, either.

        This reverses an earlier assumption.  CFBD labels the FBS and FCS
        teams it tracks, so the records with nothing in that field belong to
        opponents further down — NAIA and Division II schools appearing as the
        visitor on an FCS schedule.  Treating a blank as FBS put six of them
        into the 2025 rankings.

        The cost of the stricter rule is that a response with no
        classification fields at all yields no games rather than all of them.
        That is the safer failure: an empty output says plainly that something
        is wrong, where the old one quietly ranked whoever showed up.  main()
        detects it and says so.
        """
        raw = [{"homeTeam": "Texas", "homePoints": 31,
                "awayTeam": "Oklahoma", "awayPoints": 24}]
        rows, skipped = fetch_games.extract_fbs_games(raw)
        self.assertEqual(rows, [])
        self.assertEqual(skipped["unclassified"], 1)

    def test_drops_records_missing_team_names(self):
        raw = [{"homePoints": 31, "awayPoints": 24}]
        rows, skipped = fetch_games.extract_fbs_games(raw)
        self.assertEqual(rows, [])
        self.assertEqual(skipped["missing_fields"], 1)


class TestDuplicateHandling(unittest.TestCase):

    def setUp(self):
        self.rows, _ = fetch_games.extract_fbs_games([
            fbs_game("Texas", 31, "Oklahoma", 24, date="2025-10-11"),
            fbs_game("Ohio State", 28, "Michigan", 20, date="2025-11-29"),
            # Conference championship: same pair, home and away reversed.
            fbs_game("Oklahoma", 27, "Texas", 21, date="2025-12-06"),
        ])

    def test_finds_the_rematch_despite_reversed_sides(self):
        duplicates = fetch_games.find_duplicate_pairs(self.rows)
        self.assertEqual(list(duplicates), [("Oklahoma", "Texas")])
        self.assertEqual(len(duplicates[("Oklahoma", "Texas")]), 2)

    def test_no_false_positives(self):
        rows, _ = fetch_games.extract_fbs_games([
            fbs_game("Texas", 31, "Oklahoma", 24),
            fbs_game("Ohio State", 28, "Michigan", 20),
        ])
        self.assertEqual(fetch_games.find_duplicate_pairs(rows), {})

    def test_keep_first_keeps_the_october_meeting(self):
        kept = fetch_games.apply_duplicate_policy(self.rows, "keep_first")
        self.assertEqual(len(kept), 2)
        game = next(r for r in kept if "Texas" in (r["home_team"], r["away_team"]))
        self.assertEqual(game["date"], "2025-10-11")

    def test_keep_last_keeps_the_december_meeting(self):
        kept = fetch_games.apply_duplicate_policy(self.rows, "keep_last")
        self.assertEqual(len(kept), 2)
        game = next(r for r in kept if "Texas" in (r["home_team"], r["away_team"]))
        self.assertEqual(game["date"], "2025-12-06")

    def test_either_policy_leaves_data_the_ranker_accepts(self):
        for policy in ("keep_first", "keep_last"):
            kept = fetch_games.apply_duplicate_policy(self.rows, policy)
            ranker = FBSRoundRobinRanker()
            for row in kept:
                ranker.add_game(row["home_team"], row["away_team"],
                                row["home_score"], row["away_score"])
            self.assertEqual(len(ranker.rank()), 4)


class TestCsvHandoff(unittest.TestCase):
    """What the fetcher writes is what the ranker reads."""

    def _write_rows(self, raw):
        rows, _ = fetch_games.extract_fbs_games(raw)
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        fetch_games.write_csv(rows, fh.name)
        return fh.name

    def test_header_matches_what_the_ranker_expects(self):
        path = self._write_rows([fbs_game("Texas", 31, "Oklahoma", 24)])
        with open(path, newline="", encoding="utf-8") as fh:
            header = next(csv.reader(fh))
        self.assertEqual(header, ["date", "home_team", "home_score",
                                  "away_team", "away_score",
                                  "home_ranked", "away_ranked"])

    def test_ranker_loads_the_written_file(self):
        path = self._write_rows([
            fbs_game("Texas", 31, "Oklahoma", 24),
            fbs_game("Oklahoma", 28, "Baylor", 10),
            fbs_game("Texas", 35, "Baylor", 14),
        ])
        ranker = FBSRoundRobinRanker()
        ranker.load_csv(path)
        results = ranker.rank()
        self.assertEqual([r["team"] for r in results],
                         ["Texas", "Oklahoma", "Baylor"])

    def test_a_written_rematch_becomes_a_season_series(self):
        """Both meetings survive the hand-off and count as a 1-1 series."""
        path = self._write_rows([
            fbs_game("Texas", 31, "Oklahoma", 24, date="2025-10-11"),
            fbs_game("Oklahoma", 27, "Texas", 21, date="2025-12-06"),
        ])
        ranker = FBSRoundRobinRanker()
        ranker.load_csv(path)
        self.assertEqual(len(ranker.get_results("Texas", "Oklahoma")), 2)
        self.assertEqual(ranker._overall_record("Texas")[:2], (1, 1))

    def test_a_written_rematch_is_refused_under_the_strict_policy(self):
        path = self._write_rows([
            fbs_game("Texas", 31, "Oklahoma", 24, date="2025-10-11"),
            fbs_game("Oklahoma", 27, "Texas", 21, date="2025-12-06"),
        ])
        with self.assertRaises(DuplicateGameError):
            FBSRoundRobinRanker(on_duplicate="error").load_csv(path)


class TestUnrankedUpsets(unittest.TestCase):
    """Losses to an unranked opponent are found and surfaced."""

    def _fcs_game(self, fbs, fbs_pts, fcs, fcs_pts):
        return {"homeTeam": fbs, "homePoints": fbs_pts,
                "awayTeam": fcs, "awayPoints": fcs_pts,
                "homeClassification": "fbs", "awayClassification": "fcs",
                "startDate": "2025-09-06T16:00:00.000Z"}

    def test_finds_a_loss_to_an_unranked_opponent(self):
        rows, _ = fetch_games.extract_fbs_games([
            self._fcs_game("Alabama", 55, "Mercer", 3),      # routine win
            self._fcs_game("Army", 20, "Tarleton State", 27),  # the upset
        ])
        upsets = fetch_games.find_unranked_upsets(rows)
        self.assertEqual([(w, l) for _, w, l in upsets],
                         [("Tarleton State", "Army")])

    def test_an_all_ranked_game_is_never_an_upset(self):
        rows, _ = fetch_games.extract_fbs_games(
            [fbs_game("Oklahoma", 24, "Texas", 31)])
        self.assertEqual(fetch_games.find_unranked_upsets(rows), [])

    def test_the_upset_reaches_the_ranker_as_a_loss(self):
        rows, _ = fetch_games.extract_fbs_games([
            self._fcs_game("Army", 20, "Tarleton State", 27),
            fbs_game("Army", 30, "Navy", 10),
        ])
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        fetch_games.write_csv(rows, fh.name)
        ranker = FBSRoundRobinRanker()
        ranker.load_csv(fh.name)
        self.assertEqual(ranker._overall_record("Army")[:2], (1, 1))
        self.assertNotIn("Tarleton State", ranker.teams)
        self.assertNotIn("Tarleton State",
                         [r["team"] for r in ranker.rank()])


class TestApiLayer(unittest.TestCase):
    """fetch_games() is exercised against a stub; no network is used."""

    def _stub(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        response = mock.MagicMock()
        response.read.return_value = body
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return response

    def test_sends_the_key_as_a_bearer_token(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["auth"] = request.get_header("Authorization")
            return self._stub([fbs_game("Texas", 31, "Oklahoma", 24)])

        with mock.patch.object(fetch_games.urllib.request, "urlopen", fake_urlopen):
            games = fetch_games.fetch_games(2025, "regular", "SECRET")

        self.assertEqual(captured["auth"], "Bearer SECRET")
        self.assertIn("year=2025", captured["url"])
        self.assertIn("seasonType=regular", captured["url"])
        self.assertIn("division=fbs", captured["url"])
        self.assertEqual(len(games), 1)

    def test_a_rejected_key_exits_with_a_useful_message(self):
        def fake_urlopen(request, timeout=None):
            raise fetch_games.urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {}, None)

        with mock.patch.object(fetch_games.urllib.request, "urlopen", fake_urlopen):
            with self.assertRaises(SystemExit) as ctx:
                fetch_games.fetch_games(2025, "regular", "BAD")
        self.assertIn("401", str(ctx.exception))

    def test_default_writes_both_meetings_of_a_rematch(self):
        """The fetcher's default must match the ranker's: keep every meeting.

        Regression: the fetcher defaulted to "error" after the ranker moved to
        "combine", so the documented no-flag run refused to write anything and
        steered the user toward dropping a game.
        """
        raw = [fbs_game("Texas", 31, "Oklahoma", 24, date="2025-10-11"),
               fbs_game("Oklahoma", 27, "Texas", 21, date="2025-12-06")]
        out = os.path.join(tempfile.mkdtemp(), "games.csv")
        with mock.patch.object(fetch_games, "fetch_games", return_value=raw):
            code = fetch_games.main(["--year", "2025", "--out", out,
                                     "--api-key", "x"])
        self.assertEqual(code, 0)
        ranker = FBSRoundRobinRanker()
        ranker.load_csv(out)
        self.assertEqual(len(ranker.get_results("Texas", "Oklahoma")), 2)
        self.assertEqual(ranker._overall_record("Texas")[:2], (1, 1))

    def test_combine_policy_is_a_no_op_on_the_rows(self):
        rows, _ = fetch_games.extract_fbs_games([
            fbs_game("Texas", 31, "Oklahoma", 24, date="2025-10-11"),
            fbs_game("Oklahoma", 27, "Texas", 21, date="2025-12-06"),
        ])
        self.assertEqual(fetch_games.apply_duplicate_policy(rows, "combine"), rows)

    def test_error_policy_still_refuses_to_write(self):
        raw = [fbs_game("Texas", 31, "Oklahoma", 24, date="2025-10-11"),
               fbs_game("Oklahoma", 27, "Texas", 21, date="2025-12-06")]
        out = os.path.join(tempfile.mkdtemp(), "games.csv")
        with mock.patch.object(fetch_games, "fetch_games", return_value=raw):
            code = fetch_games.main(["--year", "2025", "--out", out,
                                     "--api-key", "x",
                                     "--on-duplicate", "error"])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(out), "no file should be written")

    def test_main_writes_when_a_policy_resolves_the_rematch(self):
        raw = [fbs_game("Texas", 31, "Oklahoma", 24, date="2025-10-11"),
               fbs_game("Oklahoma", 27, "Texas", 21, date="2025-12-06")]
        out = os.path.join(tempfile.mkdtemp(), "games.csv")
        with mock.patch.object(fetch_games, "fetch_games", return_value=raw):
            code = fetch_games.main(["--year", "2025", "--out", out,
                                     "--api-key", "x",
                                     "--on-duplicate", "keep_last"])
        self.assertEqual(code, 0)
        ranker = FBSRoundRobinRanker()
        ranker.load_csv(out)          # must not raise
        self.assertEqual(len(ranker._game_map), 1)


class TestClassificationMustBeExplicit(unittest.TestCase):
    """Only an explicit "fbs" counts as FBS.

    CFBD labels the FBS and FCS teams it tracks, so a blank classification
    belongs to something further down — an NAIA or Division II visitor on an
    FCS schedule.  Reading a blank as FBS put six such schools into the 2025
    rankings, each 1-0 with no group and no points.
    """

    FBS_GAME = {"homeTeam": "Indiana", "awayTeam": "Purdue",
                "homePoints": 30, "awayPoints": 10,
                "homeClassification": "fbs", "awayClassification": "fbs",
                "startDate": "2025-11-29"}
    FCS_GAME = {"homeTeam": "Army", "awayTeam": "Tarleton State",
                "homePoints": 20, "awayPoints": 27,
                "homeClassification": "fbs", "awayClassification": "fcs",
                "startDate": "2025-08-30"}
    NAIA_GAME = {"homeTeam": "North Dakota State", "awayTeam": "Dakota State",
                 "homePoints": 10, "awayPoints": 30,
                 "homeClassification": "fcs", "awayClassification": None,
                 "startDate": "2025-08-30"}

    def test_fbs_versus_fbs_is_kept(self):
        rows, _ = fetch_games.extract_fbs_games([self.FBS_GAME])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["home_ranked"], "true")
        self.assertEqual(rows[0]["away_ranked"], "true")

    def test_fbs_versus_fcs_keeps_the_game_and_marks_the_fcs_side(self):
        rows, _ = fetch_games.extract_fbs_games([self.FCS_GAME])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["home_ranked"], "true")
        self.assertEqual(rows[0]["away_ranked"], "false")

    def test_an_unclassified_team_is_not_treated_as_fbs(self):
        rows, skipped = fetch_games.extract_fbs_games([self.NAIA_GAME])
        self.assertEqual(rows, [])
        self.assertEqual(skipped["unclassified"], 1)

    def test_both_sides_unclassified_is_dropped(self):
        game = dict(self.NAIA_GAME, homeClassification=None)
        rows, skipped = fetch_games.extract_fbs_games([game])
        self.assertEqual(rows, [])
        self.assertEqual(skipped["unclassified"], 1)

    def test_an_explicit_non_fbs_pair_is_counted_separately(self):
        game = dict(self.NAIA_GAME, awayClassification="ii")
        rows, skipped = fetch_games.extract_fbs_games([game])
        self.assertEqual(rows, [])
        self.assertEqual(skipped["no_fbs_side"], 1)
        self.assertEqual(skipped["unclassified"], 0)

    def test_classification_matching_ignores_case_and_padding(self):
        game = dict(self.FBS_GAME, homeClassification=" FBS ")
        rows, _ = fetch_games.extract_fbs_games([game])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["home_ranked"], "true")

    def test_a_mixed_season_keeps_only_what_it_should(self):
        rows, skipped = fetch_games.extract_fbs_games(
            [self.FBS_GAME, self.FCS_GAME, self.NAIA_GAME])
        self.assertEqual([r["home_team"] for r in rows], ["Indiana", "Army"])
        self.assertEqual(skipped["unclassified"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
