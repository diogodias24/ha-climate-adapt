# SPDX-License-Identifier: Apache-2.0
"""Night coast: no cooling on a night that is forecast to get colder.

Pure logic, no Home Assistant imports, in the style of ``heat_load``. The ladder
asks ``coast_window`` whether it is night (or dusk) and the next hours will be
colder than the comfort band's low edge, and ``reached`` whether the room is
already at its ceiling. Every input that is missing means the rung does not
apply: the signal can only hold cooling back when everything is readable. With no
usable forecast the current outside temperature stands in for the forecast minimum
(``forecast_stand_in``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

SUN_BELOW_HORIZON = "below_horizon"
SUN_ABOVE_HORIZON = "above_horizon"

DEFAULT_ENABLED = True

CEILING_MIN = 22.0
CEILING_MAX = 30.0
CEILING_STEP = 0.5
DEFAULT_CEILING = 25.0

FORECAST_HOURS_MIN = 2
FORECAST_HOURS_MAX = 12
FORECAST_HOURS_STEP = 1
DEFAULT_FORECAST_HOURS = 8

MARGIN_MIN = 0.0
MARGIN_MAX = 5.0
MARGIN_STEP = 0.5
DEFAULT_MARGIN = 0.0

DEFAULT_COAST_FAN_STEP = True

COAST_FAN_MINUTES_MIN = 5
COAST_FAN_MINUTES_MAX = 60
COAST_FAN_MINUTES_STEP = 5
DEFAULT_COAST_FAN_MINUTES = 20

# On some IR-controlled units `auto` fan speed in fan_only mode transmits nothing,
# so the default is `low`, the quietest speed that reliably does.
DEFAULT_COAST_FAN_SPEED = "low"
COAST_FAN_SPEED_FALLBACKS = ("auto", "low", "mid", "high")

DEFAULT_DUSK_ENABLED = True

DUSK_ELEVATION_MIN = 0.0
DUSK_ELEVATION_MAX = 30.0
DUSK_ELEVATION_STEP = 1.0
DEFAULT_DUSK_ELEVATION = 10.0

DUSK_PV_MIN = 0.0
DUSK_PV_MAX = 5.0
DUSK_PV_STEP = 0.1
DEFAULT_DUSK_PV = 1.0

DUSK_GAP_MIN = 0.0
DUSK_GAP_MAX = 10.0
DUSK_GAP_STEP = 0.5
DEFAULT_DUSK_GAP = 2.0

# Hysteresis: once dusk holds it stays while the entry tests, loosened by these,
# still pass, so a cloud edge across the PV threshold does not flip the unit.
DUSK_EXIT_ELEVATION_C = 3.0
DUSK_EXIT_PV_KW = 0.5
DUSK_EXIT_GAP_RELAX_K = 1.0

SOURCE_FORECAST = "forecast"
SOURCE_OUTSIDE = "outside_fallback"
SOURCE_NONE = "none"

FORECAST_REFRESH_MINUTES = 30
FORECAST_RETRY_MINUTES = 2
FORECAST_MAX_AGE_HOURS = 3
FORECAST_MIN_ENTRIES = 2


def clamp_setting(value: Any, low: float, high: float, default: float) -> float:
    """A stored setting, clamped into its range; anything unreadable is the default."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if not math.isfinite(value):
        return default
    return min(high, max(low, float(value)))


def is_night(sun_state: str | None) -> bool:
    """True only when the sun entity says it is below the horizon."""
    return sun_state is not None and str(sun_state).strip().lower() == SUN_BELOW_HORIZON


@dataclass(frozen=True)
class CoastInputs:
    """What the ladder is told about night coast for one tick."""

    night: bool
    forecast_min: float | None
    forecast_hours: int
    ceiling: float
    margin: float
    dusk: bool = False
    dusk_note: str | None = None
    forecast_source: str = SOURCE_FORECAST
    fan_step: bool = DEFAULT_COAST_FAN_STEP
    fan_minutes: int = DEFAULT_COAST_FAN_MINUTES
    fan_speed: str = DEFAULT_COAST_FAN_SPEED


