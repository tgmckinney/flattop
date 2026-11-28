import sqlite3

import pytest
from test_engine import AUTO, JP, US, staged

from flattop.bot import bot_orders
from flattop.engine import DEFAULT_INTERRUPTS, apply_orders, step
from flattop.geo import destination, distance_nm
from flattop.model import GameState
from flattop.orders import Orders
from flattop.scenario import load_scenario
from flattop.store import GameConfig, Store
from flattop.view import view


def play_japan(s: GameState, hours: int) -> tuple[GameState, list]:
    events = []
    for _ in range(hours):
        o = bot_orders(view(s, JP))
        apply_orders(s.model_copy(deep=True), JP, o, [])  # every bot order must be legal
        r = step(s, {JP: o}, minutes=60, resolver=AUTO)
        events += r.events
        s = r.state
    return s, events


def test_bot_finds_and_strikes():
    s, events = play_japan(staged(load_scenario("eastern_solomons")), 6)
    kinds = [e.kind for e in events if JP in e.visible_to]
    assert "sighting" in kinds and "strike_launched" in kinds
    assert kinds.index("sighting") < kinds.index("strike_launched")
    assert any(e.kind == "attack" and US in e.visible_to for e in events), "US carriers should come under attack"
    assert s.tf("KB").cap > 0


def test_bot_is_blind_to_enemy_truth():
    s = staged(load_scenario("eastern_solomons"))
    moved = s.model_copy(deep=True)
    moved.tf("TF16").pos = destination(moved.tf("TF16").pos, 90, 80)  # unseen, so it must not matter
    assert view(s, JP) == view(moved, JP)
    assert bot_orders(view(s, JP)) == bot_orders(view(moved, JP))


def test_bot_does_not_restrike_a_spent_report():
    s, events = play_japan(staged(load_scenario("eastern_solomons")), 10)
    launches = [(e.time, e.refs["contact"]) for e in events if e.kind == "strike_launched" and JP in e.visible_to]
    reported = {c.id: [] for c in s.contacts if c.side is JP}
    for e in events:
        if e.kind == "sighting" and JP in e.visible_to:
            reported[e.refs["contact"]].append(e.time)
    for i, (t, cid) in enumerate(launches):
        earlier = [t0 for t0, c in launches[:i] if c == cid and t0 < t]  # same-phase launches from two carriers are fine
        if earlier:
            assert any(max(earlier) < r <= t for r in reported[cid]), f"{cid} struck again at {t} without a new report"


def test_solo_game_runs_the_bot(tmp_path):
    db = tmp_path / "t.db"
    store = Store(db)
    _, seats = store.create(GameConfig(mode="solo", solo_side="allied"))
    with sqlite3.connect(db) as c:
        c.execute("UPDATE games SET state_json = ?", (staged(load_scenario("eastern_solomons")).model_dump_json(),))
    for _ in range(4):
        store.submit(seats["allied"], Orders(), "phase")
    _, _, state, status = store.seat(seats["allied"])
    assert status.phase == 5
    assert any(e.kind == "attack" and US in e.visible_to for e in state.log)
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT count(*) FROM actions WHERE kind = 'bot' AND role = 'japan'").fetchone()[0] >= 4


def test_solo_run_bot_acts_without_leaking_its_timing(tmp_path):
    """One 'run' submission: the bot re-plans on its private events, but the player's clock only
    stops on something the player can see."""
    db = tmp_path / "t.db"
    store = Store(db)
    _, seats = store.create(GameConfig(mode="solo", solo_side="allied"))
    with sqlite3.connect(db) as c:
        c.execute("UPDATE games SET state_json = ?", (staged(load_scenario("eastern_solomons")).model_dump_json(),))
    seen_attack = False
    for _ in range(6):  # each run returns at the next allied-visible interrupt
        store.submit(seats["allied"], Orders(), "run")
        _, _, s, _ = store.seat(seats["allied"])
        last_tick = [e for e in s.log if e.time == s.time]
        assert any(US in e.visible_to and e.kind in DEFAULT_INTERRUPTS for e in last_tick) or s.time >= s.end, \
            f"clock stopped at {s.time} with nothing the player can see"
        seen_attack |= any(e.kind == "attack" and US in e.visible_to for e in s.log)
    assert seen_attack, "the bot should strike during a run"
    with sqlite3.connect(db) as c:
        bot = c.execute("SELECT count(*) FROM actions WHERE kind = 'bot'").fetchone()[0]
    assert bot > 6, "the bot should re-plan mid-run, not only at submissions"


def _with_carrier_contact(s: GameState) -> GameState:
    from flattop.model import Contact

    tf16 = s.tf("TF16")
    s.contacts.append(Contact(id="J-C1", side=JP, target_id="TF16", reported_at=s.time, reported_pos=tf16.pos,
                              reported_course=0, reported_speed_kn=0, error_nm=5, description="1 carrier, 9 warships"))
    return s


def _kb_move(s: GameState):
    return next(m for m in bot_orders(view(s, JP)).moves if m.tf_id == "KB")


