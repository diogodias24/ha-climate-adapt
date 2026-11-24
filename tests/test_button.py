# SPDX-License-Identifier: Apache-2.0
"""Tests for Climate Adapt button platform."""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.climate_adapt.const import DOMAIN


@pytest.mark.asyncio
async def test_send_now_button_exists(hass: HomeAssistant, mock_config_entry) -> None:
    """The Send Now button is created, and it is the only button."""
    mock_config_entry.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "cool", {"temperature": 24.0})

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(f"button.{DOMAIN.lower()}_send_now") is not None
    assert len(hass.states.async_entity_ids("button")) == 1


@pytest.mark.asyncio
async def test_retired_learning_buttons_not_present(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """The Learn, Reset and Analyze buttons are gone."""
    mock_config_entry.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "off")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    for key in ("learn_now", "reset_learning", "analyze_history"):
        assert hass.states.get(f"button.{DOMAIN.lower()}_{key}") is None


@pytest.mark.asyncio
async def test_removed_apply_effective_target_not_present(
    hass: HomeAssistant,
    mock_config_entry,
) -> None:
    """Test the removed apply effective target button is no longer created."""
    mock_config_entry.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "off")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(f"button.{DOMAIN.lower()}_apply_effective_target") is None
