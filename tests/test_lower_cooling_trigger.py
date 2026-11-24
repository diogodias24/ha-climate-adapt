# SPDX-License-Identifier: Apache-2.0
"""Fan first at Tc + 0.8 K, then cool (ceiling, failed fan, sun load).

Tc is 23.3 throughout (the band, the triggers and the fan-keep line are derived from it
below): cooling trigger Tc + 0.8, comfort ceiling Tc + 1.5, idle trigger one kelvin
higher, fan kept while warm > Tc + 0.5. The cooling floor is a production constant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climate_adapt.comfort import (
    COOL_START_ABOVE_TC,
    HUMIDITY_NORMAL,
    comfort_band,
)
from custom_components.climate_adapt.const import MIN_COOLING_PERCEIVED_TEMP
from custom_components.climate_adapt.decision import (
    ACTION_COOL,
    ACTION_FAN,
    ACTION_HEAT,
    ACTION_OFF,
    Decision,
    DecisionInputs,
    DecisionState,
    decide,
)

NOW = datetime(2031, 10, 14, 16, 0, tzinfo=UTC)
TC = 23.3
BAND = comfort_band(TC)
LOW = BAND.low
TRIGGER = BAND.cool_start
CEILING = BAND.high
IDLE_TRIGGER = comfort_band(TC, motion_idle=True).cool_start
IDLE_CEILING = CEILING + 1.0
KEEP = TC + 0.5
FLOOR = MIN_COOLING_PERCEIVED_TEMP
# A room over the trigger and under the ceiling; a bit over the trigger.
ROOM = 24.5
OVER = 24.3


def _inputs(temp: float, **kw: object) -> DecisionInputs:
    idle = bool(kw.pop("motion_idle", False))
    tc = float(kw.pop("tc", TC))  # type: ignore[arg-type]
    return DecisionInputs(
        now=NOW,
        room_temp=temp,
        band=comfort_band(tc, idle),
        humidity=HUMIDITY_NORMAL,
        **kw,  # type: ignore[arg-type]
    )


def _go(
    temp: float,
    state: DecisionState | None = None,
    **kw: object,
) -> Decision:
    return decide(_inputs(temp, **kw), state or DecisionState())


def _off_after(last_run: str, hours: float = 10.0, **kw: object) -> DecisionState:
    return DecisionState(
        last_run=last_run, last_direction_change=NOW - timedelta(hours=hours), **kw
    )  # type: ignore[arg-type]


def _fan(minutes: float, start_temp: float | None) -> DecisionState:
    return DecisionState(
        running=ACTION_FAN,
        fan_since=NOW - timedelta(minutes=minutes),
        fan_start_temp=start_temp,
        last_run=ACTION_COOL,
        last_direction_change=NOW - timedelta(hours=10),
    )


class TestTheBand:
    def test_cool_start_is_tc_plus_point_eight_and_the_top_is_unchanged(self) -> None:
        band = comfort_band(TC)
        assert COOL_START_ABOVE_TC == 0.8
        assert (band.cool_start, band.high, band.heat_start, band.low) == pytest.approx(
            (TC + 0.8, TC + 1.5, TC - 2.0, TC - 2.0)
        )

    def test_idle_room_widens_the_trigger_by_one_kelvin(self) -> None:
        band = comfort_band(TC, motion_idle=True)
        assert (band.cool_start, band.high) == pytest.approx((TC + 1.8, TC + 1.5))


class TestAnOffUnitOverTheTrigger:
    """Room over the trigger, outside a little cooler, unit off, last run cool."""

    def test_fan_not_barred_starts_with_the_fan_at_once(self) -> None:
        got = _go(ROOM, _off_after(ACTION_COOL), outside_temp=18.5)
        assert (got.action, got.send, got.refusal) == (ACTION_FAN, True, None)
        assert f"{TRIGGER:.1f}" in got.reason
        assert "inside the comfort band" not in got.reason
        assert got.state.pending is None

    def test_the_fan_reason_says_what_happens_next(self) -> None:
        got = _go(ROOM, _off_after(ACTION_COOL), outside_temp=18.5)
        assert "fan first" in got.reason
        assert f"{CEILING:.1f}" in got.reason
        assert "0.3 K in 25 min" in got.reason

    def test_fan_barred_goes_straight_to_cool_without_ticks(self) -> None:
        barred = _off_after(ACTION_COOL, fan_failed_at=NOW - timedelta(hours=2.5))
        got = _go(ROOM, barred, outside_temp=18.5)
        assert (got.action, got.send, got.refusal) == (ACTION_COOL, True, None)
        assert f"{TRIGGER:.1f}" in got.reason
        assert "barred for 30 more" in got.reason

    def test_the_fan_is_the_same_with_the_room_cooler_than_outside(self) -> None:
        got = _go(ROOM, _off_after(ACTION_COOL), outside_temp=27.0)
        assert (got.action, got.send, got.refusal) == (ACTION_FAN, True, None)

    def test_the_fan_is_the_same_with_no_outside_reading(self) -> None:
        assert _go(ROOM, _off_after(ACTION_COOL)).action == ACTION_FAN


class TestTheTrigger:
    @pytest.mark.parametrize(
        ("temp", "action"),
        [
            (TRIGGER - 0.1, ACTION_OFF),
            (TRIGGER, ACTION_OFF),
            (TRIGGER + 0.05, ACTION_FAN),
            (TRIGGER + 0.3, ACTION_FAN),
        ],
    )
    def test_the_fan_starts_strictly_over_the_trigger(self, temp: float, action: str) -> None:
        got = _go(temp)
        assert got.action == action
        assert got.send is (action == ACTION_FAN)

    @pytest.mark.parametrize(
        ("temp", "action"),
        [
            (TRIGGER - 0.1, ACTION_OFF),
            (TRIGGER, ACTION_OFF),
            (TRIGGER + 0.05, ACTION_COOL),
            (TRIGGER + 0.3, ACTION_COOL),
        ],
    )
    def test_cool_starts_strictly_over_the_trigger_when_there_is_no_fan(
        self, temp: float, action: str
    ) -> None:
        got = _go(temp, fan_offered=False)
        assert got.action == action
        assert got.send is (action == ACTION_COOL)

    def test_a_room_under_the_trigger_is_told_so(self) -> None:
        got = _go(TRIGGER - 0.1)
        assert "inside the comfort band" in got.reason
        assert f"cooling starts over {TRIGGER:.1f}" in got.reason

    def test_a_room_between_the_trigger_and_the_band_top_is_not_told_nothing_to_do(self) -> None:
        got = _go(ROOM)
        assert got.action == ACTION_FAN
        assert "over the cooling trigger" in got.reason
        assert "inside the comfort band" not in got.reason

    def test_an_idle_room_starts_at_the_widened_trigger(self) -> None:
        under = _go(IDLE_TRIGGER - 0.1, motion_idle=True)
        assert under.action == ACTION_OFF
        assert "empty" in under.reason
        assert f"cooling only starts over {IDLE_TRIGGER:.1f}" in under.reason
        assert _go(IDLE_TRIGGER + 0.1, motion_idle=True).action == ACTION_FAN
        assert _go(IDLE_TRIGGER + 0.1, fan_offered=False, motion_idle=True).action == ACTION_COOL

    def test_the_heat_side_is_unchanged(self) -> None:
        assert _go(LOW - 0.1).action == ACTION_HEAT
        assert _go(LOW).action == ACTION_OFF
        assert _go(LOW - 0.8, motion_idle=True).action == ACTION_OFF
        assert _go(LOW - 1.2, motion_idle=True).action == ACTION_HEAT
        assert _go(LOW - 2.0).action == ACTION_HEAT


class TestTheRoomTemperatureDecides:
    """The room sensor's own reading is the only temperature the ladder reads."""

    def test_the_decision_inputs_carry_no_feels_like_reading(self) -> None:
        assert not hasattr(_inputs(ROOM), "perceived_temp")

    def test_a_reason_never_says_feels_like(self) -> None:
        for temp in (24.1, ROOM, 24.9, 25.8):
            assert "feels like" not in _go(temp).reason

    def test_the_cooling_floor_reads_the_room_and_blocks_cool_below_it(self) -> None:
        cool_tc = comfort_band(21.0)
        got = decide(
            DecisionInputs(
                now=NOW,
                room_temp=23.4,
                band=cool_tc,
                humidity=HUMIDITY_NORMAL,
                fan_offered=False,
            ),
            DecisionState(),
        )
        assert got.action == ACTION_OFF
        assert "cooling floor of 23.5" in got.reason

    def test_the_ceiling_is_the_room_reaching_it(self) -> None:
        assert _go(CEILING - 0.1, state=_fan(5, CEILING - 0.1)).action == ACTION_FAN
        assert _go(CEILING, state=_fan(5, CEILING)).action == ACTION_COOL


