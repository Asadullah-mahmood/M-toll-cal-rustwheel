"""Pakistan Motorway / Highway Toll Calculator API (M-Tag style, entry -> exit)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from .calculator import DATA_DIR, TollCalculator, TollError
from .schemas import TollRequest, TollResponse, TripRequest, TripResponse
from .updater import run_update

log = logging.getLogger("toll.updater")
calc = TollCalculator()


async def _update_loop():
    """Check official sources shortly after start-up, then every TOLL_UPDATE_HOURS (default 12)."""
    interval = float(os.getenv("TOLL_UPDATE_HOURS", "12")) * 3600
    await asyncio.sleep(5)
    while True:
        try:
            status = await asyncio.to_thread(run_update, calc)  # blocking I/O off the event loop
            applied = list(status["report"]["applied"])
            log.info("rate check done; updated roads: %s", applied or "none")
        except Exception:  # never let the scheduler die
            log.exception("rate update failed")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = None
    if os.getenv("TOLL_AUTO_UPDATE", "true").lower() == "true":
        task = asyncio.create_task(_update_loop())
    yield
    if task:
        task.cancel()


app = FastAPI(
    title="Pakistan Toll Tax Calculator",
    version="1.1.0",
    description="Calculates NHA motorway / highway toll for an entry-exit trip and vehicle class, with M-Tag surcharge handling. Rates auto-update from NHA.",
    lifespan=lifespan,
)


@app.exception_handler(TollError)
async def toll_error_handler(_: Request, exc: TollError):
    return JSONResponse(
        status_code=exc.status,
        content={"error": exc.__class__.__name__, "detail": exc.message, "suggestions": exc.suggestions},
    )


# ---------------------------------------------------------------- public
@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "tariffs_last_reviewed": calc.last_reviewed}


@app.get("/api/v1/rates/status", tags=["meta"])
def rates_status():
    """When rates were last checked / changed, and which source supplied them."""
    f = DATA_DIR / "update_status.json"
    st = json.loads(f.read_text()) if f.exists() else {}
    ok = st.get("last_success")
    age = None
    if ok:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ok)).days
    return {
        "auto_update": os.getenv("TOLL_AUTO_UPDATE", "true").lower() == "true",
        "last_checked": st.get("last_checked"),
        "last_success": ok,
        "last_change": st.get("last_change"),
        "days_since_successful_check": age,
        "stale": age is None or age > 7,
        "recent_report": st.get("report"),
    }


@app.get("/api/v1/vehicle-types", tags=["catalogue"])
def vehicle_types():
    return [{"id": k, "label": v["label"], "aliases": v.get("aliases", [])} for k, v in calc.vehicle_classes.items()]


@app.get("/api/v1/motorways", tags=["catalogue"])
def motorways():
    return calc.list_roads()


@app.get("/api/v1/motorways/{road_id}/stations", tags=["catalogue"])
def stations(road_id: str):
    road = calc.find_road(road_id)
    return {
        "motorway": road.id,
        "pricing": road.pricing,
        "stations": [{"name": s.name, "km": s.km, "aliases": s.aliases} for s in road.stations],
    }


@app.get("/api/v1/motorways/{road_id}/rates", tags=["catalogue"])
def rates(road_id: str):
    road = calc.find_road(road_id)
    return {
        "motorway": road.id,
        "currency": calc.currency,
        "full_route_rates": road.rates,
        "effective_from": road.effective_from,
        "valid_until": road.valid_until,
        "rates_verified": road.verified_rates,
    }


@app.post("/api/v1/toll/calculate", response_model=TollResponse, tags=["toll"])
def calculate(req: TollRequest):
    return calc.calculate(**req.model_dump())


@app.get("/api/v1/toll/calculate", response_model=TollResponse, tags=["toll"])
def calculate_get(
    motorway: str,
    vehicle_type: str,
    start: str | None = None,
    end: str | None = None,
    has_mtag: bool = True,
):
    return calc.calculate(motorway, vehicle_type, start, end, has_mtag)


@app.post("/api/v1/toll/trip", response_model=TripResponse, tags=["toll"])
def trip(req: TripRequest):
    """Multi-leg journey, e.g. Lahore->Islamabad on M-2 then Islamabad->Peshawar on M-1."""
    return calc.calculate_trip([leg.model_dump() for leg in req.legs])


# ---------------------------------------------------------------- admin
def require_admin(x_api_key: str | None = Header(default=None)):
    expected = os.getenv("TOLL_ADMIN_KEY")
    if not expected or not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing admin key")


@app.post("/api/v1/admin/reload", tags=["admin"], dependencies=[Depends(require_admin)])
def reload_tariffs():
    """Re-read tariffs.json after you edit rates (no server restart needed)."""
    calc.load()
    return {"status": "reloaded", "tariffs_last_reviewed": calc.last_reviewed, "roads": len(calc.roads)}


@app.post("/api/v1/admin/update-now", tags=["admin"], dependencies=[Depends(require_admin)])
async def update_now():
    """Force an immediate check of the official sources."""
    return await asyncio.to_thread(run_update, calc)