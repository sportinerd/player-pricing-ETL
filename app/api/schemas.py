# app/api/schemas.py — Pydantic request/response models

from typing import List, Optional
from pydantic import BaseModel


class PricingRunRequest(BaseModel):
    league_id: str
    season_label: str = "current"
    format_override: Optional[str] = None
    nt_league_ids: List[str] = []
    league_name: str = ""


class PricingRunResponse(BaseModel):
    run_id: str
    status: str
    message: str


class KnockoutTopupRequest(BaseModel):
    league_id: str
    season_label: str
    teams_eliminated: int
    teams_remaining: int


class PlayerQuery(BaseModel):
    league_id: str
    position: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    limit: int = 500
