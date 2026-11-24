# SPDX-License-Identifier: Apache-2.0
"""Tests for Climate Adapt switch platform."""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.climate_adapt.const import DOMAIN, MODE_AUTO, MODE_HYBRID


async def _setup(hass: HomeAssistant, mock_config_entry) -> None:
    mock_config_entry.add_to_hass(hass)
    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "65.0")
    hass.states.async_set("climate.office_ac", "off")
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_mode_switch(hass: HomeAssistant, mock_config_entry) -> None:
    """Test mode switch entity."""
    await _setup(hass, mock_config_entry)

    state = hass.states.get(f"switch.{DOMAIN.lower()}_mode")
    assert state is not None
    assert state.state == "on"  # ON = Auto mode


@pytest.mark.asyncio
async def test_turn_off_mode(hass: HomeAssistant, mock_config_entry) -> None:
    """Test turning off mode (switch to Hybrid)."""
    await _setup(hass, mock_config_entry)

    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": f"switch.{DOMAIN.lower()}_mode"},
        blocking=True,
    )

    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    assert coordinator.mode == MODE_HYBRID


@pytest.mark.asyncio
async def test_turn_on_mode(hass: HomeAssistant, mock_config_entry) -> None:
    """Test turning on mode (switch to Auto)."""
    await _setup(hass, mock_config_entry)

    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    coordinator.mode = MODE_HYBRID

    await hass.services.async_call(
        "switch",
        "turn_on",
        {"entity_id": f"switch.{DOMAIN.lower()}_mode"},
        blocking=True,
    )

    assert coordinator.mode == MODE_AUTO


@pytest.mark.asyncio
async def test_keep_climate_on_switch(
    hass: HomeAssistant,
    mock_config_entry,
) -> None:
    """Test keep climate on switch toggles the coordinator flag."""
    await _setup(hass, mock_config_entry)

    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    entity = f"switch.{DOMAIN.lower()}_keep_climate_on"

    state = hass.states.get(entity)
    assert state is not None
    assert state.state == "off"

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity}, blocking=True
    )
    assert coordinator.keep_climate_on is True

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity}, blocking=True
    )
    assert coordinator.keep_climate_on is False


@pytest.mark.asyncio
async def test_dry_mode_switch(hass: HomeAssistant, mock_config_entry) -> None:
    """Test dry mode switch toggles the coordinator flag."""
    await _setup(hass, mock_config_entry)

    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    entity = f"switch.{DOMAIN.lower()}_dry_mode"

    state = hass.states.get(entity)
    assert state is not None
    assert state.state == "off"

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity}, blocking=True
    )
    assert coordinator.dry_mode_enabled is True

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity}, blocking=True
    )
    assert coordinator.dry_mode_enabled is False


@pytest.mark.asyncio
async def test_enabled_switch(
    hass: HomeAssistant,
    mock_config_entry,
) -> None:
    """A fresh room is disabled; the switch toggles the coordinator flag."""
    await _setup(hass, mock_config_entry)

    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    entity = f"switch.{DOMAIN.lower()}_enabled"

    state = hass.states.get(entity)
    assert state is not None
    assert state.state == "off"
    assert coordinator.enabled is False

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": entity}, blocking=True
    )
    assert coordinator.enabled is True
    assert hass.states.get(entity).state == "on"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity}, blocking=True
    )
    assert coordinator.enabled is False


@pytest.mark.asyncio
async def test_removed_switches_not_present(
    hass: HomeAssistant,
    mock_config_entry,
) -> None:
    """Test the removed toggles are no longer created."""
    await _setup(hass, mock_config_entry)

    for suffix in (
        "auto_mode",
        "keep_manual_target",
        "factor_history",
        "factor_science",
        "factor_comfort",
        "factor_outdoor",
        "manual_mode",
        "prioritize_history",
    ):
        assert hass.states.get(f"switch.{DOMAIN.lower()}_{suffix}") is None
