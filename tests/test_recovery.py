from test_engine import US, staged

from flattop.engine import _land, deck_space
from flattop.geo import destination
from flattop.scenario import load_scenario


def test_hangars_fit_the_scenario():
    s = load_scenario("eastern_solomons")
    for tf in s.task_forces:
        assert deck_space(s, tf) >= 0, tf.id


def test_overflow_diverts_to_nearest_deck_with_room_then_ditches():
    s = staged(load_scenario("eastern_solomons"))
    tf16, tf11 = s.tf("TF16"), s.tf("TF11")
    tf11.pos = destination(tf16.pos, 270, 30)
    room16, room11 = deck_space(s, tf16), deck_space(s, tf11)
    for b in s.airbases:  # keep shore fields out of reach
        b.pos = destination(tf16.pos, 180, 900)
    out = []
    _land(s, US, "TF16", tf16.pos, {"SBD": int(room16 + room11 + 5)}, "Strike X", "strike_recovered", out)
    assert deck_space(s, tf16) == 0 and deck_space(s, tf11) == 0
    text = " ".join(e.text for e in out)
    assert f"{int(room16)} recovered" in text and f"{int(room11)} diverted to {tf11.name}" in text
    assert any(e.kind == "aircraft_lost" and "5 aircraft lost" in e.text for e in out)


def test_floatplanes_need_no_deck_space():
    s = staged(load_scenario("eastern_solomons"))
    tf16 = s.tf("TF16")
    tf16.air["SBD"] += int(deck_space(s, tf16))  # deck full
    out = []
    _land(s, US, "TF16", tf16.pos, {"SOC": 2}, "Search X", "search_returned", out)
    assert [e.kind for e in out] == ["search_returned"] and deck_space(s, tf16) == 0


def test_deck_out_carrier_halves_kido_butai_hangar():
    s = load_scenario("eastern_solomons")
    kb = s.tf("KB")
    before = kb.hangar
    kb.ships[0].damage = 2
    assert kb.hangar == before // 2 and kb.can_operate_aircraft
