# SPDX-License-Identifier: Apache-2.0
"""A failed fan needs no confirming ticks; a held view says it is held.

A unit in fan only after a restart: `fan_since` restored, `fan_start_temp` unknown,
last direction heat for hours, room well over the cooling trigger. The fan is unproven at
once and cool is wanted, so the guard must not ask for a second tick and fall back to fan,
and the gated held view must not publish `cool` as if it had been sent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.comfort import HUMIDITY_NORMAL, comfort_band
from custom_components.climate_adapt.decision import (
    ACTION_COOL,
    ACTION_FAN,
    ACTION_HEAT,
    ACTION_OFF,
    DIRECTION_DWELL_MINUTES,
    Decision,
    DecisionInputs,
    DecisionState,
    decide,
)

from .test_startup_gate import (
    CLIMATE_ENTITY,
    COMFORT_TEMP,
    TEMP_SENSOR,
    _decide,
    _office,
    _register,
    _seed_initial_runs,
    _seed_outside,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

NOW = datetime(2031, 10, 14, 14, 0, tzinfo=UTC)
_UNIT_ATTRS = {"target_temp_step": 0.5, "hvac_modes": ["off", "heat", "cool", "fan_only"]}
# Cooling starts at Tc + 0.8; the band top is display only.
BAND = comfort_band(23.2)


def _tick(state: DecisionState, *, temp: float = 25.8, **kw: object) -> Decision:
    return decide(
        DecisionInputs(
            now=NOW,
            room_temp=temp,
            band=BAND,
            humidity=HUMIDITY_NORMAL,
            outside_temp=kw.pop("outside_temp", 17.5),  # type: ignore[arg-type]
            **kw,  # type: ignore[arg-type]
        ),
        state,
    )


def _fan_state(**kw: object) -> DecisionState:
    base: dict[str, object] = {
        "running": ACTION_FAN,
        "last_run": ACTION_HEAT,
        "last_direction_change": NOW - timedelta(hours=5),
        "fan_since": NOW - timedelta(hours=2),
        "fan_start_temp": None,
    }
    base.update(kw)
    return DecisionState(**base)  # type: ignore[arg-type]


class TestFailedFanNeedsNoConfirmingTicks:
    def test_the_restart_sequence_cools_on_the_first_tick(self) -> None:
        got = _tick(_fan_state(), unit_running=True, keep_on=True)

        assert got.action == ACTION_COOL
        assert got.send is True
        assert got.refusal is None
        assert got.state.last_run == ACTION_COOL
        assert got.state.pending is None

    def test_a_dwell_that_has_not_run_out_still_refuses_and_names_only_the_dwell(self) -> None:
        state = _fan_state(last_direction_change=NOW - timedelta(minutes=30))

        # Over the trigger but under the cool bypass line, so the dwell still applies.
        got = _tick(state, temp=24.8, unit_running=True)
        assert got.action == ACTION_FAN
        assert got.refusal is not None
        assert "minutes have passed" in got.refusal
        assert "confirming ticks" not in got.refusal
        assert got.dwell_remaining_min == pytest.approx(DIRECTION_DWELL_MINUTES - 30)

    def test_the_hot_escalation_also_waives_the_ticks(self) -> None:
        state = _fan_state(fan_start_temp=27.0, fan_since=NOW - timedelta(minutes=10))

        got = _tick(state, temp=26.5, unit_running=True)

        assert got.action == ACTION_COOL
        assert got.refusal is None

    def test_a_proving_fan_stays_a_fan(self) -> None:
        # Dropped 0.4 K in 40 min, and under the trigger + 1.0 escalation line.
        state = _fan_state(fan_start_temp=24.8, fan_since=NOW - timedelta(minutes=40))

        got = _tick(state, temp=24.4, unit_running=True)

        assert got.action == ACTION_FAN
        assert got.refusal is None

    def test_a_unit_that_is_off_needs_no_confirming_ticks(self) -> None:
        # Off -> cool is waived from the ticks.
        state = DecisionState(
            last_run=ACTION_HEAT, last_direction_change=NOW - timedelta(hours=5)
        )

        first = _tick(state, temp=27.0)

        assert first.action == ACTION_COOL
        assert first.refusal is None
        assert first.state.pending is None

    def test_a_unit_that_is_off_still_waits_out_the_dwell(self) -> None:
        state = DecisionState(
            last_run=ACTION_HEAT, last_direction_change=NOW - timedelta(minutes=30)
        )

        # Over the trigger, under the line where the dwell is bypassed. A refusal
        # falls back to the fan whenever it is offered, with no outside condition.
        got = _tick(state, temp=24.8, outside_temp=None)

        assert got.action == ACTION_FAN
        assert got.refusal is not None
        assert "minutes have passed" in got.refusal
        assert "confirming ticks" not in got.refusal
        assert got.reason.endswith(". Fan only, high speed.")

    def test_a_unit_that_is_off_with_no_fan_offered_waits_out_the_dwell_in_off(self) -> None:
        state = DecisionState(
            last_run=ACTION_HEAT, last_direction_change=NOW - timedelta(minutes=30)
        )

        got = _tick(state, temp=24.8, outside_temp=None, fan_offered=False)

        assert got.action == ACTION_OFF
        assert got.refusal is not None
        assert "minutes have passed" in got.refusal
        assert "confirming ticks" not in got.refusal

    def test_a_unit_still_running_heat_needs_two_ticks(self) -> None:
        state = DecisionState(
            running=ACTION_HEAT,
            last_run=ACTION_HEAT,
            last_direction_change=NOW - timedelta(hours=5),
        )

        # Over the trigger by more than FAN_ESCALATE_HOT_C: cool is wanted, no fan run
        # behind it.
        first = _tick(state, temp=27.0, unit_running=True)
        assert first.action == ACTION_FAN
        assert first.refusal is not None
        assert "1 of 2 confirming ticks" in first.refusal

        second = _tick(first.state, temp=27.0, unit_running=True)
        assert second.action == ACTION_COOL
        assert second.refusal is None


async def _after_a_refused_tick(hass: HomeAssistant, entry) -> tuple:
    """Unit still in heat, room over the escalation line: tick one falls back to fan.

    The unit has to be running heat: an off unit is not made to wait for
    confirming ticks, so only a running one still gets a refused first tick here.
    """
    coordinator = await _office(hass, entry, ac_state="heat", perceived=28.0)
    _seed_initial_runs(coordinator)
    _seed_outside(hass)
    hass.states.async_set(TEMP_SENSOR, "28.0")
    hass.states.async_set(CLIMATE_ENTITY, "heat", _UNIT_ATTRS)
    coordinator.last_run_direction = ACTION_HEAT
    coordinator.last_direction_change = dt_util.utcnow() - timedelta(hours=10)
    _register(hass, calls := [])
    await _decide(coordinator, hass)
    return coordinator, calls


class TestHeldViewSaysItIsHeld:
    @pytest.mark.asyncio
    async def test_a_gated_push_after_a_fan_fallback_is_marked_not_sent(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        coordinator, calls = await _after_a_refused_tick(
            hass, mock_config_entry_with_outside
        )
        assert [c.service for c in calls] == ["set_hvac_mode"]
        assert calls[0].data["hvac_mode"] == "fan_only"
        assert coordinator.comfort is not None
        assert coordinator.comfort["decision_held_until"] is None
        stamped = coordinator.last_adaptation
        committed = coordinator._ladder_state
        assert stamped is not None

        await coordinator._async_update_data()

        assert [c.service for c in calls] == ["set_hvac_mode"]
        assert coordinator.last_adaptation == stamped
        assert coordinator._ladder_state == committed
        comfort = coordinator.comfort
        assert comfort is not None
        assert comfort["action"] == ACTION_COOL
        until = stamped + timedelta(minutes=coordinator.adaptation_interval)
        assert comfort["decision_held_until"] == until.isoformat()
        assert f"Held by the adaptation interval: not sent before {until:%H:%M} UTC." in (
            comfort["reason"]
        )

    @pytest.mark.asyncio
    async def test_the_manual_cooldown_is_named_when_it_is_the_later_gate(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        coordinator, calls = await _after_a_refused_tick(
            hass, mock_config_entry_with_outside
        )
        cooldown_end = dt_util.utcnow() + timedelta(hours=3)
        coordinator.manual_override_until = cooldown_end

        await coordinator._async_update_data()

        comfort = coordinator.comfort
        assert comfort is not None
        assert comfort["decision_held_until"] == cooldown_end.isoformat()
        assert "Held by the manual cooldown" in comfort["reason"]
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_a_held_view_that_matches_the_unit_is_not_marked(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        coordinator, _ = await _after_a_refused_tick(
            hass, mock_config_entry_with_outside
        )
        hass.states.async_set(TEMP_SENSOR, str(round(COMFORT_TEMP - 0.2, 1)))
        hass.states.async_set(CLIMATE_ENTITY, "off", _UNIT_ATTRS)

        await coordinator._async_update_data()

        comfort = coordinator.comfort
        assert comfort is not None
        assert comfort["action"] == ACTION_OFF
        assert comfort["decision_held_until"] is None
        assert "Held by" not in comfort["reason"]


class TestLastRefusalIsNotShownAsCurrent:
    @pytest.mark.asyncio
    async def test_it_carries_its_time_and_clears_when_the_change_is_accepted(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        coordinator, _ = await _after_a_refused_tick(
            hass, mock_config_entry_with_outside
        )
        first_tick = coordinator.last_adaptation
        assert first_tick is not None
        assert coordinator._comfort_last_refusal is not None
        assert "1 of 2 confirming ticks" in coordinator._comfort_last_refusal

        await coordinator._async_update_data()
        assert coordinator.comfort is not None
        assert coordinator.comfort["last_refusal"] == coordinator._comfort_last_refusal
        assert coordinator.comfort["last_refusal_at"] == first_tick.isoformat()

        coordinator.last_adaptation = first_tick - timedelta(hours=1)
        await _decide(coordinator, hass)

        assert coordinator._comfort_last_refusal is None
        assert coordinator._comfort_last_refusal_at is None
        assert coordinator.comfort["last_refusal"] is None
        assert coordinator.comfort["last_refusal_at"] is None
        stored = coordinator._comfort_state_to_store()
        assert stored["last_refusal"] is None
        assert stored["last_refusal_at"] is None

    @pytest.mark.asyncio
    async def test_the_time_and_direction_survive_a_store_round_trip(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        coordinator, _ = await _after_a_refused_tick(
            hass, mock_config_entry_with_outside
        )
        stored = coordinator._comfort_state_to_store()
        assert stored["last_refusal_direction"] == ACTION_COOL
        assert stored["last_refusal_at"] == coordinator.last_adaptation.isoformat()  # type: ignore[union-attr]

        coordinator._comfort_last_refusal = None
        coordinator._comfort_last_refusal_at = None
        coordinator._comfort_last_refusal_direction = None
        coordinator._load_comfort_state(stored)

        assert coordinator._comfort_last_refusal_at == coordinator.last_adaptation
        assert coordinator._comfort_last_refusal_direction == ACTION_COOL
