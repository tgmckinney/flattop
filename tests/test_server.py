import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from test_engine import staged

from flattop.geo import distance_nm
from flattop.model import EngagementResult, GameState
from flattop.orders import Orders, SearchOrder, StrikeOrder
from flattop.scenario import load_scenario
from flattop.server import create_app

SEARCH = Orders(searches=[SearchOrder(source_id="TF16", aircraft="SBD", count=8, bearing_from=320, bearing_to=40)])


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "t.db")


@pytest.fixture
def api(db) -> TestClient:
    return TestClient(create_app(db))


def new_game(api: TestClient, **cfg) -> dict[str, str]:
    r = api.post("/api/games", json=cfg)
    assert r.status_code == 201, r.text
    return r.json()["seats"]


def put_state(db: str, seat: str, state: GameState) -> None:
    """Test-only: stage a position by overwriting the stored state."""
    with sqlite3.connect(db) as c:
        c.execute("UPDATE games SET state_json = ? WHERE id = (SELECT game_id FROM seats WHERE token = ?)",
                  (state.model_dump_json(), seat))


def submit(api: TestClient, seat: str, orders: Orders | None = None, advance: str = "phase", ok: int = 200) -> dict:
    r = api.post(f"/api/seats/{seat}/orders", json={"orders": (orders or Orders()).model_dump(mode="json"), "advance": advance})
    assert r.status_code == ok, r.text
    return r.json()


def kb_contact_id(body: dict, truth: GameState) -> str:
    """Pick the allied contact nearest Kido Butai's true position."""
    kb = truth.tf("KB").pos

    def miss(c: dict) -> float:
        return distance_nm(kb, type(kb)(**c["estimated_pos"]))

    return min(body["view"]["contacts"], key=miss)["id"]


def test_create_rules(api):
    assert api.get("/api/scenarios").json() == ["eastern_solomons"]
    assert set(new_game(api, mode="umpire", resolver="tabletop")) == {"allied", "japan", "umpire"}
    assert set(new_game(api, mode="solo", solo_side="japan")) == {"japan"}
    assert api.post("/api/games", json={"mode": "two_player", "resolver": "tabletop"}).status_code == 422
    assert api.post("/api/games", json={"scenario": "nope"}).status_code == 404
    assert api.get("/api/seats/not-a-token").status_code == 404


def test_players_get_views_umpire_gets_truth(api):
    seats = new_game(api, mode="umpire")
    us = api.get(f"/api/seats/{seats['allied']}").json()
    assert us["state"] is None and {t["id"] for t in us["view"]["task_forces"]} == {"TF11", "TF16"}
    ump = api.get(f"/api/seats/{seats['umpire']}").json()
    assert ump["view"] is None and len(ump["state"]["task_forces"]) == 5
    assert api.get(f"/api/seats/{seats['allied']}/actions").status_code == 403


def test_two_player_phase_flow(api):
    seats = new_game(api, mode="two_player", phase_minutes=60)
    t0 = api.get(f"/api/seats/{seats['allied']}").json()["view"]["time"]
    bad = Orders(strikes=[StrikeOrder(source_id="TF16", contact_id="A-C1", aircraft={"SBD": 1})])
    submit(api, seats["allied"], bad, ok=422)
    body = submit(api, seats["allied"], advance="run")
    assert body["status"]["submitted"] == ["allied"] and body["view"]["time"] == t0  # waits for Japan
    body = submit(api, seats["japan"], advance="phase")  # the cautious request wins: one phase, not a run
    assert body["status"]["phase"] == 2 and body["status"]["submitted"] == []
    assert body["view"]["time"].endswith("05:00:00")


