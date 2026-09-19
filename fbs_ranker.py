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
  3. For every pair of teams, determine their relative order from the cliques
     they share, taking those cliques BY SIZE, largest size first:
       a. Compare win percentage within a shared clique (descending).
       b. Still tied → compare cumulative point differential within it.
       c. Reconcile the cliques of that size: if they agree (or only one had
          an opinion) that is the verdict; if they disagree, or none could
          separate the teams, the size decides nothing and the next-smaller
          size is tried.  A smaller clique can only speak where every larger
          one stayed silent; it never reverses a larger clique's ordering.
       d. No shared clique → compare by overall (all-games) win percentage
          regressed toward .500 (see _shrunk_win_pct), then overall point
          differential.
       e. Still nothing → the teams are tied, and no ordering is invented
          from their names.
     Each comparison records its STRENGTH: the size of the group that decided
     it (0 for the overall-record fallback in step d).
  4. Combine the pairwise results into one global order using Tideman's
     ranked-pairs procedure: sort every comparison strongest-first, then lock
     each one in unless it contradicts what the already-locked (stronger)
     comparisons imply.  When pairwise results conflict, the ordering made
     inside the largest group therefore wins, and the result is acyclic by
     construction.
  5. A team's rank is one plus the number of teams that outrank it, so teams
     nothing separates share a rank and the next rank skips (1, 2, 2, 4).
  6. Each team's displayed record is their win-loss-PF-PA within their
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
from itertools import groupby

import networkx as nx


#: Values in a home_ranked / away_ranked column that mean "do not rank this team".
_UNRANKED_VALUES = frozenset({"false", "0", "no", "n", "unranked"})


def _is_ranked(value) -> bool:
    """Interpret a home_ranked / away_ranked cell.  Missing or blank = ranked."""
    if value is None:
        return True
    text = str(value).strip().lower()
    return text not in _UNRANKED_VALUES if text else True


class DuplicateGameError(ValueError):
    """Raised when a dataset contains two games between the same pair of teams.

    The ranker stores one result per pair, so a second meeting would overwrite
    the first and make the rankings depend on input order.  Rather than decide
    silently which game counts, loading stops and asks.

    Real seasons do hit this: a conference championship game is often a rematch
    of a regular-season meeting.
    """

