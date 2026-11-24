# SPDX-License-Identifier: Apache-2.0
"""A latched heat run ends early when it is mild outside.

Tc is 23.0 throughout: band 21.0 - 24.5, heat setpoint 22.5, cool setpoint 23.5
(also the cooling floor). The heat exit needs the room at the low edge plus
``HEAT_EXIT_MARGIN_C`` or more and outside at ``HEAT_EXIT_OUTSIDE_C`` or more.
Starting is never touched, and the cool latch has no exit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from custom_components.climate_adapt.comfort import HUMIDITY_NORMAL, comfort_band
from custom_components.climate_adapt.decision import (
    ACTION_COOL,
    ACTION_FAN,
    ACTION_HEAT,
    ACTION_OFF,
    HEAT_EXIT_MARGIN_C,
    HEAT_EXIT_OUTSIDE_C,
    Decision,
    DecisionInputs,
    DecisionState,
    decide,
)

from .test_startup_gate import (
    BAND,
    CLIMATE_ENTITY,
    HEAT_SETPOINT,
    OUTSIDE_TEMP_SENSOR,
    _decide,
    _office,
    _register,
    _seed_initial_runs,
    _seed_outside,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

NOW = datetime(2031, 10, 14, 15, 0, tzinfo=UTC)
TC = 23.0
LOW, HIGH = 21.0, 24.5
COOL_START = TC + 0.8

#: Scenario inputs, all derived from the exit thresholds under test.
ROOM_INSIDE = round(LOW + HEAT_EXIT_MARGIN_C + 0.9, 2)
MILD_OUTSIDE = round(HEAT_EXIT_OUTSIDE_C + 7.3, 1)
COLD_OUTSIDE = round(HEAT_EXIT_OUTSIDE_C - 7.4, 1)
WARM_OUTSIDE = round(HEAT_EXIT_OUTSIDE_C + 10.1, 1)
BELOW_BAND_ROOM = round(LOW - 1.15, 2)
WIRED_ROOM = round(BAND.low + HEAT_EXIT_MARGIN_C + 0.8, 1)
WIRED_MILD = "26.2"
WIRED_COLD = "9.4"


def _go(
    temp: float,
    running: str | None,
    outside: float | None,
    *,
    tc: float = TC,
    motion_idle: bool = False,
    **kw: object,
) -> Decision:
    kw.setdefault("unit_running", running is not None)
    inputs = DecisionInputs(
        now=NOW,
        room_temp=temp,
        band=comfort_band(tc, motion_idle),
        humidity=HUMIDITY_NORMAL,
        outside_temp=outside,
        **kw,  # type: ignore[arg-type]
    )
    return decide(inputs, _state(running))


def _state(running: str | None) -> DecisionState:
    direction = running if running in (ACTION_HEAT, ACTION_COOL) else None
    return DecisionState(
        running=running,
        fan_since=NOW - timedelta(minutes=5) if running == ACTION_FAN else None,
        last_run=direction,
        last_direction_change=NOW - timedelta(hours=10) if direction else None,
    )


class TestConstants:
    def test_the_decided_numbers(self) -> None:
        assert HEAT_EXIT_MARGIN_C == 0.5
        assert HEAT_EXIT_OUTSIDE_C == 18.0


class TestHeatExit:
    def test_a_mild_outside_stops_a_latched_run_and_says_so(self) -> None:
        got = _go(ROOM_INSIDE, ACTION_HEAT, MILD_OUTSIDE)
        assert (got.action, got.row, got.send, got.setpoint) == (ACTION_OFF, 5, True, None)
        assert got.state.running is None
        assert got.state.last_run == ACTION_HEAT
        assert got.reason == (
            f"Room {ROOM_INSIDE} C is inside the comfort band "
            "(band 21.0-24.5, comfort 23.0, cooling starts over 23.8); "
            f"stopped heating, it is mild outside ({MILD_OUTSIDE} C): nothing to do."
        )

    def test_cold_outside_keeps_the_latch_and_says_inside_the_band(self) -> None:
        got = _go(ROOM_INSIDE, ACTION_HEAT, COLD_OUTSIDE)
        assert (got.action, got.row, got.setpoint) == (ACTION_HEAT, 1, 22.5)
        assert got.reason == (
            f"Room {ROOM_INSIDE} C is inside the comfort band "
            "(band 21.0-24.5, comfort 23.0, cooling starts over 23.8); "
            "continuing to heat to 22.5 C (latched)."
        )

    @pytest.mark.parametrize(
        ("outside", "action"),
        [(HEAT_EXIT_OUTSIDE_C, ACTION_OFF), (HEAT_EXIT_OUTSIDE_C - 0.1, ACTION_HEAT)],
    )
    def test_the_outside_threshold_is_inclusive(self, outside: float, action: str) -> None:
        assert _go(ROOM_INSIDE, ACTION_HEAT, outside).action == action

    @pytest.mark.parametrize(
        ("temp", "action"),
        [(LOW + HEAT_EXIT_MARGIN_C, ACTION_OFF), (LOW + HEAT_EXIT_MARGIN_C - 0.1, ACTION_HEAT)],
    )
    def test_the_room_needs_the_margin_above_the_low_edge(self, temp: float, action: str) -> None:
        assert _go(temp, ACTION_HEAT, WARM_OUTSIDE).action == action

    def test_just_under_the_margin_keeps_heating_even_when_warm_outside(self) -> None:
        got = _go(LOW + HEAT_EXIT_MARGIN_C - 0.3, ACTION_HEAT, MILD_OUTSIDE)
        assert got.action == ACTION_HEAT
        assert "inside the comfort band" in got.reason

    def test_below_the_band_heats_whatever_the_outside_says(self) -> None:
        got = _go(BELOW_BAND_ROOM, ACTION_HEAT, WARM_OUTSIDE)
        assert (got.action, got.row) == (ACTION_HEAT, 1)
        assert "below the comfort band" in got.reason
        assert "heating to 22.5 C" in got.reason

    def test_a_start_is_not_touched(self) -> None:
        got = _go(BELOW_BAND_ROOM, None, WARM_OUTSIDE)
        assert got.action == ACTION_HEAT
        assert "below the comfort band" in got.reason

    def test_an_unusable_outside_keeps_the_latch(self) -> None:
        got = _go(ROOM_INSIDE, ACTION_HEAT, None)
        assert got.action == ACTION_HEAT
        assert "continuing to heat to 22.5 C (latched)" in got.reason

    def test_a_unit_that_is_not_running_is_not_started_in_band(self) -> None:
        got = _go(ROOM_INSIDE, None, COLD_OUTSIDE)
        assert (got.action, got.row, got.send) == (ACTION_OFF, 5, False)

    def test_the_empty_room_widened_band_exits_the_same_way(self) -> None:
        # The start edge is widened; the exit margin is measured from the unwidened low.
        exit_edge = LOW + HEAT_EXIT_MARGIN_C
        assert _go(exit_edge - 0.1, ACTION_HEAT, MILD_OUTSIDE, motion_idle=True).action == (
            ACTION_HEAT
        )
        assert _go(exit_edge, ACTION_HEAT, MILD_OUTSIDE, motion_idle=True).action == ACTION_OFF
        assert _go(BELOW_BAND_ROOM, ACTION_HEAT, WARM_OUTSIDE, motion_idle=True).action == (
            ACTION_HEAT
        )

    def test_a_drifting_room_exits_once_and_restarts_only_under_the_start_edge(self) -> None:
        state = _state(ACTION_HEAT)
        actions = []
        for temp in (22.35, 21.45, 20.65, 21.35, 21.15):
            got = decide(
                DecisionInputs(
                    now=NOW,
                    room_temp=temp,
                    band=comfort_band(TC),
                    humidity=HUMIDITY_NORMAL,
                    outside_temp=MILD_OUTSIDE,
                    unit_running=state.running is not None,
                ),
                state,
            )
            actions.append(got.action)
            state = got.state
        # The first reading is over the exit edge and exits; the second is in band with
        # the unit off, so no restart; the third is under the start edge and restarts;
        # the latch then holds through readings under the exit edge.
        assert actions == [ACTION_OFF, ACTION_OFF, ACTION_HEAT, ACTION_HEAT, ACTION_HEAT]

    def test_outside_noise_around_the_threshold_cannot_relatch_after_the_drop(self) -> None:
        state = _go(ROOM_INSIDE, ACTION_HEAT, HEAT_EXIT_OUTSIDE_C + 0.9).state
        assert state.running is None
        for outside in (
            HEAT_EXIT_OUTSIDE_C - 1.4,
            HEAT_EXIT_OUTSIDE_C + 0.6,
            HEAT_EXIT_OUTSIDE_C - 0.2,
            HEAT_EXIT_OUTSIDE_C + 1.1,
        ):
            got = decide(
                DecisionInputs(
                    now=NOW,
                    room_temp=ROOM_INSIDE,
                    band=comfort_band(TC),
                    humidity=HUMIDITY_NORMAL,
                    outside_temp=outside,
                ),
                state,
            )
            assert got.action == ACTION_OFF


class TestHeatExitWithKeepClimateOn:
    def test_the_latch_is_released_and_nothing_is_sent(self) -> None:
        got = _go(ROOM_INSIDE, ACTION_HEAT, MILD_OUTSIDE, keep_on=True)
        assert (got.action, got.send, got.row) == (ACTION_OFF, False, 5)
        assert "stopped" not in got.reason
        assert "Keep Climate On holds the unit" in got.reason
        assert "nothing is sent" in got.reason
        assert "below the comfort band" not in got.reason

    def test_cold_outside_keeps_heating_with_keep_on(self) -> None:
        got = _go(ROOM_INSIDE, ACTION_HEAT, COLD_OUTSIDE, keep_on=True)
        assert (got.action, got.send) == (ACTION_HEAT, True)

    def test_a_shadow_room_commands_nothing_so_it_never_says_stopped(self) -> None:
        got = _go(ROOM_INSIDE, ACTION_HEAT, MILD_OUTSIDE, commands_unit=False)
        assert got.action == ACTION_OFF
        assert "stopped" not in got.reason
        assert "heating is no longer needed" in got.reason


class TestCoolLatchIsUnchanged:
    @pytest.mark.parametrize("outside", [None, COLD_OUTSIDE, MILD_OUTSIDE, WARM_OUTSIDE])
    def test_a_latched_cool_run_inside_the_band_keeps_cooling_whatever_the_outside(
        self, outside: float | None
    ) -> None:
        # Cooling starts over Tc + 0.8, so a room just under it is the latched case.
        room = round(COOL_START - 0.2, 1)
        got = _go(room, ACTION_COOL, outside)
        assert (got.action, got.row, got.setpoint) == (ACTION_COOL, 3, 23.5)
        assert got.reason == (
            f"Room {room} C is inside the comfort band "
            "(band 21.0-24.5, comfort 23.0, cooling starts over 23.8); "
            "continuing to cool to 23.5 C (latched)."
        )

    def test_over_the_cooling_trigger_cools_with_the_trigger_wording(self) -> None:
        got = _go(round(COOL_START + 0.35, 2), ACTION_COOL, COLD_OUTSIDE)
        assert (got.action, got.row) == (ACTION_COOL, 3)
        assert "over the cooling trigger" in got.reason

    def test_the_cool_latch_ends_at_the_setpoint_as_before(self) -> None:
        assert _go(TC + 0.5, ACTION_COOL, COLD_OUTSIDE).action == ACTION_OFF


class TestReasonText:
    @pytest.mark.parametrize("running", [None, ACTION_HEAT, ACTION_COOL, ACTION_FAN])
    @pytest.mark.parametrize(
        "outside", [None, 4.6, HEAT_EXIT_OUTSIDE_C - 0.1, HEAT_EXIT_OUTSIDE_C, 26.4, 31.4]
    )
    @pytest.mark.parametrize("keep_on", [False, True])
    @pytest.mark.parametrize("motion_idle", [False, True])
    def test_below_the_comfort_band_is_said_iff_the_room_is_under_the_low_edge(
        self, running: str | None, outside: float | None, keep_on: bool, motion_idle: bool
    ) -> None:
        band = comfort_band(TC, motion_idle)
        for tenth in range(180, 290):
            temp = tenth / 10
            got = _go(temp, running, outside, keep_on=keep_on, motion_idle=motion_idle)
            said = "below the comfort band" in got.reason
            # Never said of a room at or over the low edge. Said of every room under it,
            # except where a refused direction change reports the refusal instead.
            assert not said or temp < band.low, (temp, running, outside, got.reason)
            assert said or got.refusal is not None or temp >= band.low, (
                temp,
                running,
                outside,
                got.reason,
            )

    @pytest.mark.parametrize("running", [None, ACTION_HEAT, ACTION_COOL])
    @pytest.mark.parametrize("outside", [None, COLD_OUTSIDE, MILD_OUTSIDE, 31.4])
    def test_above_the_comfort_band_is_never_said_of_a_room_inside_it(
        self, running: str | None, outside: float | None
    ) -> None:
        for tenth in range(180, 290):
            temp = tenth / 10
            got = _go(temp, running, outside)
            if "above the comfort band" in got.reason:
                assert temp > HIGH, (temp, running, outside, got.reason)


class TestCoordinatorWiring:
    """A latched heating room inside the band with a mild outside."""

    @pytest.mark.asyncio
    async def test_the_unit_is_turned_off_and_the_reason_is_published(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        coordinator = await _office(
            hass, mock_config_entry_with_outside, ac_state="heat", perceived=WIRED_ROOM
        )
        coordinator.keep_climate_on = False
        _seed_initial_runs(coordinator)
        _seed_outside(hass)
        hass.states.async_set(OUTSIDE_TEMP_SENSOR, WIRED_MILD)
        _register(hass, calls := [])

        await _decide(coordinator, hass)

        assert [c.service for c in calls] == ["turn_off"]
        assert calls[0].data["entity_id"] == CLIMATE_ENTITY
        reason = coordinator.command_history[-1]["reason"]
        assert f"stopped heating, it is mild outside ({WIRED_MILD} C)" in reason
        assert "below the comfort band" not in reason
        assert coordinator.comfort is not None
        assert coordinator.comfort["reason"] == reason
        assert coordinator.comfort["action"] == ACTION_OFF

    @pytest.mark.asyncio
    async def test_keep_climate_on_sends_nothing_and_records_the_hold(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        coordinator = await _office(
            hass, mock_config_entry_with_outside, ac_state="heat", perceived=WIRED_ROOM
        )
        assert coordinator.keep_climate_on is True
        _seed_initial_runs(coordinator)
        _seed_outside(hass)
        hass.states.async_set(OUTSIDE_TEMP_SENSOR, WIRED_MILD)
        _register(hass, calls := [])

        await _decide(coordinator, hass)

        assert calls == []
        row = coordinator.command_history[-1]
        assert row["kind"] == "skip_comfort_hold"
        assert "Keep Climate On holds the unit" in row["reason"]
        assert "stopped" not in row["reason"]
        assert coordinator.comfort is not None
        assert coordinator.comfort["reason"] == row["reason"]

    @pytest.mark.asyncio
    async def test_cold_outside_keeps_heating(
        self, hass: HomeAssistant, mock_config_entry_with_outside
    ) -> None:
        coordinator = await _office(
            hass, mock_config_entry_with_outside, ac_state="heat", perceived=WIRED_ROOM
        )
        coordinator.keep_climate_on = False
        _seed_initial_runs(coordinator)
        _seed_outside(hass)
        hass.states.async_set(OUTSIDE_TEMP_SENSOR, WIRED_COLD)
        _register(hass, calls := [])

        await _decide(coordinator, hass)

        assert [c.service for c in calls] == ["set_temperature"]
        assert (
            f"continuing to heat to {HEAT_SETPOINT} C (latched)"
            in coordinator.command_history[-1]["reason"]
        )
