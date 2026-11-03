"""Tabletop engagement sheet: everything needed to fight an engagement off-screen."""

from __future__ import annotations

from flattop.geo import fmt_pos
from flattop.model import SHIP_CLASSES, AircraftType, Engagement


def engagement_sheet(e: Engagement, types: dict[str, AircraftType]) -> str:
    lines = [
        f"# Engagement {e.id}",
        "",
        f"- Time: {e.time:%d %b %Y %H:%M} ({'day' if e.daylight else 'night'})",
        f"- Position: {fmt_pos(e.pos)}",
        f"- Attacker: {e.attacker_side} (strike {e.strike_id})",
        "",
        "## Attacking aircraft",
        *[f"- {n} × {types[k].name} ({types[k].role})" for k, n in sorted(e.attackers.items()) if n],
        "",
        "## Defending CAP",
        *([f"- {n} × {types[k].name}" for k, n in sorted(e.defender_cap.items()) if n] or ["- none"]),
        "",
        "## Ships",
        "| Ship | Type | Damage | Capacity | Flak |",
        "|---|---|---|---|---|",
        *[f"| {s.name} | {s.type} | {s.damage} | {SHIP_CLASSES[s.type].capacity} | {SHIP_CLASSES[s.type].flak} |"
          for s in e.ships],
        "",
        "Return result as EngagementResult JSON: attacker_losses, cap_losses, ship_damage (by ship name).",
    ]
    return "\n".join(lines)
