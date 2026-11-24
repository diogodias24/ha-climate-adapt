# SPDX-License-Identifier: Apache-2.0
"""Fresh-air pause and morning cooling: the pure parts."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.util import dt as dt_util

from .comfort import COOL_START_ABOVE_TC

FRESH_AIR_DEFAULT_ENABLED = True
FRESH_AIR_MINUTES_MIN = 15.0
FRESH_AIR_MINUTES_MAX = 480.0
FRESH_AIR_MINUTES_STEP = 5.0
DEFAULT_FRESH_AIR_MINUTES = 60
FRESH_AIR_PRESS_MAX_AGE_SECONDS = 60

DEFAULT_MORNING_ENABLED = False
MORNING_COOL_START_MIN = 22.0
MORNING_COOL_START_MAX = 28.0
MORNING_COOL_START_STEP = 0.1
DEFAULT_MORNING_COOL_START = 24.3
DEFAULT_MORNING_WINDOW_START = "07:15:00"
DEFAULT_MORNING_WINDOW_END = "10:00:00"


def parse_clock(value: Any, default: str) -> time:
    """A `HH:MM[:SS]` option as a time; anything unreadable is the default."""
    for candidate in (value, default):
        try:
            parts = [int(p) for p in str(candidate).split(":")]
            if len(parts) in (2, 3):
                return time(parts[0], parts[1], parts[2] if len(parts) == 3 else 0)
        except (TypeError, ValueError):
            continue
    return time(0, 0)


def in_morning_window(now: datetime, start: time, end: time) -> bool:
    """Whether the local time of `now` is in `[start, end)`; a window past midnight wraps."""
    local = dt_util.as_local(now).time().replace(tzinfo=None)
    if start <= end:
        return start <= local < end
    return local >= start or local < end


def morning_comfort_temperature(tc: float, cool_start: float) -> float:
    """The comfort temperature that puts the cooling start at `cool_start`, never above `tc`."""
    return min(tc, cool_start - COOL_START_ABOVE_TC)


def is_fresh_press(old_state: Any, new_state: Any, now: datetime) -> bool:
    """Whether a scene state change is an activation just now.

    A scene's state is the time it was last activated. A restore after a restart
    arrives from `unknown`, and a reload can re-announce an old timestamp; neither
    is the user opening the window.
    """
    if old_state is None or new_state is None:
        return False
    unusable = (STATE_UNAVAILABLE, STATE_UNKNOWN)
    if old_state.state in unusable or new_state.state in unusable:
        return False
    pressed = dt_util.parse_datetime(new_state.state)
    if pressed is None:
        return False
    age = (now - dt_util.as_utc(pressed)).total_seconds()
    return -FRESH_AIR_PRESS_MAX_AGE_SECONDS <= age <= FRESH_AIR_PRESS_MAX_AGE_SECONDS
