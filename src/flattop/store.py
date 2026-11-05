"""Game sessions in SQLite: seats, phase submissions, and an append-only action log.

Each seat is a secret token bound to one game and one role. The engine stays pure; this module decides
who must submit, when the clock runs, and records every state-changing action for replay.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from flattop.bot import WAKE_ON as BOT_WAKE_ON
from flattop.bot import bot_orders
from flattop.engine import (
    DEFAULT_INTERRUPTS,
    EngineError,
    Interrupts,
    StepResult,
    apply_orders,
    resolve_pending,
    step,
    two_player_interrupts,
)
from flattop.model import EngagementResult, GameState, Side
from flattop.orders import OrderError, Orders
from flattop.resolve import AutoResolver, Resolver, TabletopResolver
from flattop.scenario import load_scenario
from flattop.view import view

RUN_MAX_MINUTES = 24 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id TEXT PRIMARY KEY,
    config_json TEXT NOT NULL,
    phase INTEGER NOT NULL,
    state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seats (
    token TEXT PRIMARY KEY,
    game_id TEXT NOT NULL REFERENCES games(id),
    role TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
    game_id TEXT NOT NULL REFERENCES games(id),
    phase INTEGER NOT NULL,
    role TEXT NOT NULL,
    orders_json TEXT NOT NULL,
    advance TEXT NOT NULL,
    PRIMARY KEY (game_id, phase, role)
);
CREATE TABLE IF NOT EXISTS actions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL REFERENCES games(id),
    at TEXT NOT NULL,
    role TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


class Mode(StrEnum):
    SOLO = "solo"  # one player; `flattop.bot` commands the other side from its own view
    UMPIRE = "umpire"  # two players plus an umpire who sees everything and resolves tabletop battles
    TWO_PLAYER = "two_player"  # two players, no umpire; auto-resolve only


Role = Literal["allied", "japan", "umpire"]
Advance = Literal["phase", "run"]


class GameConfig(BaseModel):
    scenario: str = "eastern_solomons"
    mode: Mode = Mode.SOLO
    resolver: Literal["auto", "tabletop"] = "auto"
    solo_side: Side = Side.ALLIED
    phase_minutes: int = Field(default=60, ge=10, le=24 * 60, multiple_of=10)


class SeatStatus(BaseModel):
    game_id: str
    role: Role
    mode: Mode
    resolver: str
    phase: int
    phase_minutes: int
    must_submit: list[Role]
    submitted: list[Role]
    awaiting_umpire: bool
    can_resolve: bool
    finished: bool


class StoreError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Write transaction. BEGIN IMMEDIATE serialises writers, so a phase resolves exactly once."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    # --- rules ------------------------------------------------------------------------------------

    @staticmethod
    def roles(cfg: GameConfig) -> list[Role]:
        if cfg.mode is Mode.SOLO:
            return [cfg.solo_side.value]  # type: ignore[list-item]
        if cfg.mode is Mode.TWO_PLAYER:
            return ["allied", "japan"]
        return ["allied", "japan", "umpire"]

    @staticmethod
    def must_submit(cfg: GameConfig) -> list[Role]:
        return [r for r in Store.roles(cfg) if r != "umpire"]

    @staticmethod
    def can_resolve(cfg: GameConfig, role: Role) -> bool:
        """Who may see engagement sheets and enter tabletop results. Sheets reveal the target's ships."""
        return cfg.resolver == "tabletop" and (role == "umpire" or cfg.mode is Mode.SOLO)

    @staticmethod
    def interrupts(cfg: GameConfig) -> Interrupts:
        # Only events a player can see may stop their clock: anything else would leak through timing.
        # Two hidden players share a clock, so only public events stop it. In solo the bot's private
        # events also stop the engine, but `_run` re-plans and carries on without returning to the player.
        if cfg.mode is Mode.SOLO:
            return {cfg.solo_side: DEFAULT_INTERRUPTS, cfg.solo_side.enemy: BOT_WAKE_ON}
        return two_player_interrupts()

    @staticmethod
    def resolver(cfg: GameConfig) -> Resolver:
        return TabletopResolver() if cfg.resolver == "tabletop" else AutoResolver()

    # --- reads ------------------------------------------------------------------------------------

    def _load(self, c: sqlite3.Connection, token: str) -> tuple[str, Role, GameConfig, int, GameState]:
        row = c.execute(
            "SELECT s.game_id, s.role, g.config_json, g.phase, g.state_json "
            "FROM seats s JOIN games g ON g.id = s.game_id WHERE s.token = ?",
            (token,),
        ).fetchone()
        if row is None:
            raise StoreError(404, "unknown seat")
        cfg = GameConfig.model_validate_json(row["config_json"])
        return row["game_id"], row["role"], cfg, row["phase"], GameState.model_validate_json(row["state_json"])

    def seat(self, token: str) -> tuple[Role, GameConfig, GameState, SeatStatus]:
        with self._conn() as c:
            game_id, role, cfg, phase, state = self._load(c, token)
            submitted = [
                r["role"] for r in c.execute(
                    "SELECT role FROM submissions WHERE game_id = ? AND phase = ? ORDER BY role", (game_id, phase)
                )
            ]
        status = SeatStatus(
            game_id=game_id, role=role, mode=cfg.mode, resolver=cfg.resolver, phase=phase, phase_minutes=cfg.phase_minutes,
            must_submit=self.must_submit(cfg), submitted=submitted, awaiting_umpire=bool(state.pending),
            can_resolve=self.can_resolve(cfg, role), finished=state.time >= state.end,
        )
        return role, cfg, state, status

    def actions(self, token: str) -> list[dict]:
        with self._conn() as c:
            game_id, role, *_ = self._load(c, token)
            if role != "umpire":
                raise StoreError(403, "umpire only")
            rows = c.execute(
                "SELECT seq, at, role, kind, payload_json FROM actions WHERE game_id = ? ORDER BY seq", (game_id,)
            ).fetchall()
        return [{**dict(r), "payload_json": json.loads(r["payload_json"])} for r in rows]

    # --- writes -----------------------------------------------------------------------------------

    def create(self, cfg: GameConfig) -> tuple[str, dict[Role, str]]:
        if cfg.mode is Mode.TWO_PLAYER and cfg.resolver == "tabletop":
            raise StoreError(422, "two-player games auto-resolve: tabletop sheets reveal the target's ships")
        try:
            state = load_scenario(cfg.scenario)
        except FileNotFoundError:
            raise StoreError(404, f"no scenario {cfg.scenario!r}") from None
        game_id = secrets.token_urlsafe(6)
        seats: dict[Role, str] = {r: secrets.token_urlsafe(16) for r in self.roles(cfg)}
        with self._tx() as c:
            c.execute(
                "INSERT INTO games (id, config_json, phase, state_json) VALUES (?, ?, 1, ?)",
                (game_id, cfg.model_dump_json(), state.model_dump_json()),
            )
            c.executemany(
                "INSERT INTO seats (token, game_id, role) VALUES (?, ?, ?)",
                [(t, game_id, r) for r, t in seats.items()],
            )
            self._log(c, game_id, "umpire", "create", cfg.model_dump(mode="json"))
        return game_id, seats

    def submit(self, token: str, orders: Orders, advance: Advance) -> SeatStatus:
        """Record a side's orders for this phase. The last required submission runs the clock."""
        with self._tx() as c:
            game_id, role, cfg, phase, state = self._load(c, token)
            if role == "umpire":
                raise StoreError(403, "the umpire does not submit orders; use advance")
            self._check_open(state)
            try:
                apply_orders(state.model_copy(deep=True), Side(role), orders, [])
            except ValueError as e:  # OrderError
                raise StoreError(422, str(e)) from None
            c.execute(
                "INSERT OR REPLACE INTO submissions (game_id, phase, role, orders_json, advance) VALUES (?, ?, ?, ?, ?)",
                (game_id, phase, role, orders.model_dump_json(), advance),
            )
            self._log(c, game_id, role, "submit", {"phase": phase, "advance": advance,
                                                   "orders": orders.model_dump(mode="json")})
            done = {r["role"] for r in c.execute(
                "SELECT role FROM submissions WHERE game_id = ? AND phase = ?", (game_id, phase))}
            if set(self.must_submit(cfg)) <= done:
                self._run(c, game_id, cfg, phase, state, role)
        return self.seat(token)[3]

    def force_advance(self, token: str, advance: Advance) -> SeatStatus:
        """Umpire runs the clock now; sides that have not submitted give no new orders."""
        with self._tx() as c:
            game_id, role, cfg, phase, state = self._load(c, token)
            if role != "umpire":
                raise StoreError(403, "umpire only")
            self._check_open(state)
            self._run(c, game_id, cfg, phase, state, role, forced=advance)
        return self.seat(token)[3]

    def resolve(self, token: str, engagement_id: str, result: EngagementResult) -> SeatStatus:
        with self._tx() as c:
            game_id, role, cfg, phase, state = self._load(c, token)
            if not self.can_resolve(cfg, role):
                raise StoreError(403, "only the umpire (or a solo player) enters tabletop results")
            try:
                res = resolve_pending(state, engagement_id, result)
            except EngineError as e:
                raise StoreError(422, str(e)) from None
            self._save(c, game_id, phase, res)
            self._log(c, game_id, role, "resolve", {"engagement": engagement_id,
                                                    "result": result.model_dump(mode="json")})
        return self.seat(token)[3]

    @staticmethod
    def _check_open(state: GameState) -> None:
        if state.pending:
            raise StoreError(409, "a tabletop engagement is awaiting the umpire's result")
        if state.time >= state.end:
            raise StoreError(409, "the scenario has ended")

    def _run(
        self, c: sqlite3.Connection, game_id: str, cfg: GameConfig, phase: int, state: GameState, role: Role,
        forced: Advance | None = None,
    ) -> None:
        rows = c.execute(
            "SELECT role, orders_json, advance FROM submissions WHERE game_id = ? AND phase = ?", (game_id, phase)
        ).fetchall()
        orders = {Side(r["role"]): Orders.model_validate_json(r["orders_json"]) for r in rows}
        wanted = [forced] if forced else [r["advance"] for r in rows]
        minutes = cfg.phase_minutes if "phase" in wanted else RUN_MAX_MINUTES  # the more cautious side wins
        solo = cfg.mode is Mode.SOLO
        remaining, start = minutes, state.time
        while True:
            if solo:
                enemy = cfg.solo_side.enemy
                orders[enemy] = _keep_valid(state, enemy, bot_orders(view(state, enemy)))
                self._log(c, game_id, enemy.value, "bot", {"phase": phase, "at": state.time.isoformat(),
                                                           "orders": orders[enemy].model_dump(mode="json")})
            res = step(state, orders, minutes=remaining, resolver=self.resolver(cfg), stop_on=self.interrupts(cfg))
            remaining -= int((res.state.time - state.time).total_seconds() // 60)
            state, orders = res.state, {}  # the player's orders are consumed by the first step
            if not (solo and res.interrupted and not state.pending and remaining > 0
                    and not _player_should_stop(state, cfg.solo_side)):
                break
        res = StepResult(state=state, events=[e for e in state.log if e.time > start], interrupted=res.interrupted)
        self._save(c, game_id, phase + 1, res)
        self._log(c, game_id, role, "advance", {"phase": phase, "minutes": minutes, "to": res.state.time.isoformat(),
                                                "interrupted": res.interrupted})

    @staticmethod
    def _save(c: sqlite3.Connection, game_id: str, phase: int, res: StepResult) -> None:
        c.execute("UPDATE games SET phase = ?, state_json = ? WHERE id = ?",
                  (phase, res.state.model_dump_json(), game_id))

    @staticmethod
    def _log(c: sqlite3.Connection, game_id: str, role: str, kind: str, payload: dict) -> None:
        c.execute("INSERT INTO actions (game_id, at, role, kind, payload_json) VALUES (?, ?, ?, ?, ?)",
                  (game_id, _now(), role, kind, json.dumps(payload)))



def _keep_valid(state: GameState, side: Side, orders: Orders) -> Orders:
    """Drop any bot order the engine would reject, rather than stall the game on it."""
    scratch = state.model_copy(deep=True)
    kept = Orders()
    for field in ("moves", "cap", "searches", "strikes"):
        for o in getattr(orders, field):
            try:
                apply_orders(scratch, side, Orders(**{field: [o]}), [])
            except OrderError:
                continue
            getattr(kept, field).append(o)
    return kept


def _player_should_stop(state: GameState, side: Side) -> bool:
    """Did the tick that just ended produce something this player sees and asked to stop for?"""
    for e in reversed(state.log):
        if e.time != state.time:
            return False
        if side in e.visible_to and e.kind in DEFAULT_INTERRUPTS:
            return True
    return False
