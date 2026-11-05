"""Solo opponent. It reads only its own `SideView`, exactly what a human on that side would see.

Doctrine, kept deliberately simple:
- Carriers hold a third of their fighters on CAP.
- With no fresh carrier contact, each unit keeps one search aloft, aimed at its best guess of the enemy.
- With a fresh contact in range, launch every bomber that can reach it, escorted by half the free fighters.
- Carrier groups close on a known enemy carrier until it is inside strike range (by day, and overnight to
  open the next morning at about strike range), and retire away from the enemy once half their decks are out.
- Groups without carriers follow the scenario's waypoints.
"""

from __future__ import annotations

from datetime import datetime

from flattop.contacts import intercept
from flattop.geo import bearing_deg, destination, distance_nm
from flattop.model import Airbase, AircraftType, EventKind, TaskForce
from flattop.orders import CapOrder, MoveOrder, Orders, SearchOrder, StrikeOrder
from flattop.view import ContactView, SideView

# Events that make the bot re-plan mid-run. They never stop the player's clock (see `Store.interrupts`).
WAKE_ON: frozenset[EventKind] = frozenset({"sighting", "strike_recovered", "search_returned", "strike_missed"})
FRESH_MINUTES = 180  # older contacts are too vague to strike
CAP_SHARE = 1 / 3
ESCORT_SHARE = 0.5
RANGE_MARGIN = 0.95  # stay inside the strike radius; contacts drift
MIN_STRIKE = 6  # do not dribble out penny packets
SEARCH_SHARE = 0.25  # of scout-bombers, when no dedicated search aircraft are aboard
SEARCH_DEG_PER_PLANE = 10
DASH_KN = 24
CLOSE_TO = 0.8  # of strike radius: where a closing carrier group aims to sit
NIGHT_MEMORY_MINUTES = 12 * 60  # how old a carrier report may be and still be worth closing on overnight
RETIRE_NM = 300

Unit = TaskForce | Airbase
BOMBER_ROLES = ("dive", "torpedo", "level")


def _score(c: ContactView) -> float:
    d = c.description
    return ("carrier" in d) * 100 + ("transport" in d) * 20 - c.age_minutes / 10


def _search_axis(u: Unit, v: SideView) -> float:
    """Best guess at where the enemy is: any contact, else the way we are heading, else rotate by the hour."""
    if v.contacts:
        c = min(v.contacts, key=lambda c: distance_nm(u.pos, c.estimated_pos))
        return bearing_deg(u.pos, c.estimated_pos)
    if isinstance(u, TaskForce) and u.waypoint and distance_nm(u.pos, u.waypoint) > 20:
        return bearing_deg(u.pos, u.waypoint)
    return (v.time.hour * 90) % 360


def _search(u: Unit, v: SideView, types: dict[str, AircraftType]) -> SearchOrder | None:
    dedicated = [k for k, n in u.air.items() if n > 0 and types[k].role == "search"]
    scouts = [k for k, n in u.air.items() if n > 0 and types[k].role in BOMBER_ROLES and types[k].search_radius_nm > 0]
    if dedicated:
        k = max(dedicated, key=lambda k: types[k].search_radius_nm)
        count = u.air[k]
    elif scouts:
        k = max(scouts, key=lambda k: u.air[k])
        count = max(2, round(u.air[k] * SEARCH_SHARE))
    else:
        return None
    width = min(359, count * SEARCH_DEG_PER_PLANE)  # 359, not 360: equal bearings mean all-round
    axis = _search_axis(u, v)
    return SearchOrder(source_id=u.id, aircraft=k, count=count,
                       bearing_from=round(axis - width / 2) % 360, bearing_to=round(axis + width / 2) % 360)


def _strike(u: Unit, c: ContactView, free_fighters: dict[str, int], types: dict[str, AircraftType]) -> StrikeOrder | None:
    bombers = {k: n for k, n in u.air.items() if n > 0 and types[k].role in BOMBER_ROLES and types[k].strike_radius_nm > 0}
    for _ in range(3):  # dropping slow or short-legged types changes the intercept; settle it
        if not bombers:
            return None
        speed = min(types[k].speed_kn for k in bombers)
        aim = intercept(u.pos, c.estimated_pos, c.reported_course, c.reported_speed_kn, speed)
        d = distance_nm(u.pos, aim)
        keep = {k: n for k, n in bombers.items() if d <= types[k].strike_radius_nm * RANGE_MARGIN}
        if keep == bombers:
            break
        bombers = keep
    else:
        return None
    if sum(bombers.values()) < MIN_STRIKE:
        return None
    escort = {k: int(n * ESCORT_SHARE) for k, n in free_fighters.items()
              if d <= types[k].strike_radius_nm * RANGE_MARGIN and types[k].speed_kn >= speed}
    return StrikeOrder(source_id=u.id, contact_id=c.id, aircraft={**bombers, **{k: n for k, n in escort.items() if n}})


