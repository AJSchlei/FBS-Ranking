# FBS Round-Robin Ranking Tool

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
      percentage, then overall point differential, then team name.

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
| 4 | Overall win percentage, then overall point differential (strength 0) |
| 5 | Alphabetical (deterministic fallback) |

Head-to-head is not a separate level: inside a 2-team group, win percentage
already *is* the head-to-head result, and in larger groups raw head-to-head is
deliberately avoided because it is non-transitive.

**Cyclic h2h example** — If A beat B, B beat C, and C beat A inside one group,
all three sit at the same win percentage, and level 2 (point differential)
separates them.

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
  3     Lions                  4     3-0      1.000   +21
  4     Mustangs               3     2-0      1.000   +21
  5     Bears                  8     6-1      0.857   +67
  6     Spartans               6     4-1      0.800   +38
  7     Colts                  8     5-2      0.714   +42
  8     Panthers               4     2-1      0.667   +0
  9     Tigers                 6     3-2      0.600   +18
  10    Dukes                  8     4-3      0.571   +0
  ...
  23    Hawks                  8     0-7      0.000   -93
  24    Zephyrs                6     0-5      0.000   -90
```

Note that group size does **not** dictate the tiers: the unbeaten Rams (6-team
group) and Lions (4-team group) outrank the one-loss Bears from the 8-team
group, while winless Hawks finish 23rd despite belonging to the largest group.

The demo also includes a **three-way cyclic tie** in the Power Conference
(Eagles, Falcons, Gators all finish 2-5, each beating one of the others) to
exercise the point-differential tiebreaker.

The demo data contains no *conflicting* pairwise verdicts, so it does not
exercise the ranked-pairs conflict rule; `TestConflictingVerdicts` covers that
case directly.

---

## Running tests

```bash
python -m pytest tests/ -v
```

46 tests cover:

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
- **Small samples in the strength-0 fallback.**  When two teams share no
  round-robin group, they are compared on raw overall win percentage, which
  takes no account of how many games each played or against whom.  A 1-0 team
  therefore outranks a 5-1 team on that comparison alone.  Ranked pairs limits
  the damage — any chain of group-based verdicts overrides the fallback — but
  it does not fix it.  Adding a minimum-games threshold or a strength-of-
  schedule adjustment to this fallback is the clearest next improvement.
- **Maximum clique complexity.**  Finding all maximal cliques is NP-hard in
  general, but FBS schedules (≈130 teams, ≈12 games each) are sparse enough
  that the Bron-Kerbosch algorithm finishes in milliseconds.
