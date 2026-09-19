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

   Bucket A and B's shared cliques BY SIZE, largest size first, and take
   each size in turn.  Ask every clique of that size for a verdict:

   a. Compare win percentage within the clique (descending).
   b. Still tied -> compare point differential within that clique.

   Then reconcile the verdicts at that size:

   - They agree, or only one clique had an opinion -> that is the answer.
   - They DISAGREE -> two equally strong groups contradict each other, so
     the size decides nothing; drop to the next-smaller size.
   - None could separate the teams -> drop to the next-smaller size.

   A smaller group can therefore only speak where every larger one stayed
   silent; it can never reverse an order a larger group established.

   c. No shared clique at all -> look at COMMON OPPONENTS.  Count the
      shared opponents the two teams did NOT fare the same against.  Once
      at least MIN_DIFFERING_RESULTS of them differ (default 2), compare
      the two teams' records against ALL their shared opponents.
      See "Common opponents".
   d. Still nothing -> compare an OPPONENT-WEIGHTED BLEND of each team's
      own record and its opponents' records, then overall point
      differential.  See "Comparing teams with no shared group".

   Each verdict carries a STRENGTH: the size of the group that decided it,
   or 1 for common opponents, or 0 for the blend.  Because every group is
   size 2 or larger, playing someone always outranks merely having played
   the same people, which always outranks the blend.

