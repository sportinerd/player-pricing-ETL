# Sportinerd Player Pricing — System Architecture & Data Flow

This document explains how the pricing pipeline works end-to-end: where data comes from, how it flows through the system, how prices are computed, and how results are written back to the master player database.

---

## Table of Contents

1. [High-Level Architecture](#1-high-level-architecture)
2. [Data Sources — GoalServe API](#2-data-sources--goalserve-api)
3. [ETL Pipeline — Step by Step](#3-etl-pipeline--step-by-step)
4. [Pricing Model — The 6 Factors](#4-pricing-model--the-6-factors)
5. [Calibration Solver](#5-calibration-solver)
6. [FGL Player Sync — Name Matching](#6-fgl-player-sync--name-matching)
7. [MongoDB Collections](#7-mongodb-collections)
8. [API Endpoints](#8-api-endpoints)
9. [Configuration Reference](#9-configuration-reference)
10. [Worked Example — Pricing a Single Player](#10-worked-example--pricing-a-single-player)

---

## 1. High-Level Architecture

```
                    GoalServe API (External)
                    ┌──────────────────────┐
                    │  4 HTTP JSON feeds    │
                    └──────┬───────────────┘
                           │ httpx async
                           ▼
              ┌────────────────────────────┐
              │   GoalServe Client         │
              │   app/extractors/          │
              │   goalserve.py             │
              │   (rate-limited, retries)  │
              └──────────┬─────────────────┘
                         │ raw JSON dicts
                         ▼
              ┌────────────────────────────┐
              │   Transformers             │
              │   app/transformers/        │
              │   ├── normalizer.py        │  ← field mapping, position codes
              │   └── factors/             │
              │       ├── season_blend.py  │  ← performance + form scoring
              │       ├── xi_estimator.py  │  ← starting XI probability
              │       ├── injury_analyzer  │  ← injury discounts
              │       └── national_team.py │  ← tournament NT factor
              └──────────┬─────────────────┘
                         │ enriched player dicts
                         ▼
              ┌────────────────────────────┐
              │   Pricing Engine           │
              │   app/pricing/engine.py    │
              │   ├── price_player()       │  ← per-player pricing formula
              │   ├── calibration solver   │  ← binary search on scale_adj
              │   └── squad simulation     │  ← 15-player median squad cost
              └──────────┬─────────────────┘
                         │ priced players
                         ▼
              ┌────────────────────────────┐      ┌──────────────────────────┐
              │   MongoDB                  │      │   FGL Player Sync        │
              │   sportinerd_pricing DB    │      │   app/loaders/fgl_sync   │
              │   ├── pricing_runs         │      │                          │
              │   ├── player_prices        │      │   7-tier name matching   │
              │   ├── standings_cache      │      │   ~98.3% accuracy        │
              │   ├── injuries_cache       │      │   (for players in both   │
              │   ├── teams               │      │    GS + SportMonks)      │
              │   └── goalserve_cache      │      └────────────┬─────────────┘
              └────────────────────────────┘                   │ $set price
                         │                                     ▼
                         │                    ┌──────────────────────────────┐
                         │                    │  Fantasy_Global_Livescore DB  │
                         │                    │  players collection           │
                         │                    │  (master player records)      │
                         │                    └──────────────────────────────┘
                         ▼
              ┌────────────────────────────┐
              │   FastAPI REST API         │
              │   app/api/routes/          │
              │   ├── health.py            │
              │   ├── pricing.py           │
              │   ├── leagues.py           │
              │   └── images.py            │
              └────────────────────────────┘

              ┌────────────────────────────┐      ┌──────────────────────────────┐
              │   Image Sync               │      │  DigitalOcean Spaces CDN     │
              │   app/services/            │─────▶│  GS/players/{id}.png         │
              │   image_service.py         │      └──────────────────────────────┘
              │   app/utils/cdn.py         │                   │ CDN URL
              │   (boto3, asyncio.thread)  │                   ▼
              │                            │      ┌──────────────────────────────┐
              │   GoalServe soccerstats/   │      │  Fantasy_Global_Livescore DB │
              │   player/{id} → base64 PNG │      │  image → CDN URL             │
              └────────────────────────────┘      │  sportsmonk_image_url →      │
                                                  │    old SM placeholder        │
                                                  └──────────────────────────────┘
```

---

## 2. Data Sources — GoalServe API

All player data comes from **GoalServe**, a third-party soccer data provider. The system makes **4 API calls** per pipeline run (optimized from 100+ in the v24 predecessor).

**Base URL**: `https://www.goalserve.com/getfeed/{API_KEY}/{path}?json=true`

### Endpoint 1: Standings

```
GET /standings/{league_id}.xml?json=true
```

Returns the league table with team rankings, points, wins/draws/losses, goals for/against.

**Response structure:**
```json
{
  "standings": {
    "tournament": {
      "season": "2025/2026",
      "team": [
        {
          "id": "9002",
          "name": "Arsenal",
          "position": "1",
          "overall": { "gp": "30", "w": "20", "d": "7", "l": "3", "gs": "59", "ga": "22" },
          "total": { "p": "67", "gd": "37" },
          "recent_form": "WWWDD"
        }
      ]
    }
  }
}
```

**Used for:** Team strength scores (points-based, normalized 0.0–1.0), current gameweek detection, fixture difficulty rating baseline.

### Endpoint 2: League Players (Teams + Squads + Stats)

```
GET /soccerleague/{league_id}?json=true
```

Returns every team in the league with their full squad and per-player season statistics. This single call replaces what used to require separate calls per team for squads and stats.

**Response structure:**
```json
{
  "league": {
    "team": [
      {
        "id": "9002",
        "name": "Arsenal",
        "squad": {
          "player": [
            {
              "id": "496730",
              "name": "Bukayo Saka",
              "position": "A",
              "number": "7",
              "appearences": "26",
              "goals": "6",
              "assists": "3",
              "minutes": "1911",
              "rating": "7.21",
              "yellowcards": "2",
              "redcards": "0",
              "saves": "0",
              "keyPasses": "45",
              "shotsOn": "22",
              "lineups": "24",
              "tackles": "18",
              "interceptions": "8",
              "injured": "False"
            }
          ]
        }
      }
    ]
  }
}
```

**Notable quirks:**
- `appearences` is misspelled (GoalServe typo) — the normalizer handles both spellings
- Position codes are single letters: `G`=Goalkeeper, `D`=Defender, `M`=Midfielder, `A`=Attacker
- `injured` is a string `"True"`/`"False"`, not a boolean
- `lineups` is a count of starting XI appearances (used for XI probability)

**Used for:** Player stats, squad depth ranking, XI probability signals, basic injury flag.

### Endpoint 3: Fixtures

```
GET /soccerfixtures/leagueid/{league_id}?json=true
```

Returns all matches (past and future) for the season, organized by week. Completed matches include embedded lineups, substitutions, and goals.

**Response structure (abbreviated):**
```json
{
  "results": {
    "tournament": [{
      "week": [{
        "match": [{
          "date": "15.08.2025",
          "status": "FT",
          "localteam": { "id": "9249", "name": "Liverpool", "score": "4" },
          "visitorteam": { "id": "9079", "name": "Bournemouth", "score": "1" },
          "lineups": {
            "localteam": {
              "formation": "4-2-3-1",
              "player": [{ "id": "102643", "name": "Alisson", "booking": "" }]
            }
          },
          "substitutions": {
            "localteam": {
              "substitution": [{
                "player_in_id": "290182",
                "player_out_name": "H. Ekitike",
                "minute": "72"
              }]
            }
          }
        }]
      }]
    }]
  }
}
```

**Used for three things:**
1. **FDR (Fixture Difficulty Rating)** — upcoming fixtures with status `NS` (Not Started) determine how hard each team's next 5 matches are
2. **Lineup history** — completed matches with embedded lineups provide started/subbed data for XI probability
3. **Form** — last 5 completed matches per team provide recent performance data (booking info, appearance records)

### Endpoint 4: Injuries (Global Feed)

```
GET /soccernew/injuries?json=true
```

Returns injuries for ALL leagues worldwide. The pipeline filters this to only keep injuries for teams in the target league.

**Response structure:**
```json
{
  "scores": {
    "category": [{
      "gid": "1204",
      "matches": {
        "match": [{
          "localteam": {
            "id": "9002",
            "name": "Arsenal",
            "sidelined": {
              "to_miss": {
                "player": [{ "id": "496730", "name": "B. Saka", "status": "Knee Injury" }]
              },
              "questionable": {
                "player": { "id": "330279", "name": "D. Raya", "status": "Knock" }
              }
            }
          }
        }]
      }
    }]
  }
}
```

**Key detail:** `questionable.player` can be a single object OR an array (GoalServe XML-to-JSON conversion quirk). The parser handles both cases.

**Categories:**
- `to_miss` → player confirmed OUT → `status = "injured"`, `injury_discount = 0.0`
- `questionable` → player uncertain → `status = "doubtful"`, `injury_discount = 0.70`

---

## 3. ETL Pipeline — Step by Step

The pipeline is orchestrated by `run_full_pipeline()` in `app/pipeline/orchestrator.py`. It runs asynchronously and makes exactly 4 GoalServe API calls.

### Step 1: Standings → Team Strength (CRITICAL)

```python
standings_raw = await client.get_standings(league_id)     # API call 1
standing_rows = extract_standings(standings_raw)           # Parse JSON → list of team dicts
current_gw = _detect_current_gw(standing_rows)            # Estimate gameweek from total games
team_strength = build_team_strength_map(standing_rows)     # Points-based: team_pts / max_pts
```

**Team strength** is computed as each team's points divided by the league leader's points. Arsenal with 67 points when the max is 67 gets strength 1.0. A mid-table team with 35 points gets 35/67 = 0.522.

**Current gameweek** is estimated by averaging total games played across all teams: `sum(won + drawn + lost) / num_teams`.

If this fetch fails, the pipeline falls back to a cached version in MongoDB (`goalserve_cache` collection). If no cache exists either, the pipeline aborts — standings data is required.

### Step 2: League Players (CRITICAL)

```python
league_raw = await client.get_league_players(league_id)   # API call 2
players = extract_all_players(league_raw, league_id)       # Parse → normalized player dicts
squad_rank_map = _parse_squad_depth(players)               # Rank 1/2/3 by position
```

**Extraction path:** `response["league"]["team"][]` → for each team, iterate `team["squad"]["player"][]` and attach the parent team's ID and name.

**Schema validation** runs before normalization: if >50% of sampled players lack a `position` field or stats fields (`appearences`, `goals`, `minutes`, `rating`), a `SchemaError` is raised to prevent silent zero-value corruption.

**Normalization** for each player:
- Map position: `G→GK`, `D→DEF`, `M→MID`, `A→FWD`
- Handle GoalServe typo: `appearences` → `appearances`
- Compute derived fields: `avg_minutes = minutes / appearances`, `lineup_rate = lineups / appearances`
- Initialize pricing placeholders: `price=None`, `injury_discount=1.0`, `fdr_multiplier=1.0`, etc.

**Squad depth ranking**: players are grouped by position and sorted by squad number. First N players per position are ranked as starters (rank 1), next M as rotation (rank 2), rest as backups (rank 3):

| Position | Starters | Rotation | Bench |
|----------|----------|----------|-------|
| GK | 1 | 1 | rest |
| DEF | 4 | 2 | rest |
| MID | 4 | 3 | rest |
| FWD | 2 | 2 | rest |

### Step 3: Fixtures → FDR + Lineups + Form

```python
fixtures_raw = await client.get_fixtures(league_id)        # API call 3

# 3a: FDR — Fixture Difficulty Rating
for each team:
    upcoming = _extract_upcoming_fixtures(fixtures_raw, team_id, n=5)
    fdr = compute_fdr_multiplier(team_id, upcoming, team_strength)
    # Range: 0.92 (hard fixtures) to 1.08 (easy fixtures)

# 3b: Lineup history (from completed matches)
completed = _extract_completed_matches(fixtures_raw)
lineup_stats = _extract_lineup_data_from_fixtures(completed, team_ids, last_n=10)
for each player:
    xi_probability = compute_xi_probability(player, lineup_stats, squad_rank)
    # Range: 0.30 to 1.00

# 3c: Form (last 5 completed matches per team)
form_lookup = _extract_form_from_fixtures(completed, team_ids, last_n=5)
for each player:
    player["recent_match_stats"] = form_lookup.get(player_id)
```

This step is **non-critical** — if it fails, FDR defaults to 1.0 (neutral), XI probability uses the no-lineup fallback weights, and form is empty (100% season blend).

### Step 4: Injuries (Global Feed, Filtered)

```python
injuries_raw = await client.get_injuries()                 # API call 4
injury_lookup = parse_injury_feed(injuries_raw, team_ids=team_id_set)

players, injured_ids = apply_current_injury_status(players, injury_lookup)
players = apply_historical_injury_discounts(players, {})
players = compute_rival_injury_bonuses(players, injured_ids, squad_rank_map)
```

The global feed is filtered using the set of team IDs obtained from standings in Step 1. Only injuries for teams in the target league are kept.

**Injury discounts applied:**
| Status | Discount | Effect |
|--------|----------|--------|
| Confirmed out (`to_miss`) | 0.0 | Player effectively priced at baseline |
| Doubtful (`questionable`) | 0.70 | 30% price reduction |
| Suspended | 0.85 | 15% price reduction |
| Fit | 1.0 | No discount |

**Rival injury bonus:** When a position-1 player (starter) at a team is injured, the first healthy player at that position gets a bonus. The bonus is scaled by position scarcity:
- GK: £0.30m (highest scarcity — only 1 starter)
- DEF/FWD: £0.195m
- MID: £0.12m (lowest — most competition)

### Step 5: National Team Factors (Tournament Mode Only)

In league mode, all players get `nt_factor = 1.0` (no effect). In tournament mode (World Cup, Euros), the NT factor ranges from 0.85 to 1.15 based on:
- 40% nation FIFA ranking strength
- 35% national team performance score
- 25% national team XI probability

### Step 6: Pricing + Calibration

```python
priced = price_all_players(players, format_type)
```

This runs the calibration solver (see section 5) and produces the final priced player list. Each player gets a `price` (£m) and `price_tier` (budget/mid-range/premium/elite).

### Step 7: Save to MongoDB (sportinerd_pricing)

```python
run_id = await save_pricing_run(league_id, season, player_count, squad_cost, ...)
await save_player_prices(run_id, priced, league_id, season)
await save_standings_cache(league_id, season, standing_rows)
await save_injuries_cache(league_id, injury_lookup)
await save_teams(league_id, season, team_strength, team_ids)
```

### Step 8: Sync Prices → Fantasy_Global_Livescore

```python
await FGLSync().sync_pricing(priced, league_id, season_label)
```

See [Section 6](#6-fgl-player-sync--name-matching) for full matching details.

Each matched player gets their `price` field updated in the `Fantasy_Global_Livescore.players` collection. This is the only field written — no other player data is touched.

---

## 4. Pricing Model — The 6 Factors

Each player's price is computed by `price_player()` in `app/pricing/engine.py`. The formula:

```
price = baseline + (perf_contrib + team_boost) × injury_discount × nt_factor × fdr + rival_bonus
```

Clamped to `[baseline, max_price]` per position, then snapped to nearest £0.1m.

### Factor 1: Performance Score (Season Blend + Form)

**Per-game performance score** (FPL-style):
```
score = (goals×6 + assists×3 + clean_sheets×CS_WEIGHT + saves×0.5[GK only]
         + yellow_cards×(-1) + red_cards×(-3) + avg_minutes×0.04
         + shots_on_target×0.3 + key_passes×0.5) / appearances
```

**Season blend**: Current season weighted against previous season using a GW-based ramp:
- GW 0: 0% current, 100% previous
- GW 10: 32.5% current, 67.5% previous
- GW 20+: 65% current, 35% previous

**Form score**: Last 5 matches within 45 days, weighted by recency (most recent = 1.0, then 0.9, 0.8, 0.7, 0.6).

**Final**: `0.70 × season_blend + 0.30 × form_score` (if form data available)

### Factor 2: XI Probability

How likely the player is to start. Three signals weighted together:

| Signal | With Lineup Data | Without Lineup Data |
|--------|-----------------|-------------------|
| Minutes ratio (avg_min/90) | 40% | 60% |
| Lineup rate (starts/appeared) | 40% | 0% |
| Squad rank score | 20% | 40% |

Result clamped to [0.30, 1.00]. Directly multiplies the performance contribution.

### Factor 3: Injury Discount

| Condition | Discount |
|-----------|----------|
| Currently injured (confirmed out) | 0.0 (= baseline price only) |
| Doubtful | 0.70 |
| Suspended | 0.85 |
| Historical long-term injuries | -12% per injury event (min 0.55) |
| Healthy | 1.0 |

### Factor 4: Fixture Difficulty Rating (FDR)

Based on average opponent strength over next 5 fixtures:
```
fdr = 1.08 - avg_opponent_strength × 0.16
```
- Easy run (weak opponents, avg strength 0.3): FDR ≈ 1.03 (price boost)
- Tough run (strong opponents, avg strength 0.9): FDR ≈ 0.94 (price penalty)
- Range: [0.92, 1.08]

### Factor 5: Team Strength Boost

```
team_boost = 0.20 × team_strength × (max_price - baseline)
```

A player at a top team (strength 1.0) gets a boost of 20% of the position's price range. A player at a bottom team (strength 0.3) gets only 6%.

### Factor 6: National Team Factor (Tournament Only)

Range [0.85, 1.15]. Computed from:
- 40% nation FIFA ranking strength (hardcoded table)
- 35% NT performance score
- 25% NT XI probability

In league mode, this is always 1.0 (no effect).

### Position Price Ranges

| Position | Baseline (Min) | Maximum |
|----------|---------------|---------|
| GK | £4.0m | £6.5m |
| DEF | £4.0m | £8.0m |
| MID | £4.5m | £14.5m |
| FWD | £4.5m | £14.0m |

### Tier Labels

| Tier | Range |
|------|-------|
| Budget | baseline to baseline + £0.5m |
| Mid-range | up to baseline + 35% of range |
| Premium | up to baseline + 65% of range |
| Elite | above 65% of range |

---

## 5. Calibration Solver

The calibration solver ensures the total cost of a realistic 15-player squad falls within **£80m–£100m**.

### How It Works

1. **Simulate a squad**: Pick median-priced players at each position:
   - 2 GK + 5 DEF + 5 MID + 3 FWD = 15 players
   - For each position, sort all players by price and pick evenly-spaced players from the middle (not the cheapest, not the most expensive)

2. **Binary search** over `scale_adj` parameter (range 0.3 to 3.0):
   - `scale_adj` multiplies the performance contribution in the pricing formula
   - Higher `scale_adj` → higher prices → higher squad cost
   - Lower `scale_adj` → lower prices → lower squad cost

3. **Convergence**: Stop when squad cost is between £80m and £100m, or after 12 iterations

4. **Safety checks**:
   - **Monotonicity guard**: If cost at scale_adj=0.3 equals cost at scale_adj=3.0, all players are clamped at their floor price — data quality issue
   - **Plateau detector**: If cost doesn't change between iterations, the solver is stuck in a flat region

### Example Run

```
Iteration 1: scale_adj=1.6500 → squad cost £65.2m  (too low, increase)
Iteration 2: scale_adj=2.3250 → squad cost £72.8m  (too low, increase)
Iteration 3: scale_adj=2.6625 → squad cost £76.1m  (too low, increase)
Iteration 4: scale_adj=2.8312 → squad cost £78.4m  (close but below 80, increase)
...
Converged or best result accepted
```

---

## 6. FGL Player Sync — Name Matching

After pricing, `app/loaders/fgl_sync.py` writes the computed `price` back onto the matching player document in `Fantasy_Global_Livescore.players`.

### The Problem

GoalServe and SportMonks (the source for FGL players) use **completely separate player ID spaces** — there is no shared numeric ID. The only reliable link between the two systems is player name, which itself has several inconsistency patterns.

### Name Inconsistency Patterns Found

| Pattern | GoalServe | FGL (SportMonks) |
|---------|-----------|-----------------|
| Name order reversed | "Gomes Joao" | "João Gomes" |
| Nickname vs full name | "Sam Byram" | "Samuel Byram" |
| Accent stripping | "Martin Odegaard" | "Martin Ødegaard" |
| Single-name players | "Gabriel" | "Gabriel Magalhães" |
| Legal name difference | "Gabriel Jesus" | "Gabriel Fernando de Jesus" |
| Abbreviated display | "Tolu Arokodare" | "Emmanuel Arokodare Toluwalase" |

### 7-Tier Matching Cascade

Matching runs in order, stopping at the first tier that produces a confident result:

| Tier | Strategy | Coverage |
|------|----------|---------|
| T1 | Exact normalised `display_name` match | ~86% |
| T2 | Exact normalised full `name` match | ~3% |
| T3 | Token-set match — order-insensitive ("Jesus Gabriel" ↔ "Gabriel Jesus") | ~3% |
| T4 | Token-subset — GS name ⊆ FGL name with ≤2 extra tokens | ~0.2% |
| T5 | First-initial + last-token match ("Samuel" → "Sam") | ~1% |
| T6 | Single-token name + position disambiguation ("Gabriel" DEF) | ~0.8% |
| T7 | Reverse token-subset — FGL name ⊆ GS name | covers edge cases |

**Normalisation applied at every tier:**
- Extended character map: `ø→o`, `ß→ss`, `ı→i`, `ğ→g`, `ð→d`, etc.
- NFD Unicode decomposition + ASCII strip (handles accented Latin chars)
- Apostrophes, hyphens, dots → spaces
- Lowercase + collapse whitespace

**Disambiguation:** When multiple candidates survive a tier, position (`GK/DEF/MID/FWD`) is used to filter. If still ambiguous, the tier is skipped and matching falls through to the next tier.

### Match Rate (EPL 2025-26, 618 players)

| Outcome | Count | % |
|---------|-------|---|
| Matched (T1–T7) | 581 | 94.0% |
| Not in FGL (youth/fringe, SportMonks hasn't catalogued) | 37 | 6.0% |
| **Achievable ceiling** (of players in both systems) | **581/581** | **98.3%** |

The 37 unmatched are youth/academy players GoalServe tracks that SportMonks has not yet catalogued. These are logged on every run. See `unmatched_players_fgl.txt` for the current EPL list.

### What Gets Written

Only one field is written to each matched FGL player document:

```json
{ "$set": { "price": 8.5 } }
```

The update is idempotent — safe to re-run on every pipeline execution. No other fields on the player document are modified.

### Adding Future Leagues

When a new league is priced, the sync automatically runs for that league. The matching logic is league-agnostic. If SportMonks adds more players over time, the match rate will naturally improve on future runs without any code changes.

---

## 7. MongoDB Collections

All collections live on the same MongoDB instance. Two databases are used:

### Database: `sportinerd_pricing`

#### `pricing_runs`
One document per pipeline execution. Tracks status, player count, squad cost, and calibration parameters.

```json
{
  "_id": "ObjectId",
  "league_id": "1204",
  "season_label": "2025-26",
  "format_type": "league",
  "player_count": 618,
  "squad_cost": 78.4,
  "calibration_scale_adj": 2.8312,
  "current_gw": 29,
  "status": "success",
  "error_message": null,
  "run_at": "2026-03-13T06:25:08.667Z"
}
```

#### `player_prices`
One document per player per pricing run. Contains all stats, factor scores, and the computed price.

```json
{
  "run_id": "69b3adc46f9ec7205191e677",
  "league_id": "1204",
  "player_id": "441484",
  "name": "Erling Haaland",
  "position": "FWD",
  "team_id": "9259",
  "team_name": "Manchester City",
  "price": 9.6,
  "price_tier": "premium",
  "goals": 22.0,
  "assists": 7.0,
  "appearances": 28.0,
  "rating": 7.34,
  "xi_probability": 0.795,
  "injury_discount": 1.0,
  "fdr_multiplier": 0.982,
  "team_strength": 0.896,
  "_pricing_debug": {
    "calibration_scale_adj": 2.8312,
    "calibration_squad_cost": 78.4,
    "has_previous_data": false,
    "form_used": false
  }
}
```

#### `standings_cache`
Latest standings per league. Updated on each pipeline run.

#### `injuries_cache`
Latest injury records per league. Keyed by player_id.

#### `teams`
Team IDs and strength map per league/season.

#### `goalserve_cache`
Raw GoalServe API responses with a 4-hour TTL. Used as fallback when live API calls fail. MongoDB automatically deletes expired documents via a TTL index.

#### `image_sync_jobs`
One document per bulk image sync run. Tracks progress in real-time as the background job processes players.

```json
{
  "_id": "uuid-string",
  "started_at": "2026-03-15T19:13:22Z",
  "status": "queued | running | completed | failed",
  "total": 276,
  "processed": 276,
  "updated": 156,
  "skipped_empty_image": 120,
  "skipped_already_set": 0,
  "failed": 0,
  "completed_at": "2026-03-15T19:19:01Z"
}
```

**Indexes:**
- `player_prices`: `(run_id, league_id)`, `(player_id)`, `(position, price DESC)`
- `pricing_runs`: `(league_id, run_at DESC)`
- `goalserve_cache`: TTL index on `ttl_expires_at`

### Database: `Fantasy_Global_Livescore`

#### `players`
Master player records sourced from SportMonks. The pricing pipeline writes only the `price` field here — all other fields are managed by the SportMonks sync process.

```json
{
  "_id": "ObjectId",
  "api_player_id": 4125,
  "name": "Mohamed Salah Ghaly",
  "display_name": "Mohamed Salah",
  "common_name": "M. Salah",
  "first_name": "Mohamed",
  "last_name": "Salah Ghaly",
  "position": "Attacker",
  "position_group": "FWD",
  "price": 8.5
}
```

---

## 8. API Endpoints

### Health

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Returns `{"status": "ok"/"degraded", "mongodb": bool, "goalserve": bool}` |

### Pipeline

| Method | Path | Description |
|--------|------|-------------|
| POST | `/pricing/run` | Triggers pipeline in background. Returns 202 immediately. |
| POST | `/pricing/run/sync` | Triggers pipeline synchronously. Waits for completion. |
| GET | `/pricing/runs?league_id=X` | Lists recent pipeline runs with status and metrics. |

**Request body for `/pricing/run` and `/pricing/run/sync`:**
```json
{
  "league_id": "1204",
  "season_label": "2025-26",
  "league_name": "Premier League",
  "format_override": null,
  "nt_league_ids": []
}
```

**Response from `/pricing/run/sync`:**
```json
{
  "run_id": "69b456b7b513021ed6e4965d",
  "league_id": "1204",
  "season_label": "2025-26",
  "format_type": "league",
  "players_priced": 618,
  "injured": 72,
  "current_gw": 29,
  "fgl_matched": 581,
  "fgl_unmatched": 37,
  "fgl_written": 581,
  "status": "success",
  "calibration_scale_adj": 2.9578,
  "calibration_squad_cost": 78.1
}
```

### Players

| Method | Path | Query Params | Description |
|--------|------|-------------|-------------|
| GET | `/players` | `league_id` (required), `position`, `min_price`, `max_price`, `limit` | Latest priced players sorted by price DESC |
| GET | `/players/{player_id}` | `league_id` (required) | Single player with full pricing breakdown including `_pricing_debug` |

### Leagues

| Method | Path | Description |
|--------|------|-------------|
| GET | `/leagues/{league_id}/standings` | Cached standings with team rankings |
| GET | `/leagues/{league_id}/injuries` | Cached injury records |
| GET | `/leagues/{league_id}/teams` | Team IDs and strength map |

### Images

| Method | Path | Description |
|--------|------|-------------|
| GET | `/images/placeholder-stats` | Count FGL players still carrying SM placeholder image, broken down by GS ID availability |
| POST | `/images/sync/bulk` | Bulk-replace placeholder images via GoalServe + DO CDN. `dry_run=true` previews without writing. Returns 202 + `job_id` when `dry_run=false` |
| GET | `/images/sync/bulk/status/{job_id}` | Poll progress of a running or completed bulk sync job |
| GET | `/images/sync/runs` | List all past image sync jobs, most recent first. Supports `?status=` filter |
| POST | `/images/sync/player/{api_player_id}` | Sync image for a single player by SM `api_player_id`. Supports `dry_run=true` |

---

## 9. Configuration Reference

All tuning constants are in `app/core/config.py` and can be overridden via environment variables.

### Environment Variables (`.env`)

| Variable | Required | Description |
|----------|----------|-------------|
| `GOALSERVE_API_KEY` | Yes | GoalServe API key for live data |
| `MONGO_URI` | Yes | MongoDB connection string (used for both pricing DB and FGL DB) |
| `PRICING_DB` | No | Pricing database name (default: `sportinerd_pricing`) |
| `FGL_DB_NAME` | No | FGL database name (default: `Fantasy_Global_Livescore`) |
| `DO_ACCESS_KEY_ID` | Yes* | DigitalOcean Spaces access key (*required for image sync) |
| `DO_BUCKET_SECRET_KEY` | Yes* | DigitalOcean Spaces secret key |
| `DO_BUCKET_NAME` | No | Spaces bucket name (default: `fantasyfootball`) |
| `DO_ORIGIN_ENDPOINT` | No | Spaces origin endpoint URL |
| `DO_CDN_ENDPOINT` | No | Spaces CDN endpoint URL |
| `DO_IMAGES_FOLDER` | No | Folder path inside bucket (default: `GS/players`) |

### Stat Weights (Performance Score)

| Stat | Weight | Notes |
|------|--------|-------|
| Goals | 6.0 | |
| Assists | 3.0 | |
| Clean sheets (GK) | 6.0 | |
| Clean sheets (DEF) | 6.0 | |
| Clean sheets (MID) | 1.0 | |
| Clean sheets (FWD) | 0.0 | Not credited |
| Saves per game | 0.5 | GK only |
| Yellow cards | -1.0 | |
| Red cards | -3.0 | |
| Minutes played | 0.04 | Per minute |
| Shots on target | 0.3 | |
| Key passes | 0.5 | |

### Season Blend

| Constant | Value | Meaning |
|----------|-------|---------|
| CURRENT_SEASON_WEIGHT | 0.65 | Max weight for current season |
| BLEND_FULL_GW | 20 | GW at which current season reaches max weight |
| MIN_MATCHES_THRESHOLD | 3 | Below this, player gets baseline price |

### Form

| Constant | Value | Meaning |
|----------|-------|---------|
| FORM_WINDOW | 5 | Number of recent matches |
| FORM_WEIGHT | 0.30 | Weight in final score |
| SEASON_STAT_WEIGHT | 0.70 | Season's weight in final score |
| FORM_MAX_AGE_DAYS | 45 | Discard matches older than this |

### Calibration

| Constant | Value | Meaning |
|----------|-------|---------|
| CALIBRATION_TARGET_MIN | £80m | Squad cost floor |
| CALIBRATION_TARGET_MAX | £100m | Squad cost ceiling |
| CALIBRATION_MAX_ITER | 12 | Max binary search iterations |
| LEAGUE_SCALE | 0.28 | Base performance multiplier |

---

## 10. Worked Example — Pricing a Single Player

**Player:** Bukayo Saka (Arsenal, FWD)

**Raw data from GoalServe:**
- appearances: 26, goals: 6, assists: 3, minutes: 1911, rating: 7.21
- Team strength: 1.0 (Arsenal, league leaders)
- XI probability: 0.675 (avg_minutes = 73.5, slightly below 90)
- FDR: 0.974 (slightly tough upcoming fixtures)
- Injury: healthy (discount = 1.0)
- NT factor: 1.0 (league mode)

**Pricing calculation:**

```
1. Position setup
   baseline = £4.5m (FWD)
   max_price = £14.0m (FWD)
   scale = 0.28 × 2.83 (calibrated scale_adj) = 0.792

2. Performance score
   per_game = (6×6 + 3×3 + 0×0 + 0 + 2×(-1) + 0×(-3) + 73.5×0.04 + 22×0.3 + 45×0.5) / 26
            = (36 + 9 + 0 + 0 - 2 + 0 + 2.94 + 6.6 + 22.5) / 26
            = 75.04 / 26 = 2.886

3. Components
   perf_contrib = 2.886 × 0.792 × 0.675 = 1.543
   team_boost = 0.20 × 1.0 × (14.0 - 4.5) = 1.9
   above_baseline = 1.543 + 1.9 = 3.443

4. Apply multipliers
   adjusted = 3.443 × 1.0 (injury) × 1.0 (NT) × 0.974 (FDR) = 3.353
   + rival_bonus = 0.0

5. Final price
   raw = 4.5 + 3.353 = 7.853
   clamped = clamp(7.853, 4.5, 14.0) = 7.853
   snapped = £7.9m → premium tier
```

**Result:** Saka is priced at **£7.9m** (premium tier).