class FBSRoundRobinRanker:
    """
    Discovers round-robin groups among FBS teams and produces an ordered ranking.

    Attributes:
        teams (set): All team names seen in the loaded game data.
        on_duplicate (str): What to do when the same pair of teams appears
            more than once — "combine" (default), "error", "keep_first", or
            "keep_last".
        unranked_opponents (set): Names of opponents that were marked unranked
            and so are excluded from the rankings.

    Class attributes:
        PRIOR_GAMES: Phantom .500 games added to each team's overall record
            before comparing two teams that share no round-robin group.
            See _shrunk_win_pct.  Set to 0 to compare on raw overall record.
        MIN_DIFFERING_RESULTS: How many shared opponents must have given the
            two teams different results before the common-opponent tier
            (strength 1) issues a verdict.  Set to 0 to disable that tier.
        BLEND_WEIGHTS: (own, opponents', opponents' opponents') weights for
            the strength-0 metric.  (1, 0, 0) is the plain shrunk record.
    """

    # Four is enough to stop a 2-0 record outranking a 7-1 one, and small
    # enough to leave full-season records essentially untouched.  The ordering
    # it produces is stable for anything from roughly 2 to 10.
    PRIOR_GAMES = 4.0

    # How many common opponents must have given DIFFERENT results before the
    # common-opponent tier speaks.  Two teams sharing five opponents but
    # getting the same result against four of them have one game of evidence,
    # not five, so this counts what actually discriminates rather than what is
    # merely shared.  Set to 0 to switch the tier off.
    MIN_DIFFERING_RESULTS = 2

    # Weights for the strength-0 metric, as (own record, opponents' records,
    # opponents' opponents' records); they should sum to 1.  (1, 0, 0) is the
    # plain shrunk record.
    #
    # The default gives opponents a quarter weight.  Nominal weight overstates
    # a term's influence, because the three metrics do not vary equally: across
    # the 2025 field own record spans .133-.875 while opponents' records span
    # only .383-.617, each averaging layer pulling harder toward .500.  Weight
    # times spread is what actually decides, and at (.75, .25, 0) own record
    # outweighs opponents about 11 to 1.  An even split makes it 3.7 to 1,
    # which moves 94 of 136 teams and drops App State 22 places on the strength
    # of three bad opponents.
    BLEND_WEIGHTS = (0.75, 0.25, 0.0)

    #: What to do when a pair of teams meets more than once.
    #: "combine" (default) counts every meeting, so a split season series is a
    #: 1-1 record with points from both games; "error" refuses the dataset;
    #: "keep_first" ignores later meetings; "keep_last" keeps only the latest.
    DUPLICATE_POLICIES = ("combine", "error", "keep_first", "keep_last")

    def __init__(self, on_duplicate: str = "combine"):
        if on_duplicate not in self.DUPLICATE_POLICIES:
            raise ValueError(
                f"on_duplicate must be one of {self.DUPLICATE_POLICIES}, "
                f"got {on_duplicate!r}"
            )
        self.on_duplicate = on_duplicate
        self.teams: set = set()
        # Games against opponents that are not themselves ranked — an FCS team
        # on an FBS schedule, say.  team -> [(opponent, points_for, points_against)]
        # They count toward the ranked team's record but never become nodes in
        # the game graph, never form a group, and never appear in the output.
        self._unranked_games: dict = {}
        # Cache for the derived opponent metrics; cleared whenever a game is
        # added, rebuilt on first use.
        self._metrics_cache = None
        #: Names of every unranked opponent seen, for reporting.
        self.unranked_opponents: set = set()
        #: Opponents some rows marked ranked and others unranked, which
        #: load_csv resolved as unranked.  Usually a sign that the source
        #: data left a classification field blank on some games.
        self.demoted_opponents: set = set()
        # Canonical key: (team_a, team_b) with team_a < team_b (lexicographic).
        # Value: list of (score_for_team_a, score_for_team_b), in the order the
        # games were added.  A pair that met twice — a conference championship
        # rematch, say — keeps both meetings, so a split series counts as 1-1.
        self._game_map: dict = {}

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def load_csv(self, filepath: str) -> None:
        """Load game results from a CSV file.

        Required columns: home_team, home_score, away_team, away_score

        Optional columns home_ranked / away_ranked mark an opponent that should
        not be ranked (see add_game).  Accepted values are false/0/no/n for
        unranked; anything else, including a missing column, means ranked.

        A team marked unranked in ANY row is treated as unranked in EVERY row.
        The two markings are not equally trustworthy: "unranked" is positive
        evidence that a team is outside the ranked division, while "ranked" is
        also what a missing or absent value defaults to.  Taking each row at
        face value lets one row with a blank classification field promote an
        FCS team into the rankings on a fraction of its schedule -- which puts
        it near the top, since the games it lost are the ones that named it
        correctly.  Teams corrected this way are listed in
        `demoted_opponents`.

        Raises:
            DuplicateGameError: if two rows describe the same pair of teams and
                on_duplicate is "error".  The message names the offending line
                so it can be found in the file.
        """
        with open(filepath, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = [(reader.line_num, row) for row in reader]

        # First pass: anyone ever marked unranked is unranked throughout.
        unranked = set()
        marked_ranked = set()
        for _, row in rows:
            for side in ("home", "away"):
                team = row[f"{side}_team"].strip()
                if _is_ranked(row.get(f"{side}_ranked")):
                    marked_ranked.add(team)
                else:
                    unranked.add(team)
        self.demoted_opponents |= (unranked & marked_ranked)

        # Second pass: load the games with that correction applied.
        for line_num, row in rows:
            home = row["home_team"].strip()
            away = row["away_team"].strip()
            home_score = int(row["home_score"])
            away_score = int(row["away_score"])
            try:
                self.add_game(home, away, home_score, away_score,
                              home_ranked=home not in unranked,
                              away_ranked=away not in unranked)
            except DuplicateGameError as exc:
                raise DuplicateGameError(
                    f"{filepath} line {line_num}: {exc}"
                ) from None

    def add_game(self, home: str, away: str, home_score: int, away_score: int,
                 home_ranked: bool = True, away_ranked: bool = True) -> None:
        """Add a single game result programmatically.

        Set home_ranked or away_ranked to False for an opponent that should not
        be ranked — an FCS team on an FBS schedule, for instance.  That game
        still counts toward the ranked team's record and points, which is what
        makes an upset loss visible, but the unranked team never becomes a node
        in the game graph, never forms a round-robin group, and never appears
        in the rankings.

        An unranked opponent played by only one ranked team could not help
        compare two ranked teams anyway, so keeping it out of the graph costs
        nothing; and letting it in on a single game would place it on almost no
        evidence, which measurably distorts everything below it.

        A game between two unranked teams is ignored entirely.
        """
        if home_ranked and away_ranked:
            self._add_game(home, away, home_score, away_score)
        elif home_ranked:
            self._add_unranked_game(home, away, home_score, away_score)
        elif away_ranked:
            self._add_unranked_game(away, home, away_score, home_score)
        # neither ranked: nothing to record

    def _add_unranked_game(self, team: str, opponent: str,
                           points_for: int, points_against: int) -> None:
        self.teams.add(team)
        self.unranked_opponents.add(opponent)
        self._unranked_games.setdefault(team, []).append(
            (opponent, points_for, points_against))
        self._metrics_cache = None

    def get_unranked_results(self, team: str) -> list:
        """Games this team played against unranked opponents.

        Returns a list of (opponent, points_for, points_against).
        """
        return list(self._unranked_games.get(team, ()))

    def _add_game(self, team_a: str, team_b: str, score_a: int, score_b: int) -> None:
        # Canonicalise so A-vs-B and B-vs-A land on the same key.
        if team_a < team_b:
            key, value = (team_a, team_b), (score_a, score_b)
        else:
            key, value = (team_b, team_a), (score_b, score_a)

        if key in self._game_map:
            if self.on_duplicate == "keep_first":
                return
            if self.on_duplicate == "error":
                prev_a, prev_b = self._game_map[key][0]
                raise DuplicateGameError(
                    f"{key[0]} and {key[1]} appear twice in this dataset "
                    f"({key[0]} {prev_a}-{prev_b} {key[1]}, then "
                    f"{key[0]} {value[0]}-{value[1]} {key[1]}). "
                    f"This ranker was constructed with on_duplicate='error', "
                    f"so it refuses datasets containing a rematch. Use the "
                    f"default 'combine' to count both meetings as a season "
                    f"series, or 'keep_first' / 'keep_last' to pick one."
                )
            if self.on_duplicate == "keep_last":
                self._game_map[key] = [value]
                return
            # "combine" falls through and appends the extra meeting.

        self.teams.add(team_a)
        self.teams.add(team_b)
        self._game_map.setdefault(key, []).append(value)
        self._metrics_cache = None

    # ------------------------------------------------------------------
    # Game result helpers
    # ------------------------------------------------------------------

    def get_results(self, team_a: str, team_b: str) -> list:
        """Every meeting between two teams, as (score_a, score_b) from team_a's
        point of view, in the order the games were added.

        Returns an empty list if the two never played.
        """
        if team_a < team_b:
            return list(self._game_map.get((team_a, team_b), ()))
        return [(b, a) for a, b in self._game_map.get((team_b, team_a), ())]

    def get_result(self, team_a: str, team_b: str):
        """The FIRST meeting between two teams, or None if they never played.

        A convenience for the common case of a single meeting.  Use
        get_results when a pair may have met more than once.
        """
        results = self.get_results(team_a, team_b)
        return results[0] if results else None

    def total_games(self) -> int:
        """Games loaded: every meeting of a repeated pair, plus games against
        unranked opponents."""
        return (sum(len(m) for m in self._game_map.values())
                + sum(len(g) for g in self._unranked_games.values()))

    def repeat_meetings(self) -> int:
        """Extra games beyond one per pair — the size of the season series.

        Counted across ranked pairs AND games against unranked opponents, so
        it cannot be derived by subtracting the ranked-pair count from the
        total game count: those two do not cover the same games.
        """
        ranked = (sum(len(m) for m in self._game_map.values())
                  - len(self._game_map))
        unranked = 0
        for games in self._unranked_games.values():
            opponents = [opponent for opponent, _, _ in games]
            unranked += len(opponents) - len(set(opponents))
        return ranked + unranked

    def _record_in_group(self, team: str, group) -> tuple:
        """Return (wins, losses, points_for, points_against) for *team* vs every
        other member of *group*."""
        wins = losses = pf = pa = 0
        for opp in group:
            if opp == team:
                continue
            # Every meeting counts: a split series against a group opponent is
            # one win and one loss, with points from both games.
            for st, so in self.get_results(team, opp):
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

    def _shrunk_win_pct(self, wins: int, losses: int) -> float:
        """Overall win percentage regressed toward .500 by PRIOR_GAMES games.

        Raw win percentage ranks a 2-0 team above a 7-1 team, which only holds
        up if two games say as much as eight.  Adding a fixed number of
        phantom .500 games corrects that without a hard cutoff: the phantom
        games are a large share of a short schedule and a small share of a
        long one, so a team with little evidence is pulled toward .500 while a
        team with plenty barely moves.

            2-0  ->  (2 + 2) / (2 + 4)  =  .667
            7-1  ->  (7 + 2) / (8 + 4)  =  .750

        This is used ONLY for the strength-0 fallback, where the two teams
        share no round-robin group and may have played schedules of wildly
        different lengths.  Comparisons inside a group are deliberately left
        on raw win percentage: every member of a group played every other
        member, so those records are already directly comparable.

        Setting PRIOR_GAMES to 0 makes this identical to _win_pct.
        """
        k = float(self.PRIOR_GAMES)
        total = wins + losses
        if total + k <= 0:
            return 0.0
        return (wins + k / 2.0) / (total + k)

    def _overall_record(self, team: str) -> tuple:
        """Return (wins, losses, points_for, points_against) across all games."""
        w = l = pf = pa = 0
        for (ta, tb), meetings in self._game_map.items():
            if team not in (ta, tb):
                continue
            for sa, sb in meetings:
                st, so = (sa, sb) if ta == team else (sb, sa)
                pf += st; pa += so
                if st > so: w += 1
                else: l += 1

        # Games against unranked opponents count here and nowhere else.
        for _opponent, st, so in self._unranked_games.get(team, ()):
            pf += st; pa += so
            if st > so: w += 1
            else: l += 1
        return w, l, pf, pa

    def _verdict_in_group(self, team_a: str, team_b: str, group):
        """One group's opinion on two of its members.

        Returns ``(cmp, win_pct_gap, point_diff_gap)`` — cmp is -1 if team_a
        ranks higher inside this group, 1 if team_b does — or None if the
        group cannot separate them at all.
        """
        wa, la, pfa, paa = self._record_in_group(team_a, group)
        wb, lb, pfb, pab = self._record_in_group(team_b, group)

        wpc_a = self._win_pct(wa, la)
        wpc_b = self._win_pct(wb, lb)
        diff_a, diff_b = pfa - paa, pfb - pab
        wpc_gap, diff_gap = abs(wpc_a - wpc_b), abs(diff_a - diff_b)

        if wpc_gap > 1e-9:
            return (-1 if wpc_a > wpc_b else 1), wpc_gap, diff_gap

        # Tied on win pct: compare point differential within this group.
        # (Using point diff avoids the non-transitivity that raw h2h creates
        # in 3-way cycles; for 2-team groups win pct already encodes h2h.)
        if diff_a != diff_b:
            return (-1 if diff_a > diff_b else 1), 0.0, diff_gap

        return None

    # ------------------------------------------------------------------
    # Common opponents (strength 1)
    # ------------------------------------------------------------------

    def opponents(self, team: str) -> dict:
        """Every opponent this team faced, mapped to (wins, losses) against it.

        Includes unranked opponents: a non-FBS team both sides played is still
        a shared result.
        """
        out: dict = {}
        for other in self.teams:
            if other == team:
                continue
            for pf, pa in self.get_results(team, other):
                w, l = out.get(other, (0, 0))
                out[other] = (w + 1, l) if pf > pa else (w, l + 1)
        for other, pf, pa in self._unranked_games.get(team, ()):
            w, l = out.get(other, (0, 0))
            out[other] = (w + 1, l) if pf > pa else (w, l + 1)
        return out

    def teams_without_ranked_opponents(self) -> set:
        """Ranked teams whose entire schedule was unranked opponents.

        Such a team is in the rankings but connected to nothing in them: it can
        share no round-robin group, and its position rests on however few games
        it played.  In real data this almost always means the input is wrong —
        a team that does not belong in the ranked pool was marked as if it did.
        """
        played = set()
        for team_a, team_b in self._game_map:
            played.add(team_a)
            played.add(team_b)
        return self.teams - played

    def _record_against(self, team: str, opponent_names) -> tuple:
        """(wins, losses, points_for, points_against) versus a set of opponents.

        Unlike _record_in_group this counts unranked opponents too, so a shared
        non-FBS opponent is not silently skipped.
        """
        w = l = pf = pa = 0
        for other in opponent_names:
            if other == team:
                continue
            results = self.get_results(team, other)
            results += [(f, a) for o, f, a in self._unranked_games.get(team, ())
                        if o == other]
            for st, so in results:
                pf += st; pa += so
                if st > so: w += 1
                else: l += 1
        return w, l, pf, pa

    def common_opponents(self, team_a: str, team_b: str):
        """Return (shared, differing) opponent-name sets for two teams.

        *differing* holds the shared opponents the two teams did not fare
        identically against — the only ones carrying any comparative
        information.
        """
        opp_a, opp_b = self.opponents(team_a), self.opponents(team_b)
        shared = set(opp_a) & set(opp_b)
        differing = {o for o in shared
                     if (opp_a[o][0] > opp_a[o][1]) != (opp_b[o][0] > opp_b[o][1])}
        return shared, differing

    def _common_opponent_verdict(self, team_a: str, team_b: str):
        """Compare two teams on their records against shared opponents.

        Returns (cmp, win_pct_gap, point_diff_gap), or None when the teams
        share too few DIFFERING results (see MIN_DIFFERING_RESULTS) or their
        shared records are level.
        """
        if not self.MIN_DIFFERING_RESULTS:
            return None
        shared, differing = self.common_opponents(team_a, team_b)
        if len(differing) < self.MIN_DIFFERING_RESULTS:
            return None

        wa, la, pfa, paa = self._record_against(team_a, shared)
        wb, lb, pfb, pab = self._record_against(team_b, shared)
        if not (wa + la) or not (wb + lb):
            return None
        pct_a, pct_b = self._win_pct(wa, la), self._win_pct(wb, lb)
        if abs(pct_a - pct_b) <= 1e-9:
            return None
        return ((-1 if pct_a > pct_b else 1), abs(pct_a - pct_b),
                abs((pfa - paa) - (pfb - pab)))

    # ------------------------------------------------------------------
    # Opponent-weighted metric (strength 0)
    # ------------------------------------------------------------------

    def _metrics(self) -> dict:
        """Per-team shrunk win pct, opponents' average, and theirs in turn.

        An opponent's record is computed with the games against the team in
        question removed: otherwise beating an opponent lowers their record and
        so penalises the team that beat them.  Unranked opponents are left out
        of these averages entirely, since the data says nothing about how they
        fared against anyone else.
        """
        # Keyed on PRIOR_GAMES so that changing the shrinkage on an existing
        # ranker recomputes rather than returning stale numbers.
        if (self._metrics_cache is not None
                and self._metrics_cache[0] == self.PRIOR_GAMES):
            return self._metrics_cache[1]

        opponents = {t: self.opponents(t) for t in self.teams}

        def record_excluding(team, excluded):
            w = l = 0
            for other, (ow, ol) in opponents[team].items():
                if other == excluded:
                    continue
                w += ow; l += ol
            return w, l

        win_pct = {t: self._shrunk_win_pct(*record_excluding(t, None))
                   for t in self.teams}
        opp_pct = {}
        for team in self.teams:
            vals = [self._shrunk_win_pct(*record_excluding(o, team))
                    for o in opponents[team] if o in self.teams]
            opp_pct[team] = sum(vals) / len(vals) if vals else 0.5
        opp_opp_pct = {}
        for team in self.teams:
            vals = [opp_pct[o] for o in opponents[team] if o in self.teams]
            opp_opp_pct[team] = sum(vals) / len(vals) if vals else 0.5

        self._metrics_cache = (self.PRIOR_GAMES,
                               {"wp": win_pct, "owp": opp_pct,
                                "oowp": opp_opp_pct})
        return self._metrics_cache[1]

    def blended_score(self, team: str) -> float:
        """The strength-0 metric: own record blended with opponent quality."""
        a, b, c = self.BLEND_WEIGHTS
        m = self._metrics()
        return a * m["wp"][team] + b * m["owp"][team] + c * m["oowp"][team]

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
          1. Find every clique containing BOTH teams (shared cliques) and
             bucket them BY SIZE, largest size first.
          2. For each size, ask every clique of that size for its verdict:
               - They agree, or only one has an opinion → that is the answer.
               - They disagree → equally strong groups contradict each other,
                 so neither wins; drop to the next-smaller size.
               - None can separate the teams → drop to the next-smaller size.
             A smaller group can therefore only speak where every larger one
             stayed silent; it can never reverse a larger group's ordering.
          3. If no shared clique (or no size produced an agreed verdict),
             fall back to overall win pct regressed toward .500 by
             PRIOR_GAMES phantom games, then overall point diff.  This
             fallback reports group_size 0 — the weakest strength there is —
             so any chain of group-based orderings overrides it.
          4. If even that cannot separate them, return 0.  The two teams are
             genuinely tied and rank() gives them the same rank; nothing is
             invented to break the tie.
        """
        # All cliques containing both teams, largest first.
        shared = sorted(
            [c for c in all_cliques if team_a in c and team_b in c],
            key=lambda c: -len(c),
        )

        for size, group_of_cliques in groupby(shared, key=len):
            verdicts = [v for v in
                        (self._verdict_in_group(team_a, team_b, c)
                         for c in group_of_cliques)
                        if v is not None]
            if not verdicts:
                continue          # no clique this size can separate them

            directions = {v[0] for v in verdicts}
            if len(directions) > 1:
                # Two groups of identical size reach opposite conclusions.
                # Equally strong evidence pointing both ways is not evidence,
                # so this size decides nothing and we drop to the next one.
                continue

            cmp = directions.pop()
            strength = (size,
                        max(v[1] for v in verdicts),
                        max(v[2] for v in verdicts))
            return cmp, strength

        # No shared clique.  Next best evidence is a shared opponent: strength
        # 1 sits below every group (playing someone beats sharing an opponent
        # with them) and above the record-based metric.
        verdict = self._common_opponent_verdict(team_a, team_b)
        if verdict is not None:
            cmp, pct_gap, diff_gap = verdict
            return cmp, (1, pct_gap, diff_gap)

        # Nothing connects them.  Fall back to the blended metric at strength
        # 0, the weakest evidence there is, so ranked pairs discards it first
        # whenever it conflicts with anything above.
        _, _, opf_a, opa_a = self._overall_record(team_a)
        _, _, opf_b, opa_b = self._overall_record(team_b)

        wpc_a = self.blended_score(team_a)
        wpc_b = self.blended_score(team_b)
        diff_a = opf_a - opa_a
        diff_b = opf_b - opa_b

        if abs(wpc_a - wpc_b) > 1e-9:
            strength = (0, abs(wpc_a - wpc_b), abs(diff_a - diff_b))
            return (-1 if wpc_a > wpc_b else 1), strength

        if diff_a != diff_b:
            return (-1 if diff_a > diff_b else 1), (0, 0.0, abs(diff_a - diff_b))

        # Nothing distinguishes them.  Report a tie rather than inventing an
        # ordering from the team names; rank() will give them the same rank.
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
                rank          - integer position (1 = best).  Teams that
                                nothing separates share a rank, and the next
                                rank skips accordingly (1, 2, 2, 4, ...).
                team          - team name string
                overall_wins  - wins across every game in the loaded data,
                                including games against unranked opponents
                overall_losses- losses on the same basis
                overall_win_pct - those wins / those games, rounded to 3 dp
                                (this is the record within the dataset; it
                                matches a published record only if the data
                                covers every game the team played)
                group_size    - size of the team's largest (primary) clique
                wins          - wins inside that primary clique only
                losses        - losses inside that primary clique only
                win_pct       - in-clique wins / games, rounded to 3 dp
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

        for _strength, winner, loser in candidates:
            if desc[loser] & bit[winner]:
                # Stronger verdicts already put loser above winner.  The larger
                # group's ordering wins, so this weaker verdict is discarded.
                continue
            if desc[winner] & bit[loser]:
                continue                  # already implied transitively
            above, below = anc[winner], desc[loser]
            for t in set_bits(above):
                desc[t] |= below
            for t in set_bits(below):
                anc[t] |= above

        # ---- Turn the locked ordering into rank numbers ----
        # anc[t] holds every team that outranks t, plus t itself, so its bit
        # count is exactly standard competition ranking:
        #
        #     rank = 1 + (number of teams that rank strictly above t)
        #
        # Teams share a rank only when neither outranks the other.  An ordered
        # pair can never share one: if w outranks l then anc[w] is a strict
        # subset of anc[l], so rank(w) < rank(l).  That also makes sorting by
        # rank a valid ordering — it never contradicts a locked verdict.
        #
        # Ranks repeat and then skip, the way sports standings do: two teams
        # tied for 3rd are both 3rd and the next team is 5th.  Teams sharing a
        # rank are listed alphabetically for stable output; it is the equal
        # rank NUMBER, not the listing order, that reports the tie.
        team_rank = {t: bin(anc[t]).count("1") for t in teams_list}
        sorted_teams = sorted(teams_list, key=lambda t: (team_rank[t], t))

        # ---- Build output records ----
        results = []
        for team in sorted_teams:
            primary = team_primary[team]
            w, l, pf, pa = self._record_in_group(team, primary)
            ow, ol, _, _ = self._overall_record(team)
            results.append({
                "rank": team_rank[team],
                "team": team,
                "overall_wins": ow,
                "overall_losses": ol,
                "overall_win_pct": round(self._win_pct(ow, ol), 3),
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
    print("=" * 96)
    print("  FBS ROUND-ROBIN RANKINGS")
    print("  Overall = record across every game in this dataset, including "
          "games against unranked opponents.")
    print("  In-Grp / Grp% / PF / PA / Diff cover games inside the team's "
          "round-robin group only.")
    print("=" * 96)
    hdr = (f"  {'Rank':<5} {'Team':<22} {'Overall':<8} {'Grp':<4} "
           f"{'In-Grp':<7} {'Grp%':<7} {'PF':<6} {'PA':<6} {'Diff'}")
    print(hdr)
    sep = "-" * 96

    # A rank held by more than one team is shown as "T3", the way standings
    # mark a tie.  The equal rank number is the tie; listing order is not.
    shared_ranks = {r["rank"] for r in results
                    if sum(1 for x in results if x["rank"] == r["rank"]) > 1}

    prev_size = None
    for r in results:
        if prev_size is not None and r["group_size"] != prev_size:
            print()   # blank line between tiers
        print(sep)
        overall = f"{r['overall_wins']}-{r['overall_losses']}"
        wl = f"{r['wins']}-{r['losses']}"
        diff = f"{r['point_diff']:+d}"
        label = f"T{r['rank']}" if r["rank"] in shared_ranks else str(r["rank"])
        print(
            f"  {label:<5} {r['team']:<22} {overall:<8} {r['group_size']:<4} "
            f"{wl:<7} {r['win_pct']:<7.3f} {r['points_for']:<6} "
            f"{r['points_against']:<6} {diff}"
        )
        prev_size = r["group_size"]

    print("=" * 96)
    print()


def save_csv(results: list, filepath: str) -> None:
    """Save rankings to a CSV file."""
    fieldnames = [
        "rank", "team", "overall_wins", "overall_losses", "overall_win_pct",
        "group_size", "wins", "losses", "win_pct",
        "points_for", "points_against", "point_diff",
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
        try:
            ranker.load_csv(args.input)
        except DuplicateGameError as exc:
            print(f"\nError: {exc}", file=sys.stderr)
            sys.exit(1)

    games = ranker.total_games()
    repeats = ranker.repeat_meetings()
    extra = f"  |  Repeat meetings: {repeats}" if repeats else ""
    print(f"Teams: {len(ranker.teams)}  |  Games: {games}{extra}")

    if ranker.demoted_opponents:
        names = ", ".join(sorted(ranker.demoted_opponents))
        print(f"\nNote: {len(ranker.demoted_opponents)} opponent(s) were marked "
              f"unranked on some games and ranked on others. Treating them as "
              f"unranked throughout, since a blank classification field also "
              f"reads as ranked:\n  {names}")

    stranded = ranker.teams_without_ranked_opponents()
    if stranded:
        names = ", ".join(sorted(stranded))
        print(f"\nNote: {len(stranded)} ranked team(s) played no ranked "
              f"opponent at all, so nothing in the rankings is connected to "
              f"them:\n  {names}\n"
              f"  If these do not belong in the rankings, the input marked "
              f"them as if they did.")

    results = ranker.rank()
    print_rankings(results)

    if args.output:
        save_csv(results, args.output)


if __name__ == "__main__":
    main()
