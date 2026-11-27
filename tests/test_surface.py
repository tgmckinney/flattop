import random
from datetime import UTC, datetime

import pytest

from flattop.engine import DEFAULT_INTERRUPTS, advance, step, two_player_interrupts
from flattop.geo import destination, distance_nm
from flattop.model import GameState, Side, SurfaceAction
from flattop.resolve import AutoResolver, AutoSurfaceResolver
from flattop.scenario import load_scenario
from flattop.view import view

AUTO = AutoResolver()
US, JP = Side.ALLIED, Side.JAPAN


@pytest.fixture
def night() -> GameState:
    """Two hours after dusk on day one, clear skies, every group stopped."""
    s = advance(load_scenario("eastern_solomons"), resolver=AUTO, stop_on=frozenset({"dusk"})).state
    s = step(s, minutes=120, resolver=AUTO).state.model_copy(deep=True)
    assert not s.is_day()
    s.weather = []
    for tf in s.task_forces:
        tf.waypoint, tf.speed_kn = None, 0
    return s


def place(s: GameState, tan_nm: float, bearing: float = 0) -> GameState:
    """TAN `tan_nm` from TF 16; every other group far away."""
    s = s.model_copy(deep=True)
    tf16 = s.tf("TF16")
    s.tf("TAN").pos = destination(tf16.pos, bearing, tan_nm)
    for tf in s.task_forces:
        if tf.id not in ("TF16", "TAN"):
            tf.pos = destination(tf16.pos, 180 if tf.side is US else 0, 400)
    return s


def kinds(events, side=None) -> list[str]:
    return [e.kind for e in events if side is None or side in e.visible_to]


def test_same_state_same_action(night):
    s = place(night, 3)
    a = step(s, minutes=10, resolver=AUTO)
    b = step(s, minutes=10, resolver=AUTO)
    assert "surface_action" in kinds(a.events)
    assert a.state == b.state and a.events == b.events


def test_groups_passing_inside_one_tick_still_meet(night):
    s = place(night, 5)
    tf16, tan = s.tf("TF16"), s.tf("TAN")
    tf16.waypoint, tf16.speed_kn = destination(tf16.pos, 0, 100), 30
    tan.waypoint, tan.speed_kn = destination(tan.pos, 180, 100), 30
    r = step(s, minutes=10, resolver=AUTO)
    # They end the tick about 5 nm apart, on the far side of each other; the action happened as they passed.
    assert distance_nm(r.state.tf("TF16").pos, r.state.tf("TAN").pos) > 4
    assert kinds(r.events).count("surface_action") == 2  # one report per side


def test_no_contact_no_action(night):
    s = place(night, 3)
    s.tf("TF16").ships = [sh.model_copy(update={"radar_nm": 0}) for sh in s.tf("TF16").ships]
    for side in Side:
        s.doctrine[side].night_lookout_nm = 0
    r = step(s, minutes=60, resolver=AUTO)
    assert not {"surface_contact", "surface_action"} & set(kinds(r.events))


def test_same_groups_fight_once_per_cooldown(night):
    s = place(night, 3)
    r = step(s, minutes=110, resolver=AUTO)
    assert kinds(r.events).count("surface_action") == 2
    r = step(s, minutes=130, resolver=AUTO)
    if r.state.tf("TAN").afloat and r.state.tf("TF16").afloat:
        assert kinds(r.events).count("surface_action") == 4


def test_radar_contact_outside_gun_range(night):
    s = place(night, 9)  # inside TF 16's 10 nm radar, outside 6 nm gun range
    s.doctrine[JP].night_lookout_nm = 4  # and outside Japanese lookouts, even by chance (twice lookout range)
    r = advance(s, resolver=AUTO, max_minutes=60)
    assert kinds(r.events, US) == ["surface_contact"]
    assert "surface_contact" not in kinds(r.events, JP) and r.interrupted
    c = next(c for c in r.state.contacts if c.side is US and c.target_id == "TAN")
    assert c.error_nm <= 1
    # Held in contact, it reports again only after the gap.
    r2 = step(r.state, minutes=50, resolver=AUTO)
    assert "surface_contact" not in kinds(r2.events)


def test_two_player_private_contact_does_not_stop_clock_but_action_does(night):
    s = place(night, 9)
    r = advance(s, resolver=AUTO, max_minutes=60, stop_on=two_player_interrupts())
    assert not r.interrupted and "surface_contact" in kinds(r.events, US)
    s = place(night, 3)
    r = advance(s, resolver=AUTO, max_minutes=60, stop_on=two_player_interrupts())
    assert r.interrupted and r.events[-1].kind in ("surface_action", "ship_sunk", "aircraft_lost")


def test_action_reports_leak_no_enemy_truth(night):
    s = place(night, 3)
    r = step(s, minutes=10, resolver=AUTO)
    us, jp = s.tf("TF16"), s.tf("TAN")
    for side, enemy in ((US, jp), (JP, us)):
        v = view(r.state, side).model_dump_json()
        assert enemy.id not in v and enemy.name not in v
        assert not any(sh.name in v for sh in enemy.ships)


def test_resolver_surprised_side_holds_fire_in_round_one():
    sc = load_scenario("eastern_solomons")
    a = SurfaceAction(id="A1", time=datetime(1942, 8, 24, tzinfo=UTC), pos=sc.tf("TF16").pos, daylight=False,
                      tfs={US: "TF16", JP: "TAN"}, ships={US: sc.tf("TF16").ships, JP: sc.tf("TAN").ships})

    def dealt_to_japan(surprised: list[Side]) -> int:
        act = a.model_copy(update={"surprised": surprised})
        return sum(sum(AutoSurfaceResolver().resolve(act, sc.doctrine, random.Random(i)).ship_damage[JP].values())
                   for i in range(200))

    assert dealt_to_japan([US]) < dealt_to_japan([])


def test_damage_control_fixes_only_repairable_hits_and_restores_deck(night):
    s = night.model_copy(deep=True)
    ent = s.tf("TF16").ships[0]
    assert ent.type == "CV"
    ent.damage, ent.repairable = 3, 2  # deck out at 2
    r = step(s, minutes=24 * 60, resolver=AUTO)
    ent = r.state.tf("TF16").ships[0]
    assert (ent.damage, ent.repairable) == (1, 0) and not ent.deck_out
    assert any("flight deck is in use again" in e.text for e in r.events if e.kind == "repaired")


def test_sunk_ship_is_never_repaired(night):
    s = night.model_copy(deep=True)
    dd = next(sh for sh in s.tf("TF16").ships if sh.type == "DD")
    dd.damage, dd.repairable = 2, 1  # capacity 2: sunk
    r = step(s, minutes=12 * 60, resolver=AUTO)
    assert next(sh for sh in r.state.tf("TF16").ships if sh.name == dd.name).damage == 2


def test_default_interrupts_include_surface_events():
    assert {"surface_contact", "surface_action"} <= DEFAULT_INTERRUPTS
