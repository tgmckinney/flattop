"""Dead reckoning for contacts: where a side thinks an enemy group is now, and how sure it is."""

from __future__ import annotations

from datetime import datetime

from flattop.geo import Pos, destination, distance_nm
from flattop.model import Contact

# Unknown course changes since the report widen the circle by this much per hour.
DRIFT_NM_PER_HOUR = 12.0


def age_hours(c: Contact, now: datetime) -> float:
    return max(0.0, (now - c.reported_at).total_seconds() / 3600)


def estimate(c: Contact, now: datetime) -> tuple[Pos, float]:
    """Estimated position and uncertainty radius (nm) at `now`."""
    h = age_hours(c, now)
    pos = destination(c.reported_pos, c.reported_course, c.reported_speed_kn * h)
    return pos, c.error_nm + DRIFT_NM_PER_HOUR * h


def intercept(origin: Pos, est: Pos, course: float, speed_kn: float, air_speed_kn: float) -> Pos:
    """Where aircraft from `origin` meet a group now at `est` holding course and speed (two passes)."""
    aim = est
    for _ in range(2):
        hours = distance_nm(origin, aim) / air_speed_kn
        aim = destination(est, course, speed_kn * hours)
    return aim
