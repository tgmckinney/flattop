from datetime import datetime

import pytest

from flattop.engine import DEFAULT_INTERRUPTS, EngineError, advance, resolve_pending, step, two_player_interrupts
from flattop.geo import Pos, destination, distance_nm
from flattop.model import EngagementResult, GameState, Side
from flattop.orders import CapOrder, OrderError, Orders, SearchOrder, StrikeOrder
from flattop.resolve import AutoResolver, TabletopResolver
from flattop.scenario import load_scenario
from flattop.sheet import engagement_sheet
from flattop.view import view

AUTO = AutoResolver()
US, JP = Side.ALLIED, Side.JAPAN


@pytest.fixture
def s0() -> GameState:
    return load_scenario("eastern_solomons")


def to_dawn(s: GameState) -> GameState:
    r = advance(s, resolver=AUTO, stop_on=frozenset({"dawn"}))
    assert r.state.is_day()
    return r.state


def staged(s: GameState, dist_nm: float = 150) -> GameState:
    """Kido Butai stopped `dist_nm` due north of TF 16 at dawn."""
    s = to_dawn(s).model_copy(deep=True)
    kb, tf16 = s.tf("KB"), s.tf("TF16")
    kb.pos, kb.waypoint, kb.speed_kn = destination(tf16.pos, 0, dist_nm), None, 0
    for tf in s.task_forces:
        tf.waypoint, tf.speed_kn = None, 0
    return s


def search_north(s: GameState) -> GameState:
    o = {US: Orders(searches=[SearchOrder(source_id="TF16", aircraft="SBD", count=8, bearing_from=320, bearing_to=40)])}
    r = advance(s, o, resolver=AUTO, stop_on=frozenset(), max_minutes=150)
    assert any(c.target_id == "KB" for c in r.state.contacts), "search should find Kido Butai at 150 nm"
    return r.state


def kb_contact(s: GameState) -> str:
    """Umpire lookup: the allied contact that is really Kido Butai."""
    return next(c.id for c in s.contacts if c.side is US and c.target_id == "KB")


def test_geo_roundtrip():
    a = Pos(lat=-9.43, lon=160.05)
    b = destination(a, 45, 200)
    assert distance_nm(a, b) == pytest.approx(200, abs=0.1)


def test_step_is_deterministic(s0):
    o = {US: Orders(cap=[CapOrder(unit_id="TF16", fighters=12)])}
    a = step(s0, o, minutes=24 * 60, resolver=AUTO)
    b = step(s0, o, minutes=24 * 60, resolver=AUTO)
    assert a.state.model_dump_json() == b.state.model_dump_json()


def test_step_does_not_mutate_input(s0):
    before = s0.model_dump_json()
    step(s0, minutes=120, resolver=AUTO)
    assert s0.model_dump_json() == before


def test_movement_and_time(s0):
    r = step(s0, minutes=60, resolver=AUTO)
    assert r.state.time == datetime(1942, 8, 23, 5, 0)
    assert distance_nm(s0.tf("TF11").pos, r.state.tf("TF11").pos) == pytest.approx(12, abs=0.2)


def test_advance_stops_at_dawn(s0):
    r = advance(s0, resolver=AUTO)
    assert r.interrupted and r.events[-1].kind == "dawn"
    assert r.state.time.strftime("%H:%M") == "06:10"


def test_orders_validated(s0):
    with pytest.raises(OrderError, match="daylight"):
        step(s0, {US: Orders(searches=[SearchOrder(source_id="TF16", aircraft="SBD", count=4, bearing_from=0, bearing_to=90)])},
             minutes=10, resolver=AUTO)
    with pytest.raises(OrderError, match="no such allied unit"):
        step(s0, {US: Orders(cap=[CapOrder(unit_id="KB", fighters=4)])}, minutes=10, resolver=AUTO)
    with pytest.raises(OrderError, match="CAP requested"):
        step(s0, {US: Orders(cap=[CapOrder(unit_id="TF16", fighters=99)])}, minutes=10, resolver=AUTO)


def test_player_view_hides_truth(s0):
    s = search_north(staged(s0))
    v = view(s, US)
    assert v.contacts, "allied side should hold a contact"
    blob = v.model_dump_json()
    for tf in s.task_forces:
        if tf.side is JP:
            assert tf.id not in blob
            assert tf.name not in blob
            for ship in tf.ships:
                assert ship.name not in blob
            assert f"{tf.pos.lat}" not in blob
    # Japan sees nothing of the allied side's private picture either.
    assert "TF16" not in view(s, JP).model_dump_json()


