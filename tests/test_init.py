# SPDX-License-Identifier: Apache-2.0
"""Tests for Climate Adapt integration setup."""

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant, is_callback
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.climate_adapt as init_mod
from custom_components.climate_adapt.const import DOMAIN


@pytest.mark.asyncio
async def test_setup_and_unload(hass: HomeAssistant, mock_config_entry) -> None:
    """Test setting up and unloading the integration."""
    mock_config_entry.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "off")

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert DOMAIN in hass.data
    assert mock_config_entry.entry_id in hass.data[DOMAIN]

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.entry_id not in hass.data[DOMAIN]


@pytest.mark.asyncio
async def test_setup_creates_entities(hass: HomeAssistant, mock_config_entry) -> None:
    """Test that setup creates all expected entities."""
    mock_config_entry.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "off")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(f"sensor.{DOMAIN.lower()}_perceived_temperature") is not None
    assert hass.states.get(f"sensor.{DOMAIN.lower()}_mode") is not None
    assert hass.states.get(f"sensor.{DOMAIN.lower()}_button_activity") is not None
    assert (
        hass.states.get(f"sensor.{DOMAIN.lower()}_recommended_temperature") is not None
    )
    assert hass.states.get(f"sensor.{DOMAIN.lower()}_effective_target") is not None
    assert hass.states.get(f"number.{DOMAIN.lower()}_target_temperature") is not None
    assert hass.states.get(f"switch.{DOMAIN.lower()}_mode") is not None
    assert hass.states.get(f"switch.{DOMAIN.lower()}_keep_climate_on") is not None
    assert hass.states.get(f"button.{DOMAIN.lower()}_send_now") is not None
    assert hass.states.get(f"button.{DOMAIN.lower()}_apply_effective_target") is None


@pytest.mark.asyncio
async def test_setup_with_outside_sensors(
    hass: HomeAssistant, mock_config_entry_with_outside
) -> None:
    """Test setup with outside sensors configured."""
    mock_config_entry_with_outside.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("sensor.outside_temperature", "32.0")
    hass.states.async_set("sensor.outside_humidity", "70.0")
    hass.states.async_set("climate.office_ac", "off")

    await hass.config_entries.async_setup(mock_config_entry_with_outside.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][mock_config_entry_with_outside.entry_id]
    assert coordinator.outside_temp_sensor == "sensor.outside_temperature"
    assert coordinator.outside_humidity_sensor == "sensor.outside_humidity"


@pytest.mark.asyncio
async def test_setup_with_motion_sensor(
    hass: HomeAssistant, mock_config_entry_with_motion
) -> None:
    """Test setup with motion sensor configured."""
    mock_config_entry_with_motion.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("binary_sensor.office_motion", "on")
    hass.states.async_set("climate.office_ac", "off")

    await hass.config_entries.async_setup(mock_config_entry_with_motion.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][mock_config_entry_with_motion.entry_id]
    assert coordinator.enable_motion is True
    assert coordinator.motion_sensor == "binary_sensor.office_motion"


@pytest.mark.asyncio
async def test_sensor_change_triggers_throttled_refresh(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test that sensor changes trigger refresh, throttled to avoid command spam."""
    mock_config_entry.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "cool")

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    coordinator.auto_mode = False
    first_hi = coordinator.perceived_temp
    assert first_hi > 0

    # Within the throttle window the refresh is skipped.
    coordinator._last_sensor_refresh = dt_util.now()
    hass.states.async_set("sensor.office_temperature", "30.0")
    await hass.async_block_till_done()
    assert coordinator.perceived_temp == first_hi

    # Once the throttle is cleared, the next change refreshes immediately.
    coordinator._last_sensor_refresh = None
    hass.states.async_set("sensor.office_humidity", "80.0")
    await hass.async_block_till_done()
    assert coordinator.perceived_temp != first_hi


@pytest.mark.asyncio
async def test_stale_hvac_mode_option_is_cleaned(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """Test that a stale hvac_mode option is removed during setup."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Office Auto AC",
        data=dict(mock_config_entry.data),
        options={"hvac_mode": "auto"},
    )
    entry.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "cool")

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert "hvac_mode" not in entry.options


@pytest.mark.asyncio
async def test_sensor_change_listener_is_event_loop_callback(
    hass: HomeAssistant, mock_config_entry
) -> None:
    """The state-change listener must be @callback so HassJob keeps it on the loop."""
    mock_config_entry.add_to_hass(hass)

    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "cool")

    captured = []
    orig = init_mod.async_track_state_change_event

    def spy(hass, entity_ids, action):
        captured.append((list(entity_ids), action))
        return orig(hass, entity_ids, action)

    with patch.object(init_mod, "async_track_state_change_event", spy):
        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        try:
            tracked = [entity for entity_ids, _ in captured for entity in entity_ids]
            assert tracked == [
                "sensor.office_temperature",
                "sensor.office_humidity",
                "climate.office_ac",
            ], "both sensors and the climate entity must be tracked"
            for entity_ids, action in captured:
                assert is_callback(action), (
                    f"state change listener for {entity_ids} must be decorated "
                    "with @callback so HassJob routes it on the event loop, "
                    "not the executor"
                )
        finally:
            await hass.config_entries.async_unload(mock_config_entry.entry_id)
            await hass.async_block_till_done()
