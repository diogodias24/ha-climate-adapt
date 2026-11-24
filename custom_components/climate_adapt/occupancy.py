# SPDX-License-Identifier: Apache-2.0
"""Who is in the room, and when an arrival earns an evaluation.

Pure logic, no Home Assistant imports, in the style of ``decision``. Two jobs:

* ``read_occupancy``: is the room occupied or empty right now. Only a sensor that has
  been ``off`` for the whole hold counts as empty; ``on``, ``off`` for less than the
  hold, and every unreadable state count as occupied, so the wide start edges are the
  exception and a dead sensor can never widen them.
* ``ArrivalGate``: which ``off`` -> ``on`` events earn one extra evaluation. Only when
  the unit is off, and at most one per ``ARRIVAL_MIN_INTERVAL_MINUTES``; the events it
  refuses are dropped, with nothing queued behind them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

SENSOR_ON = "on"
SENSOR_OFF = "off"
UNIT_OFF = "off"

# The ledger and decision source of a tick this module admits.
SOURCE_OCCUPANCY = "occupancy"

# One occupancy-triggered evaluation per room in this window, measured from the last
# one and from nothing else (regular ticks do not reset it).
ARRIVAL_MIN_INTERVAL_MINUTES = 5.0


@dataclass(frozen=True)
class Occupancy:
    """One reading of the room's occupancy sensor."""

    sensor: str | None
    tracked: bool
    raw_state: str | None
    occupied: bool | None
    empty_for_minutes: float | None
    occupied_since: datetime | None
    hold_minutes: int

    @property
    def idle(self) -> bool:
        """True only for a tracked room that has been empty for the whole hold."""
        return self.tracked and self.occupied is False


def read_occupancy(
    *,
    sensor: str | None,
    enabled: bool,
    state: str | None,
    last_changed: datetime | None,
    now: datetime,
    hold_minutes: int,
) -> Occupancy:
    """Read the room as occupied or empty from the sensor's state and its age.

    ``state`` is None for a sensor Home Assistant does not know. Stateless on purpose:
    "empty" is ``off`` and ``now - last_changed >= hold``, so there is nothing to
    persist and nothing to lose in a restart (which restarts the clock, and so reads
    as occupied for one hold).
    """
    tracked = bool(enabled and sensor)
    if not tracked:
        return Occupancy(sensor, False, state, None, None, None, hold_minutes)
    if state == SENSOR_ON:
        return Occupancy(sensor, True, state, True, None, last_changed, hold_minutes)
    if state == SENSOR_OFF and last_changed is not None:
        quiet = max(0.0, (now - last_changed).total_seconds() / 60.0)
        return Occupancy(
            sensor,
            True,
            state,
            quiet < hold_minutes,
            round(quiet, 1),
            None,
            hold_minutes,
        )
    return Occupancy(sensor, True, state, True, None, None, hold_minutes)


class ArrivalGate:
    """Decides which sensor events run an arrival evaluation, and counts the rest."""

    def __init__(self) -> None:
        self.last_tick_at: datetime | None = None
        self.ticks = 0
        self.dropped_rate_limit = 0
        self.skipped_unit_running = 0
        self.skipped_not_off_event = 0

    def admit(
        self, old: str | None, new: str | None, *, unit_state: str | None, now: datetime
    ) -> bool:
        """Return True when this event runs an evaluation now; count it either way.

        Order matters: a unit that is running is never evaluated, so it cannot spend
        the rate window; and only an evaluation that runs stamps it.
        """
        if old != SENSOR_OFF or new != SENSOR_ON:
            self.skipped_not_off_event += 1
            return False
        if unit_state != UNIT_OFF:
            self.skipped_unit_running += 1
            return False
        if self.last_tick_at is not None:
            elapsed = (now - self.last_tick_at).total_seconds() / 60.0
            if 0.0 <= elapsed < ARRIVAL_MIN_INTERVAL_MINUTES:
                self.dropped_rate_limit += 1
                return False
        self.last_tick_at = now
        self.ticks += 1
        return True

    def counters(self) -> dict[str, Any]:
        return {
            "arrival_ticks": self.ticks,
            "arrival_dropped_rate_limit": self.dropped_rate_limit,
            "arrival_skipped_unit_running": self.skipped_unit_running,
            "arrival_skipped_not_off_event": self.skipped_not_off_event,
        }
