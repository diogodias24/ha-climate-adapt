# SPDX-License-Identifier: Apache-2.0
"""`motion_hold_minutes` is a real per-entry option, hot-copied on save."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from homeassistant.data_entry_flow import InvalidData

from custom_components.climate_adapt import update_listener
from custom_components.climate_adapt.const import (
    CONF_MANUAL_COOLDOWN,
    CONF_MOTION_HOLD_MINUTES,
    CONF_MOTION_SENSOR,
    DEFAULT_MOTION_HOLD_MINUTES,
)
from custom_components.climate_adapt.coordinator import SOURCE_DEFAULT, SOURCE_OPTIONS

from .test_options_correctness import (
    MOTION_SENSOR,
    OTHER_MOTION_SENSOR,
    _coordinator,
    _options_entry,
    _save_options,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@pytest.mark.asyncio
async def test_the_default_is_twenty_minutes(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass, _options_entry())

    assert coordinator.motion_hold_minutes == DEFAULT_MOTION_HOLD_MINUTES == 20
    resolved = coordinator.resolved_options()[CONF_MOTION_HOLD_MINUTES]
    assert resolved["resolved_from"] == SOURCE_DEFAULT


@pytest.mark.asyncio
async def test_the_form_offers_the_running_value(hass: HomeAssistant) -> None:
    entry = _options_entry(options={CONF_MOTION_HOLD_MINUTES: 35})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    key = next(k for k in result["data_schema"].schema if str(k) == CONF_MOTION_HOLD_MINUTES)
    assert key.default() == 35


@pytest.mark.asyncio
async def test_an_options_edit_takes_effect_without_a_restart(hass: HomeAssistant) -> None:
    entry = _options_entry()
    coordinator = _coordinator(hass, entry)

    await _save_options(hass, entry, **{CONF_MOTION_HOLD_MINUTES: 45.0})
    await update_listener(hass, entry)

    assert coordinator.motion_hold_minutes == 45
    assert isinstance(coordinator.motion_hold_minutes, int)
    resolved = coordinator.resolved_options()[CONF_MOTION_HOLD_MINUTES]
    assert (resolved["value"], resolved["resolved_from"], resolved["applied"]) == (
        45,
        SOURCE_OPTIONS,
        True,
    )
    assert coordinator.build_diagnostics()["inputs"]["motion"]["hold_minutes"] == 45


@pytest.mark.asyncio
async def test_an_unrelated_edit_does_not_reset_it(hass: HomeAssistant) -> None:
    entry = _options_entry(options={CONF_MOTION_HOLD_MINUTES: 45})
    coordinator = _coordinator(hass, entry)
    assert coordinator.motion_hold_minutes == 45

    # Through the form: the field arrives pre-filled with the running value.
    await _save_options(hass, entry, **{CONF_MANUAL_COOLDOWN: 90})
    await update_listener(hass, entry)
    assert coordinator.motion_hold_minutes == 45
    assert entry.options[CONF_MOTION_HOLD_MINUTES] == 45

    # And by a write that never mentions it.
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_MANUAL_COOLDOWN: 120}
    )
    await update_listener(hass, entry)
    assert coordinator.motion_hold_minutes == 45
    assert coordinator.manual_cooldown_minutes == 120


@pytest.mark.parametrize("value", [0, 241])
@pytest.mark.asyncio
async def test_the_form_bounds_are_one_to_two_hundred_forty(
    hass: HomeAssistant, value: int
) -> None:
    entry = _options_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], user_input={CONF_MOTION_HOLD_MINUTES: value}
        )


@pytest.mark.asyncio
async def test_a_new_motion_sensor_moves_the_subscription(hass: HomeAssistant) -> None:
    entry = _options_entry()
    coordinator = _coordinator(hass, entry)
    coordinator.async_track_motion_sensor()
    first = coordinator._motion_unsub
    assert first is not None

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_MOTION_SENSOR: OTHER_MOTION_SENSOR}
    )
    await update_listener(hass, entry)

    assert coordinator.motion_sensor == OTHER_MOTION_SENSOR != MOTION_SENSOR
    assert coordinator._motion_unsub is not None
    assert coordinator._motion_unsub is not first

    hass.config_entries.async_update_entry(entry, options={**entry.options, "enable_motion": False})
    await update_listener(hass, entry)
    assert coordinator._motion_unsub is None