4. Combine the verdicts into one global order (Tideman's ranked pairs)

   a. Sort every verdict strongest-first: largest deciding group, then the
      most decisive margin inside that group.
   b. Lock each verdict in one at a time, SKIPPING any verdict that
      contradicts what the already-locked verdicts imply.

   Because stronger verdicts are locked first, a conflict is always
   resolved in favour of the larger group — and the result is acyclic by
   construction, so no separate cycle-breaking step is needed.

5. Read the final ranking off the locked ordering.
   A team's rank is one plus the number of teams that outrank it, so teams
   nothing separates SHARE a rank and the next rank skips: 1, 2, 2, 4.
   Nothing is invented from team names to break a genuine tie.
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
| 3 | Repeat levels 1-2 at the next-smaller shared group size |
| 4 | Shrunk overall win percentage, then overall point differential (strength 0) |
| 5 | No tiebreak — the teams are tied and share a rank |

Head-to-head is not a separate level: inside a 2-team group, win percentage
already *is* the head-to-head result, and in larger groups raw head-to-head is
deliberately avoided because it is non-transitive.

**Cyclic h2h example** — If A beat B, B beat C, and C beat A inside one group,
all three sit at the same win percentage, and level 2 (point differential)
separates them.

---

## When equally sized groups disagree

A pair of teams can belong to more than one round-robin group of the same
size, and those groups can reach opposite conclusions:

| Shared group | A's record | B's record | Verdict |
|--------------|-----------|-----------|---------|
| `{A, B, C, D}` | 1-2 | 2-1 | B over A |
| `{A, B, E, F}` | 3-0 | 0-3 | A over B |

Both groups are the same size, so neither outranks the other as evidence.
The tool treats that size as having decided **nothing** and moves to the next
size down.  Equally strong evidence pointing both ways is not evidence.

The alternative — picking one of the two groups by some incidental property —
would make the result depend on something that has nothing to do with football.

---

## Getting real game data

`fetch_games.py` pulls a season from [CollegeFootballData.com]
(https://collegefootballdata.com) and writes a CSV in exactly the format
`fbs_ranker.py` reads.

It needs a free API key from https://collegefootballdata.com/key.  Keep it in
an environment variable rather than on the command line, so it stays out of
your shell history:

```bash
export CFBD_API_KEY=your-key-here

python fetch_games.py --year 2025 --out games_2025.csv
python fbs_ranker.py games_2025.csv --output rankings_2025.csv
```

Options:

| Flag | Meaning |
|------|---------|
| `--year` | Season year (required) |
| `--out` | CSV file to write (required) |
| `--season-type` | `regular` (default), `postseason`, or `both` |
| `--on-duplicate` | `combine` (default), `error`, `keep_first`, `keep_last` |
| `--api-key` | Key, if you would rather not use `CFBD_API_KEY` |

The fetcher keeps only **completed FBS-vs-FBS games**, and reports what it
dropped:

```
Fetching 2025 regular games …
  5 records returned
  3 completed FBS-vs-FBS games kept
  skipped: 1 not final, 1 not an FBS-vs-FBS matchup, 0 missing fields
```

Games against FCS opponents are **kept**, with the FCS side marked unranked —
see "Unranked opponents" below.  Games where neither side is FBS are dropped.
Because those games are counted, the records shown match published ones.

Losses to an unranked opponent are listed explicitly, since they are the
results most worth seeing:

```
4 loss(es) to an unranked opponent — these count against the FBS team:
  2025-08-30  Tarleton State 27-20 Army
  2025-09-06  Austin Peay 24-17 Middle Tennessee
  ...
```

Pairs that met more than once are listed, and **both meetings are written**,
because the ranker counts every meeting:

```
7 pair(s) met more than once:
  Alabama vs Georgia
    2025-09-27  Georgia 21-24 Alabama
    2025-12-06  Alabama 7-28 Georgia
  ...

Both meetings are kept: the ranker counts a split series as 1-1
with points from both games.
```

Use `--on-duplicate keep_first` or `keep_last` to write only one meeting, or
`error` to refuse a season containing any rematch.  The script only reads from
the API; it never writes anything back.

---

## Regular season or postseason

`--season-type` takes `regular` (the default), `postseason`, or `both`.
Conference championship games are part of the regular season as CFBD files
them, so `regular` already runs through Army-Navy; `both` adds the bowls and
the playoff.  This repository carries each season in both forms —
`games_2024.csv` and `games_2024_both.csv` — because the choice is a question
about what the ranking is *for*, not one the data settles.

The postseason is about 45 games against 900, but its effect is larger than
that suggests, because bowls are cross-conference edges and the graph is short
of those:

| season | games added | pairs promoted out of strength 0 | via a new shared opponent | because the two played |
|---|---|---|---|---|
| 2022 | 42 | 107 | 48 | 43 |
| 2023 | 42 | 104 | 48 | 43 |
| 2024 | 46 | 151 | 64 | 47 |
| 2025 | 46 | 120 | 56 | 40 |

Every season shows the same two things.  **The multiplier is 2.5 to 3.3** — a
bowl game settles its own pair and then ripples outward — and **the ripple is
the larger half**: more pairs gain a shared opponent than actually play.  The
share of pairs still reaching strength 0 falls by about one point (88.9% to
87.9% in 2022, similarly elsewhere), so the structure barely moves.

**Where it lands is the opposite of what you would guess.**  The top 10 is the
most stable part of the ranking in all four seasons — 6 to 9 teams move, mean
shift 2.0 to 6.6 — while 45 to 50 teams move in the 51-100 band every year.
Those teams sit at strength 0 relative to almost everyone, so one bowl game is
an enormous relative addition to their evidence.  Teams that played no bowl at
all move too: in 2024 Kansas finished 5-7, played nothing in December, and rose
68 to 33 because other teams' games reordered what it could be compared to.

Postseason games mainly change **who is comparable to whom**, not who is good.

On whether the champion finishes first:

| season | champion | regular season | with postseason |
|---|---|---|---|
| 2022 | Georgia | #1 (13-0) | #1 (15-0) |
| 2023 | Michigan | #1 (13-0) | #1 (15-0) |
| 2024 | Ohio State | #4 (10-2) | #2 (14-2) |
| 2025 | Indiana | #1 (13-0) | #1 (16-0) |

Three of four, and in each of those the champion was undefeated, so the group
tiers had already settled it.  2024 is the case worth understanding: Ohio State
went 10-2, won four playoff games including beating Oregon 41-21, and still
finishes behind Oregon's 13-0 regular season.  A bowl result is strength 2, and
Oregon's place was fixed by a 4-team group whose verdicts outrank it.  That is
the tier hierarchy working as designed — "best season" and "best team in
January" are different questions, and this system answers the first.

---

## Unranked opponents

An FBS schedule usually includes one FCS opponent.  Those games are loaded, but
the FCS team is marked **unranked**: the result counts toward the FBS team's
record and points, while the FCS team never becomes a node in the game graph,
never forms a round-robin group, and never appears in the output.

```python
ranker.add_game("Army", "Tarleton State", 20, 27, away_ranked=False)
```

In a CSV the optional `home_ranked` / `away_ranked` columns carry the same
information — `false`, `0`, `no`, `n` or `unranked` mean unranked; anything
else, including a missing column, means ranked.  `fetch_games.py` writes them.

**Only an explicit `fbs` classification counts as FBS.**  `fetch_games.py` used
to treat a *missing* classification as FBS, on the reasoning that a blank is
not evidence a team is non-FBS.  That was backwards.  CFBD labels the FBS and
FCS teams it tracks, so the records with nothing in that field belong to
opponents further down — NAIA and Division II schools that appear as the
visitor on an FCS schedule.  Reading a blank as FBS let six of them into the
2025 rankings (Dakota State, Valley City State, Madonna, Webber International,
Eastern Oregon, Wayland Baptist), each sitting in the 50s and 60s on a 1-0
record with no group and no points, having beaten an FCS team that *was*
correctly marked unranked.

If an FBS team ever ends up with no ranked opponent at all, the CLI says so by
name.  That is always a sign the input marked someone as ranked who should not
have been.

**A team marked unranked in any row is treated as unranked in every row.**  The
two markings are not equally trustworthy: `false` is positive evidence that a
team sits outside the ranked division, while `true` is also what a blank or
missing value defaults to.  CFBD does leave the classification field empty on
some games, and taking each row at face value promotes an FCS team into the
rankings on the fraction of its schedule that was left blank — near the *top*
of them, because the rows naming it correctly are the ones where it played an
FBS team, so the games it lost are exactly the ones dropped.  In a four-game
test file that put Tarleton State 1st on a 1-0 record that was really 1-1.

Teams corrected this way are listed in `demoted_opponents`, and the CLI prints
a note naming them.  If you see that note, the source data is inconsistent —
the ranking is right, but the input is worth a look.

**Why count the game but not rank the team.**  Dropping these games hides the
most damning result a team can have: in 2025 Army, Middle Tennessee, Eastern
Michigan and Massachusetts each lost to an FCS team and none of it showed.  But
ranking an FCS team on its single FBS game places it on almost no evidence — an
FCS team that went 1-0 landed around 36th of 259 in testing, dragging
everything below it down.  Counting the result without ranking the opponent
avoids both failures.

**What it is worth, honestly.**  Adding the four 2025 upsets moves Eastern
Michigan from 91st to 101st and Middle Tennessee by one place.  Army does not
move at all: its position is fixed by a 6-team group whose verdicts outrank any
record-based one.  Massachusetts does not move either, being already last.  The
real gain is that records match published ones; the ranking effect is small.

It also cannot express *how bad* a loss was.  Army lost to an 11-1 Tarleton
State and Massachusetts to a 3-9 Bryant, and both count the same, because the
only magnitude in this system is the shrunk win percentage — one loss is one
loss.  Weighing a loss by opponent quality needs the strength-of-schedule work
described under "Assumptions & limitations".

---

## Rematches and season series

Two teams can meet more than once — a conference championship game is often a
rematch, and some pairs are scheduled home-and-home.  **Every meeting counts.**
A pair that splits its series is 1-1 against each other, with points from both
games; a sweep is 2-0.

This matters because picking one game declares a winner the season did not.  In
the 2025 FBS regular season, seven pairs met twice and **five of them split**:

| Pair | First | Second |
|------|-------|--------|
| Alabama / Georgia | Alabama 24-21 | Georgia 28-7 |
| Duke / Virginia | Virginia 34-17 | Duke 27-20 |
| Jacksonville St / Kennesaw St | Jax St 35-26 | Kennesaw 19-15 |
| Miami (OH) / W. Michigan | Miami 26-17 | W. Michigan 23-13 |
| Oregon St / Washington St | Oregon St 10-7 | Washington St 32-8 |

For those five, keeping only the first game and keeping only the last name
different winners.  Counting both leaves the pair even and lets the rest of the
evidence place them — Georgia finishes 4th, between the 5th it gets from the
first game alone and the 2nd it gets from the second.

Other policies are available when you want them:

```python
FBSRoundRobinRanker()                             # count every meeting (default)
FBSRoundRobinRanker(on_duplicate="error")         # refuse a dataset with a rematch
FBSRoundRobinRanker(on_duplicate="keep_first")    # only the earliest meeting
FBSRoundRobinRanker(on_duplicate="keep_last")     # only the latest meeting
```

`get_results(a, b)` returns every meeting between two teams, oriented to the
first team named; `get_result(a, b)` returns just the first.

---

## Ties

Two teams are tied when nothing in the data separates them: no shared group
can split them, and their shrunk overall records and point differentials are
identical.  Rather than break such a tie arbitrarily, the tool reports it.

Ranks use standard competition ranking, as sports standings do — a team's rank
is one plus the number of teams that outrank it:

```
  T1    Xray                   2     1-0      1.000   +20
  T1    Yankee                 2     1-0      1.000   +20
  T3    X1                     2     0-1      0.000   -20
  T3    Y1                     2     0-1      0.000   -20
```

Two teams tied for 1st are both 1st and the next rank is 3.  The printed table
marks a shared rank with a `T` prefix; the `rank` field in the CSV and Python
output simply repeats the number.

Teams sharing a rank are listed alphabetically so that output is stable, but
that order carries no meaning — the equal rank NUMBER is what reports the tie.
Renaming a team never changes its rank.

Note that a tie means "no direct verdict", not "same record".  If other
evidence orders two teams transitively (A outranks C, C outranks B), then A
outranks B and they are not tied, even with no head-to-head between them.

---

## Common opponents

Two teams that never met may still have played some of the same opponents.  If
A beat C and B lost to C, that is real comparative evidence, and it sits at
strength 1 -- below every round-robin group, above the blend.

The tier counts **differing** results, not shared opponents.  In the 2025 data
LSU and Missouri share six opponents (Alabama, Arkansas, Oklahoma, South
Carolina, Texas A&M, Vanderbilt) and got the *same* result against all six.
Six common opponents, zero information.  Counting shared opponents would call
that overwhelming evidence; counting differing ones correctly calls it none.

`MIN_DIFFERING_RESULTS` (default 2) is how many must differ before the tier
speaks.  One differing result is a single game, and a single game between two
teams who never met is thin enough to hand a team with a poor record a large
jump; requiring two keeps the tier to cases with corroboration.  Set it to 0
to switch the tier off entirely.

Once the threshold is met, the comparison uses each team's record against
**every** shared opponent -- including the ones they agreed on, which act as a
common baseline -- then the point differential in those games.  Opponents
marked unranked count here: a shared FCS opponent is still a shared opponent.

On the 2025 regular season, of the 8,425 pairs who never played each other:

| Differing common opponents | Pairs | |
|---|---|---|
| 0 | 6,746 | tier silent |
| 1 | 1,341 | tier silent at the default threshold |
| 2 or more | 338 | tier speaks (286 produce a verdict; the rest are level) |

Those 286 verdicts move 17 teams and change nothing inside the top 25.

---

## Comparing teams with no shared group

When two teams never played, share no round-robin group, and have too few
differing common opponents, the tool falls back to their records.  Raw win
percentage is a poor judge here, because it ignores how many games each record
covers -- it ranks a 2-0 team above a 7-1 team.

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

### Weighting opponents

A shrunk record still treats every win as equal.  At strength 0 the two teams
have no evidentiary relationship at all, so the only thing left to compare is
who each of them played.  `BLEND_WEIGHTS` sets that blend, as
`(own record, opponents' records, opponents' opponents' records)`:

```
blended score = a x WP + b x OWP + c x OOWP     # default (0.75, 0.25, 0.0)
```

`(1.0, 0.0, 0.0)` is the plain shrunk record and reproduces the older
behaviour exactly.  Every term is shrunk the same way, and an opponent's
record is computed with the games against the team being scored **removed** --
otherwise beating an opponent would lower their record and so penalise the
team that beat them.  Unranked opponents are left out of OWP and OOWP, since
the data says nothing about how they fared against anyone else.

### Choosing the weights

A weight does not tell you how much a term decides, because the three metrics
do not vary equally.  Each averaging layer pulls harder toward .500: across the
2025 field own record spans .133-.875, opponents' records only .383-.617, and
opponents' opponents' only .436-.568.  Weight times spread is what actually
separates teams:

| weights | own | opponents | opps' opps | own : opponent |
|---------|-----|-----------|------------|----------------|
| (1, 0, 0) | .1692 | -- | -- | no contest |
| **(.75, .25, 0)** | .1269 | .0116 | -- | **11.0 : 1** |
| (.67, .22, .11) | .1134 | .0102 | .0028 | 8.7 : 1 |
| (.60, .27, .13) | .1015 | .0125 | .0033 | 6.4 : 1 |
| (.60, .30, .10) | .1015 | .0139 | .0025 | 6.2 : 1 |
| (.50, .50, 0) | .0846 | .0232 | -- | 3.7 : 1 |

Measured against the plain shrunk record on four seasons.  "Record inversions"
counts pairs where the lower-ranked team has a win percentage at least .150
better; some are legitimate — a group verdict outranking a record is the system
working — so what matters is how the count *moves*.

| weights | 2022 | 2023 | 2024 | 2025 |
|---------|------|------|------|------|
| (1, 0, 0) | 392 | 502 | 631 | 617 |
| **(.75, .25, 0)** | **392 (+0.0%)** | **503 (+0.2%)** | **635 (+0.6%)** | **617 (+0.0%)** |
| (.67, .22, .11) | 392 (+0.0%) | 503 (+0.2%) | 635 (+0.6%) | 617 (+0.0%) |
| (.60, .27, .13) | 394 (+0.5%) | 503 (+0.2%) | 638 (+1.1%) | 621 (+0.6%) |
| (.60, .30, .10) | 398 (+1.5%) | 503 (+0.2%) | 638 (+1.1%) | 621 (+0.6%) |
| (.50, .50, 0) | 447 (+14.0%) | 517 (+3.0%) | 668 (+5.9%) | 638 (+3.4%) |

The default is flat in all four, never more than six tenths of a percent from
the baseline in either direction.  Every weighting that drops own record to .60
or below costs more, and the even split costs most of all.  The even split is worse in all four, and 2022
punishes it by 14%.  That consistency is the argument for the default — not any
single season's figure.

### Why the third term is zero

The third weight is supported — `blended_score` honours any non-zero value —
but ships at 0, because measurement did not justify carrying it.

Opponents' opponents are nearly redundant with opponents: OOWP correlates about
.45 with OWP and varies a sixth as much as own record.  Running `(.67, .22,
.11)` against the shipped two-term weighting changes this much of four seasons'
top 25, and the top 4 never moves:

| season | change | OOWP edge to the team that rises | OWP already agreed? |
|---|---|---|---|
| 2022 | Tennessee over Troy (#5/#6) | +.035 | yes |
| 2022 | Alabama over Tulane (#7/#8) | +.054 | yes |
| 2023 | Oklahoma State in at #25, Ohio out | +.048 | yes |
| 2024 | none | -- | -- |
| 2025 | Tennessee over Missouri (#19/#20) | +.014 | yes |

The direction is consistent — every change lifts the team with the better
opponents' opponents.  **But in every case the immediate opponents' records
already pointed the same way**, so nothing here needs a third layer to explain
it.  An earlier version of this file claimed 2025 supplied a case where OOWP
contradicted OWP and won, Ole Miss over Texas A&M on a worse opponents' record.
That was an artifact of a `games_2025.csv` missing its games against unranked
opponents; on the corrected file the pair does not move, and no case in four
seasons shows the third term overruling the second.

**The own-record weight is the only parameter that really matters.**  How the
remainder splits between the two opponent layers is very nearly a free choice:
`(.60, .27, .13)` and `(.60, .30, .10)` produce an *identical* 2024 ranking and
differ on at most 16 teams in any season, while moving own record from .67 to
.60 shifts two to three times as many teams as folding the third term away
entirely.  Choosing a weighting means choosing `a`; the rest is decoration.

So the two-term form ships: it costs no more inversions than any three-term
version tried, and it has one fewer moving part to explain.

2025 alone would have oversold it: there it slightly *reduced* inversions,
which looked like a point in its favour until the other three showed that was a
one-season accident rather than a property of the weighting.

| season | weights | teams moved | median shift | max shift |
|---|---|---|---|---|
| 2022 | **(.75, .25, 0)** | 54 of 131 | 1 | 9 |
| 2022 | (.50, .50, 0) | 109 of 131 | 2 | 34 |
| 2023 | **(.75, .25, 0)** | 58 of 133 | 1 | 25 |
| 2023 | (.50, .50, 0) | 93 of 133 | 2 | 24 |
| 2024 | **(.75, .25, 0)** | 47 of 134 | 2 | 17 |
| 2024 | (.50, .50, 0) | 94 of 134 | 2 | 42 |
| 2025 | **(.75, .25, 0)** | 24 of 136 | 1 | 12 |
| 2025 | (.50, .50, 0) | 89 of 136 | 1 | 23 |

Two figures worth keeping in view while reading those, because they are easy
to mistake for each other:

| season | teams | largest group | pairs reaching strength 0 | win pct spread |
|---|---|---|---|---|
| 2022 | 131 | 10 | 7,572 of 8,515 (88.9%) | .206 |
| 2023 | 133 | 9 | 7,816 of 8,778 (89.0%) | .219 |
| 2024 | 134 | 7 | 7,904 of 8,911 (88.7%) | .213 |
| 2025 | 136 | 7 | 8,157 of 9,180 (88.9%) | .225 |

Realignment shrank the largest round-robin group from 10 to 7, and changed the
share of pairs reaching the blend not at all.  **About 89% of pairs reach
strength 0 in every season measured**, which looks like a property of how FBS
schedules are shaped rather than of any particular conference alignment: a
10-team round-robin settles 45 pairs out of 8,515.  The blend is not an
edge-case handler.

2022's much lower inversion count is not the group structure either — its
records were simply the most compressed of the four (a win-percentage spread of
.206 against 2025's .243), so fewer pairs had a large record gap available to
invert.

### The case the blend exists for

2023 finished with four undefeated teams, so their records cannot separate
them at all — every one is 13-0, win percentage .882:

| team | record | own | opponents' |
|------|--------|-----|-----------|
| Washington | 13-0 | .882 | .552 |
| Michigan | 13-0 | .882 | .552 |
| Florida State | 13-0 | .882 | .540 |
| **Liberty** | **13-0** | **.882** | **.468** |

Liberty went undefeated in Conference USA.  Five of its twelve opponents
finished 3-8, and its best was a 10-2 New Mexico State.  On record alone it
ranks 3rd, ahead of Washington.  As opponent weight rises it falls to 4th,
then 6th, then 8th.

Nothing else in the system can make that distinction: no group contains both
Liberty and Washington, they share no opponents, and their records are
identical to the decimal.  This is the whole reason the strength-0 tier weighs
opponents at all.

Two notes from these tables:

- **An even split is a large change, not a moderate one.**  On 2025 it moves 80
  of 136 teams against the default, puts LSU (7-5) above Missouri and Tennessee
  (both 8-4) on the strength of a .09 opponents'-record gap, and drops App
  State 23 places for having played a weak schedule.
- **A third term earns little at any weight tried.**  (.60, .30, .10) and
  (.60, .40, 0) produce identical top 25s; (.60, .27, .13) and (.60, .30, .10)
  produce an identical 2024 ranking.  What separates any two of these
  weightings is the own-record weight, not how the remainder is split.

The large record inversions that remain (James Madison at 11-1 sitting below
several 6-6 SEC teams) come from the group tiers and no weighting touches them.

`compare_weights.py` produces these tables for any season:

```bash
python compare_weights.py games_2024.csv
python compare_weights.py games_2024.csv --weights 0.8,0.2,0 0.6,0.4,0 --top 40
```

`TestOpponentWeightingOnAFullSchedule` covers the behaviour in isolation: two
teams with six games each and no shared opponent, one 3-3 against a strong pool
and one 4-2 against a weak one.  Record alone picks the 4-2 team; the blend
picks the 3-3 team.

### How close is too close

Two blended scores can differ in the fourth decimal for no reason a game could
account for, because they are averages of averages.  `BLEND_EPSILON` is how far
apart they must be before the difference is treated as real; below it the two
are level and overall point differential decides instead.

Measured across 2023-2025, one game is worth this much blend:

| | change in blended score |
|---|---|
| the team flips one of its own games | .047 |
| one of its opponents flips one of theirs | .0045 |

So a gap of .001 is about a fifth of one opponent-game — finer than any
evidence the data can express.

**The default is `.001`.**  It used to be a bare `1e-9` — float noise, so any
difference at all decided.  In 2023 that put Washington above Michigan on a gap
of .00012, overriding a 171-point differential (+354 against +183).  The two
are undefeated, never played, share one opponent they both beat, and have win
percentages identical to the last decimal; the only thing between them was a
fourth-decimal difference in their opponents' records.  Under the current
default they are level on the blend and Michigan's point differential decides.

What each threshold costs, measured on the ~8,000 strength-0 pairs in each
season:

| threshold | pairs below it (2023 / 2024 / 2025) | teams whose rank changes |
|---|---|---|
| .0001 | 4 / 2 / 1 | 0 / 0 / 0 |
| **.001 (default)** | **44 / 22 / 37** | **6 / 0 / 4** |
| .005 | 214 / 184 / 188 | 7 / 0 / 8 |
| .01 | 403 / 372 / 373 | 11 / 2 / 17 |

Far fewer teams move than pairs reorder, because ranked pairs discards most
strength-0 verdicts anyway — stronger evidence already orders those teams.
Note also that pairs level on *both* blend and point differential are almost
nonexistent (0 to 2 a season), so a wider threshold means "let point
differential decide", not "declare more ties".

---

**The blend can never reorder teams a group or common opponents settled.**
Strength 0 is the last tier consulted, and ranked pairs locks stronger
verdicts first, so a blend verdict is discarded whenever it contradicts one.
`TestTierHierarchyHolds` asserts this across five weightings.

---

## Conflicts between groups

A worked example, as covered by `TestConflictingVerdicts`:

| Verdict | Decided in | Strength |
|---------|-----------|----------|
| A over B | 4-team group `{A, B, X1, X2}` | 4 |
| B over C | 3-team group `{B, C, Y1}` | 3 |
| C over A | no shared group — opponent-weighted blend | 0 |

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
# Two games between the same pair raise DuplicateGameError by default; pass
# on_duplicate="keep_first" or "keep_last" to pick one instead.

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
| `rank` | int | Position (1 = best).  Tied teams share a rank and the next rank skips: 1, 2, 2, 4 |
| `team` | str | Team name |
| `overall_wins` | int | Wins across every game in the loaded data, including games against unranked opponents |
| `overall_losses` | int | Losses across every game in the loaded data |
| `overall_win_pct` | float | Those wins / those games, rounded to 3 dp |
| `group_size` | int | Number of teams in the round-robin group used for ranking |
| `wins` | int | Wins against group opponents **only** |
| `losses` | int | Losses against group opponents **only** |
| `win_pct` | float | In-group wins / games, rounded to 3 dp |
| `points_for` | int | Cumulative points scored vs. group opponents |
| `points_against` | int | Cumulative points allowed vs. group opponents |
| `point_diff` | int | points_for − points_against |

---

## Demo output

`python fbs_ranker.py --demo` builds 24 teams across five groups: an 8-team
Power Conference, a 6-team Mid-Major, a 4-team Small Conference, a 3-team Trio,
and three Independents connected only by single cross-over games.

```
  Rank  Team                   Overall  Grp  In-Grp  Grp%    PF     PA     Diff
  1     Aces                   8-0      8    7-0     1.000   303    106    +197
  2     Rams                   5-0      6    5-0     1.000   174    76     +98
  3     Bears                  7-1      8    6-1     0.857   222    155    +67
  4     Lions                  4-0      4    3-0     1.000   66     45     +21
  ...
```

`Overall` is the team's full-season record.  Everything after `Grp` covers only
games played inside that team's round-robin group — with real data a group
covers a fraction of a season, so the two differ for nearly every team.

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

212 tests cover:

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
- Equally sized groups that disagree deciding nothing, and the result not
  depending on the other group members' names
- Genuine ties sharing a rank, ranks skipping after a tie, and renaming a
  team never changing its rank
- Duplicate meetings refused (including home/away reversed), the rejected
  duplicate leaving data untouched, the keep_first / keep_last policies, and
  CSV errors naming the offending file and line
- Season series: both meetings kept, a split counted as 1-1 with points from
  both games, a sweep as 2-0, and combine landing between keep_first and
  keep_last rather than picking a winner
- The overall record reported alongside the in-group one
- Unranked opponents: counted in the record and points, kept out of the teams
  list, the graph and the output; both-unranked games ignored; every accepted
  CSV column spelling; and a fetched upset reaching the ranker as a loss
- Common opponents: shared versus differing results counted correctly, the
  tier staying silent below the threshold and speaking at it,
  MIN_DIFFERING_RESULTS = 0 disabling it, level shared records deciding
  nothing, unranked and twice-played shared opponents both counted, and two
  teams that met never reaching strength 1 at all (a shared opponent
  completes the triangle, so they are always in a group together)
- The blend on a full schedule: six games per team, no shared opponents, and
  the strength-0 verdict flipping from the better record to the better
  opposition — while the final order does not move, because chained 2-clique
  verdicts already separate the two and outrank anything the blend says
- The shipped defaults themselves, so a weighting cannot drift unnoticed
- `BLEND_EPSILON`: a gap above it deciding, a gap below it handing over to
  point differential, equal scores on both counting as a genuine tie, and no
  width of threshold reaching above strength 0
- Inconsistent ranked markings: a team marked unranked on one row and ranked on
  another staying out of the rankings, both of its opponents' losses still
  counting, consistent files reporting nothing, and the two-pass load still
  naming the offending line on a duplicate
- The blend: (1, 0, 0) reproducing the shrunk record, weights interpolating,
  an opponent's record excluding the team being scored, unranked opponents
  staying out of the averages, cached metrics recomputing when a game is added
  or PRIOR_GAMES changes, and group order surviving all five weightings tried
- Teams whose entire schedule was unranked opponents being reported, and still
  ranked rather than silently dropped
- `fetch_games.py`: both of CFBD's field-naming styles, unplayed and non-FBS
  games dropped, rematches found despite reversed sides, and the CSV it writes
  loading into the ranker (the API layer runs against a stub, never the network)
- Repeat meetings counted across ranked pairs and unranked opponents alike,
  rather than by subtracting two figures that do not cover the same games
- `compare_weights.py`: movement between orderings, record inversions at a
  given margin (including a winless team not dividing by zero), the influence
  figures, an undifferentiated field yielding no ratio rather than a huge one,
  and the CLI rejecting malformed weights
- `fetch_games.py` classification: only an explicit `fbs` counting as FBS, an
  unclassified team not slipping through as one, case and padding ignored, and
  a response with no classification fields at all failing loudly rather than
  keeping everything
- Ranking is always a strict total order (no cycles survive)
- Determinism across input orderings
- CSV loading (with and without a `date` column)
- Output field validation
- Full demo-data smoke test

---

## Assumptions & limitations

- **Round-robin groups are small in practice, and realignment has not changed
  that.**  Modern conferences are far larger than the number of conference
  games each team plays, so complete round-robins barely exist.  The largest
  group was 10 teams in 2022 and 7 in 2025 — but the share of pairs that reach
  the weakest tier held at about 89% in all four seasons measured, because a
  10-team round-robin settles 45 pairs out of 8,515.  Group evidence still does
  heavy lifting, since those verdicts lock in first and chain transitively to
  settle roughly 75% of all pairs, but the weaker tiers carry more of the load
  than the name suggests, and no plausible conference structure changes that.
- **No overtime distinction.**  A win is a win regardless of overtime.
- **Non-FBS opponents must be marked unranked.**  Loading an FCS opponent as a
  ranked team puts it in the game graph on the strength of one game, which
  places it on almost no evidence: measured on the 2025 data, an FCS team that
  went 1-0 landed around 36th of 259.  `fetch_games.py` marks them for you.
- **Strength of schedule lives only in the weakest tier.**  Ranked pairs
  produces an *ordering*, not a rating: it can say one team finishes below
  another but never by how much.  Only the strength-0 blend carries magnitude,
  which is why opponent quality is applied there and nowhere else.  That is
  deliberate — it is what guarantees schedule strength can never overturn a
  result on the field — but it also means two teams a group has already
  separated are never re-examined in light of whom they played.
- **One differing common opponent is ignored by default.**  1,341 pairs in the
  2025 data have exactly one, and the default threshold of 2 sends all of them
  to the blend.  Some of those single results are genuinely informative;
  `MIN_DIFFERING_RESULTS = 1` uses them, at the cost of letting one game move
  a team a long way.
- **Maximum clique complexity.**  Finding all maximal cliques is NP-hard in
  general, but FBS schedules (≈130 teams, ≈12 games each) are sparse enough
  that the Bron-Kerbosch algorithm finishes in milliseconds.