def test_bot_closes_on_a_carrier_out_of_range():
    s = _with_carrier_contact(staged(load_scenario("eastern_solomons"), dist_nm=400))
    m = _kb_move(s)
    tf16 = s.tf("TF16").pos
    assert m.speed_kn > 20
    assert distance_nm(m.waypoint, tf16) == pytest.approx(0.8 * 250, abs=5)  # stand off at 80% of D3A/B5N radius


def test_bot_holds_when_in_range_and_retires_when_decks_are_out():
    s = _with_carrier_contact(staged(load_scenario("eastern_solomons"), dist_nm=150))
    assert not [m for m in bot_orders(view(s, JP)).moves if m.tf_id == "KB"]
    for ship in s.tf("KB").ships:
        if ship.type == "CV":
            ship.damage = 2  # flight decks out
    m = _kb_move(s)
    kb, tf16 = s.tf("KB").pos, s.tf("TF16").pos
    assert distance_nm(m.waypoint, tf16) > distance_nm(kb, tf16) + 250, "should run away from the enemy carrier"


def test_snooper_warns_the_found_side_and_steers_bot_searches():
    from flattop.orders import SearchOrder

    s = staged(load_scenario("eastern_solomons"))
    us = {US: Orders(searches=[SearchOrder(source_id="TF16", aircraft="SBD", count=8, bearing_from=320, bearing_to=40)])}
    snoops = []
    for seed in range(1, 6):  # the spot roll is random; across a few seeds it must happen
        s2 = s.model_copy(update={"seed": seed}, deep=True)
        r = step(s2, us, minutes=150, resolver=AUTO)
        snoops += [(r.state, e) for e in r.events if e.kind == "snooper"]
    assert snoops, "Kido Butai should spot at least one US scout"
    state, e = snoops[0]
    assert e.visible_to == [JP] and "from the south" in e.text
    assert e in view(state, JP).log and e not in view(state, US).log
    axis = float(e.refs["bearing"])
    jp = view(state, JP).model_copy(update={"contacts": []})  # no contacts of its own: the snooper is all it has
    searches = bot_orders(jp).searches
    assert searches
    for o in searches:
        mid = (o.bearing_from + ((o.bearing_to - o.bearing_from) % 360) / 2) % 360
        assert abs((mid - axis + 180) % 360 - 180) <= 1


# --- weather ------------------------------------------------------------------------------------

def _front(s: GameState, centre, radius_nm=60, **kw):
    from flattop.model import Front

    return Front(id="WX", name="Test front", pos=centre, at=s.time, radius_nm=radius_nm, **kw)


def test_threatened_bot_hides_under_nearby_cloud_within_strike_range():
    s = _with_carrier_contact(staged(load_scenario("eastern_solomons"), dist_nm=150))
    s.weather = [_front(s, destination(s.tf("KB").pos, 270, 100))]
    m = _kb_move(s)
    assert distance_nm(m.waypoint, s.weather[0].pos) < 1
    assert distance_nm(m.waypoint, s.tf("TF16").pos) <= 250 * 0.95, "must stay in strike range of the target"
    s.weather = [_front(s, destination(s.tf("KB").pos, 0, 150))]  # cloud out of range of the target: stay put
    assert not [m for m in bot_orders(view(s, JP)).moves if m.tf_id == "KB"]


def test_unthreatened_bot_ignores_cloud():
    s = staged(load_scenario("eastern_solomons"))
    s.weather = [_front(s, destination(s.tf("KB").pos, 270, 100))]
    assert not [m for m in bot_orders(view(s, JP)).moves if m.tf_id == "KB"]


def test_hidden_bot_keeps_pace_with_a_drifting_front():
    s = _with_carrier_contact(staged(load_scenario("eastern_solomons"), dist_nm=150))
    s.weather = [_front(s, s.tf("KB").pos, drift_deg=90, drift_kn=10)]
    m = _kb_move(s)
    assert distance_nm(m.waypoint, destination(s.tf("KB").pos, 90, 30)) < 1 and m.speed_kn == pytest.approx(10, abs=0.2)


def test_crippled_bot_retires_into_cloud_on_the_way_out():
    s = _with_carrier_contact(staged(load_scenario("eastern_solomons"), dist_nm=150))
    for ship in s.tf("KB").ships:
        if ship.type == "CV":
            ship.damage = 2
    away = destination(s.tf("KB").pos, 0, 120)  # north: away from TF 16
    s.weather = [_front(s, away), _front(s, destination(s.tf("KB").pos, 180, 60))]  # the nearer one is toward the enemy
    s.weather[1].id = "WY"
    assert distance_nm(_kb_move(s).waypoint, away) < 1


