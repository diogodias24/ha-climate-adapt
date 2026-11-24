# SPDX-License-Identifier: Apache-2.0
"""Tests for dynamic hvac_mode resolution when select is heat_cool.

Hybrid pins Tc to the target, so with target 24.0 the band is 22.0-25.5, cool
sends 24.5 and heat sends 23.5.
"""

import pytest
from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.climate_adapt.const import (
    HVAC_MODE_COOL,
    HVAC_MODE_HEAT,
    HVAC_MODE_HEAT_COOL,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

from .comfort_helpers import FULL_UNIT, decide, prepare_live, register_climate

HOT = 26.5
COLD = 20.0


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    """Create a coordinator instance."""
    mock_config_entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, mock_config_entry)


@pytest.fixture
def calls(hass: HomeAssistant) -> list[ServiceCall]:
    recorded: list[ServiceCall] = []
    register_climate(hass, recorded)
    return recorded


def _prepare(
    coordinator: ClimateAdaptCoordinator, hvac_mode: str, room: float = HOT, **kwargs
) -> None:
    prepare_live(
        coordinator, target=24.0, room=room, hvac_mode=hvac_mode, keep_on=True, **kwargs
    )


class TestDynamicModeResolution:
    """heat_cool select resolves to the direction the room needs."""

    @pytest.mark.asyncio
    async def test_above_threshold_resolves_cool(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Room above the band -> heat_cool resolves to cool."""
        _prepare(coordinator, HVAC_MODE_HEAT_COOL)

        await decide(hass, coordinator, "off")

        assert len(calls) == 1
        assert calls[0].service == "set_temperature"
        assert calls[0].data["hvac_mode"] == HVAC_MODE_COOL
        assert calls[0].data["temperature"] == 24.5

    @pytest.mark.asyncio
    async def test_below_band_resolves_heat(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Room below the band -> heat_cool resolves to heat."""
        _prepare(coordinator, HVAC_MODE_HEAT_COOL, room=COLD)

        await decide(hass, coordinator, "off")

        assert len(calls) == 1
        assert calls[0].service == "set_temperature"
        assert calls[0].data["hvac_mode"] == HVAC_MODE_HEAT
        assert calls[0].data["temperature"] == 23.5

    @pytest.mark.asyncio
    async def test_in_band_keep_climate_maintains(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """In band with keep_climate_on -> no mode command at all."""
        _prepare(coordinator, HVAC_MODE_HEAT_COOL, room=24.0)

        await decide(hass, coordinator, "cool")

        assert [c for c in calls if c.service == "set_hvac_mode"] == []

    @pytest.mark.asyncio
    async def test_heat_cool_same_direction_resends_both(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Same-direction tick re-sends mode AND setpoint (IR retry)."""
        _prepare(coordinator, HVAC_MODE_HEAT_COOL)

        await decide(hass, coordinator, "cool", {**FULL_UNIT, "temperature": 22.0})

        assert len(calls) == 1
        assert calls[0].service == "set_temperature"
        assert calls[0].data["hvac_mode"] == HVAC_MODE_COOL
        assert calls[0].data["temperature"] == 24.5

    @pytest.mark.asyncio
    async def test_pinned_cool_stays_cool_even_when_cold(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Select pinned to cool never commands heat."""
        _prepare(coordinator, HVAC_MODE_COOL, room=COLD)

        await decide(hass, coordinator, "off")

        assert calls == []
        assert coordinator.comfort["action"] == "off"

    @pytest.mark.asyncio
    async def test_pinned_heat_stays_heat_even_when_hot(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Select pinned to heat never commands cool."""
        _prepare(coordinator, HVAC_MODE_HEAT)

        await decide(hass, coordinator, "off")

        assert calls == []
        assert coordinator.comfort["action"] == "off"

    @pytest.mark.asyncio
    async def test_dry_row_unaffected_by_dynamic_resolution(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Dry path still works: humid in-band heat_cool can go dry."""
        _prepare(coordinator, HVAC_MODE_HEAT_COOL, room=24.0, humidity=67.0, dry=True)

        await decide(hass, coordinator, "cool")

        assert [c.service for c in calls] == ["set_hvac_mode"]
        assert calls[0].data["hvac_mode"] == "dry"
        assert coordinator.dry_active is True


class TestResendEveryTick:
    """Out-of-band decisions re-send mode+setpoint every tick (one-way IR)."""

    @pytest.mark.asyncio
    async def test_above_band_matching_state_resends(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """AC believed cool@22, room still hot -> setpoint re-sent anyway."""
        _prepare(coordinator, HVAC_MODE_HEAT_COOL)

        await decide(hass, coordinator, "cool", {**FULL_UNIT, "temperature": 22.0})

        temp_calls = [c for c in calls if c.service == "set_temperature"]
        assert len(temp_calls) == 1
        assert temp_calls[0].data["temperature"] == 24.5

    @pytest.mark.asyncio
    async def test_above_band_matching_state_resends_mode(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Believed mode and setpoint already match -> both re-sent for IR retry."""
        _prepare(coordinator, HVAC_MODE_HEAT_COOL)

        await decide(hass, coordinator, "cool", {**FULL_UNIT, "temperature": 24.5})

        assert len(calls) == 1
        assert calls[0].service == "set_temperature"
        assert calls[0].data["hvac_mode"] == HVAC_MODE_COOL
        assert calls[0].data["temperature"] == 24.5
