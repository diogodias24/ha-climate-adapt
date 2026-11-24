# SPDX-License-Identifier: Apache-2.0
"""The comfort model.

Pure logic, no Home Assistant imports, in the style of ``direction_lock``: the
outdoor running mean behind the adaptive comfort temperature, the comfort band,
the humidity status with its hysteresis, and the fallback chain for a running
mean that has no history yet. The coordinator feeds it readings and asks it
questions; ``decision`` turns the answers into an action.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .const import OUTSIDE_SAMPLE_HOLD_MINUTES

# --- Comfort temperature (ASHRAE 55 adaptive, clamped) ---------------------

TC_SLOPE = 0.31
TC_OFFSET = 17.8
TC_MIN_C = 21.0
TC_MAX_C = 25.5
# Below this running mean the adaptive model is not valid and Tc holds at TC_MIN_C.
TRM_VALID_MIN_C = 10.0

# --- Band -------------------------------------------------------------------

BAND_BELOW_C = 2.0
BAND_ABOVE_C = 1.5
# Cooling starts this far over Tc, well under the band top.
# The band top stays the display, learning and `band` attribute edge.
COOL_START_ABOVE_TC = 0.8
# Extra room beyond an edge before a unit may *start* when nobody is moving.
MOTION_IDLE_WIDEN_C = 1.0
# Heat dead-zone: heat starts only when the room is more than this far under the
# band's low edge. A delay, not a limit: past it the ladder is unchanged. 0
# disables it. The supported range is 0.3-0.5 K; 0.5-1.0 K is allowed but
# there is no evidence that a deeper dip recovers on its own.
HEAT_DEAD_ZONE_MIN_C = 0.0
HEAT_DEAD_ZONE_MAX_C = 1.0
HEAT_DEAD_ZONE_STEP_C = 0.1
DEFAULT_HEAT_DEAD_ZONE_C = 0.4

# --- Outdoor running mean ---------------------------------------------------

# EN 16798-1 weights for the previous 7 days, yesterday first.
TRM_WEIGHTS = (1.0, 0.8, 0.6, 0.5, 0.4, 0.3, 0.2)
DAY_MIN_COVERAGE_HOURS = 18.0
TRAILING_HOURS = 24
# A 6 h window on a first evening covers the afternoon only and reads far warmer
# than the true day, so Tc would run high; a day's worth is the bar for any mean.
TRAILING_MIN_COVERAGE_HOURS = 18.0
# Hourly buckets older than this are dropped (14 days of daily means, plus one).
BUCKET_KEEP_DAYS = 15
# Fixed monthly mean outdoor temperatures, January first. Last resort of the chain.
MONTHLY_MEAN_C = (10.0, 11.0, 13.0, 15.0, 18.0, 21.0, 23.0, 23.0, 21.0, 17.0, 13.0, 11.0)

SOURCE_RUNNING_MEAN = "running_mean"
SOURCE_TRAILING = "trailing_24h"
SOURCE_MONTHLY = "monthly_table"

# --- Humidity ---------------------------------------------------------------

HUMID_RH = 65.0
HUMID_CLEAR_RH = 58.0
HUMID_DEW_POINT_C = 17.0
# RH alone does not make air humid: 65 % at 21 C is a 14 C dew point. The RH path
# into 'humid' also needs this much moisture, and the way out is the dew point.
HUMID_RH_MIN_DEW_POINT_C = 16.0
HUMID_CLEAR_DEW_POINT_C = 15.5
HUMID_CONFIRM_TICKS = 2
DRY_AIR_RH = 30.0

HUMIDITY_UNKNOWN = "unknown"
HUMIDITY_NORMAL = "normal"
HUMIDITY_HUMID = "humid"
HUMIDITY_DRY_AIR = "dry_air"

_HOLD = timedelta(minutes=OUTSIDE_SAMPLE_HOLD_MINUTES)
_HOUR = timedelta(hours=1)


def dew_point(temp_c: float, rh: float) -> float | None:
    """Magnus dew point in C, or None when the pair cannot describe air."""
    if not (0.0 < rh <= 100.0) or not math.isfinite(temp_c):
        return None
    a, b = 17.62, 243.12
    gamma = math.log(rh / 100.0) + a * temp_c / (b + temp_c)
    return b * gamma / (a - gamma)


def comfort_temperature(trm: float, hybrid_target: float | None = None) -> float:
    """Return Tc: the Hybrid target if there is one, else the adaptive value."""
    if hybrid_target is not None:
        return float(hybrid_target)
    if trm < TRM_VALID_MIN_C:
        return TC_MIN_C
    return min(max(TC_SLOPE * trm + TC_OFFSET, TC_MIN_C), TC_MAX_C)


@dataclass(frozen=True)
class Band:
    """The comfort band around Tc and the edges a unit starts at."""

    tc: float
    low: float
    high: float
    heat_start: float
    cool_start: float
    heat_dead_zone: float = 0.0


def comfort_band(
    tc: float, motion_idle: bool = False, heat_dead_zone: float = 0.0
) -> Band:
    """Return the asymmetric band; `motion_idle` widens only the start edges.

    Heat starts under ``low - max(widen, heat_dead_zone)``: the dead-zone and the
    idle widening are both "how far under the edge before a unit may start", so
    the larger one rules and they never add. The rounding is only on the
    dead-zone edge, so a room exactly X under compares as exactly X.
    """
    low = tc - BAND_BELOW_C
    high = tc + BAND_ABOVE_C
    widen = MOTION_IDLE_WIDEN_C if motion_idle else 0.0
    heat_start = low - widen
    if heat_dead_zone > widen:
        heat_start = round(low - heat_dead_zone, 6)
    return Band(
        tc=tc,
        low=low,
        high=high,
        heat_start=heat_start,
        cool_start=tc + COOL_START_ABOVE_TC + widen,
        heat_dead_zone=heat_dead_zone,
    )


# --- Humidity status --------------------------------------------------------


@dataclass(frozen=True)
class HumidityState:
    """Humidity status plus the consecutive-tick count that confirms 'humid'."""

    status: str = HUMIDITY_UNKNOWN
    high_ticks: int = 0


def next_humidity(
    previous: HumidityState, rh: float | None, temp_c: float | None
) -> tuple[HumidityState, float | None]:
    """Advance the humidity status one tick; return it and the dew point.

    Humid is entered at once on a dew point >= 17, or on RH >= 65 confirmed over
    two ticks with a dew point >= 16, and left when the dew point is under 15.5
    (a room that cools overnight can sit at RH 58-65 with a 12-14 C dew
    point, which is not humid and must not hold the latch). Only when the dew
    point cannot be computed does RH decide alone: in at 65, out at 58. A
    missing or impossible RH is `unknown`: dry air is never entered from it and
    the confirm count starts over.
    """
    if rh is None or not math.isfinite(rh) or not (0.0 <= rh <= 100.0):
        return HumidityState(HUMIDITY_UNKNOWN, 0), None
    dew = dew_point(temp_c, rh) if temp_c is not None else None
    ticks = previous.high_ticks + 1 if rh >= HUMID_RH else 0
    if previous.status == HUMIDITY_HUMID:
        humid = rh > HUMID_CLEAR_RH if dew is None else dew >= HUMID_CLEAR_DEW_POINT_C
    else:
        rh_confirmed = ticks >= HUMID_CONFIRM_TICKS and (
            dew is None or dew >= HUMID_RH_MIN_DEW_POINT_C
        )
        humid = rh_confirmed or (dew is not None and dew >= HUMID_DEW_POINT_C)
    if humid:
        return HumidityState(HUMIDITY_HUMID, ticks), dew
    status = HUMIDITY_DRY_AIR if rh < DRY_AIR_RH else HUMIDITY_NORMAL
    return HumidityState(status, ticks), dew


def humidity_in_range(rh: float | None, dew: float | None) -> bool | None:
    """Replay/report definition: 30 <= RH <= 65 and dew point under 17."""
    if rh is None:
        return None
    return DRY_AIR_RH <= rh <= HUMID_RH and (dew is None or dew < HUMID_DEW_POINT_C)


# --- Outdoor running mean ---------------------------------------------------


@dataclass(frozen=True)
class RunningMean:
    """Trm with the rung of the fallback chain it came from."""

    value: float
    source: str
    days: int = 0


def _bucket_key(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H")


class OutsideDailyMeans:
    """Time-weighted outside temperature kept in hourly buckets.

    The 6 h sample ring of ``OutsideTrend`` is too short for a day, so this
    integrates the same accepted samples as they arrive. A sample covers the
    time until the next one, at most the hold; the rest of a long silence is
    simply not counted, which is what the 18 h coverage rule is for. Buckets
    are keyed by the hour of the datetimes it is given: pass local time and the
    days are local days.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, list[float]] = {}
        self._last: tuple[datetime, float] | None = None
        self._cursor: datetime | None = None

    def add(self, reported: datetime, temp: float) -> bool:
        """Offer an accepted sample; return False if it is out of order."""
        if self._last is not None and reported <= self._last[0]:
            return False
        self.advance(reported)
        self._last = (reported, temp)
        self._cursor = reported
        return True

    def advance(self, now: datetime) -> None:
        """Integrate the open sample up to `now` (capped by the hold)."""
        if self._last is None or self._cursor is None:
            return
        end = min(now, self._last[0] + _HOLD)
        if end <= self._cursor:
            return
        self._integrate(self._cursor, end, self._last[1])
        self._cursor = end

    def _integrate(self, start: datetime, end: datetime, value: float) -> None:
        cursor = start
        while cursor < end:
            hour_start = cursor.replace(minute=0, second=0, microsecond=0)
            stop = min(end, hour_start + _HOUR)
            seconds = (stop - cursor).total_seconds()
            bucket = self._buckets.setdefault(_bucket_key(cursor), [0.0, 0.0])
            bucket[0] += value * seconds
            bucket[1] += seconds
            cursor = stop
        horizon = _bucket_key(end - timedelta(days=BUCKET_KEEP_DAYS))
        for key in [k for k in self._buckets if k < horizon]:
            del self._buckets[key]

    def day(self, day: date) -> tuple[float | None, float]:
        """Return (mean, covered hours) for one calendar day."""
        prefix = day.isoformat()
        total = seconds = 0.0
        for key, (weighted, covered) in self._buckets.items():
            if key.startswith(prefix):
                total += weighted
                seconds += covered
        return (total / seconds if seconds else None), seconds / 3600.0

    def trailing(self, now: datetime, hours: int = TRAILING_HOURS) -> tuple[float | None, float]:
        """Return (mean, covered hours) over the whole hourly buckets of the last `hours`."""
        first = _bucket_key(now - timedelta(hours=hours))
        last = _bucket_key(now)
        total = seconds = 0.0
        for key, (weighted, covered) in self._buckets.items():
            if first < key <= last:
                total += weighted
                seconds += covered
        return (total / seconds if seconds else None), seconds / 3600.0

    def complete_days(self, today: date) -> list[float | None]:
        """Daily means for the previous 7 days, yesterday first, None if short."""
        out: list[float | None] = []
        for age in range(1, len(TRM_WEIGHTS) + 1):
            mean, hours = self.day(today - timedelta(days=age))
            out.append(mean if hours >= DAY_MIN_COVERAGE_HOURS else None)
        return out

    def running_mean(self, now: datetime) -> RunningMean:
        """Trm through the fallback chain: 7-day weights, trailing 24 h, month table."""
        self.advance(now)
        days = self.complete_days(now.date())
        weight = total = 0.0
        count = 0
        for w, mean in zip(TRM_WEIGHTS, days, strict=True):
            if mean is not None:
                weight += w
                total += w * mean
                count += 1
        if count:
            return RunningMean(total / weight, SOURCE_RUNNING_MEAN, count)
        mean, hours = self.trailing(now)
        if mean is not None and hours >= TRAILING_MIN_COVERAGE_HOURS:
            return RunningMean(mean, SOURCE_TRAILING)
        return RunningMean(MONTHLY_MEAN_C[now.month - 1], SOURCE_MONTHLY)

    def to_store(self) -> dict[str, Any]:
        return {
            "buckets": {k: [v[0], v[1]] for k, v in sorted(self._buckets.items())},
            "last": [self._last[0].isoformat(), self._last[1]] if self._last else None,
            "cursor": self._cursor.isoformat() if self._cursor else None,
        }

    def load(self, stored: dict[str, Any]) -> None:
        """Restore from the store; anything malformed is dropped, not raised."""
        self._buckets = {}
        self._last = None
        self._cursor = None
        raw = stored.get("buckets")
        if isinstance(raw, dict):
            for key, pair in raw.items():
                try:
                    self._buckets[str(key)] = [float(pair[0]), float(pair[1])]
                except (TypeError, ValueError, IndexError, KeyError):
                    continue
        try:
            last = stored.get("last")
            cursor = stored.get("cursor")
            if last and cursor:
                self._last = (datetime.fromisoformat(last[0]), float(last[1]))
                self._cursor = datetime.fromisoformat(cursor)
        except (TypeError, ValueError, IndexError):
            self._last = None
            self._cursor = None
