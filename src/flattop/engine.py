"""Deterministic phase engine.

`step(state, orders, minutes=..., resolver=...)` applies each side's orders, then advances in
ticks of `state.tick_minutes`. Randomness comes only from `state.seed` and the tick number, so
the same state and orders always produce the same result. Setting `stop_on` ends the phase early
at the first tick that raises one of those events; this is what "advance until something
happens" is built on. A tabletop engagement always stops the phase until `resolve_pending`.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from datetime import datetime, timedelta

from pydantic import BaseModel

from flattop.contacts import estimate
from flattop.geo import Pos, bearing_deg, destination, distance_nm, fmt_pos, in_sector, sector_width
from flattop.model import (
    SHIP_CLASSES,
    Airbase,
    Contact,
    Engagement,
    EngagementResult,
    Event,
    EventKind,
    GameState,
    SearchMission,
    Side,
    StrikeMission,
    TaskForce,
)
from flattop.orders import CapOrder, MoveOrder, OrderError, Orders, SearchOrder, StrikeOrder
from flattop.resolve import Resolver

SEARCH_DEG_PER_PLANE = 10.0
SEARCH_BASE_P = 0.85
ACQUIRE_NM_DAY = 30.0
ACQUIRE_NM_NIGHT = 8.0
MUTUAL_CAP_NM = 25.0
DIVERT_NM = 200.0

DEFAULT_INTERRUPTS: frozenset[EventKind] = frozenset(
    {"dawn", "dusk", "waypoint", "sighting", "attack", "engagement_pending"}
)
# Two-player: the clock is shared, so stopping on a private event (one side's sighting) would tell
# the other side something happened. Only events both sides receive may stop the clock.
PUBLIC_INTERRUPTS: frozenset[EventKind] = frozenset({"dawn", "dusk", "attack", "engagement_pending"})

Interrupts = frozenset[EventKind] | Mapping[Side, frozenset[EventKind]]


def two_player_interrupts(kinds: frozenset[EventKind] = PUBLIC_INTERRUPTS) -> dict[Side, frozenset[EventKind]]:
    return {side: kinds for side in Side}


def _should_stop(events: list[Event], stop_on: Interrupts) -> bool:
    if isinstance(stop_on, frozenset):
        return any(e.kind in stop_on for e in events)
    return any(e.kind in stop_on.get(side, frozenset()) for e in events for side in e.visible_to)


class EngineError(RuntimeError):
    pass


class StepResult(BaseModel):
    state: GameState
    events: list[Event]
    interrupted: bool


Unit = TaskForce | Airbase


# --- helpers -----------------------------------------------------------------------------------


def _emit(state: GameState, out: list[Event], kind: EventKind, text: str, *sides: Side) -> None:
    ev = Event(time=state.time, kind=kind, visible_to=list(sides), text=text)
    state.log.append(ev)
    out.append(ev)


def _rng(state: GameState, salt: str = "") -> random.Random:
    return random.Random(f"{state.seed}:{state.tick}:{salt}")


def _ceil_tick(state: GameState, t: datetime) -> datetime:
    step = timedelta(minutes=state.tick_minutes)
    n = math.ceil((t - state.time) / step)
    return state.time + max(1, n) * step


def _own_unit(state: GameState, side: Side, unit_id: str) -> Unit:
    u = state.unit(unit_id)
    if u is None or u.side is not side:
        raise OrderError(f"{unit_id}: no such {side} unit")
    return u


def _fighter_keys(state: GameState, u: Unit) -> list[str]:
    return [k for k in sorted(u.air) if state.aircraft_types[k].role == "fighter"]


def _available(state: GameState, u: Unit, key: str) -> int:
    n = u.air.get(key, 0)
    if state.aircraft_types[key].role == "fighter":
        held = u.cap
        for k in _fighter_keys(state, u):
            take = min(held, u.air.get(k, 0))
            if k == key:
                return n - take
            held -= take
    return n


def _cap_by_type(state: GameState, u: Unit) -> dict[str, int]:
    out: dict[str, int] = {}
    held = u.cap
    for k in _fighter_keys(state, u):
        take = min(held, u.air.get(k, 0))
        if take:
            out[k] = take
        held -= take
    return out


def _land(state: GameState, side: Side, source_id: str, at: Pos, aircraft: dict[str, int], label: str,
          kind: EventKind, out: list[Event]) -> None:
    src = state.unit(source_id)
    dest: Unit | None = src if src is not None and src.can_operate_aircraft else None
    if dest is None:
        friends: list[Unit] = [u for u in [*state.task_forces, *state.airbases]
                               if u.side is side and u.can_operate_aircraft]
        friends = [u for u in friends if distance_nm(at, u.pos) <= DIVERT_NM]
        dest = min(friends, key=lambda u: distance_nm(at, u.pos), default=None)
    n = sum(aircraft.values())
    if dest is None:
        if n:
            _emit(state, out, "aircraft_lost", f"{label}: no deck or field in reach, {n} aircraft lost.", side)
        return
    for k, c in aircraft.items():
        dest.air[k] = dest.air.get(k, 0) + c
    where = "" if dest.id == source_id else f" (diverted to {dest.name})"
    _emit(state, out, kind, f"{label}: {n} aircraft recovered{where}.", side)


def _describe(tf: TaskForce, rng: random.Random) -> str:
    afloat = tf.afloat
    carriers = sum(1 for s in afloat if s.type in ("CV", "CVL") and rng.random() > 0.2)
    transports = sum(1 for s in afloat if s.type == "AP")
    warships = max(0, len(afloat) - carriers - transports + rng.randint(-2, 2))
    parts = []
    if carriers:
        parts.append(f"{carriers} carrier{'s' if carriers > 1 else ''}")
    if warships:
        parts.append(f"{warships} warship{'s' if warships > 1 else ''}")
    if transports:
        parts.append(f"{transports} transport{'s' if transports > 1 else ''}")
    return ", ".join(parts) or "unidentified ships"


def _course(tf: TaskForce) -> float:
    if tf.waypoint is None or tf.speed_kn == 0:
        return 0.0
    return bearing_deg(tf.pos, tf.waypoint)


# --- orders ------------------------------------------------------------------------------------


def _apply_move(state: GameState, side: Side, o: MoveOrder) -> None:
    tf = _own_unit(state, side, o.tf_id)
    if not isinstance(tf, TaskForce):
        raise OrderError(f"{o.tf_id}: airbases do not move")
    tf.waypoint, tf.speed_kn = o.waypoint, o.speed_kn


def _apply_cap(state: GameState, side: Side, o: CapOrder) -> None:
    u = _own_unit(state, side, o.unit_id)
    have = sum(u.air.get(k, 0) for k in _fighter_keys(state, u))
    if o.fighters > have:
        raise OrderError(f"{u.name}: {o.fighters} CAP requested, {have} fighters ready")
    u.cap = o.fighters


def _apply_search(state: GameState, side: Side, o: SearchOrder, out: list[Event]) -> None:
    u = _own_unit(state, side, o.source_id)
    at = state.aircraft_types.get(o.aircraft)
    if at is None or at.search_radius_nm <= 0:
        raise OrderError(f"{o.aircraft}: not a search aircraft")
    if not state.is_day():
        raise OrderError("searches launch in daylight only")
    if not u.can_operate_aircraft:
        raise OrderError(f"{u.name}: cannot launch aircraft")
    if _available(state, u, o.aircraft) < o.count:
        raise OrderError(f"{u.name}: only {_available(state, u, o.aircraft)} {o.aircraft} available")
    radius = min(o.radius_nm or at.search_radius_nm, at.search_radius_nm)
    u.air[o.aircraft] -= o.count
    m = SearchMission(
        id=state.new_id("S"), side=side, source_id=u.id, aircraft=o.aircraft, count=o.count,
        origin=u.pos, bearing_from=o.bearing_from, bearing_to=o.bearing_to, radius_nm=radius,
        launched_at=state.time,
    )
    state.searches.append(m)


def _apply_strike(state: GameState, side: Side, o: StrikeOrder, out: list[Event]) -> None:
    u = _own_unit(state, side, o.source_id)
    c = next((c for c in state.contacts if c.id == o.contact_id and c.side is side), None)
    if c is None:
        raise OrderError(f"{o.contact_id}: no such contact")
    if not state.is_day():
        raise OrderError("strikes launch in daylight only")
    if not u.can_operate_aircraft:
        raise OrderError(f"{u.name}: cannot launch aircraft")
    aircraft = {k: n for k, n in o.aircraft.items() if n > 0}
    if not aircraft:
        raise OrderError("strike has no aircraft")
    for k, n in aircraft.items():
        at = state.aircraft_types.get(k)
        if at is None or at.strike_radius_nm <= 0:
            raise OrderError(f"{k}: not a strike aircraft")
        if _available(state, u, k) < n:
            raise OrderError(f"{u.name}: only {_available(state, u, k)} {k} available")
    speed = min(state.aircraft_types[k].speed_kn for k in aircraft)
    reach = min(state.aircraft_types[k].strike_radius_nm for k in aircraft)
    # Aim where the contact should be on arrival (two passes of dead reckoning is plenty).
    aim, _ = estimate(c, state.time)
    for _ in range(2):
        arrive = state.time + timedelta(hours=distance_nm(u.pos, aim) / speed)
        aim, _ = estimate(c, arrive)
    if distance_nm(u.pos, aim) > reach:
        raise OrderError(f"target at {distance_nm(u.pos, aim):.0f} nm, strike radius {reach:.0f} nm")
    for k, n in aircraft.items():
        u.air[k] -= n
    m = StrikeMission(
        id=state.new_id("K"), side=side, source_id=u.id, contact_id=c.id, aircraft=aircraft, aim=aim,
        launched_at=state.time, arrive_at=_ceil_tick(state, state.time + timedelta(hours=distance_nm(u.pos, aim) / speed)),
    )
    state.strikes.append(m)
    _emit(state, out, "strike_launched",
          f"Strike {m.id} launched from {u.name}: {sum(aircraft.values())} aircraft against contact {c.id}, "
          f"{distance_nm(u.pos, aim):.0f} nm.", side)


def apply_orders(state: GameState, side: Side, orders: Orders, out: list[Event]) -> None:
    for mo in orders.moves:
        _apply_move(state, side, mo)
    for co in orders.cap:
        _apply_cap(state, side, co)
    for so in orders.searches:
        _apply_search(state, side, so, out)
    for ko in orders.strikes:
        _apply_strike(state, side, ko, out)


# --- per-tick phases ---------------------------------------------------------------------------


def _daylight_events(state: GameState, prev: datetime, out: list[Event]) -> None:
    for t, kind, text in (
        (state.daylight.sunrise, "dawn", "First light."),
        (state.daylight.sunset, "dusk", "Sunset. Air operations end for the night."),
    ):
        mark = datetime.combine(state.time.date(), t, tzinfo=state.time.tzinfo)
        if prev < mark <= state.time:
            _emit(state, out, kind, text, Side.ALLIED, Side.JAPAN)


def _move(state: GameState, hours: float, out: list[Event]) -> None:
    for tf in state.task_forces:
        if tf.waypoint is None or tf.speed_kn == 0 or not tf.afloat:
            continue
        run = tf.speed_kn * hours
        left = distance_nm(tf.pos, tf.waypoint)
        if run >= left:
            tf.pos, tf.waypoint, tf.speed_kn = tf.waypoint, None, 0
            _emit(state, out, "waypoint", f"{tf.name} reached its waypoint at {fmt_pos(tf.pos)}.", tf.side)
        else:
            tf.pos = destination(tf.pos, bearing_deg(tf.pos, tf.waypoint), run)


def _report(state: GameState, m: SearchMission, tf: TaskForce, rng: random.Random, out: list[Event]) -> None:
    err = abs(rng.gauss(0, 12)) + 3
    pos = destination(tf.pos, rng.uniform(0, 360), err)
    course = (_course(tf) + rng.gauss(0, 20)) % 360
    speed = max(0.0, round(tf.speed_kn + rng.gauss(0, 3)))
    desc = _describe(tf, rng)
    c = next((c for c in state.contacts if c.side is m.side and c.target_id == tf.id), None)
    if c is None:
        c = Contact(id=state.new_id("C"), side=m.side, target_id=tf.id, reported_at=state.time,
                    reported_pos=pos, reported_course=course, reported_speed_kn=speed, error_nm=err,
                    description=desc)
        state.contacts.append(c)
    else:
        c.reported_at, c.reported_pos, c.reported_course = state.time, pos, course
        c.reported_speed_kn, c.error_nm, c.description = speed, err, desc
    src = state.unit(m.source_id)
    _emit(state, out, "sighting",
          f"Search {m.id} ({src.name if src else '?'}) reports {desc} at {fmt_pos(pos)}, "
          f"course {course:03.0f}, speed {speed:.0f}. Contact {c.id}.", m.side)


def _search(state: GameState, rng: random.Random, out: list[Event]) -> None:
    day = state.is_day()
    for m in list(state.searches):
        at = state.aircraft_types[m.aircraft]
        elapsed_h = (state.time - m.launched_at).total_seconds() / 3600
        reach = min(m.radius_nm, at.speed_kn * elapsed_h)
        if day:
            p = min(1.0, m.count * SEARCH_DEG_PER_PLANE / sector_width(m.bearing_from, m.bearing_to)) * SEARCH_BASE_P
            for tf in state.task_forces:
                if tf.side is m.side or tf.id in m.checked or not tf.afloat:
                    continue
                if distance_nm(m.origin, tf.pos) <= reach and in_sector(
                    bearing_deg(m.origin, tf.pos), m.bearing_from, m.bearing_to
                ):
                    m.checked.append(tf.id)
                    if rng.random() < p:
                        _report(state, m, tf, rng, out)
        if elapsed_h >= 2 * m.radius_nm / at.speed_kn:
            state.searches.remove(m)
            _land(state, m.side, m.source_id, m.origin, {m.aircraft: m.count}, f"Search {m.id}", "search_returned", out)


def _engage(state: GameState, m: StrikeMission, resolver: Resolver, rng: random.Random, out: list[Event]) -> None:
    acquire = ACQUIRE_NM_DAY if state.is_day() else ACQUIRE_NM_NIGHT
    targets = [t for t in state.task_forces
               if t.side is m.side.enemy and t.afloat and distance_nm(t.pos, m.aim) <= acquire]
    if not targets:
        _emit(state, out, "strike_missed", f"Strike {m.id} found nothing at {fmt_pos(m.aim)} and is returning.", m.side)
        _turn_home(state, m)
        return
    target = min(targets, key=lambda t: distance_nm(t.pos, m.aim))
    covering = [t for t in state.task_forces
                if t.side is target.side and t.afloat and distance_nm(t.pos, target.pos) <= MUTUAL_CAP_NM]
    cap: dict[str, int] = {}
    for t in covering if state.is_day() else []:
        for k, n in _cap_by_type(state, t).items():
            cap[k] = cap.get(k, 0) + n
    e = Engagement(
        id=state.new_id("E"), time=state.time, pos=target.pos, daylight=state.is_day(),
        attacker_side=m.side, strike_id=m.id, attackers=dict(m.aircraft), target_tf=target.id,
        covering_tfs=[t.id for t in covering], defender_cap=cap,
        ships=[s.model_copy() for s in target.afloat],
    )
    result = resolver.resolve(e, state.aircraft_types, rng)
    if result is None:
        state.pending.append(e)
        m.phase = "inbound"  # held over target until resolved
        m.return_at = None
        _emit(state, out, "engagement_pending",
              f"Strike {m.id} has found an enemy group. Awaiting the umpire's result.", m.side)
        _emit(state, out, "engagement_pending",
              f"{target.name} is under air attack. Awaiting the umpire's result.", target.side)
        return
    _apply_result(state, e, result, out)


def _turn_home(state: GameState, m: StrikeMission) -> None:
    src = state.unit(m.source_id)
    home = src.pos if src else m.aim
    speed = min(state.aircraft_types[k].speed_kn for k in m.aircraft) if m.aircraft else 150
    m.phase = "inbound"
    m.return_at = _ceil_tick(state, state.time + timedelta(hours=distance_nm(m.aim, home) / speed))


def _apply_result(state: GameState, e: Engagement, r: EngagementResult, out: list[Event]) -> None:
    m = next(s for s in state.strikes if s.id == e.strike_id)
    for k, n in r.attacker_losses.items():
        m.aircraft[k] = max(0, m.aircraft.get(k, 0) - n)
    for k, n in r.cap_losses.items():
        for tf_id in e.covering_tfs:
            tf = state.tf(tf_id)
            if tf is None or n <= 0:
                continue
            take = min(n, _cap_by_type(state, tf).get(k, 0))
            tf.air[k] -= take
            tf.cap -= take
            n -= take
    target = state.tf(e.target_tf)
    assert target is not None
    sunk: list[str] = []
    hit: list[str] = []
    for ship in target.ships:
        d = r.ship_damage.get(ship.name, 0)
        if d <= 0 or ship.sunk:
            continue
        ship.damage = min(SHIP_CLASSES[ship.type].capacity, ship.damage + d)
        (sunk if ship.sunk else hit).append(ship.name)
    if not target.can_operate_aircraft and target.air and not any(
        s.type in ("CV", "CVL") and not s.sunk for s in target.ships
    ):
        n = sum(target.air.values())
        target.air, target.cap = {}, 0
        if n:
            _emit(state, out, "aircraft_lost", f"{n} aircraft lost with the carriers of {target.name}.", target.side)

    defender = target.side
    lost_att = sum(r.attacker_losses.values())
    lost_cap = sum(r.cap_losses.values())
    _emit(state, out, "attack",
          f"{target.name} under air attack by {sum(e.attackers.values())} aircraft. "
          f"Hit: {', '.join(hit) or 'none'}. Sunk: {', '.join(sunk) or 'none'}. "
          f"CAP lost {lost_cap}; claimed {lost_att} attackers.", defender)
    _emit(state, out, "attack",
          f"Strike {m.id} attacked an enemy group at {fmt_pos(e.pos)}: "
          f"{len(hit) + len(sunk)} ships seen hit{', a ship seen sinking' if sunk else ''}. "
          f"{lost_att} of ours lost.", m.side)
    for name in sunk:
        _emit(state, out, "ship_sunk", f"{name} has sunk.", defender)
    _turn_home(state, m)


def _strikes(state: GameState, resolver: Resolver, rng: random.Random, out: list[Event]) -> None:
    for m in list(state.strikes):
        if m.phase == "outbound" and state.time >= m.arrive_at:
            _engage(state, m, resolver, rng, out)
        elif m.phase == "inbound" and m.return_at is not None and state.time >= m.return_at:
            state.strikes.remove(m)
            src = state.unit(m.source_id)
            _land(state, m.side, m.source_id, src.pos if src else m.aim, m.aircraft, f"Strike {m.id}", "strike_recovered", out)


# --- public API --------------------------------------------------------------------------------


def step(
    state: GameState,
    orders: Mapping[Side, Orders] | None = None,
    *,
    minutes: int,
    resolver: Resolver,
    stop_on: Interrupts = frozenset(),
) -> StepResult:
    """Apply orders, then run up to `minutes` of game time. Never mutates the input state."""
    if state.pending:
        raise EngineError("tabletop engagement pending; call resolve_pending first")
    s = state.model_copy(deep=True)
    out: list[Event] = []
    for side in sorted(orders or {}):
        apply_orders(s, side, (orders or {})[side], out)

    hours = s.tick_minutes / 60
    for _ in range(max(0, minutes // s.tick_minutes)):
        if s.time >= s.end:
            break
        prev = s.time
        s.time += timedelta(minutes=s.tick_minutes)
        s.tick += 1
        rng = _rng(s)
        before = len(out)
        _daylight_events(s, prev, out)
        _move(s, hours, out)
        _search(s, rng, out)
        _strikes(s, resolver, rng, out)
        if s.pending or _should_stop(out[before:], stop_on):
            return StepResult(state=s, events=out, interrupted=True)
    return StepResult(state=s, events=out, interrupted=False)


def advance(
    state: GameState,
    orders: Mapping[Side, Orders] | None = None,
    *,
    resolver: Resolver,
    max_minutes: int = 24 * 60,
    stop_on: Interrupts = DEFAULT_INTERRUPTS,
) -> StepResult:
    """Speed-up: run quiet time until something happens that a player should react to.

    `stop_on` is one set of kinds (solo, umpire) or a set per side (two-player): an event stops
    the clock only if a side that can see it has listed its kind.
    """
    return step(state, orders, minutes=max_minutes, resolver=resolver, stop_on=stop_on)


def _check_result(e: Engagement, r: EngagementResult) -> None:
    problems: list[str] = []
    names = {sh.name for sh in e.ships}
    problems += [f"ship {n!r} not in engagement" for n in r.ship_damage if n not in names]
    problems += [f"ship {n!r}: negative damage" for n, d in r.ship_damage.items() if d < 0]
    for label, losses, flown in (("attacker", r.attacker_losses, e.attackers), ("CAP", r.cap_losses, e.defender_cap)):
        for k, n in losses.items():
            if n < 0 or n > flown.get(k, 0):
                problems.append(f"{label} losses {k}={n}, but {flown.get(k, 0)} flew")
    if problems:
        raise EngineError("tabletop result rejected: " + "; ".join(problems))


def resolve_pending(state: GameState, engagement_id: str, result: EngagementResult) -> StepResult:
    """Feed a tabletop result back in. The strike then turns for home."""
    s = state.model_copy(deep=True)
    e = next((e for e in s.pending if e.id == engagement_id), None)
    if e is None:
        raise EngineError(f"{engagement_id}: no pending engagement")
    _check_result(e, result)
    s.pending.remove(e)
    out: list[Event] = []
    _apply_result(s, e, result, out)
    return StepResult(state=s, events=out, interrupted=bool(s.pending))