class TestTheFanProofFollowsTheTrigger:
    def test_a_fan_that_does_not_drop_0_3_k_in_25_minutes_has_failed(self) -> None:
        got = _go(OVER, state=_fan(26, OVER))
        assert (got.action, got.send, got.refusal) == (ACTION_COOL, True, None)
        assert got.state.fan_failed_at == NOW
        assert "the fan has not lowered it" in got.reason

    def test_a_fan_that_drops_0_4_k_in_25_minutes_keeps_going(self) -> None:
        got = _go(OVER, state=_fan(26, OVER + 0.4))
        assert got.action == ACTION_FAN
        assert got.state.fan_failed_at is None

    def test_a_fan_in_the_first_minutes_is_not_judged(self) -> None:
        assert _go(OVER, state=_fan(10, OVER)).action == ACTION_FAN

    def test_a_fan_that_is_down_to_the_trigger_is_not_judged_a_failure(self) -> None:
        assert _go(TRIGGER, state=_fan(60, TRIGGER)).action == ACTION_FAN


class TestTheCeiling:
    def test_the_room_reaching_the_ceiling_inside_the_proof_window_goes_to_cool_at_once(
        self,
    ) -> None:
        got = _go(CEILING, state=_fan(10, OVER))
        assert (got.action, got.send, got.refusal) == (ACTION_COOL, True, None)
        assert got.state.fan_failed_at == NOW
        assert "ceiling" in got.reason

    def test_just_under_the_ceiling_is_still_fan(self) -> None:
        got = _go(CEILING - 0.1, state=_fan(10, OVER))
        assert (got.action, got.refusal) == (ACTION_FAN, None)

    def test_a_fan_that_is_working_below_the_ceiling_runs_on_with_no_thirty_minute_rule(
        self,
    ) -> None:
        assert _go(CEILING - 0.1, state=_fan(90, CEILING + 1.1)).action == ACTION_FAN

    def test_no_plus_one_or_plus_two_offsets_apply(self) -> None:
        for offset_room in (CEILING + 0.2, CEILING + 0.4):
            assert _go(offset_room, state=_fan(31, CEILING + 0.9)).action == ACTION_COOL
        assert _go(CEILING - 0.1, state=_fan(31, CEILING + 0.9)).action == ACTION_FAN

    def test_an_off_unit_already_over_the_ceiling_cools_without_the_fan_stage(self) -> None:
        got = _go(CEILING + 0.2, state=_off_after(ACTION_COOL))
        assert (got.action, got.send, got.refusal) == (ACTION_COOL, True, None)

    def test_the_idle_ceiling_moves_with_the_idle_trigger(self) -> None:
        assert _go(IDLE_CEILING - 0.1, motion_idle=True).action == ACTION_FAN
        assert _go(IDLE_CEILING, motion_idle=True).action == ACTION_COOL