def _carriers(tf: TaskForce) -> tuple[int, int]:
    """(carrier decks we started with, decks still working)."""
    cv = [s for s in tf.ships if s.type in ("CV", "CVL")]
    return len(cv), sum(1 for s in cv if not s.sunk and not s.deck_out)


def _move(tf: TaskForce, v: SideView, types: dict[str, AircraftType]) -> MoveOrder | None:
    total, working = _carriers(tf)
    if not total:
        return None
    if working * 2 < total:  # retire: away from the nearest known enemy, else toward the nearest friendly field
        if v.contacts:
            c = min(v.contacts, key=lambda c: distance_nm(tf.pos, c.estimated_pos))
            away = (bearing_deg(c.estimated_pos, tf.pos)) % 360
        elif v.airbases:
            away = bearing_deg(tf.pos, min(v.airbases, key=lambda b: distance_nm(tf.pos, b.pos)).pos)
        else:
            return None
        return MoveOrder(tf_id=tf.id, waypoint=destination(tf.pos, away, RETIRE_NM), speed_kn=DASH_KN)
    reach = max((types[k].strike_radius_nm for k, n in tf.air.items()
                 if n > 0 and types[k].role in BOMBER_ROLES), default=0)
    memory = FRESH_MINUTES if v.is_day else NIGHT_MEMORY_MINUTES
    carriers = [c for c in v.contacts if "carrier" in c.description and c.age_minutes <= memory]
    if not reach or not carriers:
        return None
    c = min(carriers, key=lambda c: distance_nm(tf.pos, c.estimated_pos))
    d = distance_nm(tf.pos, c.estimated_pos)
    if d <= reach * RANGE_MARGIN:
        return None  # in range: hold course for the strike and its recovery
    stand_off = reach * CLOSE_TO
    goal = destination(tf.pos, bearing_deg(tf.pos, c.estimated_pos), d - stand_off)
    return MoveOrder(tf_id=tf.id, waypoint=goal, speed_kn=DASH_KN)


def bot_orders(v: SideView) -> Orders:
    o = Orders()
    types = v.aircraft_types
    searching = {m.source_id for m in v.searches}
    # A report we have already struck is spent: strike again only after a newer sighting.
    last_struck: dict[str, datetime] = {}
    for e in v.log:
        if e.kind == "strike_launched" and (cid := e.refs.get("contact")):
            last_struck[cid] = e.time
    targets = sorted((c for c in v.contacts if c.age_minutes <= FRESH_MINUTES
                      and (c.id not in last_struck or c.reported_at > last_struck[c.id])), key=_score, reverse=True)
    carrier_known = any("carrier" in c.description for c in targets)  # a spent report sends the scouts out again
    for tf in v.task_forces:
        if (m := _move(tf, v, types)) is not None:
            o.moves.append(m)
    for u in [*v.task_forces, *v.airbases]:
        if not u.can_operate_aircraft:
            continue
        fighters = {k: n for k, n in u.air.items() if types[k].role == "fighter" and n > 0}
        cap = u.cap
        if isinstance(u, TaskForce) and fighters:
            want = round(sum(fighters.values()) * CAP_SHARE)
            if want != u.cap:
                o.cap.append(CapOrder(unit_id=u.id, fighters=want))
                cap = want
        if not v.is_day:
            continue
        free = dict(fighters)  # engine holds CAP from fighter types in key order
        for k in sorted(free):
            take = min(cap, free[k])
            free[k] -= take
            cap -= take
        struck = False
        for c in targets:
            if (k := _strike(u, c, free, types)) is not None:
                o.strikes.append(k)
                struck = True
                break
        if not struck and not carrier_known and u.id not in searching and (s := _search(u, v, types)):
            o.searches.append(s)
    return o
