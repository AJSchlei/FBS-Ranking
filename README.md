# FBS Round-Robin Ranking Tool

[![Tests](https://github.com/AJSchlei/FBS-Ranking/actions/workflows/tests.yml/badge.svg)](https://github.com/AJSchlei/FBS-Ranking/actions/workflows/tests.yml)

Ranks FBS football teams by discovering **round-robin groups** — sets of teams
where every team played every other team — and using those groups as the
evidence for every head-to-head judgement.  Two teams are compared inside the
largest group they share; when those judgements conflict with each other, the
one made inside the larger group wins.

---

## Algorithm

```
1. Build an undirected game graph
   - Node  = team
   - Edge  = at least one game was played between the two teams

2. Find every maximal clique (Bron-Kerbosch via NetworkX)
   A maximal clique is the largest round-robin group that cannot be
   extended by adding another team.

3. Compare every pair of teams, and record how strong the comparison is

   For teams A and B, walk their shared cliques from largest to smallest:

   a. Compare win percentage within that shared clique (descending).
   b. Still tied -> compare point differential within that clique.
   c. Still tied -> move on to the next-smaller shared clique.
      A smaller shared clique can only break a tie left by a larger one;
      it can never reverse an order the larger clique established.
   d. No shared clique at all -> compare overall (all-games) win
      percentage REGRESSED TOWARD .500, then overall point differential,
      then team name.  See "Comparing teams with no shared group".

   Each verdict carries a STRENGTH: the size of the group that decided it.
   Step (d) has strength 0 — the weakest evidence there is.

4. Combine the verdicts into one global order (Tideman's ranked pairs)

   a. Sort every verdict strongest-first: largest deciding group, then the
      most decisive margin inside that group.
   b. Lock each verdict in one at a time, SKIPPING any verdict that
      contradicts what the already-locked verdicts imply.

   Because stronger verdicts are locked first, a conflict is always
   resolved in favour of the larger group — and the result is acyclic by
   construction, so no separate cycle-breaking step is needed.

5. Read the final ranking off the locked ordering.
   Teams left mutually unordered (only possible on an exact tie) fall
   back to alphabetical order.
```

---

## Why ranked pairs

Pairwise comparisons alone do not give a ranking: they can disagree with one
another.  A can beat out B inside a 6-team conference, B can beat out C inside
a 4-team conference, and C can still look better than A on raw overall record.
Something has to decide which of those three statements to throw away.

This tool throws away the weakest one — the verdict resting on the smallest
group.  That is exactly [Tideman's ranked-pairs
method](https://en.wikipedia.org/wiki/Ranked_pairs), a Condorcet method that
sorts pairwise results by strength and locks them in until a cycle would form.

The practical effect: **group evidence beats record-only evidence.**  A team
cannot climb over an opponent that a real round-robin group placed above it
just by padding its win percentage against teams nobody else played.

---

## Tie-breaking detail

Within a single shared group:

| Level | Rule |
|-------|------|
| 1 | Win percentage within the shared round-robin group |
| 2 | Point differential within that group |
| 3 | Repeat levels 1-2 in the next-smaller shared group |
| 4 | Shrunk overall win percentage, then overall point differential (strength 0) |
| 5 | Alphabetical (deterministic fallback) |

Head-to-head is not a separate level: inside a 2-team group, win percentage
already *is* the head-to-head result, and in larger groups raw head-to-head is
deliberately avoided because it is non-transitive.

**Cyclic h2h example** — If A beat B, B beat C, and C beat A inside one group,
all three sit at the same win percentage, and level 2 (point differential)
separates them.

---

## Comparing teams with no shared group

When two teams never played and belong to no common round-robin group, there
is no group evidence to go on and the tool falls back to their overall
records.  Raw win percentage is a poor judge here, because it ignores how many
games each record covers -- it ranks a 2-0 team above a 7-1 team.

So the fallback adds `PRIOR_GAMES` phantom games, split evenly as wins and
losses, to every team before comparing:

```
shrunk win pct = (wins + PRIOR_GAMES/2) / (games + PRIOR_GAMES)
```

With the default of 4:

| Record | Raw | Shrunk | Moved by |
|--------|-----|--------|----------|
| 2-0 | 1.000 | .667 | -.333 |
| 8-0 | 1.000 | .833 | -.167 |
| 7-1 | .875 | .750 | -.125 |
| 3-3 | .500 | .500 | 0 |

The phantom games are a large share of a short schedule and a small share of a
long one, so a thin record is pulled hard toward .500 while a full season
barely moves -- no cutoff, no cliff.  An even record is unmoved at any length.

Two deliberate limits on this:

- **It applies only to the strength-0 fallback.**  Inside a round-robin group
  every team played every other team, so those records are already comparable
  and are compared on raw win percentage.
- **It is a comparison device, not a reported statistic.**  The `win_pct`
  field in the output remains the team's real record within its group.

`PRIOR_GAMES` is a class attribute on `FBSRoundRobinRanker`, so it can be
changed (`ranker.PRIOR_GAMES = 6`) or switched off entirely by setting it to
0, which restores raw overall win percentage.  The ordering it produces is
stable anywhere from roughly 2 to 10.

---

## Conflicts between groups

A worked example, as covered by `TestConflictingVerdicts`:

| Verdict | Decided in | Strength |
|---------|-----------|----------|
| A over B | 4-team group `{A, B, X1, X2}` | 4 |
| B over C | 3-team group `{B, C, Y1}` | 3 |
| C over A | no shared group — overall record only | 0 |

Those three verdicts form a cycle.  Ranked pairs locks in *A over B* (strength
4), then *B over C* (strength 3), and then discards *C over A* because the two
stronger verdicts already imply the opposite.  Final order: **A, B, C.**

---

## Installation

```bash
pip install -r requirements.txt
```

Python 3.9+ required.

---

## Usage

### Command line

```bash
# Rank from your own game data
python fbs_ranker.py games.csv

# Save the rankings to a CSV file
python fbs_ranker.py games.csv --output rankings.csv

# Run the built-in demo (also writes demo_games.csv)
python fbs_ranker.py --demo

# Demo and save output
python fbs_ranker.py --demo --output demo_rankings.csv
```

### Input CSV format

| Column | Required | Notes |
|--------|----------|-------|
| `home_team` | yes | Team name string |
| `home_score` | yes | Integer |
| `away_team` | yes | Team name string |
| `away_score` | yes | Integer |
| `date` | no | Ignored; included for record-keeping |

Example:

```csv
date,home_team,home_score,away_team,away_score
2024-09-07,Alabama,45,Georgia,17
2024-09-14,Georgia,31,LSU,24
2024-09-21,Alabama,28,LSU,21
```

### Python API

```python
from fbs_ranker import FBSRoundRobinRanker

ranker = FBSRoundRobinRanker()

# Add games programmatically
ranker.add_game("Alabama", "Georgia", 45, 17)
ranker.add_game("Georgia", "LSU",     31, 24)
ranker.add_game("Alabama", "LSU",     28, 21)

# Or load from a CSV file
# ranker.load_csv("games.csv")

results = ranker.rank()

for row in results:
    print(f"{row['rank']:>3}. {row['team']:<25} "
          f"{row['wins']}-{row['losses']}  "
          f"(group size {row['group_size']})")
```

### Output fields

| Field | Type | Description |
|-------|------|-------------|
| `rank` | int | Position (1 = best) |
| `team` | str | Team name |
| `group_size` | int | Number of teams in the round-robin group used for ranking |
| `wins` | int | Wins against group opponents |
| `losses` | int | Losses against group opponents |
| `win_pct` | float | wins / (wins + losses), rounded to 3 dp |
| `points_for` | int | Cumulative points scored vs. group opponents |
| `points_against` | int | Cumulative points allowed vs. group opponents |
| `point_diff` | int | points_for − points_against |

---

## Demo output

`python fbs_ranker.py --demo` builds 24 teams across five groups: an 8-team
Power Conference, a 6-team Mid-Major, a 4-team Small Conference, a 3-team Trio,
and three Independents connected only by single cross-over games.

```
  Rank  Team                   Grp   W-L      Win%    Diff
  1     Aces                   8     7-0      1.000   +197
  2     Rams                   6     5-0      1.000   +98
  3     Bears                  8     6-1      0.857   +67
  4     Lions                  4     3-0      1.000   +21
  5     Spartans               6     4-1      0.800   +38
  6     Mustangs               3     2-0      1.000   +21
  7     Colts                  8     5-2      0.714   +42
  8     Panthers               4     2-1      0.667   +0
  9     Tigers                 6     3-2      0.600   +18
  10    Dukes                  8     4-3      0.571   +0
  ...
  23    Zephyrs                6     0-5      0.000   -90
  24    Hawks                  8     0-7      0.000   -93
```

Note that group size does **not** dictate the tiers: the unbeaten Rams (6-team
group) and Lions (4-team group) outrank teams from the 8-team group, while
winless Hawks finish last despite belonging to the largest group.

Note also where the shrinkage bites.  Bears (7-1 overall) place above Lions
(4-0) and Mustangs (2-0), because eight games of evidence outweigh four or
two.  On raw win percentage both unbeaten teams would have ranked higher.

The demo also includes a **three-way cyclic tie** in the Power Conference
(Eagles, Falcons, Gators all finish 2-5, each beating one of the others) to
exercise the point-differential tiebreaker.

The demo data contains no *conflicting* pairwise verdicts, so it does not
exercise the ranked-pairs conflict rule; `TestConflictingVerdicts` covers that
case directly.

---

## Running tests

```bash
pip install pytest          # not needed to run the ranker, only the tests
python -m pytest tests/ -v
```

The tests are written with the standard library's `unittest`, so they also run
with no extra packages installed:

```bash
python -m unittest discover -s tests
```

Every push and pull request runs the suite automatically on Python 3.9 through
3.13 via GitHub Actions (`.github/workflows/tests.yml`); the badge at the top
of this file shows the latest result.

55 tests cover:

- Empty ranker
- Single game (2-clique)
- Clear win-percentage ordering
- Two-team h2h tiebreaker
- Cyclic three-way tie -> point differential resolution
- Multiple group tiers
- Overlapping groups (team in two cliques compared via the larger one)
- Independent teams
- Conflicting pairwise verdicts resolved in favour of the largest group
- Verdict strengths reported per comparison
- Shrunk win percentage arithmetic, and PRIOR_GAMES = 0 restoring raw records
- A long record outranking a short perfect one via the fallback
- Shrinkage never affecting comparisons made inside a group
- Ranking is always a strict total order (no cycles survive)
- Determinism across input orderings
- CSV loading (with and without a `date` column)
- Output field validation
- Full demo-data smoke test

---

## Assumptions & limitations

- **One game per pair per dataset.**  If a CSV contains duplicate matchups,
  the second result silently overwrites the first.  For multi-season datasets
  covering the same pair of teams, split by season and run separately, or
  pre-process the CSV to keep only the desired game.
- **No overtime distinction.**  A win is a win regardless of overtime.
- **FBS-only games recommended.**  Including FCS or non-D1 opponents may
  create unexpected edges in the game graph.  Filter to FBS-vs-FBS games
  before loading for best results.
- **No strength of schedule.**  The strength-0 fallback now accounts for how
  many games a record covers, but still not for whom they were against: two
  teams with identical shrunk records are separated by point differential, not
  by the quality of their opponents.  An opponent-adjusted fallback (in the
  style of RPI) is the clearest next improvement, at the cost of running a
  second rating system alongside the round-robin logic.
- **Maximum clique complexity.**  Finding all maximal cliques is NP-hard in
  general, but FBS schedules (≈130 teams, ≈12 games each) are sparse enough
  that the Bron-Kerbosch algorithm finishes in milliseconds.