class TestTheFanKeepsRunning:
    def test_the_fan_is_kept_while_the_room_is_over_tc_plus_half_a_kelvin(self) -> None:
        assert _go(KEEP + 0.1, state=_fan(10, ROOM)).action == ACTION_FAN
        assert _go(KEEP, state=_fan(10, ROOM)).action == ACTION_OFF

    def test_keeping_the_fan_needs_no_cooler_outside(self) -> None:
        got = _go(KEEP + 0.3, state=_fan(10, ROOM), outside_temp=28.0)
        assert got.action == ACTION_FAN
        assert "keeping the fan on" in got.reason


class TestTheBarAndTheTicks:
    def test_after_a_failed_fan_a_hot_room_goes_straight_to_cool_inside_the_bar(self) -> None:
        state = _off_after(ACTION_COOL, fan_failed_at=NOW - timedelta(minutes=100))
        got = _go(OVER, state=state)
        assert (got.action, got.send, got.refusal) == (ACTION_COOL, True, None)

    def test_after_the_bar_the_fan_is_tried_again(self) -> None:
        state = _off_after(ACTION_COOL, fan_failed_at=NOW - timedelta(minutes=181))
        assert _go(OVER, state=state).action == ACTION_FAN

    def test_without_a_fan_a_hot_room_cools(self) -> None:
        got = _go(OVER, state=_off_after(ACTION_HEAT, hours=3), fan_offered=False)
        assert (got.action, got.send, got.refusal) == (ACTION_COOL, True, None)

    def test_heat_to_cool_with_the_dwell_open_is_refused_by_the_dwell_only_and_falls_back_to_fan(
        self,
    ) -> None:
        state = DecisionState(
            running=ACTION_FAN,
            fan_since=NOW - timedelta(minutes=5),
            fan_start_temp=ROOM - 0.2,
            last_run=ACTION_HEAT,
            last_direction_change=NOW - timedelta(minutes=30),
        )
        got = _go(CEILING, state=state)
        assert got.action == ACTION_FAN
        assert got.refusal is not None
        assert "minutes" in got.refusal
        assert "confirming" not in got.refusal

    def test_heat_to_cool_with_the_dwell_closed_goes_fan_to_cool_without_ticks(self) -> None:
        state = DecisionState(
            running=ACTION_FAN,
            fan_since=NOW - timedelta(minutes=5),
            fan_start_temp=ROOM - 0.2,
            last_run=ACTION_HEAT,
            last_direction_change=NOW - timedelta(hours=3),
        )
        got = _go(CEILING, state=state)
        assert (got.action, got.send, got.refusal) == (ACTION_COOL, True, None)

    def test_a_barred_off_unit_refused_by_the_dwell_still_gets_the_fan(self) -> None:
        state = _off_after(ACTION_HEAT, hours=0.5, fan_failed_at=NOW - timedelta(minutes=60))
        got = _go(ROOM, state=state)
        assert got.action == ACTION_FAN
        assert got.refusal is not None
        assert "confirming" not in got.refusal

    def test_beyond_the_cool_start_plus_one_and_a_half_the_dwell_is_bypassed(self) -> None:
        state = _off_after(ACTION_HEAT, hours=0.5)
        assert _go(TRIGGER + 1.5, state=state, fan_offered=False).refusal is not None
        got = _go(TRIGGER + 1.6, state=state, fan_offered=False)
        assert (got.action, got.refusal, got.send) == (ACTION_COOL, None, True)

    def test_a_unit_that_is_running_dry_still_confirms_a_change_to_cool(self) -> None:
        state = DecisionState(
            running="dry",
            last_run=ACTION_HEAT,
            last_direction_change=NOW - timedelta(hours=3),
        )
        got = _go(ROOM, state=state, fan_offered=False)
        assert got.refusal is not None
        assert "1 of 2 confirming ticks" in got.refusal

    def test_the_same_direction_is_never_guarded(self) -> None:
        got = _go(ROOM, state=_off_after(ACTION_COOL, hours=0.1), fan_offered=False)
        assert (got.action, got.refusal) == (ACTION_COOL, None)


