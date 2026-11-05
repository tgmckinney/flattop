import sqlite3

from test_engine import AUTO, JP, US, staged

from flattop.bot import bot_orders
from flattop.engine import DEFAULT_INTERRUPTS, apply_orders, step
from flattop.geo import destination
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
