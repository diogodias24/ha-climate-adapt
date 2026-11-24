# SPDX-License-Identifier: Apache-2.0
"""Tests for the redesigned Hybrid mode: the user's target IS the setpoint.

Hybrid is the "I know what I want" lever: target_temp is commanded directly,
and the comfort band derives from it (target - 2.0 .. target + 1.5).
"""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.climate_adapt.const import HVAC_MODE_COOL, HVAC_MODE_HEAT
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

from .comfort_helpers import decide, prepare_live, register_climate, seed_running_mean


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    """Create a coordinator instance."""
    mock_config_entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, mock_config_entry)


class TestHybridUserTargetIsSetpoint:
    """Hybrid pins the comfort temperature to the user's target."""

    @pytest.mark.asyncio
    async def test_below_band_commands_user_target(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Room under the band -> heat at the user's target - 0.5."""
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=24.5, room=20.0)

        await decide(hass, coordinator, "off")

        assert [(c.data["hvac_mode"], c.data["temperature"]) for c in calls] == [
            (HVAC_MODE_HEAT, 24.0)
        ]

    @pytest.mark.asyncio
    async def test_above_band_commands_user_target(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Room over the band -> cool at the user's target + 0.5."""
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=24.5, room=28.0)

        await decide(hass, coordinator, "off")

        assert [(c.data["hvac_mode"], c.data["temperature"]) for c in calls] == [
            (HVAC_MODE_COOL, 25.0)
        ]

    @pytest.mark.asyncio
    async def test_band_derives_from_target(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Comfort temperature = target, so the band is [target - 2.0, target + 1.5]."""
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=24.5, room=24.4)

        await decide(hass, coordinator, "off")

        assert calls == []
        assert coordinator.comfort["comfort_temperature"] == 24.5
        assert coordinator.comfort["band_low"] == 22.5
        assert coordinator.comfort["band_high"] == 26.0

        prepare_live(coordinator, target=20.0, room=24.4)
        await decide(hass, coordinator, "off")

        assert coordinator.comfort["comfort_temperature"] == 20.0
        assert coordinator.comfort["band_low"] == 18.0
        assert coordinator.comfort["band_high"] == 21.5


class TestHybridNoOutsideInfluence:
    """Hybrid ignores the outside: the user's number is absolute."""

    @pytest.mark.asyncio
    async def test_outside_sensor_does_not_shift_band(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A cold outside must not pull the Hybrid comfort temperature down.

        Auto with this running mean (5 C) would settle at 21.0 and leave a
        22.0 C room alone; Hybrid keeps the user's 24.5, whose band starts at
        22.5 (heat starts 0.4 K under it), so the same room is heated.
        """
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=24.5, room=22.0)
        seed_running_mean(coordinator, 5.0)

        await decide(hass, coordinator, "off")

        assert coordinator.comfort["comfort_temperature"] == 24.5
        assert [(c.data["hvac_mode"], c.data["temperature"]) for c in calls] == [
            (HVAC_MODE_HEAT, 24.0)
        ]


class TestAutoUnchanged:
    """Auto mode ignores the manual target."""

    @pytest.mark.asyncio
    async def test_auto_follows_the_outside_not_the_manual_target(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Running mean 20 C -> Tc 24.0, whatever the number entity says."""
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=30.0, room=20.0, mode="auto")
        seed_running_mean(coordinator, 20.0)

        await decide(hass, coordinator, "off")

        assert coordinator.comfort["comfort_temperature"] == pytest.approx(24.0, abs=0.05)
        assert [(c.data["hvac_mode"], c.data["temperature"]) for c in calls] == [
            (HVAC_MODE_HEAT, 23.5)
        ]


class TestEffectiveAttrs:
    """The effective_target attribute exposes the redesign coherently."""

    @pytest.mark.asyncio
    async def test_hybrid_effective_target_is_what_was_sent(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """build_state publishes the setpoint the ladder commanded."""
        register_climate(hass, calls := [])
        prepare_live(coordinator, target=24.5, room=20.0)

        await decide(hass, coordinator, "off")

        assert calls
        assert coordinator.build_state()["effective_target"] == 24.0

    def test_retired_knob_is_gone(self) -> None:
        """The zombie heat_threshold knob no longer exists on the coordinator."""
        assert not hasattr(ClimateAdaptCoordinator, "heat_threshold")
