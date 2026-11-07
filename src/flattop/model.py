"""Game state. This is the umpire's truth; never send it to a player client (see `flattop.view`)."""

from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from flattop.geo import Pos, destination, distance_nm


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
    capacity: int  # damage that sinks her
    flak: float
    deck_out_at: int | None = None  # damage that closes a flight deck
    hangar: int = 0  # carrier aircraft she can operate (floatplanes on cruisers do not count)


SHIP_CLASSES: dict[str, ShipClass] = {
    "CV": ShipClass(capacity=6, flak=3, deck_out_at=2, hangar=90),
    "CVL": ShipClass(capacity=3, flak=2, deck_out_at=1, hangar=37),
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

    @property
    def hangar(self) -> int:
        return sum(SHIP_CLASSES[s.type].hangar for s in self.ships if not s.sunk and not s.deck_out)


class Airbase(BaseModel):
    id: str
    side: Side
    name: str
    pos: Pos
    operational: bool = True
    hangar: int | None = None  # None: no practical limit
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
    "snooper",  # your group spotted the enemy scout that found it
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
    refs: dict[str, str] = Field(default_factory=dict)  # ids the reader may act on; same visibility as the text


class Front(BaseModel):
    """A weather area: cloud and rain that hide ships from the air. Public to both sides.

    Its centre drifts in a straight line from `pos` at time `at`. Outside `start`..`end` it does not exist.
    """

    id: str
    name: str
    pos: Pos
    at: datetime
    radius_nm: float
    drift_deg: float = 0
    drift_kn: float = 0
    start: datetime | None = None
    end: datetime | None = None
    search_factor: float = Field(0.3, ge=0, le=1)  # multiplies the chance a search sees a group inside
    acquire_nm: float = 10.0  # how close a strike must come to find a group inside

    def centre(self, t: datetime) -> Pos | None:
        if (self.start and t < self.start) or (self.end and t >= self.end):
            return None
        return destination(self.pos, self.drift_deg, self.drift_kn * (t - self.at).total_seconds() / 3600)

    def covers(self, p: Pos, t: datetime) -> bool:
        c = self.centre(t)
        return c is not None and distance_nm(c, p) <= self.radius_nm


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
    weather: list[Front] = Field(default_factory=list)
    # Per-side counters: a shared counter would let a player infer enemy activity from gaps in their own ids.
    next_id: dict[str, int] = Field(default_factory=dict)

    def new_id(self, prefix: str, side: Side | None = None) -> str:
        """`side=None` is for umpire-only objects (engagements)."""
        key = side.value if side else "umpire"
        i = self.next_id.get(key, 0) + 1
        self.next_id[key] = i
        return f"{key[0].upper()}-{prefix}{i}"

    def is_day(self, t: datetime | None = None) -> bool:
        tod = (t or self.time).time()
        return self.daylight.sunrise <= tod < self.daylight.sunset

    def front_over(self, p: Pos) -> Front | None:
        """The thickest active weather over `p` (lowest search factor), if any."""
        over = [f for f in self.weather if f.covers(p, self.time)]
        return min(over, key=lambda f: f.search_factor) if over else None

    def tf(self, id_: str) -> TaskForce | None:
        return next((t for t in self.task_forces if t.id == id_), None)

    def unit(self, id_: str) -> TaskForce | Airbase | None:
        return self.tf(id_) or next((b for b in self.airbases if b.id == id_), None)
