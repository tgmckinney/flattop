"""Battle resolution. A resolver returns a result now (auto) or None to defer to the tabletop."""

from __future__ import annotations

import random
from typing import Protocol

from flattop.model import (
    SHIP_CLASSES,
    AircraftType,
    Doctrine,
    Engagement,
    EngagementResult,
    Ship,
    Side,
    SurfaceAction,
    SurfaceResult,
)

ESCORT_KILL = 0.12  # per fighter, per exchange, scaled by air_combat
CAP_VS_BOMBER = 0.35
ESCORTS_TIE_UP = 0.5  # CAP fighters held off per escort
FLAK_PER_POINT = 0.012
FLAK_CAP = 0.3
CARRIER_FIXATION = 0.8
GUN_HIT_DAY = 0.06  # per gun roll, per round
GUN_HIT_NIGHT = 0.10  # night actions are fought at close range
TORPEDO_HIT = 0.15  # per torpedo roll, times the side's torpedo factor
TORPEDO_DAMAGE = 2
ROUNDS_DAY = 3
ROUNDS_NIGHT = 2


class Resolver(Protocol):
    def resolve(
        self, e: Engagement, types: dict[str, AircraftType], rng: random.Random
    ) -> EngagementResult | None: ...


class TabletopResolver:
    """Defers every engagement: the engine pauses until `engine.resolve_pending` is called."""

    def resolve(self, e: Engagement, types: dict[str, AircraftType], rng: random.Random) -> None:
        return None


def _take_one(pool: dict[str, int], rng: random.Random) -> str | None:
    keys = [k for k, n in sorted(pool.items()) for _ in range(n)]
    if not keys:
        return None
    k = rng.choice(keys)
    pool[k] -= 1
    return k


def _add(d: dict[str, int], k: str, n: int = 1) -> None:
    d[k] = d.get(k, 0) + n


class AutoResolver:
    """Seeded abstract air-sea combat: escorts vs CAP, CAP vs bombers, flak, then attacks."""

    def resolve(self, e: Engagement, types: dict[str, AircraftType], rng: random.Random) -> EngagementResult:
        res = EngagementResult()
        escorts = {k: n for k, n in sorted(e.attackers.items()) if types[k].role == "fighter" and n > 0}
        bombers = {k: n for k, n in sorted(e.attackers.items()) if types[k].role != "fighter" and n > 0}
        cap = {k: n for k, n in sorted(e.defender_cap.items()) if n > 0} if e.daylight else {}

        # Escorts and CAP fight each other.
        for k, n in list(escorts.items()):
            for _ in range(n):
                if rng.random() < ESCORT_KILL * types[k].air_combat and (lost := _take_one(cap, rng)):
                    _add(res.cap_losses, lost)
        for k, n in list(cap.items()):
            for _ in range(n):
                if rng.random() < ESCORT_KILL * types[k].air_combat and (lost := _take_one(escorts, rng)):
                    _add(res.attacker_losses, lost)

        # CAP not tied up by escorts goes for the bombers.
        free = max(0, round(sum(cap.values()) - ESCORTS_TIE_UP * sum(escorts.values())))
        hunters = [k for k, n in cap.items() for _ in range(n)]
        rng.shuffle(hunters)
        for k in hunters[:free]:
            if rng.random() < CAP_VS_BOMBER * types[k].air_combat and (lost := _take_one(bombers, rng)):
                _add(res.attacker_losses, lost)

        # Flak: half of its toll falls before release.
        afloat = [s for s in e.ships if not s.sunk]
        p_flak = min(FLAK_CAP, FLAK_PER_POINT * sum(SHIP_CLASSES[s.type].flak for s in afloat))
        releasing: list[str] = []
        for k, n in list(bombers.items()):
            for _ in range(n):
                shot = rng.random() < p_flak
                if shot and rng.random() < 0.5:
                    _add(res.attacker_losses, k)
                    continue
                releasing.append(k)
                if shot:
                    _add(res.attacker_losses, k)

        carriers = [s for s in afloat if s.type in ("CV", "CVL")]
        for k in releasing:
            if not afloat:
                break
            pool = carriers if carriers and rng.random() < CARRIER_FIXATION else afloat
            ship = rng.choice(pool)
            if rng.random() < types[k].hit_prob:
                _add(res.ship_damage, ship.name, types[k].damage)

        released = len(releasing)
        hits = sum(1 for _ in res.ship_damage)
        res.notes.append(
            f"{sum(e.attackers.values())} attackers, {sum(cap.values())} CAP; "
            f"{released} released; {hits} ships hit"
        )
        return res


class SurfaceResolver(Protocol):
    """Resolves a surface action now, or returns None to defer it to the tabletop."""

    def resolve(
        self, a: SurfaceAction, doctrine: dict[Side, Doctrine], rng: random.Random
    ) -> SurfaceResult | None: ...


class TabletopSurfaceResolver:
    """Defers every surface action: the engine pauses until `engine.resolve_pending` is called."""

    def resolve(self, a: SurfaceAction, doctrine: dict[Side, Doctrine], rng: random.Random) -> None:
        return None


class AutoSurfaceResolver:
    """Seeded rounds of gunfire, with one torpedo attack per side. A surprised side does not fire in round 1."""

    def resolve(self, a: SurfaceAction, doctrine: dict[Side, Doctrine], rng: random.Random) -> SurfaceResult:
        fleet: dict[Side, dict[str, Ship]] = {
            side: {s.name: s.model_copy() for s in ships if not s.sunk} for side, ships in sorted(a.ships.items())
        }
        added: dict[Side, dict[str, int]] = {side: {} for side in fleet}
        torpedoes_fired: set[Side] = set()
        p_gun = GUN_HIT_DAY if a.daylight else GUN_HIT_NIGHT
        rounds = ROUNDS_DAY if a.daylight else ROUNDS_NIGHT
        for rnd in range(rounds):
            incoming: dict[Side, dict[str, int]] = {side: {} for side in fleet}
            for side in sorted(fleet):
                if rnd == 0 and side in a.surprised:
                    continue
                enemy = [s for s in fleet.get(side.enemy, {}).values() if not s.sunk]
                if not enemy:
                    continue
                fire_torpedoes = side not in torpedoes_fired
                torpedoes_fired.add(side)
                p_torp = TORPEDO_HIT * doctrine[side].torpedo_factor
                for ship in (s for s in fleet[side].values() if not s.sunk):
                    cls = SHIP_CLASSES[ship.type]
                    for _ in range(cls.guns):
                        if rng.random() < p_gun:
                            _add(incoming[side.enemy], rng.choice(enemy).name)
                    for _ in range(cls.torpedoes if fire_torpedoes else 0):
                        if rng.random() < p_torp:
                            _add(incoming[side.enemy], rng.choice(enemy).name, TORPEDO_DAMAGE)
            for side, hits in incoming.items():  # fire in one round is simultaneous
                for name, d in sorted(hits.items()):
                    ship = fleet[side][name]
                    d = min(d, SHIP_CLASSES[ship.type].capacity - ship.damage)
                    if d > 0:
                        ship.damage += d
                        _add(added[side], name, d)
        sunk = {side: sum(1 for s in ships.values() if s.sunk) for side, ships in fleet.items()}
        return SurfaceResult(
            ship_damage=added,
            notes=[f"{rounds} rounds; " + "; ".join(
                f"{side}: {len(added[side])} ships hit, {sunk[side]} sunk" for side in sorted(fleet))],
        )