class TestUnderSunLoad:
    """A room with sun on the window: cool at the trigger, no fan stage."""

    def test_an_off_room_over_the_trigger_cools_at_once_and_says_why(self) -> None:
        got = _go(
            ROOM,
            state=_off_after(ACTION_COOL),
            heat_load=True,
            heat_load_note="sun on the window, PV 3.4 kW, blind open",
        )
        assert (got.action, got.send, got.refusal) == (ACTION_COOL, True, None)
        assert "sun on the window, PV 3.4 kW" in got.reason
        assert "not fan first" in got.reason

    def test_no_load_is_the_fan(self) -> None:
        assert _go(ROOM, state=_off_after(ACTION_COOL), heat_load=False).action == ACTION_FAN

    def test_load_does_not_cool_a_room_under_the_trigger(self) -> None:
        got = _go(TRIGGER - 0.1, state=_off_after(ACTION_COOL), heat_load=True)
        assert got.action == ACTION_OFF

    def test_a_running_fan_goes_to_cool_on_the_tick_the_load_appears(self) -> None:
        got = _go(
            OVER,
            state=_fan(5, OVER),
            heat_load=True,
            heat_load_note="sun on the window, PV 2.7 kW, blind open",
        )
        assert (got.action, got.send, got.refusal) == (ACTION_COOL, True, None)
        assert "not fan first" in got.reason

    def test_load_with_a_real_heat_to_cool_flip_still_waits_out_the_dwell_on_the_fan(self) -> None:
        state = _off_after(ACTION_HEAT, hours=0.5)
        got = _go(OVER, state=state, heat_load=True)
        assert got.action == ACTION_FAN
        assert got.refusal is not None
        assert "confirming" not in got.refusal

    def test_the_exit_is_unchanged_cool_stops_at_the_floor(self) -> None:
        running = DecisionState(
            running=ACTION_COOL,
            last_run=ACTION_COOL,
            last_direction_change=NOW - timedelta(hours=1),
        )
        # Tc 23.0: Tc + 0.5 is the floor, and both are under the cooling trigger.
        assert _go(FLOOR + 0.1, state=running, heat_load=True, tc=23.0).action == ACTION_COOL
        assert _go(FLOOR, state=running, heat_load=True, tc=23.0).action == ACTION_OFF

    def test_load_never_cools_below_the_floor(self) -> None:
        assert _go(FLOOR - 0.1, heat_load=True, tc=21.0).action == ACTION_OFF

    def test_an_empty_room_waits_for_the_idle_trigger_even_under_load(self) -> None:
        assert _go(IDLE_TRIGGER - 0.1, heat_load=True, motion_idle=True).action == ACTION_OFF
        assert _go(IDLE_TRIGGER + 0.1, heat_load=True, motion_idle=True).action == ACTION_COOL
