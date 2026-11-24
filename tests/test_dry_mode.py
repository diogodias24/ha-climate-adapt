# SPDX-License-Identifier: Apache-2.0
"""Tests for the dry row (row 4) of the comfort ladder.

Hybrid pins Tc to the target, so with target 24.0 the band is 22.0-25.5, cool
sends 24.5 and heat sends 23.5. Humid is RH >= 65 for two ticks or a dew point
>= 17 (24 C at 68 % is 17.7, so HUMID is humid on the first tick), and it
clears when the dew point is under 15.5. Dry runs while the room is at
or over Tc and stops half a degree under it.
"""

import pytest
from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.climate_adapt.comfort import (
    HUMID_CLEAR_DEW_POINT_C,
    HUMID_CLEAR_RH,
    HUMIDITY_HUMID,
    HumidityState,
    dew_point,
)
from custom_components.climate_adapt.const import HVAC_MODE_COOL
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

from .comfort_helpers import decide, prepare_live, register_climate

HUMID = 67.0
NORMAL = 50.0
HELD = 62.0


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


def _dry_calls(calls: list[ServiceCall]) -> list[ServiceCall]:
    return [c for c in calls if c.service == "set_hvac_mode" and c.data.get("hvac_mode") == "dry"]


class TestDryRow:
    """In the comfort band, humid air with the dry switch on switches the AC to dry."""

    @pytest.mark.asyncio
    async def test_enters_dry_when_in_band_and_humid(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        prepare_live(coordinator, room=24.0, humidity=HUMID, keep_on=True, dry=True)

        await decide(hass, coordinator, HVAC_MODE_COOL)

        assert [c.service for c in calls] == ["set_hvac_mode"]
        assert calls[0].data["hvac_mode"] == "dry"
        assert coordinator.dry_active is True
        assert coordinator.last_command["mode"] == "dry"
        assert "humid" in coordinator.last_command["reason"]

    @pytest.mark.asyncio
    async def test_no_dry_when_disabled(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """dry_mode_enabled=False keeps the classic keep-climate behavior."""
        prepare_live(coordinator, room=24.0, humidity=HUMID, keep_on=True, dry=False)

        await decide(hass, coordinator, HVAC_MODE_COOL)

        assert calls == []
        assert coordinator.dry_active is False
        assert coordinator.last_command["kind"] == "skip_comfort_hold"

    @pytest.mark.asyncio
    async def test_dry_exit_with_keep_climate_on_resumes_the_last_direction(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """The unit was heating before dry; it goes back to holding heat."""
        prepare_live(coordinator, room=24.0, humidity=NORMAL, keep_on=True, dry=True)
        coordinator.last_run_direction = "heat"

        await decide(hass, coordinator, "dry")

        assert [c.service for c in calls] == ["set_temperature"]
        assert calls[0].data["hvac_mode"] == "heat"
        assert calls[0].data["temperature"] == 23.5
        assert coordinator.dry_active is False

    @pytest.mark.asyncio
    async def test_stale_humid_latch_does_not_start_dry_in_a_heated_room(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """A humid latch is released on the dew point although RH is over the RH-only level."""
        room, rh = 22.4, 61.3
        dew = dew_point(room, rh)
        assert dew is not None
        assert dew < HUMID_CLEAR_DEW_POINT_C
        assert rh > HUMID_CLEAR_RH
        prepare_live(coordinator, room=room, humidity=rh, keep_on=True, dry=True)
        coordinator.last_run_direction = "heat"
        coordinator._humidity_state = HumidityState(HUMIDITY_HUMID, 0)

        await decide(hass, coordinator, "heat")

        assert _dry_calls(calls) == []
        assert coordinator.dry_active is False
        assert coordinator.comfort["humidity_status"] == "normal"
        assert coordinator.comfort["heat_starts_below"] == round(
            coordinator.comfort["band_low"] - coordinator.heat_dead_zone, 2
        )
        # Cooling starts at Tc + 0.8 (24.8 at Tc 24.0), well under the band top.
        assert coordinator.comfort["cool_starts_above"] == 24.8
        assert coordinator.comfort["band_high"] == 25.5

    @pytest.mark.asyncio
    async def test_cooling_outranks_dry_when_the_room_is_hot(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Felt heat wins: above the band the normal cool path runs."""
        prepare_live(coordinator, room=26.0, humidity=HUMID, keep_on=True, dry=True)

        await decide(hass, coordinator, "off")

        assert [c.service for c in calls] == ["set_temperature"]
        assert calls[0].data["hvac_mode"] == HVAC_MODE_COOL
        assert calls[0].data["temperature"] == 24.5
        assert coordinator.dry_active is False

    @pytest.mark.asyncio
    async def test_no_dry_for_hvac_mode_heat(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """A heat-pinned selector never goes dry."""
        prepare_live(
            coordinator,
            room=24.0,
            humidity=HUMID,
            hvac_mode="heat",
            keep_on=True,
            dry=True,
        )

        await decide(hass, coordinator, "heat")

        assert _dry_calls(calls) == []
        assert calls == []
        assert coordinator.dry_active is False

    @pytest.mark.asyncio
    async def test_no_dry_without_humidity(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Humidity sensor missing -> dry row inert."""
        prepare_live(coordinator, room=24.0, keep_on=True, dry=True)
        coordinator.humidity = None

        await decide(hass, coordinator, HVAC_MODE_COOL)

        assert _dry_calls(calls) == []
        assert coordinator.dry_active is False

    @pytest.mark.asyncio
    async def test_already_dry_is_re_sent_every_tick(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Unit already in dry, still humid -> re-send dry (one-way IR self-heal)."""
        prepare_live(coordinator, room=24.0, humidity=HUMID, keep_on=True, dry=True)

        await decide(hass, coordinator, "dry")

        assert [c.service for c in calls] == ["set_hvac_mode"]
        assert calls[0].data["hvac_mode"] == "dry"
        assert coordinator.dry_active is True

    @pytest.mark.asyncio
    async def test_humid_hysteresis_holds_dry_then_releases_it(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Between 58 and 65 % a humid room stays humid; at 50 % the unit is stopped."""
        prepare_live(coordinator, room=24.0, humidity=HUMID, keep_on=False, dry=True)
        await decide(hass, coordinator, "dry")
        assert coordinator.dry_active is True

        coordinator.humidity = HELD
        coordinator.last_adaptation = None
        await decide(hass, coordinator, "dry")
        assert [c.data["hvac_mode"] for c in calls] == ["dry", "dry"]
        assert coordinator.dry_active is True

        coordinator.humidity = NORMAL
        coordinator.last_adaptation = None
        await decide(hass, coordinator, "dry")
        assert [c.service for c in calls] == ["set_hvac_mode", "set_hvac_mode", "turn_off"]
        assert coordinator.dry_active is False

    @pytest.mark.asyncio
    async def test_dry_exit_with_keep_climate_on_sends_nothing(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Once the air is normal again the ladder holds; it does not stop a kept-on unit."""
        prepare_live(coordinator, room=24.0, humidity=NORMAL, keep_on=True, dry=True)

        await decide(hass, coordinator, "dry")

        assert calls == []
        assert coordinator.dry_active is False
        assert coordinator.last_command["kind"] == "skip_comfort_hold"

    @pytest.mark.asyncio
    async def test_below_band_leaves_dry_for_heat(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator, calls
    ) -> None:
        """Too cold outranks humid: a dry unit is sent to heat, not turned off."""
        prepare_live(coordinator, room=20.0, humidity=HUMID, keep_on=True, dry=True)

        await decide(hass, coordinator, "dry")

        assert [c.service for c in calls] == ["set_temperature"]
        assert calls[0].data["hvac_mode"] == "heat"
        assert calls[0].data["temperature"] == 23.5
        assert coordinator.dry_active is False


# Every ladder row, dry included, may start a stopped unit
# (see tests/test_comfort_wiring.py::TestLiveLadder).
