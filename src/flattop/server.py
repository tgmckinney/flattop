"""HTTP API. A seat token is the only credential; players only ever receive `view(state, side)`."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from flattop.model import Engagement, EngagementResult, GameState, Side
from flattop.orders import Orders
from flattop.scenario import list_scenarios
from flattop.sheet import engagement_sheet
from flattop.store import Advance, GameConfig, Role, SeatStatus, Store, StoreError
from flattop.view import SideView, WeatherView, view, weather_now


class FreshStaticFiles(StaticFiles):
    """Static files the browser must revalidate on every load (a cheap 304 when unchanged).

    Without Cache-Control, browsers cache heuristically and keep running an old app.js after an update."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        r = super().file_response(*args, **kwargs)
        r.headers["Cache-Control"] = "no-cache"
        return r


class Created(BaseModel):
    game_id: str
    seats: dict[Role, str]


class Submit(BaseModel):
    orders: Orders = Orders()
    advance: Advance = "phase"


class AdvanceRequest(BaseModel):
    advance: Advance = "phase"


class SeatView(BaseModel):
    status: SeatStatus
    view: SideView | None = None  # players
    state: GameState | None = None  # umpire only
    weather: list[WeatherView] | None = None  # umpire only: fronts where they are now (players have it in `view`)


class PendingSheet(BaseModel):
    engagement: Engagement
    sheet_md: str


def create_app(db_path: str | None = None) -> FastAPI:
    store = Store(db_path or os.environ.get("FLATTOP_DB", "flattop.db"))
    app = FastAPI(title="flattop")

    @app.exception_handler(StoreError)
    async def _store_error(_: Request, e: StoreError) -> JSONResponse:
        return JSONResponse({"detail": e.detail}, status_code=e.status)

    def seat_view(token: str) -> SeatView:
        role, _, state, status = store.seat(token)
        if role == "umpire":
            return SeatView(status=status, state=state, weather=weather_now(state))
        return SeatView(status=status, view=view(state, Side(role)))

    @app.get("/api/scenarios")
    def scenarios() -> list[str]:
        return list_scenarios()

    @app.post("/api/games", status_code=201)
    def create(cfg: GameConfig) -> Created:
        game_id, seats = store.create(cfg)
        return Created(game_id=game_id, seats=seats)

    @app.get("/api/seats/{token}")
    def get_seat(token: str) -> SeatView:
        return seat_view(token)

    @app.post("/api/seats/{token}/orders")
    def submit(token: str, body: Submit) -> SeatView:
        store.submit(token, body.orders, body.advance)
        return seat_view(token)

    @app.post("/api/seats/{token}/advance")
    def force_advance(token: str, body: AdvanceRequest) -> SeatView:
        store.force_advance(token, body.advance)
        return seat_view(token)

    @app.get("/api/seats/{token}/engagements")
    def engagements(token: str) -> list[PendingSheet]:
        role, cfg, state, _ = store.seat(token)
        if not store.can_resolve(cfg, role):
            raise HTTPException(403, "only the umpire (or a solo player) sees engagement sheets")
        return [PendingSheet(engagement=e, sheet_md=engagement_sheet(e, state.aircraft_types)) for e in state.pending]

    @app.post("/api/seats/{token}/engagements/{engagement_id}")
    def resolve(token: str, engagement_id: str, result: EngagementResult) -> SeatView:
        store.resolve(token, engagement_id, result)
        return seat_view(token)

    @app.get("/api/seats/{token}/actions")
    def actions(token: str) -> list[dict]:
        return store.actions(token)

    app.mount("/", FreshStaticFiles(directory=Path(__file__).parent / "web", html=True), name="web")
    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