@dataclass(frozen=True)
class CoastReading:
    """What one tick made of night coast, for the comfort sensor."""

    window: bool
    active: bool
    overridden: bool
    night: bool
    forecast_min: float | None
    threshold: float | None
    forecast_hours: int
    ceiling: float
    margin: float
    dusk: bool = False
    forecast_source: str = SOURCE_FORECAST
    fan_step: bool = DEFAULT_COAST_FAN_STEP
    fan_minutes: int = DEFAULT_COAST_FAN_MINUTES
    fan_speed: str = DEFAULT_COAST_FAN_SPEED
    fan_running: bool = False
    fan_elapsed: float | None = None
    fan_done: bool = False


def threshold(coast: CoastInputs, band_low: float) -> float:
    return band_low - coast.margin


def coast_window(coast: CoastInputs, band_low: float) -> bool:
    """Night or dusk, and the forecast minimum is strictly under the band low edge less margin."""
    return (
        (coast.night or coast.dusk)
        and coast.forecast_min is not None
        and coast.forecast_min < threshold(coast, band_low)
    )


def reached(coast: CoastInputs, room: float) -> str | None:
    """``"room"`` when the room temperature is at its ceiling (``>=``), else None."""
    return "room" if room >= coast.ceiling else None


def coasting_reason(coast: CoastInputs, band_low: float, room: float) -> str:
    low = f"{band_low:.1f}" + (f" - {coast.margin:.1f}" if coast.margin > 0 else "")
    when = "night"
    if coast.dusk:
        when = f"dusk ({coast.dusk_note})" if coast.dusk_note else "dusk"
    cold = (
        f"outside {coast.forecast_min:.1f} C (no forecast)"
        if coast.forecast_source == SOURCE_OUTSIDE
        else f"forecast min {coast.forecast_min:.1f} C"
    )
    return (
        f"Coasting: {when}, {cold} < band {low}, "
        f"room {room:.1f} C < {coast.ceiling:.1f}"
    )


def fan_step_running_reason(coast_text: str, elapsed: float, minutes: int, speed: str) -> str:
    return (
        f"{coast_text}. Coast fan step, {int(elapsed)} of {minutes} min: "
        f"fan only, {speed} speed, then off"
    )


def fan_step_done_reason(coast_text: str) -> str:
    return (
        f"{coast_text}. Coast fan step done: turning the unit off, "
        "staying off until the coast ends"
    )


def fan_step_latched_reason(coast_text: str) -> str:
    return f"{coast_text}. Coast fan step done earlier: the unit stays off until the coast ends"


def fan_step_skipped_reason(coast_text: str, why: str) -> str:
    return f"{coast_text}. Coast fan step skipped, {why}: off until the coast ends"


def forecast_stand_in(
    forecast_min: float | None, outside: float | None, room: float, gap: float
) -> tuple[float | None, str]:
    """The forecast minimum; with none, the outside temperature when ``gap`` K under the room.

    No forecast and no outside reading, or an outside that is not cooler than the room by
    the gap, leaves no value: the rung does not apply.
    """
    if forecast_min is not None:
        return forecast_min, SOURCE_FORECAST
    if outside is not None and outside <= room - gap + 1e-9:
        return outside, SOURCE_OUTSIDE
    return None, SOURCE_NONE


@dataclass(frozen=True)
class DuskSettings:
    enabled: bool
    elevation: float
    pv_kw: float
    gap: float


@dataclass(frozen=True)
class DuskReading:
    """One tick's look at dusk: what was read and whether it holds."""

    holds: bool
    elevation: float | None
    pv_kw: float | None
    gap: float | None

    @property
    def note(self) -> str | None:
        if not self.holds or self.gap is None:
            return None
        parts = []
        if self.elevation is not None:
            parts.append(f"sun {self.elevation:.1f} deg")
        if self.pv_kw is not None:
            parts.append(f"PV {self.pv_kw:.1f} kW")
        parts.append(f"outside {self.gap:.1f} K cooler")
        return ", ".join(parts)


