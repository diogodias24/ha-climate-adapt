# SPDX-License-Identifier: Apache-2.0
"""The command ledger must name the branch that decided to send.

A reason that claims a band violation for an in-band maintenance re-send or a
dry-mode exit would mislead a diagnosis. The reason is the comfort ladder's own
plain sentence, so each row of the ladder is told apart by what it says about
the band.
"""

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

from .comfort_helpers import FULL_UNIT, decide, prepare_live, register_climate

# Hybrid pins Tc = 24.0: band 22.0-25.5, heat sends 23.5, cool sends 24.5.
EDGES = "band 22.0-25.5, comfort 24.0, cooling starts over 24.8"


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    """Create a coordinator instance."""
    mock_config_entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, mock_config_entry)


class TestLedgerReasons:
    """Each sending call site records its own reason."""

    @pytest.mark.asyncio
    async def test_above_band_says_cooling_from_above(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A room at the ceiling says it reached it and the fan is not enough."""
        prepare_live(coordinator, room=26.0)
        register_climate(hass, [])

        await decide(hass, coordinator, "cool")

        assert coordinator.last_command is not None
        assert coordinator.last_command["reason"] == (
            f"Room 26.0 C has reached the comfort ceiling 25.5 C ({EDGES}) "
            "and the fan is not enough: cooling to 24.5 C."
        )

    @pytest.mark.asyncio
    async def test_over_the_trigger_says_fan_first(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A room over the trigger but under the ceiling starts the fan first."""
        prepare_live(coordinator, room=25.0)
        register_climate(hass, [])

        await decide(hass, coordinator, "off")

        assert coordinator.last_command is not None
        assert coordinator.last_command["mode"] == "fan_only"
        assert coordinator.last_command["reason"] == (
            f"Room 25.0 C is over the cooling trigger ({EDGES}): fan first, high speed. "
            "Cooling starts if the room reaches 25.5 C or the fan does not lower it "
            "0.3 K in 25 min."
        )

    @pytest.mark.asyncio
    async def test_below_band_is_distinguishable_from_above_band(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A too-cold activation says the room is below the band."""
        prepare_live(coordinator, room=15.0)
        register_climate(hass, [])

        await decide(hass, coordinator, "heat")

        assert coordinator.last_command is not None
        assert coordinator.last_command["reason"] == (
            f"Room 15.0 C is below the comfort band ({EDGES}): heating to 23.5 C."
        )

    @pytest.mark.asyncio
    async def test_in_band_hold_is_not_labelled_as_a_band_violation(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The keep_climate_on hold says the room is inside the band."""
        prepare_live(coordinator, room=24.0, keep_on=True)
        register_climate(hass, calls := [])

        await decide(hass, coordinator, "cool")

        assert calls == []
        assert coordinator.last_command is not None
        assert coordinator.last_command["kind"] == "skip_comfort_hold"
        reason = coordinator.last_command["reason"]
        assert reason == (
            f"Room 24.0 C is inside the comfort band ({EDGES}): nothing to do."
        )

    @pytest.mark.asyncio
    async def test_dry_exit_is_not_labelled_as_a_band_violation(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Turning a dry unit off because the air is no longer humid says in-band."""
        prepare_live(coordinator, room=24.0, humidity=50.0, dry=True)
        register_climate(hass, calls := [])

        await decide(hass, coordinator, "dry")

        assert [c.service for c in calls] == ["turn_off"]
        assert coordinator.last_command is not None
        reason = coordinator.last_command["reason"]
        assert reason == (
            f"Room 24.0 C is inside the comfort band ({EDGES}): nothing to do."
        )

    @pytest.mark.asyncio
    async def test_dry_entry_and_repeat_say_the_air_is_humid(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Entering dry and re-sending dry both name the humidity, not the band."""
        prepare_live(coordinator, room=24.0, humidity=85.0, dry=True)
        register_climate(hass, calls := [])
        expected = (
            f"Room 24.0 C is comfortable ({EDGES}) but the air is humid: drying."
        )

        await decide(hass, coordinator, "cool")

        assert coordinator.last_command is not None
        assert coordinator.last_command["reason"] == expected

        coordinator.last_adaptation = datetime.now(UTC) - timedelta(
            minutes=coordinator.adaptation_interval + 1
        )
        await decide(hass, coordinator, "dry")

        assert [c.data["hvac_mode"] for c in calls] == ["dry", "dry"]
        assert coordinator.last_command["reason"] == expected


class TestSendNowKeepsItsSource:
    """Send Now is identified by `source`, not by overwriting the reason."""

    @pytest.mark.asyncio
    async def test_send_now_records_source_and_decision_reason(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The ledger shows both who triggered it and what it concluded."""
        prepare_live(coordinator, room=26.0)
        hass.states.async_set("climate.office_ac", "cool", FULL_UNIT)
        register_climate(hass, [])

        await coordinator.async_send_now()

        assert coordinator.last_command is not None
        assert coordinator.last_command["source"] == "send_now"
        assert "comfort ceiling" in coordinator.last_command["reason"]
        assert "cooling to 24.5 C" in coordinator.last_command["reason"]
