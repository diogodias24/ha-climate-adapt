# SPDX-License-Identifier: Apache-2.0
"""Shared setup for tests that drive the comfort ladder through the coordinator.

The ladder's comfort temperature follows the outside running mean, which falls
back to a table by calendar month, so a test that leaves the mean alone would
decide differently in different months. Hybrid mode pins Tc to the target the
test sets, which is why `prepare_live` defaults to it: band is
[target - 2.0, target + 1.5], cool sends target + 0.5, heat sends target - 0.5.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

    from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

CLIMATE_SERVICES = (
    "set_temperature",
    "set_hvac_mode",
    "set_fan_mode",
    "turn_off",
    "turn_on",
    "set_preset_mode",
    "set_swing_mode",
)

FULL_UNIT = {
    "hvac_modes": ["off", "heat", "cool", "dry", "fan_only", "heat_cool"],
    "fan_modes": ["auto", "low", "medium", "high"],
    "target_temp_step": 0.5,
}


def register_climate(hass: HomeAssistant, calls: list[ServiceCall]) -> None:
    """Capture every `climate.*` service call in `calls`."""

    async def _capture(call: ServiceCall) -> None:
        calls.append(call)

    for service in CLIMATE_SERVICES:
        hass.services.async_register("climate", service, _capture)


def prepare_live(
    coordinator: ClimateAdaptCoordinator,
    *,
    target: float = 24.0,
    room: float = 24.0,
    humidity: float = 50.0,
    mode: str = "hybrid",
    hvac_mode: str = "heat_cool",
    keep_on: bool = False,
    dry: bool = False,
) -> None:
    """Put the coordinator in a state a decision can be taken from."""
    coordinator.mode = mode
    coordinator.target_temp = target
    coordinator.hvac_mode = hvac_mode
    coordinator.keep_climate_on = keep_on
    coordinator.dry_mode_enabled = dry
    coordinator.temperature = room
    coordinator.perceived_temp = room
    coordinator.perceived_temp_valid = True
    coordinator.humidity = humidity
    coordinator.last_adaptation = None
    coordinator.enabled = True


def seed_running_mean(coordinator: ClimateAdaptCoordinator, trm: float) -> None:
    """Fill the daily means so the running mean is `trm` (8 days, every 30 min)."""
    now = dt_util.now()
    moment = now - timedelta(days=8)
    while moment < now:
        coordinator._daily_means.add(moment, trm)
        moment += timedelta(minutes=30)


async def decide(
    hass: HomeAssistant,
    coordinator: ClimateAdaptCoordinator,
    unit_state: str,
    attributes: dict[str, Any] | None = None,
    **kwargs: Any,
) -> None:
    """Set the climate entity's state and run one decision."""
    hass.states.async_set("climate.office_ac", unit_state, attributes or FULL_UNIT)
    await coordinator.make_decision(hass.states.get("climate.office_ac"), **kwargs)


MOTION_SENSOR = "binary_sensor.office_motion"


def set_motion(
    hass: HomeAssistant,
    freezer: Any,
    state: str,
    quiet_minutes: float = 0.0,
    *,
    entity_id: str = MOTION_SENSOR,
) -> None:
    """Put the motion sensor in `state`, having been there for `quiet_minutes`.

    The clock is moved to a whole second first, so a boundary such as exactly the
    hold is not lost to float rounding of the state's timestamp.
    """
    now = dt_util.utcnow().replace(microsecond=0)
    freezer.move_to(now)
    stamp = (now - timedelta(minutes=quiet_minutes)).timestamp()
    if hass.states.get(entity_id) is not None:
        hass.states.async_remove(entity_id)  # a same-state set keeps last_changed
    hass.states.async_set(entity_id, state, timestamp=stamp)
