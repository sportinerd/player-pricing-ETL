# Sportinerd Player Pricing

Fantasy football player pricing engine for the Sportinerd platform. Fetches live soccer data from GoalServe, computes fantasy prices using a 6-factor model, and writes prices directly onto player records in the master Fantasy_Global_Livescore database.

Supports both **league** formats (Premier League, La Liga, etc.) and **tournament** formats (World Cup, Euros).

---

## How It Works

The pipeline makes **4 API calls** to GoalServe and prices all players in a league end-to-end:

```
GoalServe API (4 calls)
  ├── Standings        → team strength, current gameweek
  ├── League players   → all squads + season stats
  ├── Fixtures         → upcoming FDR, lineup history, form
  └── Injuries         → global feed, filtered to league teams
         ↓
  6-factor pricing model + calibration solver
         ↓
  sportinerd_pricing DB  ←  internal records + cache
         ↓
  Fantasy_Global_Livescore.players  ←  price field updated
```

For full technical details, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Prerequisites

- Python 3.10+
- MongoDB instance (local or remote)
- GoalServe API key

---

## Setup

```bash
# 1. Create and activate virtual environment
python -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env — minimum required:
#   GOALSERVE_API_KEY=...
#   MONGO_URI=...
```

### Minimum `.env`

```env
GOALSERVE_API_KEY=your_goalserve_key
MONGO_URI=mongodb://user:password@host:27017/?authSource=admin

# DigitalOcean Spaces (required for image sync)
DO_ACCESS_KEY_ID=your_do_access_key
DO_BUCKET_SECRET_KEY=your_do_secret_key
DO_BUCKET_NAME=your_bucket_name
DO_ORIGIN_ENDPOINT=https://your-bucket.region.digitaloceanspaces.com
DO_CDN_ENDPOINT=https://your-bucket.region.cdn.digitaloceanspaces.com
DO_IMAGES_FOLDER=GS/players
```

---

## Running the Server

```bash
source venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

API docs available at `http://localhost:8000/docs` once running.

---

## Triggering a Pricing Run

```bash
# Synchronous — waits for result, returns full summary
curl -X POST http://localhost:8000/pricing/run/sync \
  -H "Content-Type: application/json" \
  -d '{"league_id": "1204", "season_label": "2025-26", "league_name": "Premier League"}'

# Asynchronous — fires in background, returns 202 immediately
curl -X POST http://localhost:8000/pricing/run \
  -H "Content-Type: application/json" \
  -d '{"league_id": "1204", "season_label": "2025-26", "league_name": "Premier League"}'
```

**Response:**
```json
{
  "run_id": "69b456b7b513021ed6e4965d",
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

---

## API Reference

### Health

```
GET /health
```

### Pricing Pipeline

```
POST /pricing/run          # Background (202)
POST /pricing/run/sync     # Synchronous
GET  /pricing/runs         # Recent run history
```

### Players (internal pricing DB)

```
GET /players?league_id=1204
GET /players?league_id=1204&position=FWD&min_price=8.0&max_price=14.0
GET /players/{player_id}?league_id=1204
```

### Leagues

```
GET /leagues/{league_id}/standings
GET /leagues/{league_id}/injuries
GET /leagues/{league_id}/teams
```

### Images

```
GET  /images/placeholder-stats              # Count players with SM placeholder images
POST /images/sync/bulk                      # Bulk replace images via GoalServe CDN (background job)
GET  /images/sync/bulk/status/{job_id}      # Poll bulk job progress
GET  /images/sync/runs                      # List all past image sync runs
POST /images/sync/player/{api_player_id}    # Replace image for a single player
```

**Bulk sync request body:**
```json
{ "dry_run": false, "batch_size": 10, "min_image_bytes": 1000 }
```

**Single player request body:**
```json
{ "dry_run": false }
```

---

## Supported Leagues

Any league available on GoalServe. Common IDs:

| League | ID |
|--------|----|
| Premier League | 1204 |
| La Liga | 1399 |
| Serie A | 1269 |
| Bundesliga | 1221 |
| Champions League | 1 |
| World Cup | 6 |
| Euros | 7 |

---

## Project Structure

```
player_pricing/
├── app/
│   ├── main.py                      # FastAPI app + lifespan
│   ├── core/
│   │   ├── config.py                # All settings + tuning constants
│   │   └── database.py              # Motor async MongoDB client
│   ├── api/
│   │   ├── schemas.py               # Request/response models
│   │   └── routes/
│   │       ├── health.py
│   │       ├── pricing.py           # Pipeline + player endpoints
│   │       ├── leagues.py
│   │       └── images.py            # Image sync endpoints
│   ├── extractors/
│   │   └── goalserve.py             # Async GoalServe API client (httpx)
│   ├── transformers/
│   │   ├── normalizer.py            # GoalServe → internal field mapping
│   │   └── factors/
│   │       ├── season_blend.py      # Season + form performance scoring
│   │       ├── xi_estimator.py      # Starting XI probability
│   │       ├── injury_analyzer.py   # Injury feed parsing + discounts
│   │       └── national_team.py     # NT duty factor (tournament mode)
│   ├── pricing/
│   │   └── engine.py                # Price formula + calibration solver
│   ├── pipeline/
│   │   └── orchestrator.py          # 4-call async ETL orchestration
│   ├── services/
│   │   └── image_service.py         # Image fetch → CDN upload → FGL DB update
│   ├── utils/
│   │   └── cdn.py                   # DigitalOcean Spaces client (boto3)
│   └── loaders/
│       ├── mongo_loader.py          # sportinerd_pricing DB persistence
│       └── fgl_sync.py              # FGL player price sync (7-tier name matching)
├── ARCHITECTURE.md                  # Full system documentation
├── requirements.txt
└── .env
```

---

## MongoDB

Two databases on the same instance:

| Database | Purpose |
|----------|---------|
| `sportinerd_pricing` | Pricing runs, player prices, standings/injury/GoalServe cache, image sync jobs |
| `Fantasy_Global_Livescore` | Master player records — pipeline writes `price`; image sync writes `image` + `sportsmonk_image_url` |

---

## Pricing Model Summary

| Factor | Range | Source |
|--------|-------|--------|
| Performance (season blend + form) | 0 → ∞ | GoalServe stats |
| XI probability | 0.30 – 1.00 | Lineup history + minutes |
| Injury discount | 0.0 – 1.0 | Global injury feed |
| Fixture difficulty (FDR) | 0.92 – 1.08 | Upcoming fixtures |
| Team strength boost | 0 – 20% of range | League standings |
| National team factor | 0.85 – 1.15 | Tournament mode only |

Position price ranges: GK £4.0–6.5m · DEF £4.0–8.0m · MID £4.5–14.5m · FWD £4.5–14.0m

A calibration solver (binary search) adjusts the performance scale factor until the median 15-player squad cost falls within £80m–£100m.

---

## Logs

Written to `logs/sportinerd_etl.log` (rotating, 5 MB max, 3 backups) and stdout.

---

*Proprietary — Sportinerd*
