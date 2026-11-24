# SPDX-License-Identifier: Apache-2.0
"""Tests for Hybrid mode acting on the climate entity.

The decision loop must run in both Auto and Hybrid so the manual target
actually steers the AC: in Hybrid the comfort ladder's centre is pinned to it.
"""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.climate_adapt.const import (
    HVAC_MODE_COOL,
    HVAC_MODE_HEAT,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

from .comfort_helpers import decide, prepare_live, register_climate


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    """Create a coordinator instance."""
    mock_config_entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, mock_config_entry)


class TestHybridActs:
    """Hybrid mode must command the AC, not just compute."""

    @pytest.mark.asyncio
    async def test_hybrid_heat_below_band(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Room under the Hybrid band -> AC commanded to heat at target - 0.5."""
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=25.0, room=20.0)

        await decide(hass, coordinator, "off")

        assert [c.service for c in calls] == ["set_temperature"]
        assert (calls[0].data["hvac_mode"], calls[0].data["temperature"]) == (
            HVAC_MODE_HEAT,
            24.5,
        )

    @pytest.mark.asyncio
    async def test_hybrid_cool_above_band(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Room over the Hybrid band (26.5 for target 25) -> cool at target + 0.5."""
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=25.0, room=27.0)

        await decide(hass, coordinator, "off")

        assert [c.service for c in calls] == ["set_temperature"]
        assert (calls[0].data["hvac_mode"], calls[0].data["temperature"]) == (
            HVAC_MODE_COOL,
            25.5,
        )
