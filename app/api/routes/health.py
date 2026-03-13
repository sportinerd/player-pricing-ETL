# app/api/routes/health.py — Health check endpoints

from fastapi import APIRouter

from app.core.database import get_db
from app.extractors.goalserve import GoalServeClient

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    """System health: MongoDB connectivity and GoalServe reachability."""
    # MongoDB check
    mongo_ok = False
    try:
        db = get_db()
        await db.client.admin.command("ping")
        mongo_ok = True
    except Exception:
        pass

    # GoalServe check
    gs_ok = False
    client = GoalServeClient()
    try:
        gs_ok = await client.ping()
    except Exception:
        pass
    finally:
        await client.close()

    status = "ok" if (mongo_ok and gs_ok) else "degraded"
    return {
        "status": status,
        "mongodb": mongo_ok,
        "goalserve": gs_ok,
    }
