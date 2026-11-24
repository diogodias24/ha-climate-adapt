# SPDX-License-Identifier: Apache-2.0
"""The selected mode persists after a button press."""

import asyncio

import pytest
from homeassistant.core import HomeAssistant

from custom_components.climate_adapt.const import DOMAIN, MODE_HYBRID


@pytest.mark.asyncio
async def test_mode_persistence_after_send_now(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test that mode persists after Send Now."""
    mock_config_entry.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "off")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]

    # Switch to Hybrid mode
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": f"switch.{DOMAIN.lower()}_mode"},
        blocking=True,
    )

    assert coordinator.mode == MODE_HYBRID

    # Check storage
    stored = await coordinator.store.async_load()
    assert stored["mode"] == MODE_HYBRID

    # Press the Send Now button
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": f"button.{DOMAIN.lower()}_send_now"},
        blocking=True,
    )

    # Wait for the press to be handled
    await hass.async_block_till_done()

    # Add a delay to simulate real-world conditions
    await asyncio.sleep(1)
    await hass.async_block_till_done()

    # Check that mode is still Hybrid
    assert coordinator.mode == MODE_HYBRID

    # Check storage again
    stored = await coordinator.store.async_load()
    assert stored["mode"] == MODE_HYBRID
