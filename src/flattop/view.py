"""Per-side views. The server sends players only these, never `GameState`."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from flattop.contacts import age_hours, estimate
from flattop.geo import Pos
from flattop.model import Airbase, Event, GameState, SearchMission, Side, StrikeMission, TaskForce


class ContactView(BaseModel):
    id: str
    reported_at: datetime
    age_minutes: int
    reported_pos: Pos
    estimated_pos: Pos
    uncertainty_nm: float
    reported_course: float
    reported_speed_kn: float
    description: str


class SideView(BaseModel):
    side: Side
    time: datetime
    end: datetime
    is_day: bool
    task_forces: list[TaskForce]
    airbases: list[Airbase]
    contacts: list[ContactView]
    searches: list[SearchMission]
    strikes: list[StrikeMission]
    awaiting_umpire: bool
    log: list[Event]


def view(state: GameState, side: Side) -> SideView:
    contacts = []
    for c in state.contacts:
        if c.side is not side:
            continue
        pos, unc = estimate(c, state.time)
        contacts.append(ContactView(
            id=c.id, reported_at=c.reported_at, age_minutes=round(age_hours(c, state.time) * 60),
            reported_pos=c.reported_pos, estimated_pos=pos, uncertainty_nm=round(unc, 1),
            reported_course=c.reported_course, reported_speed_kn=c.reported_speed_kn,
            description=c.description,
        ))
    return SideView(
        side=side,
        time=state.time,
        end=state.end,
        is_day=state.is_day(),
        task_forces=[t.model_copy(deep=True) for t in state.task_forces if t.side is side],
        airbases=[b.model_copy(deep=True) for b in state.airbases if b.side is side],
        contacts=contacts,
        # `checked` holds enemy ids the search has passed over: truth, so blank it.
        searches=[m.model_copy(deep=True, update={"checked": []}) for m in state.searches if m.side is side],
        strikes=[m.model_copy(deep=True) for m in state.strikes if m.side is side],
        awaiting_umpire=bool(state.pending),
        log=[e for e in state.log if side in e.visible_to],
    )
