# SPDX-License-Identifier: Apache-2.0
"""Tests for the command ledger, merged IR call, and the Send Now button.

One-way IR has no delivery feedback, so decisions always re-send (proven
necessary: one dropped packet would strand the unit). The re-send is ONE
service call carrying both hvac_mode and temperature, so the AC receives a
single IR frame per tick instead of two identical back-to-back frames. The
ledger records every command for debugging and never gates sending.
"""

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator

from .comfort_helpers import FULL_UNIT, decide, prepare_live, register_climate

# Hybrid pins Tc = 24.0: band 22.0-25.5, so a 26.0 room cools to 24.5.
HOT_ROOM = 26.0
COOL_SETPOINT = 24.5


@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry) -> ClimateAdaptCoordinator:
    """Create a coordinator instance."""
    mock_config_entry.add_to_hass(hass)
    return ClimateAdaptCoordinator(hass, mock_config_entry)


class TestMergedSingleCall:
    """Outside-band decisions send ONE service call with mode+temperature."""

    @pytest.mark.asyncio
    async def test_single_call_carries_mode_and_temperature(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """One decision = one set_temperature call with both keys."""
        prepare_live(coordinator, room=HOT_ROOM)
        register_climate(hass, calls := [])

        await decide(hass, coordinator, "cool")

        assert len(calls) == 1
        assert calls[0].service == "set_temperature"
        assert calls[0].data["hvac_mode"] == "cool"
        assert calls[0].data["temperature"] == COOL_SETPOINT

    @pytest.mark.asyncio
    async def test_resend_on_matching_state_is_still_one_call(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Belief-matching tick still re-sends (IR retry) but only once."""
        prepare_live(coordinator, room=HOT_ROOM)
        register_climate(hass, calls := [])

        await decide(
            hass, coordinator, "cool", {**FULL_UNIT, "temperature": COOL_SETPOINT}
        )

        assert len(calls) == 1
        assert calls[0].data["hvac_mode"] == "cool"
        assert calls[0].data["temperature"] == COOL_SETPOINT


class TestCommandLedger:
    """Every command lands in the ledger with mode, temp, source, reason."""

    @pytest.mark.asyncio
    async def test_ledger_records_sent_command(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """An outside-band decision appends one ledger entry."""
        prepare_live(coordinator, room=HOT_ROOM)
        register_climate(hass, [])

        await decide(hass, coordinator, "cool")

        assert len(coordinator.command_history) == 1
        cmd = coordinator.command_history[0]
        assert cmd["mode"] == "cool"
        assert cmd["temperature"] == COOL_SETPOINT
        assert cmd["source"] == "auto"
        assert cmd["reason"] == (
            "Room 26.0 C has reached the comfort ceiling 25.5 C "
            "(band 22.0-25.5, comfort 24.0, cooling starts over 24.8) "
            "and the fan is not enough: cooling to 24.5 C."
        )
        assert cmd["time"] is not None
        assert coordinator.last_command == cmd

    @pytest.mark.asyncio
    async def test_ledger_never_gates_sending(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Identical consecutive commands are NOT suppressed (IR retry)."""
        prepare_live(coordinator, room=HOT_ROOM)
        register_climate(hass, calls := [])

        await decide(hass, coordinator, "cool")
        coordinator.last_adaptation = datetime.now(UTC) - timedelta(
            minutes=coordinator.adaptation_interval + 1
        )
        await decide(hass, coordinator, "cool")

        assert len(calls) == 2
        assert len(coordinator.command_history) == 2

    @pytest.mark.asyncio
    async def test_ledger_caps_at_20_entries(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator  # noqa: ARG002
    ) -> None:
        """The ledger keeps only the most recent 20 commands."""
        for _ in range(25):
            coordinator._record_command(
                mode="cool",
                temperature=23.0,
                source="auto",
                reason="outside band",
            )
        assert len(coordinator.command_history) == 20

    @pytest.mark.asyncio
    async def test_in_band_hold_sends_nothing_and_is_recorded_as_a_skip(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """In band with keep_climate_on and the unit running: no frame is sent.

        The ladder has nothing to ask for, so the tick is a ledger skip row
        (`sent` False) that cannot be mistaken for an IR command.
        """
        prepare_live(coordinator, room=24.0, keep_on=True)
        register_climate(hass, calls := [])

        await decide(hass, coordinator, "cool")

        assert calls == []
        assert len(coordinator.command_history) == 1
        row = coordinator.command_history[0]
        assert row["sent"] is False
        assert row["kind"] == "skip_comfort_hold"
        assert row["mode"] is None
        assert coordinator.last_command == row


class TestSendNow:
    """Send Now runs the decision immediately, ignoring interval/cooldown."""

    @pytest.mark.asyncio
    async def test_send_now_bypasses_adaptation_interval(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """A fresh last_adaptation does not block a Send Now press."""
        prepare_live(coordinator, room=HOT_ROOM)
        hass.states.async_set("climate.office_ac", "off", FULL_UNIT)
        register_climate(hass, calls := [])

        coordinator.last_adaptation = datetime.now(UTC)
        await coordinator.async_send_now()

        assert len(calls) == 1
        assert calls[0].data["hvac_mode"] == "cool"
        assert coordinator.last_button_action == "send_now"
        assert coordinator.last_button_result == "sent: cool @ 24.5"
        ledger = coordinator.command_history
        assert ledger[-1]["source"] == "send_now"

    @pytest.mark.asyncio
    async def test_send_now_bypasses_manual_cooldown(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """An active manual override does not block a Send Now press."""
        prepare_live(coordinator, room=HOT_ROOM)
        hass.states.async_set("climate.office_ac", "off", FULL_UNIT)
        register_climate(hass, calls := [])

        coordinator.manual_override_until = dt_util.now() + timedelta(hours=1)
        await coordinator.async_send_now()

        assert len(calls) == 1
        assert coordinator.last_button_result == "sent: cool @ 24.5"

    @pytest.mark.asyncio
    async def test_send_now_in_band_hold_reports_no_command_and_keeps_schedule(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """In-band press with keep_climate_on: nothing is sent, so the button
        says so and last_adaptation is put back (no IR frame went out)."""
        prepare_live(coordinator, room=24.0, keep_on=True)
        hass.states.async_set("climate.office_ac", "cool", FULL_UNIT)
        register_climate(hass, calls := [])

        stale = datetime.now(UTC) - timedelta(hours=2)
        coordinator.last_adaptation = stale
        await coordinator.async_send_now()

        assert calls == []
        assert coordinator.last_adaptation == stale
        assert coordinator.last_button_result == "no command needed (in band / sticky)"
        assert coordinator.command_history[-1]["kind"] == "skip_comfort_hold"
        assert coordinator.command_history[-1]["source"] == "send_now"

    @pytest.mark.asyncio
    async def test_send_now_unavailable_climate(
        self, hass: HomeAssistant, coordinator: ClimateAdaptCoordinator
    ) -> None:
        """Missing climate entity: error recorded, nothing sent."""
        hass.states.async_remove("climate.office_ac")
        register_climate(hass, calls := [])

        await coordinator.async_send_now()

        assert len(calls) == 0
        assert coordinator.last_button_result == "error: climate entity unavailable"
        assert coordinator.last_button_data == {"status": "error"}
