# app/main.py — FastAPI application entry point

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.database import connect_db, close_db
from app.loaders.mongo_loader import ensure_indexes
from app.api.routes.health import router as health_router
from app.api.routes.pricing import router as pricing_router, players_router
from app.api.routes.leagues import router as leagues_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await connect_db()
    await ensure_indexes()
    yield
    await close_db()


app = FastAPI(
    title="Sportinerd Player Pricing API",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(pricing_router)
app.include_router(players_router)
app.include_router(leagues_router)
