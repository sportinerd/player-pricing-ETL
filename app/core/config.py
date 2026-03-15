# app/core/config.py — Unified settings (Pydantic v2)

import os
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── GoalServe ────────────────────────────────────────────────────────────
    GOALSERVE_API_KEY: str = ""
    GOALSERVE_BASE_URL: str = "https://www.goalserve.com/getfeed"
    REQUEST_TIMEOUT: int = 30
    REQUESTS_PER_MINUTE: int = 60
    MAX_RETRIES: int = 4

    # ── MongoDB ──────────────────────────────────────────────────────────────
    MONGO_URI: str = "mongodb://127.0.0.1:27017"
    PRICING_DB: str = "sportinerd_pricing"

    # FGL (Fantasy_Global_Livescore) — DB name on the same Mongo instance as MONGO_URI
    FGL_DB_NAME: str = "Fantasy_Global_Livescore"

    # ── FastAPI ──────────────────────────────────────────────────────────────
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = True

    # ── Pricing constants (from v24 settings.py) ─────────────────────────────
    TOURNAMENT_LEAGUE_IDS: set = {1, 2, 3, 6, 7, 8, 9, 17, 964}

    SEASON_FORMAT_BY_LEAGUE: dict = {
        "1204": "YYYY", "1221": "YYYY", "1408": "YYYY",
        "1352": "YYYY", "1399": "YYYY/YY", "1269": "YYYY/YY",
        "1322": "YYYY/YY", "1286": "YYYY/YY",
    }
    SEASON_FORMAT_DEFAULT: str = "YYYY"

    POSITION_BASELINE: dict = {"GK": 4.0, "DEF": 4.0, "MID": 4.5, "FWD": 4.5}
    POSITION_MAX: dict = {"GK": 6.5, "DEF": 8.0, "MID": 14.5, "FWD": 14.0}

    STAT_WEIGHTS: dict = {
        "goals": 6.0, "assists": 3.0,
        "clean_sheet_gk": 6.0, "clean_sheet_def": 6.0,
        "clean_sheet_mid": 1.0, "clean_sheet_fwd": 0.0,
        "saves_per_game": 0.5, "yellow_cards": -1.0, "red_cards": -3.0,
        "minutes": 0.04, "shots_on_target": 0.3, "key_passes": 0.5,
    }

    CURRENT_SEASON_WEIGHT: float = 0.65
    PREVIOUS_SEASON_WEIGHT: float = 0.35
    BLEND_FULL_GW: int = 20
    MIN_MATCHES_THRESHOLD: int = 3

    FORM_WINDOW: int = 5
    FORM_WEIGHT: float = 0.30
    SEASON_STAT_WEIGHT: float = 0.70
    FORM_MAX_AGE_DAYS: int = 45

    FDR_WINDOW: int = 5
    FDR_MIN: float = 0.92
    FDR_MAX: float = 1.08
    FDR_WEIGHT: float = 0.10

    XI_WEIGHTS: dict = {"minutes_ratio": 0.40, "lineup_rate": 0.40, "squad_rank": 0.20}
    XI_WEIGHTS_NO_LINEUP: dict = {"minutes_ratio": 0.60, "lineup_rate": 0.00, "squad_rank": 0.40}
    SQUAD_RANK_SCORE: dict = {"1": 1.0, "2": 0.65, "3": 0.35}
    XI_PROB_MIN: float = 0.30
    XI_PROB_MAX: float = 1.00

    INJURY_DISCOUNT_PER_LONG_TERM: float = 0.12
    INJURY_DISCOUNT_MIN: float = 0.55
    CURRENT_INJURY_DISCOUNT: float = 0.0
    RIVAL_INJURY_MIN_DAYS: int = 28

    RIVAL_INJURY_BONUS_BASE: float = 0.30
    RIVAL_INJURY_SCARCITY: dict = {"GK": 1.00, "DEF": 0.65, "FWD": 0.65, "MID": 0.40}
    INJURY_SNAPSHOT_WEEKS: int = 104

    NT_FACTOR_MIN: float = 0.85
    NT_FACTOR_MAX: float = 1.15
    NT_WEIGHT_IN_PRICE: float = 0.15
    MIN_NT_APPEARANCES: int = 3

    LEAGUE_SCALE: float = 0.28
    TOURNAMENT_SCALE: float = 0.82
    TEAM_STRENGTH_WEIGHT: float = 0.20

    CALIBRATION_TARGET_MIN: float = 80.0
    CALIBRATION_TARGET_MAX: float = 100.0
    CALIBRATION_MAX_ITER: int = 12

    TEAM_STRENGTH_BLEND_GW: int = 20
    SQUAD_BUDGET: float = 100.0
    KNOCKOUT_BUDGET_TOPUP: float = 5.0

    # ── Previous season enrichment ───────────────────────────────────────────
    FETCH_PREV_SEASON_STATS: bool = True       # set False for fast/test runs
    PREV_SEASON_CACHE_DAYS: int = 180          # prev-season stats don't change

    # ── DigitalOcean Spaces (CDN) ─────────────────────────────────────────────
    DO_ACCESS_KEY_ID: str = ""
    DO_BUCKET_SECRET_KEY: str = ""
    DO_BUCKET_NAME: str = "fantasyfootball"
    DO_ORIGIN_ENDPOINT: str = "https://fantasyfootball.sgp1.digitaloceanspaces.com"
    DO_CDN_ENDPOINT: str = "https://fantasyfootball.sgp1.cdn.digitaloceanspaces.com"
    DO_IMAGES_FOLDER: str = "GS/players"


settings = Settings()
