# SPDX-License-Identifier: Apache-2.0
"""Tests for Climate Adapt number platform."""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.climate_adapt.const import (
    DEFAULT_TARGET_TEMP,
    DOMAIN,
    MODE_HYBRID,
)


@pytest.mark.asyncio
async def test_target_temperature_number_unavailable_in_auto(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test target temperature number entity is unavailable in Auto mode."""
    mock_config_entry.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "off")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    coordinator.enabled = True
    coordinator.async_set_updated_data(coordinator.build_state())
    await hass.async_block_till_done()

    state = hass.states.get(f"number.{DOMAIN.lower()}_target_temperature")
    assert state is not None
    assert state.state == "unavailable"


@pytest.mark.asyncio
async def test_target_temperature_number_available_in_hybrid(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test target temperature number entity is available in Hybrid mode."""
    mock_config_entry.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "off")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    coordinator.mode = MODE_HYBRID
    coordinator.enabled = True
    coordinator.async_set_updated_data(coordinator.build_state())
    await hass.async_block_till_done()

    state = hass.states.get(f"number.{DOMAIN.lower()}_target_temperature")
    assert state is not None
    assert float(state.state) == DEFAULT_TARGET_TEMP


@pytest.mark.asyncio
async def test_set_target_temperature(hass: HomeAssistant, mock_config_entry) -> None:
    """Test setting target temperature value in Hybrid mode."""
    mock_config_entry.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "off")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    coordinator.mode = MODE_HYBRID
    coordinator.enabled = True
    coordinator.async_set_updated_data(coordinator.build_state())
    await hass.async_block_till_done()

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": f"number.{DOMAIN.lower()}_target_temperature", "value": 23.5},
        blocking=True,
    )

    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    assert coordinator.target_temp == 23.5
    # A manual change starts the cooldown.
    assert coordinator.manual_override_until is not None
