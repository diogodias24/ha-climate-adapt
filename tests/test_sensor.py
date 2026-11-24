# SPDX-License-Identifier: Apache-2.0
"""Tests for Climate Adapt sensor platform."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.const import DOMAIN


@pytest.mark.asyncio
async def test_perceived_temp_sensor(hass: HomeAssistant, mock_config_entry) -> None:
    """Test heat index sensor creation and state."""
    mock_config_entry.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "off")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get(f"sensor.{DOMAIN.lower()}_perceived_temperature")
    assert state is not None


@pytest.mark.asyncio
async def test_perceived_temp_sensor_attributes(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test heat index sensor exposes rich diagnostics."""
    mock_config_entry.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "cool")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    coordinator.mode = "hybrid"
    coordinator.last_adaptation = dt_util.utcnow()
    coordinator.async_set_updated_data(coordinator.build_state())
    await hass.async_block_till_done()

    state = hass.states.get(f"sensor.{DOMAIN.lower()}_perceived_temperature")
    assert state is not None
    assert state.state != "unknown"
    assert state.attributes.get("unit_of_measurement") == "°C"

    attrs = state.attributes
    assert attrs["target_temp"] == coordinator.target_temp
    assert attrs["effective_target"] == coordinator.get_published_target()
    assert attrs["mode"] == coordinator.mode
    assert attrs["ac_state"] == "cool"
    for retired in ("season", "outside_influence", "historical_base"):
        assert retired not in attrs
    assert attrs["motion_active"] == coordinator.motion_active
    assert attrs["last_adaptation"] == coordinator.last_adaptation.isoformat()


@pytest.mark.asyncio
async def test_button_activity_sensor_shows_pressed_and_returned(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test the activity sensor shows what was pressed and the returned values."""
    mock_config_entry.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "cool")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    coordinator.last_button_action = "send_now"
    coordinator.last_button_time = dt_util.utcnow()
    coordinator.last_button_result = "success: sent heat @ 24.0"
    coordinator.last_button_data = {
        "status": "success",
        "recommended_temperature": 24.0,
    }
    coordinator.async_set_updated_data(coordinator.build_state())
    await hass.async_block_till_done()

    state = hass.states.get(f"sensor.{DOMAIN.lower()}_button_activity")
    assert state is not None
    assert state.state == "send_now"
    assert state.attributes["last_pressed"] == coordinator.last_button_time.isoformat()
    assert "sent heat" in state.attributes["result"]
    assert state.attributes["returned"]["recommended_temperature"] == 24.0


async def _setup_room(hass: HomeAssistant, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "cool")
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][mock_config_entry.entry_id]


@pytest.mark.asyncio
async def test_recommended_temperature_sensor_publishes_the_comfort_temperature(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """A 25.0 manual target and a 23.1 comfort centre publish 23.1."""
    coordinator = await _setup_room(hass, mock_config_entry)
    coordinator.mode = "auto"
    coordinator.target_temp = 25.0
    assert coordinator.comfort is not None
    coordinator.comfort = {**coordinator.comfort, "comfort_temperature": 23.14}
    coordinator.async_set_updated_data(coordinator.build_state())
    await hass.async_block_till_done()

    state = hass.states.get(f"sensor.{DOMAIN.lower()}_recommended_temperature")
    assert state is not None
    assert state.state == "23.1"
    assert coordinator.get_recommended_temperature() == 23.1
    assert state.attributes["unit_of_measurement"] == "°C"
    assert state.attributes["source"] == "comfort"
    # The stored target is a leftover in Auto; no surface shows it as 25.0.
    assert "manual_target" not in state.attributes
    effective = hass.states.get(f"sensor.{DOMAIN.lower()}_effective_target")
    assert "manual_target" not in effective.attributes
    assert effective.attributes["recommended_temperature"] == 23.1
    perceived = hass.states.get(f"sensor.{DOMAIN.lower()}_perceived_temperature")
    assert "target_temp" not in perceived.attributes


@pytest.mark.asyncio
async def test_recommended_temperature_hybrid_still_shows_the_manual_target(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """In Hybrid the typed target is the user's setting and stays visible."""
    coordinator = await _setup_room(hass, mock_config_entry)
    coordinator.mode = "hybrid"
    coordinator.target_temp = 25.0
    coordinator.async_set_updated_data(coordinator.build_state())
    await hass.async_block_till_done()

    state = hass.states.get(f"sensor.{DOMAIN.lower()}_recommended_temperature")
    assert state is not None
    perceived = hass.states.get(f"sensor.{DOMAIN.lower()}_perceived_temperature")
    assert perceived.attributes["target_temp"] == 25.0
