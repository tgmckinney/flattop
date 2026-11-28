"""Generated weather: random fronts for a scenario, from a seed. Same state and seed give the same fronts."""

from __future__ import annotations

import random
from datetime import timedelta

from pydantic import BaseModel

from flattop.geo import Pos, bearing_deg
from flattop.model import Front, GameState

COMPASS = ("north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west")
FRONTS_PER_DAY = 3.0
THICK_RADIUS_NM = 90  # fronts this large are thick cloud; smaller ones are squalls


class Area(BaseModel):
    """A lat/lon box. Fronts start inside it."""

    south: float
    north: float
    west: float
    east: float

    @property
    def centre(self) -> Pos:
        return Pos(lat=(self.south + self.north) / 2, lon=(self.west + self.east) / 2)


def play_area(state: GameState, margin_deg: float = 1.0) -> Area:
    """The box around every task force and airbase, plus `margin_deg` on each side."""
    pts = [u.pos for u in [*state.task_forces, *state.airbases]]
    return Area(south=min(p.lat for p in pts) - margin_deg, north=max(p.lat for p in pts) + margin_deg,
                west=min(p.lon for p in pts) - margin_deg, east=max(p.lon for p in pts) + margin_deg)


def generate_weather(
    state: GameState,
    seed: int,
    *,
    area: Area | None = None,
    prevailing_deg: float | None = None,
    fronts_per_day: float = FRONTS_PER_DAY,
) -> list[Front]:
    """Random fronts across the scenario's time span.

    Each front starts at a random point in `area` (default: `play_area`), at a random time between the
    scenario start and 6 hours before its end, and lasts 12-36 hours. Fronts drift on the prevailing
    direction (random if not given) plus or minus 40 degrees, at 3-10 knots.
    """
    rng = random.Random(f"weather:{seed}")
    area = area or play_area(state)
    prevailing = rng.uniform(0, 360) if prevailing_deg is None else prevailing_deg
    span_h = (state.end - state.time).total_seconds() / 3600
    count = max(1, round(fronts_per_day * span_h / 24))
    fronts: list[Front] = []
    for i in range(count):
        start = state.time + timedelta(hours=round(rng.uniform(-12, max(0.0, span_h - 6))))
        end = start + timedelta(hours=round(rng.uniform(12, 36)))
        start = max(start, state.time)
        end = max(end, start + timedelta(hours=6))  # a front that began before the scenario still lasts a while
        pos = Pos(lat=round(rng.uniform(area.south, area.north), 2), lon=round(rng.uniform(area.west, area.east), 2))
        radius = round(rng.uniform(40, 120))
        thick = radius >= THICK_RADIUS_NM
        c = area.centre
        where = "centre" if abs(pos.lat - c.lat) + abs(pos.lon - c.lon) < 0.5 else COMPASS[
            round(bearing_deg(c, pos) / 45) % 8]
        fronts.append(Front(
            id=f"R{i + 1}",
            name=f"{'Front' if thick else 'Squalls'} {i + 1} ({where})",
            pos=pos, at=start, radius_nm=radius,
            drift_deg=round((prevailing + rng.uniform(-40, 40)) % 360), drift_kn=round(rng.uniform(3, 10)),
            start=start if start > state.time else None, end=end if end < state.end else None,
            search_factor=0.3 if thick else 0.5, acquire_nm=10 if thick else 15,
        ))
    return fronts