def read_dusk(
    settings: DuskSettings,
    *,
    sun_state: str | None,
    rising: object,
    elevation: float | None,
    pv_kw: float | None,
    room: float,
    outside: float | None,
    was_active: bool,
) -> DuskReading:
    """Whether dusk holds: afternoon sun that is low or weak, with the outside cooler than the room.

    Entering uses the settings as they are; once active, the elevation and PV tests are
    loosened by DUSK_EXIT_ELEVATION_C / DUSK_EXIT_PV_KW and the gap by DUSK_EXIT_GAP_RELAX_K.
    A reading that is missing counts as false, and a missing ``rising`` is not dusk.
    """
    gap = None if outside is None else room - outside
    reading = DuskReading(False, elevation, pv_kw, gap)
    if not settings.enabled or gap is None:
        return reading
    if str(sun_state).strip().lower() != SUN_ABOVE_HORIZON or rising is not False:
        return reading
    extra_elevation = DUSK_EXIT_ELEVATION_C if was_active else 0.0
    extra_pv = DUSK_EXIT_PV_KW if was_active else 0.0
    needed_gap = max(settings.gap - DUSK_EXIT_GAP_RELAX_K, 0.0) if was_active else settings.gap
    low_sun = elevation is not None and elevation < settings.elevation + extra_elevation
    weak_pv = pv_kw is not None and pv_kw < settings.pv_kw + extra_pv
    holds = (low_sun or weak_pv) and gap >= needed_gap - 1e-9
    return DuskReading(holds, elevation, pv_kw, gap)


def skipped_reason(gauge: str | None, room: float) -> str:
    """Why coast is not applying while the normal ladder cools."""
    if gauge == "room":
        what = f"ceiling reached {room:.1f} C"
    else:
        what = f"ceiling reached earlier, room {room:.1f} C"
    return f"Coast skipped: {what}, cooling until the normal exit"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def parse_forecast(response: Any, entity_id: str) -> list[tuple[datetime, float]]:
    """The (time, temperature) pairs of a ``weather.get_forecasts`` response; junk is dropped."""
    if not isinstance(response, dict):
        return []
    block = response.get(entity_id)
    entries = block.get("forecast") if isinstance(block, dict) else None
    if not isinstance(entries, list):
        return []
    parsed: list[tuple[datetime, float]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        moment, temp = entry.get("datetime"), entry.get("temperature")
        if isinstance(moment, str):
            try:
                moment = datetime.fromisoformat(moment)
            except ValueError:
                continue
        if not isinstance(moment, datetime):
            continue
        if (
            isinstance(temp, bool)
            or not isinstance(temp, (int, float))
            or not math.isfinite(temp)
        ):
            continue
        parsed.append((_as_utc(moment), float(temp)))
    return parsed


def forecast_minimum(
    entries: list[tuple[datetime, float]], now: datetime, hours: int
) -> tuple[float | None, int]:
    """The lowest forecast temperature in ``[now, now + hours]`` and how many entries it saw.

    Fewer than FORECAST_MIN_ENTRIES in the window is no forecast at all.
    """
    end = now + timedelta(hours=hours)
    inside = [temp for moment, temp in entries if now <= moment <= end]
    if len(inside) < FORECAST_MIN_ENTRIES:
        return None, len(inside)
    return min(inside), len(inside)


@dataclass
class ForecastCache:
    """The last good hourly forecast of one room's coordinator."""

    entries: list[tuple[datetime, float]] = field(default_factory=list)
    fetched_at: datetime | None = None
    attempted_at: datetime | None = None

    def due(self, now: datetime) -> bool:
        """A good forecast is re-read after 30 minutes, a missing one after 2."""
        if self.attempted_at is None:
            return True
        wait = FORECAST_REFRESH_MINUTES if self.usable(now) else FORECAST_RETRY_MINUTES
        return now - self.attempted_at >= timedelta(minutes=wait)

    def age_minutes(self, now: datetime) -> float | None:
        return (
            None
            if self.fetched_at is None
            else (now - self.fetched_at).total_seconds() / 60.0
        )

    def usable(self, now: datetime) -> bool:
        age = self.age_minutes(now)
        return (
            age is not None
            and age <= FORECAST_MAX_AGE_HOURS * 60
            and bool(self.entries)
        )

    def record(self, entries: list[tuple[datetime, float]], now: datetime) -> None:
        """A read happened. An empty or failed one leaves no forecast, not the old one."""
        self.attempted_at = now
        self.entries = entries
        self.fetched_at = now if entries else None

    def minimum(self, now: datetime, hours: int) -> tuple[float | None, int]:
        if not self.usable(now):
            return None, 0
        return forecast_minimum(self.entries, now, hours)
