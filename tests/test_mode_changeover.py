# SPDX-License-Identifier: Apache-2.0
"""A completed change of direction restarts the dwell.

The 120-minute dwell and the confirming ticks are the ladder's direction guard
(``decision._guard``, covered in ``test_decision.TestDirectionGuard`` and
at the coordinator in ``test_comfort_wiring.TestDirectionGuard``). What
is pinned here is the loop over several ticks: a flip stamps the direction
clock, so a room swinging across the band cannot flip twice inside the dwell.
"""

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.const import (
    HVAC_MODE_COOL,
    HVAC_MODE_HEAT,
    MODE_CONFIRM_TICKS,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

from .comfort_helpers import decide, prepare_live, register_climate


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    """Create a coordinator instance."""
    mock_config_entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, mock_config_entry)


class TestMinimumDwell:
    """Time is one of the anti-flap mechanisms."""

    @pytest.mark.asyncio
    async def test_a_completed_flip_restarts_the_dwell_and_bounds_oscillation(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A room swinging across the band cannot flip twice inside the dwell."""
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=24.0, room=27.0)
        coordinator.last_run_direction = HVAC_MODE_HEAT
        before = dt_util.utcnow() - timedelta(minutes=130)
        coordinator.last_direction_change = before

        await decide(hass, coordinator, HVAC_MODE_HEAT)
        coordinator.last_adaptation = None
        await decide(hass, coordinator, HVAC_MODE_HEAT)

        assert [c.data["hvac_mode"] for c in calls if c.service == "set_temperature"] == [
            HVAC_MODE_COOL
        ]
        assert coordinator.last_direction_change > before
        stamped = coordinator.last_direction_change

        coordinator.temperature = coordinator.perceived_temp = 20.0
        for _ in range(MODE_CONFIRM_TICKS):
            coordinator.last_adaptation = None
            await decide(hass, coordinator, HVAC_MODE_COOL)

        assert [c.data["hvac_mode"] for c in calls if c.service == "set_temperature"] == [
            HVAC_MODE_COOL
        ]
        assert "refused" in coordinator.comfort["last_refusal"]
        assert coordinator.last_direction_change == stamped
