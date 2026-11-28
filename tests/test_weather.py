import json
from datetime import timedelta

import pytest
from test_engine import AUTO, JP, US, staged

from flattop.bot import bot_orders
from flattop.engine import advance, search_p
from flattop.geo import Pos, destination, distance_nm
from flattop.model import Contact, Front, GameState
from flattop.orders import Orders, SearchOrder, StrikeOrder
from flattop.scenario import load_scenario
from flattop.store import GameConfig, Store
from flattop.view import view
from flattop.weather import generate_weather, play_area


def front_over(s: GameState, tf_id: str, **kw) -> Front:
    return Front(id="WX", name="Test front", pos=s.tf(tf_id).pos, at=s.time, radius_nm=60, **kw)


def test_front_drifts_and_expires():
    s = load_scenario("eastern_solomons")
    f = Front(id="W", name="w", pos=Pos(lat=-8, lon=160), at=s.time, radius_nm=50, drift_deg=90, drift_kn=10,
              end=s.time + timedelta(hours=12))
    assert distance_nm(f.centre(s.time + timedelta(hours=3)), destination(f.pos, 90, 30)) < 0.1
    assert f.centre(s.time + timedelta(hours=12)) is None
    assert f.covers(destination(f.pos, 0, 49), s.time) and not f.covers(destination(f.pos, 0, 51), s.time)


def test_scenario_weather_is_public_and_old_saves_still_load():
    s = load_scenario("eastern_solomons")
    assert view(s, US).weather == view(s, JP).weather and view(s, US).weather
    old = json.loads(s.model_dump_json())
    del old["weather"]
    assert GameState.model_validate(old).weather == []


def test_cloud_cuts_search_chance():
    s = staged(load_scenario("eastern_solomons"))
    kb = s.tf("KB")
    assert search_p(s, 0.8, kb) == 0.8
    s.weather = [front_over(s, "KB", search_factor=0.25)]
    assert search_p(s, 0.8, kb) == pytest.approx(0.2)


def test_search_cannot_see_into_solid_cloud():
    s = staged(load_scenario("eastern_solomons"))
    s.weather = [front_over(s, "KB", search_factor=0.0)]
    o = {US: Orders(searches=[SearchOrder(source_id="TF16", aircraft="SBD", count=8, bearing_from=320, bearing_to=40)])}
    r = advance(s, o, resolver=AUTO, stop_on=frozenset(), max_minutes=150)
    assert not any(c.target_id == "KB" for c in r.state.contacts), "Kido Butai is under solid cloud"


def _strike_on_offset_report(s: GameState) -> list:
    """TF 16 strikes a report of Kido Butai 20 nm off its true position."""
    kb = s.tf("KB")
    s.contacts.append(Contact(id="A-C9", side=US, target_id="KB", reported_at=s.time,
                              reported_pos=destination(kb.pos, 90, 20), reported_course=0, reported_speed_kn=0,
                              error_nm=20, description="2 carriers"))
    o = {US: Orders(strikes=[StrikeOrder(source_id="TF16", contact_id="A-C9", aircraft={"SBD": 12})])}
    return advance(s, o, resolver=AUTO, stop_on=frozenset({"attack", "strike_missed"}), max_minutes=180).events


def test_strike_misses_a_target_under_cloud():
    s = staged(load_scenario("eastern_solomons"))
    assert any(e.kind == "attack" for e in _strike_on_offset_report(s.model_copy(deep=True))), "clear: 20 nm is close enough"
    s.weather = [front_over(s, "KB", acquire_nm=10)]
    kinds = [e.kind for e in _strike_on_offset_report(s)]
    assert "strike_missed" in kinds and "attack" not in kinds


def test_weather_does_not_leak_enemy_positions():
    s = staged(load_scenario("eastern_solomons"))
    s.weather = [Front(id="WX", name="w", pos=destination(s.tf("TF16").pos, 90, 100), at=s.time, radius_nm=60)]
    moved = s.model_copy(deep=True)
    moved.tf("TF16").pos = destination(moved.tf("TF16").pos, 90, 100)  # now under the front, unseen
    assert view(s, JP) == view(moved, JP)
    assert bot_orders(view(s, JP)) == bot_orders(view(moved, JP))


def test_scenario_has_weather_every_hour():
    s = load_scenario("eastern_solomons")
    t = s.time
    while t <= s.end:
        assert any(f.centre(t) for f in s.weather), f"no weather at {t}"
        t += timedelta(hours=1)


def test_generated_weather_is_seeded_and_inside_area_and_time():
    s = load_scenario("eastern_solomons")
    a = generate_weather(s, 7)
    assert a == generate_weather(s, 7) and a != generate_weather(s, 8)
    area = play_area(s)
    assert len(a) == round(3 * (s.end - s.time).total_seconds() / 86400)
    for f in a:
        assert area.south <= f.pos.lat <= area.north and area.west <= f.pos.lon <= area.east
        assert s.time <= f.at < s.end and (f.start is None or f.start == f.at)
        assert f.end is None or f.at < f.end < s.end
        assert 40 <= f.radius_nm <= 120 and 3 <= f.drift_kn <= 10 and 0 < f.search_factor < 1
    assert len({f.id for f in a}) == len(a)


def test_prevailing_drift_is_respected():
    fronts = generate_weather(load_scenario("eastern_solomons"), 3, prevailing_deg=290)
    assert all(min(abs(f.drift_deg - 290), 360 - abs(f.drift_deg - 290)) <= 41 for f in fronts)


def test_store_random_weather_game(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    _, seats = store.create(GameConfig(mode="umpire", weather="random"))
    _, _, truth, _ = store.seat(seats["umpire"])
    assert truth.weather and all(f.id.startswith("R") for f in truth.weather)
    views = [view(truth, side).weather for side in (US, JP)]
    assert views[0] == views[1]
    create = store.actions(seats["umpire"])[0]
    assert create["kind"] == "create" and generate_weather(load_scenario("eastern_solomons"),
                                                           create["payload_json"]["weather_seed"]) == truth.weather
