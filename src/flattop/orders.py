"""Player orders for one phase. All validation happens in `flattop.engine` against current state."""

from __future__ import annotations

from pydantic import BaseModel, Field

from flattop.geo import Pos


class MoveOrder(BaseModel):
    tf_id: str
    waypoint: Pos | None
    speed_kn: float = Field(ge=0, le=32)


class CapOrder(BaseModel):
    unit_id: str
    fighters: int = Field(ge=0)


class SearchOrder(BaseModel):
    source_id: str
    aircraft: str
    count: int = Field(gt=0)
    bearing_from: float = Field(ge=0, lt=360)
    bearing_to: float = Field(ge=0, lt=360)
    radius_nm: float | None = Field(default=None, gt=0)


class StrikeOrder(BaseModel):
    source_id: str
    contact_id: str
    aircraft: dict[str, int]


class Orders(BaseModel):
    moves: list[MoveOrder] = Field(default_factory=list)
    cap: list[CapOrder] = Field(default_factory=list)
    searches: list[SearchOrder] = Field(default_factory=list)
    strikes: list[StrikeOrder] = Field(default_factory=list)


class OrderError(ValueError):
    pass