def test_umpire_tabletop_flow(api, db):
    seats = new_game(api, mode="umpire", resolver="tabletop", phase_minutes=150)
    s = staged(load_scenario("eastern_solomons"))
    put_state(db, seats["umpire"], s)
    submit(api, seats["japan"])
    body = submit(api, seats["allied"], SEARCH)
    cid = kb_contact_id(body, s)
    strike = Orders(strikes=[StrikeOrder(source_id="TF16", contact_id=cid, aircraft={"SBD": 20, "TBF": 10, "F4F": 8})])
    submit(api, seats["allied"], strike, advance="run")
    body = submit(api, seats["japan"], advance="run")
    assert body["status"]["awaiting_umpire"]
    submit(api, seats["allied"], ok=409)
    assert api.get(f"/api/seats/{seats['allied']}/engagements").status_code == 403

    sheets = api.get(f"/api/seats/{seats['umpire']}/engagements").json()
    assert len(sheets) == 1 and "Shokaku" in sheets[0]["sheet_md"]
    eid = sheets[0]["engagement"]["id"]
    bad = EngagementResult(ship_damage={"Enterprise": 1}).model_dump()
    assert api.post(f"/api/seats/{seats['umpire']}/engagements/{eid}", json=bad).status_code == 422
    good = EngagementResult(ship_damage={"Shokaku": 3}, attacker_losses={"SBD": 4}).model_dump()
    assert api.post(f"/api/seats/{seats['allied']}/engagements/{eid}", json=good).status_code == 403
    body = api.post(f"/api/seats/{seats['umpire']}/engagements/{eid}", json=good).json()
    assert not body["status"]["awaiting_umpire"]
    shokaku = next(sh for t in body["state"]["task_forces"] for sh in t["ships"] if sh["name"] == "Shokaku")
    assert shokaku["damage"] == 3

    kinds = [a["kind"] for a in api.get(f"/api/seats/{seats['umpire']}/actions").json()]
    assert kinds[0] == "create" and "resolve" in kinds and kinds.count("advance") == 2
    body = api.post(f"/api/seats/{seats['umpire']}/advance", json={"advance": "phase"}).json()
    assert body["status"]["phase"] == 4


def _strings(x) -> list[str]:
    if isinstance(x, dict):
        return [s for k, v in x.items() for s in [k, *_strings(v)]]
    if isinstance(x, list):
        return [s for v in x for s in _strings(v)]
    return [x] if isinstance(x, str) else []


def test_player_responses_never_leak_enemy_truth(api, db):
    """Every byte the allied seat receives over a full strike cycle, checked for Japanese truth (and vice versa)."""
    seats = new_game(api, mode="two_player", phase_minutes=150)
    s = staged(load_scenario("eastern_solomons"))
    put_state(db, seats["allied"], s)
    seen: dict[str, list[dict]] = {"allied": [], "japan": []}

    def both(us: Orders, adv: str) -> dict:
        seen["allied"].append(submit(api, seats["allied"], us, adv))
        seen["japan"].append(submit(api, seats["japan"], advance=adv))
        seen["allied"].append(api.get(f"/api/seats/{seats['allied']}").json())
        return seen["allied"][-1]

    body = both(SEARCH, "phase")
    cid = kb_contact_id(body, s)
    both(Orders(strikes=[StrikeOrder(source_id="TF16", contact_id=cid, aircraft={"SBD": 20, "TBF": 10, "F4F": 8})]),
         "run")
    body = both(Orders(), "run")
    assert any(e["kind"] == "attack" for e in body["view"]["log"]), "strike should have attacked"
    assert any(e["kind"] == "strike_recovered" for e in body["view"]["log"])

    for side, enemy in (("allied", "japan"), ("japan", "allied")):
        tfs = [t for t in s.task_forces if t.side.value == enemy]
        ids = {t.id for t in tfs} | {b.id for b in s.airbases if b.side.value == enemy}
        names = {sh.name for t in tfs for sh in t.ships} | {t.name for t in tfs}
        strings = _strings(seen[side])
        assert not ids & set(strings), f"{side} saw enemy ids"
        leaked = {n for n in names for x in strings if n in x}
        assert not leaked, f"{side} saw enemy names {leaked}"
        assert "target_id" not in json.dumps(seen[side])


def test_simultaneous_final_submissions_resolve_once(db):
    from concurrent.futures import ThreadPoolExecutor

    from flattop.store import GameConfig, Mode, Store

    store = Store(db)
    games = [store.create(GameConfig(mode=Mode.UMPIRE))[1] for _ in range(8)]
    jobs = [(seats[r], r) for seats in games for r in ("allied", "japan")]
    with ThreadPoolExecutor(16) as pool:
        list(pool.map(lambda j: Store(db).submit(j[0], Orders(), "phase"), jobs))
    for seats in games:
        assert store.seat(seats["umpire"])[3].phase == 2
        assert [a["kind"] for a in store.actions(seats["umpire"])].count("advance") == 1


def test_serves_map_page(api):
    r = api.get("/")
    assert r.status_code == 200 and "maplibre-gl" in r.text
    assert api.get("/app.js").status_code == 200
