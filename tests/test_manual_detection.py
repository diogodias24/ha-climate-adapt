# SPDX-License-Identifier: Apache-2.0
"""A change to the AC the integration did not issue is a manual override.

The coordinator listens to the climate entity itself, so a user who touches
the climate card, the remote or a voice assistant starts the manual override
in every mode, not only Hybrid; otherwise the loop would overwrite them on
the next tick.

Attribution is by service-call context, never by comparing values: one-way
IR means every decision re-sends, and a re-send can carry exactly the value
the user dialled in by hand.
"""


import pytest
from homeassistant.core import Context, HomeAssistant

from custom_components.climate_adapt.const import DOMAIN
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

from .comfort_helpers import decide, prepare_live, register_climate

CLIMATE = "climate.office_ac"


@pytest.fixture
async def live(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    """Set the entry up for real, so the climate listener is registered."""
    mock_config_entry.add_to_hass(hass)
    hass.states.async_set("sensor.office_temperature", "26.0")
    hass.states.async_set("sensor.office_humidity", "50.0")
    hass.states.async_set(CLIMATE, "cool", {"temperature": 24.0})
    register_climate(hass, [])

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][mock_config_entry.entry_id]
    coordinator.manual_override_until = None
    return coordinator


async def _own_context(hass: HomeAssistant, coordinator) -> Context:
    """Send a real integration command and return the Context it used."""
    seen: list = []
    register_climate(hass, seen)
    await coordinator._send_ac_command("cool", 24.0, "auto", "test")
    await hass.async_block_till_done()
    assert seen, "expected the integration to issue a service call"
    return seen[-1].context


class TestExternalChangeDetection:
    """Only changes we did not cause start the cooldown."""

    @pytest.mark.asyncio
    async def test_external_temperature_change_starts_cooldown(
        self, hass: HomeAssistant, live: ClimateAdaptCoordinator
    ) -> None:
        """The user dials a different target on the card: hands off for a while."""
        hass.states.async_set(CLIMATE, "cool", {"temperature": 20.0})
        await hass.async_block_till_done()

        assert live.manual_override_active()
        assert live.last_external_change is not None
        assert "temperature" in live.last_external_change["detail"]

    @pytest.mark.asyncio
    async def test_external_hvac_mode_change_starts_cooldown(
        self, hass: HomeAssistant, live: ClimateAdaptCoordinator
    ) -> None:
        """The user switches the unit to heat by hand."""
        hass.states.async_set(CLIMATE, "heat", {"temperature": 24.0})
        await hass.async_block_till_done()

        assert live.manual_override_active()
        assert "hvac_mode" in live.last_external_change["detail"]

    @pytest.mark.asyncio
    async def test_external_turn_off_starts_cooldown(
        self, hass: HomeAssistant, live: ClimateAdaptCoordinator
    ) -> None:
        """Turning the AC off by hand is a manual change like any other."""
        hass.states.async_set(CLIMATE, "off", {"temperature": 24.0})
        await hass.async_block_till_done()

        assert live.manual_override_active()

    @pytest.mark.asyncio
    async def test_own_command_does_not_start_cooldown(
        self, hass: HomeAssistant, live: ClimateAdaptCoordinator
    ) -> None:
        """Our own IR re-send must not pause our own loop."""
        context = await _own_context(hass, live)

        hass.states.async_set(
            CLIMATE, "cool", {"temperature": 21.0}, context=context
        )
        await hass.async_block_till_done()

        assert not live.manual_override_active()
        assert live.last_external_change is None

    @pytest.mark.asyncio
    async def test_own_command_through_a_child_context_is_still_ours(
        self, hass: HomeAssistant, live: ClimateAdaptCoordinator
    ) -> None:
        """A command relayed by a script arrives as a child of our context."""
        parent = await _own_context(hass, live)

        hass.states.async_set(
            CLIMATE,
            "heat",
            {"temperature": 21.0},
            context=Context(parent_id=parent.id),
        )
        await hass.async_block_till_done()

        assert not live.manual_override_active()

    @pytest.mark.asyncio
    async def test_value_match_does_not_make_an_external_change_ours(
        self, hass: HomeAssistant, live: ClimateAdaptCoordinator
    ) -> None:
        """The user setting exactly our last target still counts as manual.

        This is the case no remembered value can settle, which is why
        attribution is by context only. The value we last sent is read off
        the command ledger.
        """
        await _own_context(hass, live)
        assert live.last_command["temperature"] == 24.0

        hass.states.async_set(CLIMATE, "cool", {"temperature": 24.0})
        hass.states.async_set(CLIMATE, "heat", {"temperature": 24.0})
        await hass.async_block_till_done()

        assert live.manual_override_active()

    @pytest.mark.asyncio
    async def test_current_temperature_report_is_not_a_manual_change(
        self, hass: HomeAssistant, live: ClimateAdaptCoordinator
    ) -> None:
        """The climate entity republishes the room reading constantly.

        Those events carry a context that is not ours. Counting them would
        hold the cooldown on forever and the loop would control nothing.
        """
        hass.states.async_set(
            CLIMATE, "cool", {"temperature": 24.0, "current_temperature": 26.4}
        )
        await hass.async_block_till_done()

        assert not live.manual_override_active()

    @pytest.mark.asyncio
    async def test_recovery_from_unavailable_is_not_a_manual_change(
        self, hass: HomeAssistant, live: ClimateAdaptCoordinator
    ) -> None:
        """An IR bridge dropping out and coming back is not the user acting."""
        hass.states.async_set(CLIMATE, "unavailable")
        await hass.async_block_till_done()
        hass.states.async_set(CLIMATE, "cool", {"temperature": 24.0})
        await hass.async_block_till_done()

        assert not live.manual_override_active()


class TestCooldownBlocksTheLoop:
    """The detected change has to actually suppress the next decision."""

    @pytest.mark.asyncio
    async def test_external_change_blocks_the_next_auto_decision(
        self, hass: HomeAssistant, live: ClimateAdaptCoordinator
    ) -> None:
        """Out of band after a manual change: still no command."""
        hass.states.async_set(CLIMATE, "cool", {"temperature": 20.0})
        await hass.async_block_till_done()
        assert live.manual_override_active()

        prepare_live(live, target=24.0, room=29.0, humidity=50.0)
        register_climate(hass, calls := [])

        await decide(hass, live, "cool")

        assert calls == []

    @pytest.mark.asyncio
    async def test_force_still_overrides_the_cooldown(
        self, hass: HomeAssistant, live: ClimateAdaptCoordinator
    ) -> None:
        """Send Now stays usable while a manual cooldown is running."""
        hass.states.async_set(CLIMATE, "cool", {"temperature": 20.0})
        await hass.async_block_till_done()

        prepare_live(live, target=24.0, room=29.0, humidity=50.0)
        register_climate(hass, calls := [])

        await decide(hass, live, "cool", force=True, source="send_now")

        assert len(calls) == 1
