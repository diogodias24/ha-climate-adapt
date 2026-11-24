# SPDX-License-Identifier: Apache-2.0
"""The fan has to prove itself, and a held mode must not oppose the room.

Tc is 23.0 unless a test says otherwise, so the numbers are round: band 21.0 - 24.5,
heat setpoint 22.5, cool setpoint 23.5, cooling trigger 23.8, comfort ceiling 24.5.
Outside is 15 C wherever a fan is expected (the fan does not depend on it), so
only the proof rule, the bar and the ceiling decide. A fan test therefore sits between
the trigger and the ceiling (23.8 < room < 24.5); at or over 24.5 the ladder cools.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.comfort import HUMIDITY_HUMID, HUMIDITY_NORMAL, comfort_band
from custom_components.climate_adapt.const import MIN_COOLING_PERCEIVED_TEMP
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.decision import (
    ACTION_COOL,
    ACTION_DRY,
    ACTION_FAN,
    ACTION_HEAT,
    ACTION_OFF,
    FAN_PROOF_DROP_C,
    FAN_PROOF_MINUTES,
    FAN_RETRY_BAR_MINUTES,
    Decision,
    DecisionInputs,
    DecisionState,
    decide,
    opposes_room,
)

from .test_comfort_wiring import _preload, _stored
from .test_startup_gate import (
    BAND,
    CLIMATE_ENTITY,
    OUTSIDE_TEMP_SENSOR,
    _decide,
    _office,
    _register,
    _seed_initial_runs,
    _seed_outside,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

NOW = datetime(2031, 10, 14, 13, 0, tzinfo=UTC)
TC = 23.0
HIGH = 24.5
# Cooling starts (and the fan has to have worked) over Tc + 0.8.
TRIGGER = TC + 0.8
COOL_SETPOINT = TC + 0.5

# Probes placed against the constants above rather than typed in.
FAN_ROOM = round(TRIGGER + 0.4, 2)
FAN_ROOM_LATER = round(FAN_ROOM + 0.15, 2)
FAN_START = round(TRIGGER + 0.55, 2)
PAST_CEILING = round(HIGH + 0.55, 2)
WARM_UNIT_ROOM = round(TC + 0.16, 2)
COOL_UNIT_ROOM = round(TC - 0.36, 2)
LOW_ROOM = round(TC - 3.6, 2)


def _go(temp: float, state: DecisionState | None = None, **kw: object) -> Decision:
    tc = float(kw.pop("tc", TC))  # type: ignore[arg-type]
    now = kw.pop("now", NOW)
    humidity = kw.pop("humidity", HUMIDITY_NORMAL)
    kw.setdefault("outside_temp", 15.0)
    inputs = DecisionInputs(
        now=now,  # type: ignore[arg-type]
        room_temp=temp,
        band=comfort_band(tc),
        humidity=humidity,  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )
    return decide(inputs, state or DecisionState())


def _fan(
    minutes: float,
    start_temp: float | None,
    *,
    failed_min_ago: float | None = None,
    **kw: object,
) -> DecisionState:
    return DecisionState(
        running=ACTION_FAN,
        fan_since=NOW - timedelta(minutes=minutes),
        fan_start_temp=start_temp,
        fan_failed_at=None if failed_min_ago is None else NOW - timedelta(minutes=failed_min_ago),
        **kw,  # type: ignore[arg-type]
    )


def _unit(action: str, last_run: str | None = None, **kw: object) -> DecisionState:
    direction = last_run or (action if action in (ACTION_HEAT, ACTION_COOL) else None)
    return DecisionState(
        running=action,
        last_run=direction,
        last_direction_change=NOW - timedelta(hours=10) if direction else None,
        **kw,  # type: ignore[arg-type]
    )


class TestConstants:
    def test_the_decided_numbers(self) -> None:
        assert FAN_PROOF_MINUTES == 25.0
        assert FAN_PROOF_DROP_C == 0.3
        assert FAN_RETRY_BAR_MINUTES == 180.0


class TestFanProof:
    @pytest.mark.parametrize(
        ("minutes", "action"),
        [
            (FAN_PROOF_MINUTES - 0.6, ACTION_FAN),
            (FAN_PROOF_MINUTES, ACTION_COOL),
            (FAN_PROOF_MINUTES + 7.5, ACTION_COOL),
        ],
    )
    def test_the_wait_is_25_minutes(self, minutes: float, action: str) -> None:
        got = _go(FAN_ROOM, _fan(minutes, FAN_ROOM))
        assert got.action == action

    @pytest.mark.parametrize(
        ("temp", "action"),
        [
            (FAN_START, ACTION_COOL),  # flat
            (round(FAN_START - FAN_PROOF_DROP_C + 0.12, 2), ACTION_COOL),  # short of the drop
            (round(FAN_START - FAN_PROOF_DROP_C + 0.01, 2), ACTION_COOL),  # just short
            (round(FAN_START - FAN_PROOF_DROP_C, 2), ACTION_FAN),  # the whole drop
            (round(FAN_START - FAN_PROOF_DROP_C - 0.2, 2), ACTION_FAN),
        ],
    )
    def test_the_room_must_fall_0_3_k_from_where_the_fan_started(
        self, temp: float, action: str
    ) -> None:
        # All of these are over the 23.8 trigger and under the 24.5 ceiling, so only
        # the proof rule decides.
        got = _go(temp, _fan(FAN_PROOF_MINUTES + 4.0, FAN_START))
        assert got.action == action

    def test_a_room_back_under_the_cooling_trigger_is_not_failed(self) -> None:
        got = _go(TRIGGER, _fan(40.0, HIGH))
        assert got.action == ACTION_FAN
        assert got.state.fan_failed_at is None

    def test_a_fan_state_without_a_start_temp_counts_as_unproven(self) -> None:
        got = _go(FAN_ROOM, _fan(FAN_PROOF_MINUTES + 62.0, None))
        assert (got.action, got.row) == (ACTION_COOL, 3)
        assert "the fan has not been seen to lower it" in got.reason
        assert got.state.fan_failed_at == NOW

    def test_but_not_before_the_wait_is_over(self) -> None:
        assert _go(FAN_ROOM, _fan(FAN_PROOF_MINUTES - 13.0, None)).action == ACTION_FAN

    def test_the_reason_says_what_the_fan_did(self) -> None:
        got = _go(23.7, _fan(55.0, 23.4), tc=22.4, now=NOW, outside_temp=16.2)
        assert got.action == ACTION_COOL
        assert got.reason == (
            "Room 23.7 C is over the cooling trigger "
            "(band 20.4-23.9, comfort 22.4, cooling starts over 23.2) and the "
            "fan has not lowered it in 55 min (23.4 -> 23.7 C): cooling to 23.5 C."
        )

    def test_the_ceiling_escalates_a_proving_fan_at_once_and_only_there(self) -> None:
        # A fan that is proving itself (1 K or more down) is still stopped for cool the
        # moment the room reaches the comfort ceiling (`>=`), at any fan age, and not below.
        far = HIGH + 2.2
        assert _go(HIGH + 1.4, _fan(4.0, far + 1.0)).action == ACTION_COOL
        assert _go(HIGH, _fan(FAN_PROOF_MINUTES + 6.0, far)).action == ACTION_COOL
        assert _go(HIGH, _fan(7.0, far)).action == ACTION_COOL
        assert _go(HIGH - 0.01, _fan(FAN_PROOF_MINUTES + 6.0, far)).action == ACTION_FAN
        assert _go(HIGH - 0.23, _fan(7.0, far)).action == ACTION_FAN

    def test_a_working_fan_goes_on(self) -> None:
        got = _go(FAN_ROOM_LATER, _fan(FAN_PROOF_MINUTES + 31.0, HIGH + 1.8))
        assert got.action == ACTION_FAN
        assert got.state.fan_failed_at is None

    def test_the_floor_keeps_the_fan_when_cool_is_not_allowed(self) -> None:
        room = round(MIN_COOLING_PERCEIVED_TEMP - 0.35, 2)
        got = _go(room, _fan(57.0, room), tc=19.8, outside_temp=9.4)
        assert got.action == ACTION_FAN
        assert got.state.fan_failed_at is None


class TestFanStartTemp:
    def test_it_is_the_room_temperature_when_the_fan_starts(self) -> None:
        got = _go(FAN_ROOM)
        assert got.action == ACTION_FAN
        assert got.state.fan_start_temp == FAN_ROOM
        assert got.state.fan_since == NOW

    def test_it_is_kept_while_the_fan_runs(self) -> None:
        got = _go(FAN_ROOM_LATER, _fan(11.0, FAN_ROOM))
        assert got.action == ACTION_FAN
        assert got.state.fan_start_temp == FAN_ROOM

    def test_a_missing_one_stays_missing_it_is_not_made_up_mid_run(self) -> None:
        got = _go(FAN_ROOM_LATER, _fan(11.0, None))
        assert got.action == ACTION_FAN
        assert got.state.fan_start_temp is None

    def test_it_clears_when_the_fan_stops(self) -> None:
        got = _go(TC, _fan(11.0, HIGH + 0.1))
        assert got.action == ACTION_OFF
        assert got.state.fan_start_temp is None


class TestRetryBar:
    def test_failing_while_the_fan_runs_stamps_the_bar(self) -> None:
        got = _go(FAN_ROOM, _fan(FAN_PROOF_MINUTES + 4.0, FAN_ROOM))
        assert got.action == ACTION_COOL
        assert got.state.fan_failed_at == NOW

    def test_the_ceiling_stamps_it_too(self) -> None:
        got = _go(HIGH + 2.3, _fan(6.0, HIGH + 3.5))
        assert got.action == ACTION_COOL
        assert got.state.fan_failed_at == NOW

    def test_a_cold_start_straight_to_cool_does_not_stamp_it(self) -> None:
        got = _go(HIGH + 2.6)
        assert got.action == ACTION_COOL
        assert got.state.fan_failed_at is None

    @pytest.mark.parametrize(
        ("age", "action"),
        [
            (0.0, ACTION_COOL),
            (FAN_RETRY_BAR_MINUTES - 2.5, ACTION_COOL),
            (FAN_RETRY_BAR_MINUTES, ACTION_FAN),
        ],
    )
    def test_the_bar_holds_the_fan_back_for_180_minutes(self, age: float, action: str) -> None:
        state = DecisionState(fan_failed_at=NOW - timedelta(minutes=age))
        got = _go(FAN_ROOM, state)
        assert got.action == action

    def test_cool_off_fan_does_not_cycle_inside_the_bar(self) -> None:
        state = _go(FAN_ROOM, _fan(FAN_PROOF_MINUTES + 4.0, FAN_ROOM)).state
        assert state.running == ACTION_COOL
        actions = []
        for step in range(1, 10):
            # Under the cool setpoint the unit goes off; back over the
            # trigger it must come back as cool (barred fan), never as fan.
            got = _go(
                FAN_ROOM_LATER if step % 2 else COOL_SETPOINT - 0.27,
                state,
                now=NOW + timedelta(minutes=17 * step),
                outside_temp=15.0,
            )
            actions.append(got.action)
            state = got.state
        assert ACTION_OFF in actions
        assert ACTION_COOL in actions
        assert ACTION_FAN not in actions

    def test_a_running_fan_inside_the_bar_is_not_kept(self) -> None:
        got = _go(FAN_ROOM, _fan(9.0, HIGH + 1.9, failed_min_ago=FAN_RETRY_BAR_MINUTES / 3))
        assert got.action == ACTION_COOL

    def test_the_guard_refusal_fallback_ignores_the_bar(self) -> None:
        state = DecisionState(
            last_run=ACTION_HEAT,
            last_direction_change=NOW - timedelta(minutes=30),
            fan_failed_at=NOW - timedelta(minutes=10),
        )
        # Over the ceiling, under the line where the dwell is bypassed.
        got = _go(PAST_CEILING, state)
        assert got.refusal is not None
        assert (got.action, got.row) == (ACTION_FAN, 2)

    def test_a_failure_is_stamped_once_per_fan_run(self) -> None:
        # Cool is refused by the guard, so the fan keeps running and keeps failing.
        state = _fan(
            FAN_PROOF_MINUTES + 4.0,
            PAST_CEILING,
            last_run=ACTION_HEAT,
            last_direction_change=NOW - timedelta(minutes=40),
        )
        first = _go(PAST_CEILING, state)
        assert (first.action, first.refusal is not None) == (ACTION_FAN, True)
        assert first.state.fan_failed_at == NOW
        later = _go(PAST_CEILING, first.state, now=NOW + timedelta(minutes=5))
        assert later.state.fan_failed_at == NOW

    def test_a_new_fan_run_can_fail_again_after_the_bar(self) -> None:
        state = _fan(
            FAN_PROOF_MINUTES + 4.0, PAST_CEILING, failed_min_ago=FAN_RETRY_BAR_MINUTES + 120.0
        )
        assert _go(PAST_CEILING, state).state.fan_failed_at == NOW


class TestFanOverASlowlyWarmingRoom:
    """Tc 22.6: band 20.6-24.1 (cooling starts over 23.4), outside 14.2.

    A fan started over the comfort ceiling would not get a fan stage (cool at once);
    here it starts just over the trigger, with the room rising slowly past the end of
    the window.
    """

    START = datetime(2031, 10, 14, 8, 15, tzinfo=UTC)

    def _run(self, every_min: int) -> tuple[list[tuple[int, str]], Decision]:
        state = DecisionState()
        seen: list[tuple[int, str]] = []
        got = None
        for minute in range(0, 81, every_min):
            temp = round(23.5 + 0.4 * minute / 80, 2)
            got = decide(
                DecisionInputs(
                    now=self.START + timedelta(minutes=minute),
                    room_temp=temp,
                    band=comfort_band(22.6),
                    humidity=HUMIDITY_NORMAL,
                    outside_temp=14.2,
                    unit_running=state.running is not None,
                    keep_on=True,
                ),
                state,
            )
            seen.append((minute, got.action))
            if got.action == ACTION_COOL:
                break
            state = got.state
        assert got is not None
        return seen, got

    def test_the_fan_starts_at_the_cool_start_and_the_ladder_reaches_cool_at_25_minutes(
        self,
    ) -> None:
        seen, got = self._run(5)
        assert seen[0] == (0, ACTION_FAN)
        assert seen[-1] == (25, ACTION_COOL)
        assert all(action == ACTION_FAN for _, action in seen[:-1])
        assert "the fan has not lowered it in 25 min (23.5 -> 23.6 C)" in got.reason

    def test_on_the_fifteen_minute_tick_it_is_the_first_tick_past_25_minutes(self) -> None:
        seen, _ = self._run(15)
        assert seen == [(0, ACTION_FAN), (15, ACTION_FAN), (30, ACTION_COOL)]


class TestHeldModeOpposesTheRoom:
    """Keep Climate On never holds heat above Tc or cool below it."""

    def test_heat_above_tc_is_turned_off_even_with_keep_climate_on(self) -> None:
        # Unit in heat set to 22.5, room over Tc 23.0: the ladder says off.
        got = _go(WARM_UNIT_ROOM, _unit(ACTION_HEAT), unit_running=True, keep_on=True)
        assert (got.action, got.send, got.row) == (ACTION_OFF, True, 5)
        assert got.state.running is None
        assert got.reason == (
            f"Room {WARM_UNIT_ROOM:.1f} C is inside the comfort band "
            "(band 21.0-24.5, comfort 23.0, cooling starts over 23.8); "
            "Keep Climate On would hold heat, but the room is above the comfort "
            "temperature (23.0 C): turning the unit off."
        )

    def test_without_keep_climate_on_the_unit_is_turned_off_as_before(self) -> None:
        for running, temp in ((ACTION_HEAT, WARM_UNIT_ROOM), (ACTION_COOL, COOL_UNIT_ROOM)):
            got = _go(temp, _unit(running), unit_running=True, keep_on=False)
            assert (got.action, got.send) == (ACTION_OFF, True), running
            assert "Keep Climate On" not in got.reason

    def test_cool_below_tc_is_turned_off_too(self) -> None:
        got = _go(COOL_UNIT_ROOM, _unit(ACTION_COOL), unit_running=True, keep_on=True)
        assert (got.action, got.send) == (ACTION_OFF, True)
        assert "Keep Climate On would hold cool, but the room is below" in got.reason

    @pytest.mark.parametrize(
        ("running", "temp"),
        [
            (ACTION_HEAT, COOL_UNIT_ROOM),  # under Tc: heat is on the room's side
            (ACTION_HEAT, TC),  # exactly Tc is not opposed
            (ACTION_COOL, TC),
            (ACTION_COOL, COOL_SETPOINT - 0.12),  # over Tc, under the cool setpoint
        ],
    )
    def test_in_step_with_the_room_it_is_held_and_nothing_is_sent(
        self, running: str, temp: float
    ) -> None:
        got = _go(temp, _unit(running), unit_running=True, keep_on=True)
        assert (got.action, got.send) == (ACTION_OFF, False)
        assert "turning the unit off" not in got.reason

    def test_a_unit_that_is_not_running_is_never_sent_an_off(self) -> None:
        got = _go(WARM_UNIT_ROOM, DecisionState(last_run=ACTION_HEAT), keep_on=True)
        assert (got.action, got.send) == (ACTION_OFF, False)

    def test_a_shadow_room_commands_nothing(self) -> None:
        got = _go(
            WARM_UNIT_ROOM, _unit(ACTION_HEAT), unit_running=True, keep_on=True, commands_unit=False
        )
        assert (got.action, got.send) == (ACTION_OFF, False)

    def test_the_helper_names_the_two_opposed_cases_only(self) -> None:
        assert opposes_room(ACTION_HEAT, WARM_UNIT_ROOM, TC)
        assert opposes_room(ACTION_COOL, COOL_UNIT_ROOM, TC)
        assert not opposes_room(ACTION_HEAT, TC, TC)
        assert not opposes_room(ACTION_FAN, 30.0, 23.0)
        assert not opposes_room(None, 30.0, 23.0)


class TestLeavingDryOrFan:
    def test_it_still_holds_the_last_direction_when_that_suits_the_room(self) -> None:
        got = _go(COOL_UNIT_ROOM, _unit(ACTION_FAN, ACTION_HEAT), unit_running=True, keep_on=True)
        assert (got.action, got.setpoint, got.send) == (ACTION_HEAT, 22.5, True)
        assert "leaving the fan and holding heat at 22.5 C" in got.reason

    def test_it_does_not_hold_heat_against_a_warm_room(self) -> None:
        got = _go(
            WARM_UNIT_ROOM + 0.12, _unit(ACTION_FAN, ACTION_HEAT), unit_running=True, keep_on=True
        )
        assert (got.action, got.setpoint, got.send) == (ACTION_OFF, None, True)
        assert "Keep Climate On would hold heat, but the room is above" in got.reason
        assert got.state.running is None

    def test_it_does_not_hold_cool_against_a_cold_room(self) -> None:
        got = _go(TC - 0.95, _unit(ACTION_DRY, ACTION_COOL), unit_running=True, keep_on=True)
        assert (got.action, got.setpoint, got.send) == (ACTION_OFF, None, True)
        assert "Keep Climate On would hold cool, but the room is below" in got.reason

    def test_it_holds_cool_over_tc(self) -> None:
        got = _go(WARM_UNIT_ROOM, _unit(ACTION_DRY, ACTION_COOL), unit_running=True, keep_on=True)
        assert (got.action, got.setpoint) == (ACTION_COOL, 23.5)

    def test_a_refused_change_still_sends_as_before(self) -> None:
        state = DecisionState(
            running=ACTION_FAN,
            fan_since=NOW - timedelta(minutes=5),
            fan_start_temp=HIGH + 1.7,
            last_run=ACTION_HEAT,
            last_direction_change=NOW - timedelta(minutes=30),
        )
        # No fan stage on this unit: with one, the refusal falls back to the fan.
        got = _go(PAST_CEILING, state, unit_running=True, keep_on=True, fan_offered=False)
        assert got.refusal is not None
        assert (got.action, got.send) == (ACTION_OFF, True)

    def test_a_refused_change_with_a_fan_falls_back_to_the_fan(self) -> None:
        state = DecisionState(
            running=ACTION_FAN,
            fan_since=NOW - timedelta(minutes=5),
            fan_start_temp=HIGH + 1.7,
            last_run=ACTION_HEAT,
            last_direction_change=NOW - timedelta(minutes=30),
        )
        got = _go(PAST_CEILING, state, unit_running=True, keep_on=True, outside_temp=None)
        assert got.refusal is not None
        assert (got.action, got.send) == (ACTION_FAN, True)
        assert got.reason.endswith(". Fan only, high speed.")

    def test_no_known_direction_is_left_alone(self) -> None:
        got = _go(WARM_UNIT_ROOM + 0.12, _unit(ACTION_FAN), unit_running=True, keep_on=True)
        assert (got.action, got.send) == (ACTION_OFF, False)

    def test_without_keep_climate_on_it_turns_off_as_before(self) -> None:
        got = _go(
            WARM_UNIT_ROOM + 0.12, _unit(ACTION_FAN, ACTION_HEAT), unit_running=True, keep_on=False
        )
        assert (got.action, got.send) == (ACTION_OFF, True)


class TestRefusalToOff:
    @staticmethod
    def _refused(running: str | None) -> DecisionState:
        return DecisionState(
            running=running,
            last_run=ACTION_HEAT,
            last_direction_change=NOW - timedelta(minutes=30),
        )

    def test_the_unit_running_heat_into_a_hot_room_is_really_turned_off(self) -> None:
        got = _go(
            PAST_CEILING,
            self._refused(ACTION_HEAT),
            unit_running=True,
            keep_on=True,
            fan_offered=False,
        )
        assert got.refusal is not None
        assert (got.action, got.send) == (ACTION_OFF, True)
        assert "the unit stays off" not in got.reason
        assert "Keep Climate On would hold heat, but the room is above" in got.reason
        assert got.reason.startswith("change from heat to cool refused")

    def test_a_unit_that_really_is_off_stays_off_and_nothing_is_sent(self) -> None:
        got = _go(
            PAST_CEILING, self._refused(None), unit_running=False, keep_on=True, fan_offered=False
        )
        assert got.refusal is not None
        assert (got.action, got.send) == (ACTION_OFF, False)
        assert got.reason.endswith("the unit stays off.")

    def test_the_cool_run_into_a_cold_room_is_turned_off(self) -> None:
        state = DecisionState(
            running=ACTION_COOL,
            last_run=ACTION_COOL,
            last_direction_change=NOW - timedelta(minutes=30),
        )
        got = _go(LOW_ROOM, state, unit_running=True, keep_on=True)
        assert got.refusal is not None
        assert (got.action, got.send) == (ACTION_OFF, True)
        assert "Keep Climate On would hold cool, but the room is below" in got.reason


class TestCoordinatorWiring:
    @staticmethod
    async def _heating_unit(hass: HomeAssistant, entry, *, keep_on: bool) -> tuple:
        coordinator = await _office(
            hass, entry, ac_state="heat", perceived=WARM_UNIT_ROOM, target=22.4
        )
        coordinator.keep_climate_on = keep_on
        _seed_initial_runs(coordinator)
        _seed_outside(hass)
        _register(hass, calls := [])
        return coordinator, calls

    @pytest.mark.asyncio
    @pytest.mark.parametrize("keep_on", [True, False])
    async def test_the_heating_unit_is_turned_off_either_way(
        self, hass: HomeAssistant, mock_config_entry_with_outside, keep_on: bool
    ) -> None:
        coordinator, calls = await self._heating_unit(
            hass, mock_config_entry_with_outside, keep_on=keep_on
        )

        await _decide(coordinator, hass)

        assert [c.service for c in calls] == ["turn_off"]
        assert calls[0].data["entity_id"] == CLIMATE_ENTITY
        reason = coordinator.command_history[-1]["reason"]
        assert coordinator.comfort is not None
        assert coordinator.comfort["reason"] == reason
        assert ("Keep Climate On would hold heat" in reason) is keep_on

    @pytest.mark.asyncio
    async def test_the_selector_turns_off_a_direction_it_does_not_allow(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        coordinator.hvac_mode = "heat"
        # Heat has run for hours, the room is hot: the ladder asks for cool, which the
        # heat selector does not allow.
        state = _unit(ACTION_HEAT, pending=ACTION_COOL, pending_ticks=1)
        inp = DecisionInputs(
            now=NOW,
            room_temp=PAST_CEILING,
            band=comfort_band(TC),
            humidity=HUMIDITY_NORMAL,
            unit_running=True,
            keep_on=True,
        )
        decision = decide(inp, state)
        assert decision.action == ACTION_COOL

        got = coordinator._apply_selector(decision, state, inp)

        assert (got.action, got.send) == (ACTION_OFF, True)
        assert "Keep Climate On would hold heat, but the room is above" in got.reason
        assert got.state.running is None

        quiet = coordinator._apply_selector(
            decision, _unit(ACTION_FAN), DecisionInputs(**{**inp.__dict__, "keep_on": True})
        )
        assert (quiet.action, quiet.send) == (ACTION_OFF, False)
        assert quiet.reason.endswith("the unit stays off.")

    @pytest.mark.asyncio
    async def test_a_restored_fan_state_without_a_start_temp_goes_to_cool_on_the_first_tick(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        entry = mock_config_entry_with_outside
        coordinator = await _office(
            hass, entry, ac_state="fan_only", perceived=round(BAND.high + 0.7, 2)
        )
        _seed_initial_runs(coordinator)
        _seed_outside(hass)
        hass.states.async_set(OUTSIDE_TEMP_SENSOR, "15.0")
        # A stored fan state without a start temp: running fan, a fan clock, no start temp.
        coordinator._ladder_state = DecisionState(
            running=ACTION_FAN, fan_since=dt_util.utcnow() - timedelta(minutes=90)
        )
        _register(hass, calls := [])

        await _decide(coordinator, hass)

        assert [c.service for c in calls] == ["set_temperature"]
        assert calls[0].data["hvac_mode"] == "cool"
        assert "fan has not been seen to lower it" in coordinator.command_history[-1]["reason"]
        assert coordinator._ladder_state.fan_failed_at is not None


class TestPersistence:
    @pytest.mark.asyncio
    async def test_both_fields_round_trip(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        key = _preload(hass_storage, mock_config_entry, _stored())
        first = ClimateAdaptCoordinator(hass, mock_config_entry)
        await first.async_setup()
        now = dt_util.utcnow()
        first._ladder_state = DecisionState(
            running=ACTION_FAN,
            fan_since=now,
            fan_start_temp=HIGH + 1.6,
            fan_failed_at=now - timedelta(hours=1),
        )
        await first.async_save()

        saved = hass_storage[key]["data"]["comfort_state"]
        assert saved["fan_start_temp"] == HIGH + 1.6
        assert saved["fan_failed_at"] == (now - timedelta(hours=1)).isoformat()

        second = ClimateAdaptCoordinator(hass, mock_config_entry)
        await second.async_setup()
        assert second._ladder_state == first._ladder_state

    @pytest.mark.asyncio
    async def test_an_old_store_without_them_loads_as_none(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        _preload(
            hass_storage,
            mock_config_entry,
            _stored(
                comfort_state={
                    "running": "fan",
                    "fan_since": "2031-10-14T09:45:00+00:00",
                    "last_run": "cool",
                }
            ),
        )
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)

        await coordinator.async_setup()

        state = coordinator._ladder_state
        assert state.running == ACTION_FAN
        assert state.fan_since == datetime(2031, 10, 14, 9, 45, tzinfo=UTC)
        assert (state.fan_start_temp, state.fan_failed_at) == (None, None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["warm", True, [], {}, float("nan"), float("inf")])
    async def test_a_malformed_start_temp_is_dropped(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry, bad: object
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        _preload(
            hass_storage,
            mock_config_entry,
            _stored(
                comfort_state={
                    "running": "fan",
                    "fan_start_temp": bad,
                    "fan_failed_at": "yesterday",
                }
            ),
        )
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)

        await coordinator.async_setup()

        assert coordinator._ladder_state.running == ACTION_FAN
        assert coordinator._ladder_state.fan_start_temp is None
        assert coordinator._ladder_state.fan_failed_at is None

    @pytest.mark.asyncio
    async def test_an_integer_start_temp_is_kept_as_a_float(
        self, hass: HomeAssistant, hass_storage: dict, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        _preload(
            hass_storage,
            mock_config_entry,
            _stored(comfort_state={"running": "fan", "fan_start_temp": 27}),
        )
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)

        await coordinator.async_setup()

        assert coordinator._ladder_state.fan_start_temp == 27.0

    @pytest.mark.asyncio
    async def test_the_input_state_carries_them_like_fan_since(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        coordinator = ClimateAdaptCoordinator(hass, mock_config_entry)
        now = dt_util.utcnow()
        failed = now - timedelta(minutes=50)
        coordinator._ladder_state = DecisionState(
            running=ACTION_FAN, fan_since=now, fan_start_temp=26.1, fan_failed_at=failed
        )

        fan = coordinator._ladder_input_state(now, "fan_only", shadow=False)
        assert (fan.fan_since, fan.fan_start_temp, fan.fan_failed_at) == (now, 26.1, failed)

        cool = coordinator._ladder_input_state(now, "cool", shadow=False)
        assert (cool.fan_since, cool.fan_start_temp) == (None, None)
        assert cool.fan_failed_at == failed

        shadow = coordinator._ladder_input_state(now, "off", shadow=True)
        assert (shadow.fan_start_temp, shadow.fan_failed_at) == (26.1, failed)


def test_humid_dry_row_is_unaffected_by_a_failed_fan_bar() -> None:
    state = DecisionState(fan_failed_at=NOW - timedelta(minutes=10))
    got = _go(23.2, state, humidity=HUMIDITY_HUMID, dry_allowed=True)
    assert got.action == ACTION_DRY