def test_bot_does_not_strike_an_old_report_under_cloud_and_searches_it_again():
    from datetime import timedelta

    s = _with_carrier_contact(staged(load_scenario("eastern_solomons"), dist_nm=150))
    c = s.contacts[0]
    c.reported_at = s.time - timedelta(minutes=60)
    s.weather = [_front(s, c.reported_pos)]
    o = bot_orders(view(s, JP))
    assert not o.strikes
    assert any(abs(_mid(x) - 180) <= 1 for x in o.searches if x.source_id == "KB"), \
        "should search back toward the lost contact (due south)"
    s.weather = []
    assert bot_orders(view(s, JP)).strikes, "in clear weather the same report is struck"


def _mid(x) -> float:
    return (x.bearing_from + ((x.bearing_to - x.bearing_from) % 360) / 2) % 360


def test_bot_searches_toward_cloud_with_no_other_lead():
    s = staged(load_scenario("eastern_solomons"))
    kb = s.tf("KB").pos
    s.weather = [_front(s, destination(kb, 60, 150))]
    o = bot_orders(view(s, JP))
    x = next(x for x in o.searches if x.source_id == "KB")
    assert abs(_mid(x) - 60) <= 1.5


# --- bot v3: surface groups and night approach -------------------------------------------------


def _night(hours_after_dusk: int = 2) -> GameState:
    """Clear night on day one, every group stopped, TF 11 and RYU far away."""
    from flattop.engine import advance

    s = advance(load_scenario("eastern_solomons"), resolver=AUTO, stop_on=frozenset({"dusk"})).state
    s = step(s, minutes=60 * hours_after_dusk, resolver=AUTO).state.model_copy(deep=True)
    s.weather = []
    for tf in s.task_forces:
        tf.waypoint, tf.speed_kn = None, 0
    s.tf("TF11").pos = destination(s.tf("TF16").pos, 180, 500)
    s.tf("RYU").pos = destination(s.tf("TF16").pos, 0, 600)
    return s


def _contact(s: GameState, side, target: str, desc: str) -> GameState:
    from flattop.model import Contact

    t = s.tf(target)
    s.contacts.append(Contact(id=f"{side.value[0].upper()}-C9", side=side, target_id=target, reported_at=s.time,
                              reported_pos=t.pos, reported_course=0, reported_speed_kn=0, error_nm=2, description=desc))
    return s


def _move_of(s: GameState, side, tf_id: str):
    return next((m for m in bot_orders(view(s, side)).moves if m.tf_id == tf_id), None)


def _warships_only(s: GameState, dist_nm: float) -> GameState:
    tan = s.tf("TAN")
    tan.ships = [sh for sh in tan.ships if sh.type != "AP"]
    tan.pos = destination(s.tf("TF16").pos, 0, dist_nm)
    return _contact(s, JP, "TF16", "1 carrier, 9 warships")


def test_surface_group_intercepts_at_night_what_it_can_reach_before_dawn():
    s = _warships_only(_night(), 120)
    m = _move_of(s, JP, "TAN")
    assert m.speed_kn == 30 and distance_nm(m.waypoint, s.tf("TF16").pos) < 5
    far = _warships_only(_night(), 400)  # 400 nm at 30 kn is past dawn
    assert _move_of(far, JP, "TAN") is None


def test_transport_group_runs_for_its_objective_at_night():
    s = _night()
    tan = s.tf("TAN")
    assert tan.objective is not None
    m = _move_of(s, JP, "TAN")
    assert m.waypoint == tan.objective and m.speed_kn == 10


def test_transport_group_turns_away_from_carriers_by_day_and_resumes_when_clear():
    s = staged(load_scenario("eastern_solomons"))
    s.tf("TAN").pos = destination(s.tf("TF16").pos, 0, 150)
    assert _move_of(s, JP, "TAN").waypoint == s.tf("TAN").objective  # no contact: keep the mission
    s = _contact(s, JP, "TF16", "1 carrier, 9 warships")
    m = _move_of(s, JP, "TAN")
    assert distance_nm(m.waypoint, s.tf("TF16").pos) == pytest.approx(250 + 30, abs=3)


def test_crippled_surface_group_retires():
    s = _warships_only(_night(), 120)
    for sh in s.tf("TAN").ships[:3]:
        sh.damage = 1
    m = _move_of(s, JP, "TAN")
    tan, tf16 = s.tf("TAN").pos, s.tf("TF16").pos
    assert distance_nm(m.waypoint, tf16) > distance_nm(tan, tf16) + 250


def test_carrier_group_avoids_enemy_ships_at_night():
    s = _night()
    s.tf("KB").pos = destination(s.tf("TF16").pos, 0, 40)
    s = _contact(s, JP, "TF16", "1 carrier, 9 warships")
    m = _move_of(s, JP, "KB")
    kb, tf16 = s.tf("KB").pos, s.tf("TF16").pos
    assert distance_nm(m.waypoint, tf16) > distance_nm(kb, tf16) + 90


def test_surface_bot_is_blind_to_enemy_truth():
    s = _warships_only(_night(), 120)
    moved = s.model_copy(deep=True)
    moved.tf("TF11").pos = destination(moved.tf("TF11").pos, 90, 80)  # unseen
    assert bot_orders(view(s, JP)) == bot_orders(view(moved, JP))
