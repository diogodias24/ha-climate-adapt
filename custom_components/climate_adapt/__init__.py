# SPDX-License-Identifier: Apache-2.0
"""The Climate Adapt integration."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN
from .coordinator import ClimateAdaptCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import Event, EventStateChangedData, HomeAssistant

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Climate Adapt from a config entry."""
    coordinator = ClimateAdaptCoordinator(hass, entry)

    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    cleaned = None
    for stale_key in ("hvac_mode", "temp_step", "area"):
        if stale_key in entry.options:
            cleaned = cleaned if cleaned is not None else dict(entry.options)
            cleaned.pop(stale_key)
    if cleaned is not None:
        hass.config_entries.async_update_entry(entry, options=cleaned)

    loop = asyncio.get_running_loop()

    def _schedule_refresh() -> None:
        hass.async_create_task(coordinator.async_sensor_changed())

    @callback
    def _handle_sensor_change(_: Event[EventStateChangedData]) -> None:
        loop.call_soon_threadsafe(_schedule_refresh)

    entry.async_on_unload(
        async_track_state_change_event(
            hass,
            [coordinator.temp_sensor, coordinator.humidity_sensor],
            _handle_sensor_change,
        )
    )

    # The climate entity needs its own listener: a change the integration did
    # not issue means the user acted (dashboard, physical remote, voice), and
    # without this the loop would overwrite them on the next tick without
    # knowing. Attribution is by service-call context, inside the coordinator.
    entry.async_on_unload(
        async_track_state_change_event(
            hass,
            [coordinator.climate_entity],
            coordinator.async_on_climate_state_event,
        )
    )

    coordinator.async_track_motion_sensor()
    coordinator.async_track_fresh_air_scene()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    coordinator.async_setup_periodic()
    entry.async_on_unload(coordinator.async_stop_periodic)

    coordinator.offers.async_start()
    entry.async_on_unload(coordinator.offers.async_stop_listening)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: ClimateAdaptCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        coordinator.async_stop_periodic()
        await coordinator.offers.async_shutdown()
        coordinator.clear_stale_issue()
        coordinator.clear_outside_stale_issue()

    return bool(unload_ok)


async def async_migrate_entry(
    hass: HomeAssistant,  # noqa: ARG001
    config_entry: ConfigEntry,  # noqa: ARG001
) -> bool:
    """Migrate old entry data to the current version."""
    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update.

    Deliberately *not* `hass.config_entries.async_reload(entry.entry_id)`:
    a reload re-runs setup, restarts the settle and seed gate and the manual
    cooldown ledger. Copying the values onto the running coordinator has a
    smaller blast radius, and `tests/test_options_correctness.py` asserts it
    stays that way.

    *Which* keys to copy is not listed here. It is derived from
    `OPTION_SPECS`, because a hand-maintained copy list can silently omit an
    option.
    """
    coordinator: ClimateAdaptCoordinator = hass.data[DOMAIN][entry.entry_id]

    changed = coordinator.apply_entry_options()

    if "adaptation_interval" in changed:
        # The poll clock is derived from the value, so copying the value is
        # not enough on its own.
        coordinator.update_interval = timedelta(minutes=coordinator.adaptation_interval)

    if changed & {"enable_motion", "motion_sensor"}:
        coordinator.async_track_motion_sensor()

    if "fresh_air_scene" in changed:
        coordinator.async_track_fresh_air_scene()

    await coordinator.async_request_refresh()
