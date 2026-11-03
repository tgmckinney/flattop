"""Game state. This is the umpire's truth; never send it to a player client (see `flattop.view`)."""

from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from flattop.geo import Pos


class Side(StrEnum):
    ALLIED = "allied"
    JAPAN = "japan"

    @property
    def enemy(self) -> Side:
        return Side.JAPAN if self is Side.ALLIED else Side.ALLIED


Role = Literal["fighter", "dive", "torpedo", "search", "level"]
ShipType = Literal["CV", "CVL", "BB", "CA", "CL", "DD", "AP"]


class AircraftType(BaseModel):
    key: str
    name: str
    role: Role
    speed_kn: float
    strike_radius_nm: float = 0
    search_radius_nm: float = 0
    hit_prob: float = 0
    damage: int = 0
    air_combat: float = 1.0


class ShipClass(BaseModel):
    capacity: int
    flak: float
    deck_out_at: int | None = None  # damage that closes a flight deck


SHIP_CLASSES: dict[str, ShipClass] = {
    "CV": ShipClass(capacity=6, flak=3, deck_out_at=2),
    "CVL": ShipClass(capacity=3, flak=2, deck_out_at=1),
    "BB": ShipClass(capacity=9, flak=5),
    "CA": ShipClass(capacity=4, flak=3),
    "CL": ShipClass(capacity=3, flak=3),
    "DD": ShipClass(capacity=2, flak=1.5),
    "AP": ShipClass(capacity=2, flak=0.5),
}


class Ship(BaseModel):
    name: str
    type: ShipType
    damage: int = 0

    @property
    def sunk(self) -> bool:
        return self.damage >= SHIP_CLASSES[self.type].capacity

    @property
    def deck_out(self) -> bool:
        at = SHIP_CLASSES[self.type].deck_out_at
        return at is not None and self.damage >= at


class TaskForce(BaseModel):
    id: str
    side: Side
    name: str
    pos: Pos
    speed_kn: float = 0
    waypoint: Pos | None = None
    ships: list[Ship]
    air: dict[str, int] = Field(default_factory=dict)  # ready aircraft by type key
    cap: int = 0  # fighters held for combat air patrol

    @property
    def afloat(self) -> list[Ship]:
        return [s for s in self.ships if not s.sunk]

    @property
    def can_operate_aircraft(self) -> bool:
        return any(s.type in ("CV", "CVL") and not s.deck_out and not s.sunk for s in self.ships)


class Airbase(BaseModel):
    id: str
    side: Side
    name: str
    pos: Pos
    operational: bool = True
    air: dict[str, int] = Field(default_factory=dict)
    cap: int = 0

    @property
    def can_operate_aircraft(self) -> bool:
        return self.operational


class Contact(BaseModel):
    """A side's belief about an enemy group. `target_id` is truth and is stripped from player views."""

    id: str
    side: Side
    target_id: str
    reported_at: datetime
    reported_pos: Pos
    reported_course: float
    reported_speed_kn: float
    error_nm: float
    description: str


class SearchMission(BaseModel):
    id: str
    side: Side
    source_id: str
    aircraft: str
    count: int
    origin: Pos
    bearing_from: float
    bearing_to: float
    radius_nm: float
    launched_at: datetime
    checked: list[str] = Field(default_factory=list)


class StrikeMission(BaseModel):
    id: str
    side: Side
    source_id: str
    contact_id: str
    aircraft: dict[str, int]
    aim: Pos
    launched_at: datetime
    arrive_at: datetime
    return_at: datetime | None = None
    phase: Literal["outbound", "inbound"] = "outbound"


class Engagement(BaseModel):
    """An air attack ready for resolution. Doubles as the tabletop engagement sheet."""

    id: str
    time: datetime
    pos: Pos
    daylight: bool
    attacker_side: Side
    strike_id: str
    attackers: dict[str, int]
    target_tf: str
    covering_tfs: list[str]
    defender_cap: dict[str, int]  # cap fighters by type over the target
    ships: list[Ship]


class EngagementResult(BaseModel):
    attacker_losses: dict[str, int] = Field(default_factory=dict)
    cap_losses: dict[str, int] = Field(default_factory=dict)
    ship_damage: dict[str, int] = Field(default_factory=dict)  # ship name -> damage added
    notes: list[str] = Field(default_factory=list)


EventKind = Literal[
    "dawn",
    "dusk",
    "waypoint",
    "sighting",
    "search_returned",
    "strike_launched",
    "strike_missed",
    "attack",
    "engagement_pending",
    "strike_recovered",
    "aircraft_lost",
    "ship_sunk",
]


class Event(BaseModel):
    time: datetime
    kind: EventKind
    visible_to: list[Side]
    text: str


class Daylight(BaseModel):
    sunrise: time
    sunset: time


class GameState(BaseModel):
    scenario: str
    seed: int
    time: datetime
    end: datetime
    tick: int = 0
    tick_minutes: int = 10
    daylight: Daylight
    aircraft_types: dict[str, AircraftType]
    task_forces: list[TaskForce]
    airbases: list[Airbase]
    contacts: list[Contact] = Field(default_factory=list)
    searches: list[SearchMission] = Field(default_factory=list)
    strikes: list[StrikeMission] = Field(default_factory=list)
    pending: list[Engagement] = Field(default_factory=list)
    log: list[Event] = Field(default_factory=list)
    next_id: int = 1

    def new_id(self, prefix: str) -> str:
        i = self.next_id
        self.next_id += 1
        return f"{prefix}{i}"

    def is_day(self, t: datetime | None = None) -> bool:
        tod = (t or self.time).time()
        return self.daylight.sunrise <= tod < self.daylight.sunset

    def tf(self, id_: str) -> TaskForce | None:
        return next((t for t in self.task_forces if t.id == id_), None)

    def unit(self, id_: str) -> TaskForce | Airbase | None:
        return self.tf(id_) or next((b for b in self.airbases if b.id == id_), None)