def test_contact_uncertainty_grows(s0):
    s = search_north(staged(s0))
    c0 = view(s, US).contacts[0]
    later = step(s, minutes=120, resolver=AUTO).state
    c1 = next(c for c in view(later, US).contacts if c.id == c0.id)
    assert c1.uncertainty_nm > c0.uncertainty_nm


def test_full_strike_cycle_auto(s0):
    s = search_north(staged(s0))
    cid = kb_contact(s)
    ready_before = sum(s.tf("TF16").air.values())
    o = {
        US: Orders(strikes=[StrikeOrder(source_id="TF16", contact_id=cid, aircraft={"F4F": 8, "SBD": 16, "TBF": 10})]),
        JP: Orders(cap=[CapOrder(unit_id="KB", fighters=20)]),
    }
    r = advance(s, o, resolver=AUTO, stop_on=frozenset({"strike_recovered"}), max_minutes=8 * 60)
    kinds = [e.kind for e in r.events]
    assert "strike_launched" in kinds and "attack" in kinds and "strike_recovered" in kinds
    assert not r.state.strikes
    kb = r.state.tf("KB")
    lost = sum(r.state.tf("TF16").air.values()) < ready_before
    damaged = any(sh.damage for sh in kb.ships)
    assert lost or damaged


def test_strike_out_of_range_rejected(s0):
    s = search_north(staged(s0))
    cid = kb_contact(s)
    s.tf("KB").pos = destination(s.tf("TF16").pos, 0, 150)  # truth irrelevant; contact drives the check
    far = s.model_copy(deep=True)
    next(c for c in far.contacts if c.id == cid).reported_pos = destination(far.tf("TF16").pos, 0, 400)
    with pytest.raises(OrderError, match="strike radius"):
        step(far, {US: Orders(strikes=[StrikeOrder(source_id="TF16", contact_id=cid, aircraft={"SBD": 10})])},
             minutes=10, resolver=AUTO)


def test_tabletop_pauses_and_resumes(s0):
    s = search_north(staged(s0))
    cid = kb_contact(s)
    o = {US: Orders(strikes=[StrikeOrder(source_id="TF16", contact_id=cid, aircraft={"SBD": 12, "TBF": 6})])}
    r = advance(s, o, resolver=TabletopResolver(), stop_on=frozenset(), max_minutes=8 * 60)
    assert r.interrupted and len(r.state.pending) == 1
    e = r.state.pending[0]
    assert "Shokaku" in engagement_sheet(e, r.state.aircraft_types)
    assert view(r.state, US).awaiting_umpire
    with pytest.raises(EngineError):
        step(r.state, minutes=10, resolver=TabletopResolver())

    r2 = resolve_pending(r.state, e.id, EngagementResult(attacker_losses={"SBD": 3}, ship_damage={"Shokaku": 6}))
    assert any(sh.name == "Shokaku" and sh.sunk for sh in r2.state.tf("KB").ships)
    r3 = advance(r2.state, resolver=TabletopResolver(), stop_on=frozenset({"strike_recovered"}), max_minutes=6 * 60)
    assert any(e.kind == "strike_recovered" for e in r3.events)
    assert not r3.state.searches  # search SBDs are home too
    assert r3.state.tf("TF16").air["SBD"] == s0.tf("TF16").air["SBD"] - 3


def test_default_interrupts_cover_player_decisions():
    assert {"sighting", "attack", "dawn", "dusk"} <= DEFAULT_INTERRUPTS


def test_two_player_private_events_do_not_stop_clock(s0):
    s = staged(s0)
    o = {US: Orders(searches=[SearchOrder(source_id="TF16", aircraft="SBD", count=8, bearing_from=320, bearing_to=40)])}
    solo = advance(s, o, resolver=AUTO, max_minutes=150)
    assert solo.interrupted and solo.events[-1].kind == "sighting"
    duo = advance(s, o, resolver=AUTO, max_minutes=150, stop_on=two_player_interrupts())
    assert not duo.interrupted, "an allied-only sighting must not stop the shared clock"
    assert any(e.kind == "sighting" for e in duo.events)


def test_tabletop_result_validated(s0):
    s = search_north(staged(s0))
    o = {US: Orders(strikes=[StrikeOrder(source_id="TF16", contact_id=kb_contact(s), aircraft={"SBD": 12})])}
    r = advance(s, o, resolver=TabletopResolver(), stop_on=frozenset(), max_minutes=8 * 60)
    eid = r.state.pending[0].id
    for bad in (
        EngagementResult(ship_damage={"Shokakku": 2}),
        EngagementResult(attacker_losses={"SBD": 13}),
        EngagementResult(attacker_losses={"TBF": 1}),
    ):
        with pytest.raises(EngineError, match="rejected"):
            resolve_pending(r.state, eid, bad)
