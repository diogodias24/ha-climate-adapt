# SPDX-License-Identifier: Apache-2.0
"""In band with `Keep Climate On` off, the AC must actually switch off.

`target_temp` is a float initialised to 25.0 and never None, and heat_cool is the
default selector, so neither may veto the turn-off: `_turn_off_ac` must stay reachable in
the default configuration. The only control over it is the `Keep Climate On` switch.
"""

import pytest
from homeassistant.core import HomeAssistant

from custom_components.climate_adapt.const import (
    DEFAULT_TARGET_TEMP,
    HVAC_MODE_COOL,
    HVAC_MODE_HEAT_COOL,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

from .comfort_helpers import decide, prepare_live, register_climate

CLIMATE = "climate.office_ac"


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    """Create a coordinator instance."""
    mock_config_entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, mock_config_entry)


def _in_band(
    coordinator: ClimateAdaptCoordinator,
    *,
    keep_on: bool,
    hvac_mode: str = HVAC_MODE_HEAT_COOL,
    target: float = 25.0,
    room: float = 24.85,
) -> None:
    """Room inside the band (23.0-26.5 at the default target), dry mode out of the way."""
    prepare_live(
        coordinator,
        target=target,
        room=room,
        humidity=45.0,
        hvac_mode=hvac_mode,
        keep_on=keep_on,
    )


class TestTurnOffInBand:
    """`keep_climate_on` is the single honest control over this."""

    @pytest.mark.asyncio
    async def test_turns_off_in_heat_cool_with_keep_climate_on_off(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The default selector must not veto the turn-off.

        Selector heat_cool, target_temp set, in band, switch off.
        """
        _in_band(coordinator, keep_on=False)
        assert coordinator.target_temp is not None
        register_climate(hass, calls := [])

        await decide(hass, coordinator, HVAC_MODE_COOL)

        assert [call.service for call in calls] == ["turn_off"]
        assert calls[0].data["entity_id"] == CLIMATE

    @pytest.mark.asyncio
    async def test_turns_off_with_the_default_target_temp(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """target_temp is never None, not even untouched at its default."""
        _in_band(coordinator, keep_on=False, target=DEFAULT_TARGET_TEMP)
        coordinator.temperature = coordinator.perceived_temp = DEFAULT_TARGET_TEMP
        register_climate(hass, calls := [])

        await decide(hass, coordinator, HVAC_MODE_COOL)

        assert [call.service for call in calls] == ["turn_off"]

    @pytest.mark.asyncio
    async def test_turns_off_with_a_directional_selector_too(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The cool selector already turned off; it must keep doing so."""
        _in_band(coordinator, keep_on=False, hvac_mode=HVAC_MODE_COOL)
        register_climate(hass, calls := [])

        await decide(hass, coordinator, HVAC_MODE_COOL)

        assert [call.service for call in calls] == ["turn_off"]

    @pytest.mark.asyncio
    async def test_turn_off_is_recorded_in_the_ledger(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A turn-off is a command sent to the unit and has to show up as one."""
        _in_band(coordinator, keep_on=False)
        register_climate(hass, [])

        await decide(hass, coordinator, HVAC_MODE_COOL)

        assert coordinator.last_command is not None
        assert coordinator.last_command["mode"] == "off"
        assert coordinator.last_command["sent"] is True
        assert "inside the comfort band" in coordinator.last_command["reason"]

    @pytest.mark.asyncio
    async def test_keep_climate_on_maintains_instead_of_turning_off(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """The other side of the pair: switch on means no turn-off, and a held row.

        The room (25.2 C) is on the cool side of Tc (25.0 C), so holding cool is in
        step with it; under Tc the hold is turned off instead.
        """
        _in_band(coordinator, keep_on=True, room=25.2)
        register_climate(hass, calls := [])

        await decide(hass, coordinator, HVAC_MODE_COOL)

        assert calls == []
        assert coordinator.last_command is not None
        assert coordinator.last_command["kind"] == "skip_comfort_hold"
        assert coordinator.last_command["sent"] is False

    @pytest.mark.asyncio
    async def test_outside_band_still_activates_rather_than_turning_off(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Turn-off belongs to the in-band branch only."""
        _in_band(coordinator, keep_on=False)
        coordinator.temperature = coordinator.perceived_temp = 29.0
        register_climate(hass, calls := [])

        await decide(hass, coordinator, HVAC_MODE_COOL)

        assert [call.service for call in calls] == ["set_temperature"]
        assert calls[0].data["temperature"] == 25.5
        assert calls[0].data["hvac_mode"] == "cool"


class TestInBandModeMismatchIsVisible:
    """In band with the AC in some other mode, nothing is sent but it is recorded.

    A held tick is a `skip_comfort_hold` ledger row and stamps
    `last_adaptation`, so a room that holds for hours does not re-evaluate on
    every sensor push. Starting or correcting a unit the user may have
    stopped on purpose stays out of the ladder's reach.
    """

    @pytest.mark.asyncio
    async def test_dry_unit_with_dry_disabled_is_recorded_not_corrected(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Dry switch turned off while the unit sits in dry, room in band."""
        _in_band(coordinator, keep_on=True)
        register_climate(hass, calls := [])

        await decide(hass, coordinator, "dry")

        assert calls == []
        assert coordinator.last_command is not None
        assert coordinator.last_command["sent"] is False
        assert coordinator.last_command["kind"] == "skip_comfort_hold"
        assert "inside the comfort band" in coordinator.last_command["reason"]
        assert coordinator.last_adaptation is not None

    @pytest.mark.asyncio
    async def test_off_unit_in_band_is_recorded_not_started(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """An AC the user stopped is not restarted just for being in band."""
        _in_band(coordinator, keep_on=True)
        register_climate(hass, calls := [])

        await decide(hass, coordinator, "off")

        assert calls == []
        assert coordinator.last_command["kind"] == "skip_comfort_hold"
        assert coordinator.last_command["sent"] is False

    @pytest.mark.asyncio
    async def test_heating_under_a_cool_selector_is_recorded(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A directional selector disagreeing with the unit is still a no-op."""
        _in_band(coordinator, keep_on=True, hvac_mode=HVAC_MODE_COOL)
        register_climate(hass, calls := [])

        await decide(hass, coordinator, "heat")

        assert calls == []
        assert coordinator.last_command["kind"] == "skip_comfort_hold"
        assert coordinator.last_command["sent"] is False
