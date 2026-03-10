# FBS Round-Robin Ranking Tool

Ranks FBS football teams by discovering **round-robin groups** — sets of teams
where every team played every other team — and ranking within those groups from
the largest group down.  Teams ranked inside a larger group always appear above
teams ranked inside a smaller group, regardless of individual win percentages.

---

## Algorithm

```
1. Build an undirected game graph
   - Node  = team
   - Edge  = at least one game was played between the two teams

2. Find every maximal clique (Bron-Kerbosch via NetworkX)
   A maximal clique is the largest round-robin group that cannot be
   extended by adding another team.

3. Sort cliques: largest first
   Same-size cliques are ordered by the group's aggregate winning
   percentage so stronger groups appear first within a tier.

4. Process each clique in order
   For each clique, rank the teams that have NOT yet been assigned
   a position:

   a. Calculate each team's win-loss record against ALL other members
      of the clique (not just the unranked members).

   b. Sort by win percentage (descending).

   c. Break ties with head-to-head record among only the tied teams.

   d. Further ties broken by cumulative point differential within the
      full clique (descending).

   e. Any remaining ties resolved alphabetically for determinism.

5. Append newly ranked teams to the global ranking.
   Teams ranked by a larger clique are never re-ranked by a smaller one.

6. Any teams with no clique membership (or only solo "cliques") are
   ranked last, ordered by their overall win percentage then point
   differential across all games played.
```

---

## Tie-breaking detail

| Level | Rule |
|-------|------|
| 1 | Win percentage within the round-robin group |
| 2 | Head-to-head win percentage among only the tied teams |
| 3 | Point differential within the round-robin group |
| 4 | Alphabetical (deterministic fallback) |

**Cyclic h2h example** — If A beat B, B beat C, and C beat A (all tied at
1-1 in h2h), level 3 (point differential) resolves the tie automatically.

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

The built-in demo creates 24 teams across five tiers:

| Tier | Teams | Group |
|------|-------|-------|
| 1–8  | Aces, Bears, Colts, Dukes, Eagles, Falcons, Gators, Hawks | 8-team Power Conference |
| 9–14 | Rams, Spartans, Tigers, Vikings, Wildcats, Zephyrs | 6-team Mid-Major Conference |
| 15–18 | Lions, Panthers, Wolves, Sharks | 4-team Small Conference |
| 19–21 | Mustangs, Bobcats, Cougars | 3-team Trio Group |
| 22–24 | Trailblazer, Lone Wolf, Road Runner | Independents (2-clique / head-to-head only) |

The demo intentionally includes a **three-way cyclic tie** in the Power
Conference (Eagles, Falcons, Gators all finish 2-5; each beats one of the
others) to exercise the point-differential tiebreaker.

---

## Running tests

```bash
python -m pytest tests/ -v
```

35 tests cover:

- Empty ranker
- Single game (2-clique)
- Clear win-percentage ordering
- Two-team h2h tiebreaker
- Cyclic three-way tie → point differential resolution
- Multiple group tiers (larger groups ranked above smaller groups)
- Overlapping groups (team in two cliques ranked by the larger one)
- Independent teams
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
- **Maximum clique complexity.**  Finding all maximal cliques is NP-hard in
  general, but FBS schedules (≈130 teams, ≈12 games each) are sparse enough
  that the Bron-Kerbosch algorithm finishes in milliseconds.
