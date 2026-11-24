# SPDX-License-Identifier: Apache-2.0
"""The decision ladder (``decision.py``).

Tc is 23.0 throughout, so the numbers are round: band 21.0 - 24.5 (the top is
display only), heat starts under 21.0, cooling starts over 23.8 (Tc + 0.8),
heat setpoint 22.5, cool setpoint 23.5, fan stop 23.5. Over the
trigger the fan comes first (no outside-air condition); cooling takes over at the
ceiling, 24.5 (the band top), when the fan fails its proof, when the fan is barred
or not offered, or at once under heat load.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climate_adapt.comfort import (
    HUMIDITY_DRY_AIR,
    HUMIDITY_HUMID,
    HUMIDITY_NORMAL,
    HUMIDITY_UNKNOWN,
    comfort_band,
)
from custom_components.climate_adapt.decision import (
    ACTION_COOL,
    ACTION_DRY,
    ACTION_FAN,
    ACTION_HEAT,
    ACTION_OFF,
    DIRECTION_DWELL_MINUTES,
    Decision,
    DecisionInputs,
    DecisionState,
    decide,
)

NOW = datetime(2031, 10, 27, 15, 0, tzinfo=UTC)
TC = 23.0
LOW, HIGH = 21.0, 24.5
COOL_START = 23.8  # Tc + 0.8: the cooling trigger; HIGH is only the band top shown
HEAT_SP, COOL_SP = 22.5, 23.5


def _go(temp: float, state: DecisionState | None = None, **kw: object) -> Decision:
    tc = float(kw.pop("tc", TC))  # type: ignore[arg-type]
    idle = bool(kw.pop("motion_idle", False))
    now = kw.pop("now", NOW)
    humidity = kw.pop("humidity", HUMIDITY_NORMAL)
    inputs = DecisionInputs(
        now=now,  # type: ignore[arg-type]
        room_temp=temp,
        band=comfort_band(tc, idle),
        humidity=humidity,  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )
    return decide(inputs, state or DecisionState())


def _running(
    action: str, since_min: float = 0.0, fan_start_temp: float | None = None
) -> DecisionState:
    return DecisionState(
        running=action,
        fan_since=NOW - timedelta(minutes=since_min) if action == ACTION_FAN else None,
        fan_start_temp=fan_start_temp if action == ACTION_FAN else None,
        last_run=action if action in (ACTION_HEAT, ACTION_COOL) else None,
        last_direction_change=(
            NOW - timedelta(hours=10) if action in (ACTION_HEAT, ACTION_COOL) else None
        ),
    )


class TestHeatRow:
    @pytest.mark.parametrize(
        ("temp", "action"), [(LOW - 0.1, ACTION_HEAT), (LOW, ACTION_OFF), (LOW + 0.1, ACTION_OFF)]
    )
    def test_starts_strictly_below_the_low_edge(self, temp: float, action: str) -> None:
        got = _go(temp)
        assert got.action == action
        assert got.row == (1 if action == ACTION_HEAT else 5)

    def test_setpoint_is_tc_minus_half(self) -> None:
        got = _go(19.0)
        assert (got.setpoint, got.fan_mode, got.send) == (HEAT_SP, None, True)
        assert "heating to 22.5" in got.reason

    def test_setpoint_is_quantised_to_the_unit_step(self) -> None:
        assert _go(15.0, tc=22.7).setpoint == 22.0  # 22.2 -> 22.0
        assert _go(15.0, tc=23.3).setpoint == 23.0  # 22.8 -> 23.0
        assert _go(15.0, tc=22.7, step=1.0).setpoint == 22.0

    @pytest.mark.parametrize(
        ("temp", "action"), [(22.4, ACTION_HEAT), (22.5, ACTION_OFF), (23.0, ACTION_OFF)]
    )
    def test_runs_on_until_the_setpoint_is_reached(self, temp: float, action: str) -> None:
        assert _go(temp, _running(ACTION_HEAT)).action == action

    def test_does_not_start_between_the_edge_and_the_setpoint(self) -> None:
        assert _go(21.5).action == ACTION_OFF
        assert _go(21.5, _running(ACTION_HEAT)).action == ACTION_HEAT

    def test_humidity_never_stops_heating(self) -> None:
        assert _go(19.0, humidity=HUMIDITY_HUMID).action == ACTION_HEAT

    def test_outside_does_not_change_heating(self) -> None:
        assert _go(19.0, outside_temp=5.0).action == ACTION_HEAT
        assert _go(19.0, outside_temp=None).action == ACTION_HEAT


class TestCoolRow:
    @pytest.mark.parametrize(
        ("temp", "action"), [(23.8, ACTION_OFF), (23.81, ACTION_COOL), (30.0, ACTION_COOL)]
    )
    def test_starts_strictly_above_the_cooling_start(self, temp: float, action: str) -> None:
        assert _go(temp, fan_offered=False).action == action

    def test_setpoint_is_tc_plus_half(self) -> None:
        got = _go(26.0, fan_offered=False)
        assert (got.setpoint, got.row, got.send) == (COOL_SP, 3, True)
        assert "cooling to 23.5" in got.reason

    @pytest.mark.parametrize(
        ("temp", "action"), [(23.6, ACTION_COOL), (23.5, ACTION_OFF), (22.0, ACTION_OFF)]
    )
    def test_runs_on_until_the_setpoint_is_reached(self, temp: float, action: str) -> None:
        assert _go(temp, _running(ACTION_COOL), fan_offered=False).action == action

    def test_cooling_floor_blocks_a_cold_room(self) -> None:
        # Tc 21.5 -> cooling start 22.3; 23.2 is above it but under the 23.5 floor.
        got = _go(23.2, tc=21.5, fan_offered=False)
        assert got.action == ACTION_OFF
        assert "cooling floor" in got.reason
        assert _go(23.5, tc=21.5, fan_offered=False).action == ACTION_COOL

    def test_floor_is_judged_on_room_temperature(self) -> None:
        """Humidity is not folded into the judged value."""
        for humidity in (HUMIDITY_HUMID, HUMIDITY_NORMAL, HUMIDITY_UNKNOWN):
            assert _go(23.2, tc=21.5, fan_offered=False, humidity=humidity).action != ACTION_COOL

    def test_setpoint_never_below_the_floor(self) -> None:
        assert _go(26.0, tc=22.0, fan_offered=False).setpoint == 23.5  # Tc + 0.5 = 22.5

    def test_hybrid_may_switch_the_floor_off(self) -> None:
        got = _go(23.2, tc=21.5, fan_offered=False, cooling_floor=None)
        assert (got.action, got.setpoint) == (ACTION_COOL, 22.0)

    def test_hot_and_humid_is_cool(self) -> None:
        assert _go(26.0, humidity=HUMIDITY_HUMID, fan_offered=False).action == ACTION_COOL


class TestFanRow:
    def test_outside_cooler_by_1_5_starts_the_fan(self) -> None:
        got = _go(24.1, outside_temp=22.6)
        assert (got.action, got.row, got.setpoint, got.fan_mode, got.send) == (
            ACTION_FAN,
            2,
            None,
            "high",
            True,
        )

    def test_outside_cooler_by_less_than_1_5_still_starts_the_fan(self) -> None:
        assert _go(24.1, outside_temp=23.5).action == ACTION_FAN

    def test_outside_equal_or_warmer_still_starts_the_fan(self) -> None:
        assert _go(24.1, outside_temp=24.1).action == ACTION_FAN
        assert _go(24.1, outside_temp=30.0).action == ACTION_FAN

    def test_outside_missing_still_starts_the_fan(self) -> None:
        got = _go(24.1, outside_temp=None)
        assert got.action == ACTION_FAN
        assert got.advice is None

    def test_fan_reason_names_the_trigger_the_ceiling_and_the_proof(self) -> None:
        got = _go(24.1, outside_temp=None)
        assert got.reason == (
            "Room 24.1 C is over the cooling trigger (band 21.0-24.5, comfort 23.0, "
            "cooling starts over 23.8): fan first, high speed. Cooling starts if the "
            "room reaches 24.5 C or the fan does not lower it 0.3 K in 25 min."
        )

    def test_fan_starts_at_once_with_no_confirming_ticks(self) -> None:
        got = _go(24.1, outside_temp=30.0)
        assert (got.action, got.refusal, got.state.pending_ticks) == (ACTION_FAN, None, 0)

    def test_unit_without_fan_only_drops_the_row(self) -> None:
        assert _go(24.1, outside_temp=10.0, fan_offered=False).action == ACTION_COOL

    def test_not_started_inside_the_band(self) -> None:
        assert _go(COOL_START, outside_temp=10.0).action == ACTION_OFF

    @pytest.mark.parametrize(
        ("temp", "outside", "action"),
        [
            (23.75, 23.25, ACTION_FAN),  # under the cooling start, over Tc + 0.5: keep going
            (23.75, 23.35, ACTION_FAN),  # the outside air does not matter
            (23.75, 30.0, ACTION_FAN),
            (23.75, None, ACTION_FAN),
            (23.6, 10.0, ACTION_FAN),
            (23.5, 10.0, ACTION_OFF),  # Tc + 0.5 reached
        ],
    )
    def test_keeps_going_until_tc_plus_half(
        self, temp: float, outside: float | None, action: str
    ) -> None:
        assert _go(temp, _running(ACTION_FAN, 5), outside_temp=outside).action == action

    def test_the_ceiling_means_cool_whatever_the_outside(self) -> None:
        for outside in (24.8, 15.0, 30.0, None):
            got = _go(25.0, _running(ACTION_FAN, 5, fan_start_temp=26.0), outside_temp=outside)
            assert got.action == ACTION_COOL

    def test_a_fan_does_not_start_inside_the_hysteresis_zone(self) -> None:
        assert _go(23.7, outside_temp=10.0).action == ACTION_OFF

    def test_fan_since_is_remembered_and_not_reset_by_a_running_fan(self) -> None:
        first = _go(24.1, outside_temp=18.0)
        assert first.state.fan_since == NOW
        later = _go(24.1, first.state, outside_temp=18.0, now=NOW + timedelta(minutes=10))
        assert later.state.fan_since == NOW

    def test_fan_since_clears_when_the_fan_stops(self) -> None:
        assert _go(23.0, _running(ACTION_FAN, 5), outside_temp=10.0).state.fan_since is None

    def test_advice_tells_the_user_a_window_would_do(self) -> None:
        got = _go(28.0, outside_temp=24.0)
        assert got.advice is not None
        assert "outside is 4 K cooler" in got.advice
        assert "window" in got.advice


class TestFanEscalation:
    @pytest.mark.parametrize(
        ("minutes", "temp", "action"),
        [
            (29.9, 24.3, ACTION_FAN),  # no time rule, only the ceiling (24.5)
            (30.0, 24.3, ACTION_FAN),
            (30.0, 24.5, ACTION_COOL),  # the ceiling is inclusive
            (45.0, 24.9, ACTION_COOL),
            (45.0, 24.0, ACTION_FAN),
            (0.0, 24.3, ACTION_FAN),
            (0.0, 24.5, ACTION_COOL),  # at the ceiling: at any time, no confirming ticks
            (5.0, 28.0, ACTION_COOL),
        ],
    )
    def test_escalates_to_cool(self, minutes: float, temp: float, action: str) -> None:
        # The fan is proving itself (the room fell 1 K since it started), so only the
        # ceiling (band top, 24.5) can escalate it.
        got = _go(temp, _running(ACTION_FAN, minutes, fan_start_temp=temp + 1.0), outside_temp=15.0)
        assert got.action == action

    @pytest.mark.parametrize(
        ("minutes", "start", "action"),
        [
            (24.9, 24.1, ACTION_FAN),  # not 25 min yet
            (25.0, 24.1, ACTION_COOL),  # 0.1 K lower than where it started: not enough
            (25.0, 24.3, ACTION_FAN),  # 0.3 K lower: proven
            (25.0, None, ACTION_COOL),  # no start temperature recorded: not seen to work
        ],
    )
    def test_a_fan_that_does_not_lower_the_room_escalates(
        self, minutes: float, start: float | None, action: str
    ) -> None:
        got = _go(24.0, _running(ACTION_FAN, minutes, fan_start_temp=start), outside_temp=15.0)
        assert got.action == action
        if action == ACTION_COOL:
            assert got.refusal is None
            assert got.state.fan_failed_at == NOW

    def test_a_barred_fan_goes_straight_to_cool_from_off(self) -> None:
        state = DecisionState(fan_failed_at=NOW - timedelta(minutes=60))
        got = _go(24.1, state, outside_temp=15.0)
        assert (got.action, got.row, got.refusal) == (ACTION_COOL, 3, None)
        assert "the fan failed 60 min ago (barred for 120 more): cooling to 23.5 C." in got.reason

    def test_the_bar_ends_after_180_minutes(self) -> None:
        state = DecisionState(fan_failed_at=NOW - timedelta(minutes=180))
        assert _go(24.1, state, outside_temp=15.0).action == ACTION_FAN

    def test_heat_load_cools_from_off_with_no_fan_stage(self) -> None:
        note = "sun on the window, PV 3.4 kW, blind open"
        got = _go(24.1, outside_temp=15.0, heat_load=True, heat_load_note=note)
        assert (got.action, got.row, got.refusal) == (ACTION_COOL, 3, None)
        assert f"{note}: cooling now, not fan first, to 23.5 C." in got.reason
        # heat load alone does not start anything at or under the trigger
        assert _go(COOL_START, heat_load=True, heat_load_note=note).action == ACTION_OFF

    def test_clearly_too_hot_cools_from_a_cold_start(self) -> None:
        got = _go(27.0, outside_temp=15.0)
        assert got.action == ACTION_COOL
        assert "fan is not enough" in got.reason

    def test_once_cooling_it_does_not_drop_back_to_the_fan(self) -> None:
        got = _go(25.0, _running(ACTION_COOL), outside_temp=15.0)
        assert got.action == ACTION_COOL

    def test_escalation_hands_the_unit_to_the_direction_guard(self) -> None:
        state = DecisionState(
            running=ACTION_FAN,
            fan_since=NOW - timedelta(minutes=45),
            last_run=ACTION_HEAT,
            last_direction_change=NOW - timedelta(minutes=20),
        )
        # 24.9 is over the ceiling but not past the dwell bypass (start + 1.5 = 25.3)
        got = _go(24.9, state, outside_temp=15.0)
        assert got.action == ACTION_FAN  # the flip is refused; the fan is still allowed
        assert got.refusal is not None
        assert got.reason.endswith(". Fan only, high speed.")

    def test_a_cold_room_that_is_too_hot_for_the_floor_keeps_the_fan(self) -> None:
        # Tc 21.5, high edge 23.0, U + 2 = 25 -> but the floor forbids cool below 23.5.
        got = _go(23.4, _running(ACTION_FAN, 60), tc=20.0, outside_temp=10.0)
        assert got.action == ACTION_FAN


class TestDryRow:
    @pytest.mark.parametrize(
        ("temp", "action"),
        [(22.99, ACTION_OFF), (23.0, ACTION_DRY), (23.5, ACTION_DRY), (23.8, ACTION_DRY)],
    )
    def test_humid_starts_only_at_or_over_tc(self, temp: float, action: str) -> None:
        got = _go(temp, humidity=HUMIDITY_HUMID)
        assert got.action == action
        assert got.setpoint is None
        assert got.row == (4 if action == ACTION_DRY else 5)

    def test_cold_and_humid_is_heat_not_dry(self) -> None:
        assert _go(20.0, humidity=HUMIDITY_HUMID).action == ACTION_HEAT

    def test_only_humid_triggers_it(self) -> None:
        for humidity in (HUMIDITY_NORMAL, HUMIDITY_DRY_AIR, HUMIDITY_UNKNOWN):
            assert _go(23.0, humidity=humidity).action == ACTION_OFF

    def test_dry_mode_switch_off_means_off(self) -> None:
        assert _go(23.0, humidity=HUMIDITY_HUMID, dry_allowed=False).action == ACTION_OFF

    def test_stops_when_the_air_clears(self) -> None:
        assert _go(23.0, _running(ACTION_DRY), humidity=HUMIDITY_NORMAL).action == ACTION_OFF

    def test_stops_when_the_room_gets_cold(self) -> None:
        assert _go(21.4, _running(ACTION_DRY), humidity=HUMIDITY_HUMID).action == ACTION_OFF

    def test_cooling_floor_under_a_hot_humid_room_falls_through_to_dry(self) -> None:
        got = _go(23.2, tc=21.5, humidity=HUMIDITY_HUMID, fan_offered=False)
        assert got.action == ACTION_DRY

    def test_advice_when_outside_air_is_drier(self) -> None:
        got = _go(23.0, humidity=HUMIDITY_HUMID, dew_point=18.0, outside_dew_point=9.0)
        assert got.advice == "outside air is drier: ventilating will lower the humidity"
        assert (
            _go(23.0, humidity=HUMIDITY_HUMID, dew_point=18.0, outside_dew_point=17.0).advice
            is None
        )
        assert _go(23.0, humidity=HUMIDITY_HUMID, dew_point=18.0).advice is None


class TestOffRow:
    def test_unit_that_ha_believes_is_running_is_turned_off(self) -> None:
        got = _go(23.0, unit_running=True)
        assert (got.action, got.send, got.setpoint) == (ACTION_OFF, True, None)

    def test_keep_climate_on_leaves_it_alone_and_sends_nothing(self) -> None:
        got = _go(23.0, unit_running=True, keep_on=True)
        assert (got.action, got.send) == (ACTION_OFF, False)

    def test_already_off_sends_nothing(self) -> None:
        assert _go(23.0, unit_running=False).send is False

    @pytest.mark.parametrize("keep_on", [True, False])
    def test_active_runs_are_resent_every_tick_whatever_keep_on_says(self, keep_on: bool) -> None:
        for temp, kw in [
            (19.0, {}),
            (26.0, {"fan_offered": False}),
            (26.0, {"outside_temp": 15.0}),
        ]:
            assert _go(temp, keep_on=keep_on, unit_running=True, **kw).send is True
        assert _go(23.0, humidity=HUMIDITY_HUMID, keep_on=keep_on).send is True


class TestInputsMissing:
    def test_every_optional_input_missing_still_decides(self) -> None:
        got = _go(19.0, outside_temp=None, humidity=HUMIDITY_UNKNOWN)
        assert got.action == ACTION_HEAT
        assert _go(26.0, outside_temp=None, humidity=HUMIDITY_UNKNOWN).action == ACTION_COOL
        assert _go(23.0, outside_temp=None, humidity=HUMIDITY_UNKNOWN).action == ACTION_OFF

    def test_hybrid_shifts_the_whole_band(self) -> None:
        assert _go(25.0, tc=26.0).action == ACTION_OFF
        assert _go(23.9, tc=26.0).action == ACTION_HEAT  # low edge 24.0


class TestMotion:
    def test_idle_room_starts_later_but_runs_to_the_same_stop(self) -> None:
        assert _go(20.6, motion_idle=True).action == ACTION_OFF  # heat starts below 20.0
        assert _go(19.7, motion_idle=True).action == ACTION_HEAT
        assert _go(20.6, _running(ACTION_HEAT), motion_idle=True).action == ACTION_HEAT
        assert (
            _go(24.5, motion_idle=True, fan_offered=False).action == ACTION_OFF
        )  # cool above 24.8
        assert _go(24.9, motion_idle=True, fan_offered=False).action == ACTION_COOL
        assert _go(24.9, motion_idle=True, outside_temp=15.0).action == ACTION_FAN


def _flip_state(last_run: str, minutes_ago: float, **kw: object) -> DecisionState:
    return DecisionState(
        last_run=last_run, last_direction_change=NOW - timedelta(minutes=minutes_ago), **kw  # type: ignore[arg-type]
    )


class TestDirectionGuard:
    def test_first_run_of_a_unit_with_no_history_starts_the_clock(self) -> None:
        got = _go(19.0)
        assert got.action == ACTION_HEAT
        assert (got.state.last_run, got.state.last_direction_change) == (ACTION_HEAT, NOW)
        assert got.dwell_remaining_min == DIRECTION_DWELL_MINUTES

    def test_same_direction_never_asks_and_never_moves_the_clock(self) -> None:
        state = _flip_state(ACTION_HEAT, 5)
        got = _go(19.0, state)
        assert got.action == ACTION_HEAT
        assert got.refusal is None
        assert got.state.last_direction_change == state.last_direction_change

    def test_flip_needs_two_confirming_ticks(self) -> None:
        state = _flip_state(ACTION_COOL, 600)
        first = _go(19.0, state)
        assert first.action == ACTION_OFF
        assert first.refusal is not None
        assert "1 of 2 confirming ticks" in first.refusal
        second = _go(19.0, first.state, now=NOW + timedelta(minutes=5))
        assert (second.action, second.refusal) == (ACTION_HEAT, None)
        assert second.state.last_run == ACTION_HEAT
        assert second.state.last_direction_change == NOW + timedelta(minutes=5)
        assert second.state.pending is None

    def test_a_tick_that_stops_asking_resets_the_count(self) -> None:
        state = _flip_state(ACTION_COOL, 600)
        first = _go(19.0, state)
        between = _go(22.0, first.state)
        assert between.state.pending_ticks == 0
        again = _go(19.0, between.state)
        assert again.action == ACTION_OFF
        assert again.state.pending_ticks == 1

    def test_flip_needs_120_minutes_since_the_last_direction_change(self) -> None:
        state = _flip_state(ACTION_COOL, 119)
        for _ in range(5):
            got = _go(19.0, state)
            state = got.state
        assert got.action == ACTION_OFF
        assert got.refusal is not None
        assert "119 of 120 minutes" in got.refusal
        assert got.dwell_remaining_min == pytest.approx(1.0)
        assert state.pending_ticks == 5  # confirmation keeps counting while the dwell runs

    def test_the_dwell_ends_at_120_and_the_confirmed_flip_goes_through(self) -> None:
        state = _flip_state(ACTION_COOL, 119, pending=ACTION_HEAT, pending_ticks=9)
        assert _go(19.0, state).action == ACTION_OFF
        assert _go(19.0, state, now=NOW + timedelta(minutes=1)).action == ACTION_HEAT

    @pytest.mark.parametrize(("temp", "bypass"), [(18.0, False), (17.9, True)])
    def test_extreme_cold_bypasses_the_dwell_but_not_the_confirm(
        self, temp: float, bypass: bool
    ) -> None:
        state = _flip_state(ACTION_COOL, 10)
        first = _go(temp, state)
        assert first.action == ACTION_OFF  # confirm tick 1 of 2 either way
        second = _go(temp, first.state)
        assert (second.action == ACTION_HEAT) is bypass

    @pytest.mark.parametrize(("temp", "bypass"), [(25.3, False), (25.4, True)])
    def test_extreme_heat_bypasses_the_dwell_and_an_off_unit_needs_no_confirm(
        self, temp: float, bypass: bool
    ) -> None:
        """Bypass is cooling start + 1.5 (25.3); off -> cool waives the confirm."""
        state = _flip_state(ACTION_HEAT, 10)  # the unit is off: state.running is None
        first = _go(temp, state, fan_offered=False)
        assert (first.action == ACTION_COOL) is bypass  # no confirming tick either way
        if not bypass:
            assert first.refusal is not None
            assert "10 of 120 minutes" in first.refusal
            assert "confirming ticks" not in first.refusal
        second = _go(temp, first.state, fan_offered=False)
        assert (second.action == ACTION_COOL) is bypass

    def test_off_to_cool_needs_no_confirming_ticks_once_the_dwell_has_passed(self) -> None:
        got = _go(26.0, _flip_state(ACTION_HEAT, 600), fan_offered=False)
        assert (got.action, got.refusal) == (ACTION_COOL, None)
        assert got.state.last_run == ACTION_COOL

    def test_a_running_unit_changing_to_cool_still_needs_two_confirming_ticks(self) -> None:
        """Only an off unit (or a failed fan) is waived; a unit in dry is not."""
        state = DecisionState(
            running=ACTION_DRY,
            last_run=ACTION_HEAT,
            last_direction_change=NOW - timedelta(hours=10),
        )
        first = _go(26.0, state, fan_offered=False)
        assert first.action == ACTION_OFF
        assert first.refusal is not None
        assert "1 of 2 confirming ticks" in first.refusal
        second = _go(26.0, first.state, fan_offered=False)
        assert (second.action, second.refusal) == (ACTION_COOL, None)

    def test_a_refusal_never_leaves_the_unit_running_the_wrong_way(self) -> None:
        state = DecisionState(
            running=ACTION_COOL,
            last_run=ACTION_COOL,
            last_direction_change=NOW - timedelta(minutes=30),
        )
        got = _go(19.0, state, unit_running=True)
        assert (got.action, got.send, got.state.running) == (ACTION_OFF, True, None)

    def test_refused_cool_goes_to_the_fan_when_the_fan_row_allows(self) -> None:
        # A fan that has not lowered the room: the ladder asks for cool at 24.9, under the
        # dwell bypass (25.3), so the 30 min old heat direction refuses it.
        state = DecisionState(
            running=ACTION_FAN,
            fan_since=NOW - timedelta(minutes=40),
            last_run=ACTION_HEAT,
            last_direction_change=NOW - timedelta(minutes=30),
        )
        got = _go(24.9, state, outside_temp=18.0)
        assert (got.action, got.fan_mode, got.row) == (ACTION_FAN, "high", 2)
        assert got.refusal is not None
        assert got.state.last_run == ACTION_HEAT

    def test_refused_cool_falls_back_to_the_fan_whatever_the_outside(self) -> None:
        """The fallback needs only fan_offered; the outside air is not asked."""
        state = _flip_state(ACTION_HEAT, 30)
        for outside in (25.0, None, 5.0):
            got = _go(24.5, state, outside_temp=outside)
            assert (got.action, got.fan_mode) == (ACTION_FAN, "high")
            assert got.refusal is not None
            assert "30 of 120 minutes" in got.refusal
            assert "confirming ticks" not in got.refusal  # off -> cool waives the ticks
            assert got.reason.endswith(". Fan only, high speed.")

    def test_refused_cool_goes_off_when_the_fan_row_does_not_allow(self) -> None:
        state = _flip_state(ACTION_HEAT, 30)
        got = _go(24.5, state, fan_offered=False)
        assert got.action == ACTION_OFF
        assert got.refusal is not None

    def test_refused_cool_keeps_a_running_fan_whatever_the_outside(self) -> None:
        state = DecisionState(
            running=ACTION_FAN,
            fan_since=NOW - timedelta(minutes=40),
            last_run=ACTION_HEAT,
            last_direction_change=NOW - timedelta(minutes=30),
        )
        for outside in (24.0, 30.0, None):
            assert _go(24.8, state, outside_temp=outside).action == ACTION_FAN

    @pytest.mark.parametrize("between", [ACTION_OFF, ACTION_DRY, ACTION_FAN])
    def test_passing_through_another_state_cannot_bypass_the_guard(self, between: str) -> None:
        """The compared direction is the last one that ran, not the current state."""
        state = _flip_state(ACTION_COOL, 40)
        if between == ACTION_DRY:
            step = _go(23.0, state, humidity=HUMIDITY_HUMID)
        elif between == ACTION_FAN:
            step = _go(24.1, state, outside_temp=15.0)  # over the trigger, under the ceiling
        else:
            step = _go(23.0, state)
        assert step.action == between
        assert step.state.last_run == ACTION_COOL
        later = _go(19.0, step.state, now=NOW + timedelta(minutes=10))
        again = _go(19.0, later.state, now=NOW + timedelta(minutes=15))
        assert later.action == again.action == ACTION_OFF
        assert again.refusal is not None
        assert "minutes" in again.refusal

    def test_a_restart_keeps_the_guard_when_the_direction_survives_it(self) -> None:
        """A state rebuilt from the store (last_run + clock) refuses like the live one."""
        rebuilt = DecisionState(
            last_run=ACTION_COOL, last_direction_change=NOW - timedelta(minutes=50)
        )
        assert _go(19.0, rebuilt).action == ACTION_OFF
        confirmed = _go(19.0, rebuilt)
        assert _go(19.0, confirmed.state).action == ACTION_OFF  # dwell, not confirm, now

    def test_a_restart_that_lost_the_direction_has_nothing_to_guard(self) -> None:
        assert _go(19.0, DecisionState()).action == ACTION_HEAT

    def test_missing_clock_with_a_known_direction_is_not_forever(self) -> None:
        state = DecisionState(last_run=ACTION_COOL, last_direction_change=None)
        first = _go(19.0, state)
        assert _go(19.0, first.state).action == ACTION_HEAT

    def test_flip_to_heat_is_not_asked_from_inside_the_band(self) -> None:
        state = _flip_state(ACTION_COOL, 600)
        got = _go(21.5, state)
        assert got.refusal is None
        assert got.state.pending is None

    def test_overshoot_needed_to_ask_is_1_3_kelvin(self) -> None:
        """Heat ends at Tc - 0.5 and cool starts at Tc + 0.8."""
        heating = _go(18.0)
        assert heating.setpoint == TC - 0.5
        assert comfort_band(TC).cool_start == pytest.approx(COOL_START)
        assert COOL_START - heating.setpoint == pytest.approx(1.3)


class TestSweep:
    """Whatever the state, the ladder never contradicts the band."""

    STATES = (
        DecisionState(),
        _running(ACTION_HEAT),
        _running(ACTION_COOL),
        _running(ACTION_FAN, 60),
        _running(ACTION_DRY),
    )

    @pytest.mark.parametrize("state", STATES, ids=lambda s: str(s.running))
    @pytest.mark.parametrize("humidity", [HUMIDITY_HUMID, HUMIDITY_NORMAL, HUMIDITY_UNKNOWN])
    @pytest.mark.parametrize("outside", [None, 5.0, 24.0, 35.0])
    def test_invariants(self, state: DecisionState, humidity: str, outside: float | None) -> None:
        for tenth in range(150, 330):
            temp = tenth / 10
            got = _go(temp, state, humidity=humidity, outside_temp=outside)
            if got.action == ACTION_HEAT:
                assert temp < HEAT_SP
            if got.action == ACTION_COOL:
                assert temp > COOL_SP
                assert temp >= 23.5
            if got.action == ACTION_FAN:
                assert temp > COOL_SP  # the outside air is not a condition
            if got.action == ACTION_DRY:
                assert humidity == HUMIDITY_HUMID
                assert temp >= LOW + 0.5
            if temp < LOW:
                # the cool state's 10 h old direction needs two confirming ticks to flip
                assert got.action == (ACTION_OFF if state.running == ACTION_COOL else ACTION_HEAT)
            if state.running is None and LOW <= temp <= COOL_START and humidity != HUMIDITY_HUMID:
                assert got.action == ACTION_OFF
            if state.running is None and temp > COOL_START:
                assert got.action != ACTION_OFF
            if state.running is None and COOL_START < temp < HIGH - 0.05:
                # fan first, whatever the outside air or the humidity
                assert got.action == ACTION_FAN
            if state.running is None and temp >= HIGH:
                assert got.action == ACTION_COOL  # at the ceiling the fan is not enough
            assert got.send == (got.action != ACTION_OFF)
            assert (got.setpoint is None) == (got.action in (ACTION_OFF, ACTION_FAN, ACTION_DRY))
            assert got.reason


def _dry_after(last_run: str | None) -> DecisionState:
    return DecisionState(
        running=ACTION_DRY,
        last_run=last_run,
        last_direction_change=NOW - timedelta(hours=10) if last_run else None,
    )


class TestDryIsACoolingCycle:
    """Dry belongs to the cooling cycle: a humid room just under Tc is not dried after heat."""

    def test_humid_room_just_under_tc_is_not_dried(self) -> None:
        got = _go(22.6, _running(ACTION_HEAT), humidity=HUMIDITY_HUMID)
        assert got.action == ACTION_OFF

    def test_dry_keeps_running_down_to_half_a_degree_under_tc(self) -> None:
        assert _go(22.6, _dry_after(ACTION_HEAT), humidity=HUMIDITY_HUMID).action == ACTION_DRY
        assert _go(22.5, _dry_after(ACTION_HEAT), humidity=HUMIDITY_HUMID).action == ACTION_OFF

    def test_heat_to_dry_is_not_a_direction_change(self) -> None:
        got = _go(23.2, _running(ACTION_HEAT), humidity=HUMIDITY_HUMID)
        assert (got.action, got.refusal) == (ACTION_DRY, None)
        assert got.state.last_run == ACTION_HEAT

    def test_dry_then_cold_is_not_held_up_by_a_dry_direction_guard(self) -> None:
        state = _dry_after(ACTION_HEAT)
        got = _go(20.6, state, humidity=HUMIDITY_HUMID)
        assert (got.action, got.refusal) == (ACTION_HEAT, None)


class TestLeavingDry:
    """A unit in dry that the ladder no longer asks for is not left in dry."""

    def test_without_keep_climate_on_it_is_turned_off(self) -> None:
        got = _go(23.0, _dry_after(ACTION_HEAT), unit_running=True, humidity=HUMIDITY_NORMAL)
        assert (got.action, got.send) == (ACTION_OFF, True)

    def test_with_keep_climate_on_it_returns_to_holding_its_last_direction_heat(self) -> None:
        got = _go(
            23.0, _dry_after(ACTION_HEAT), unit_running=True, keep_on=True, humidity=HUMIDITY_NORMAL
        )
        assert (got.action, got.setpoint, got.send) == (ACTION_HEAT, HEAT_SP, True)
        assert got.refusal is None
        assert "leaving dry and holding heat at 22.5" in got.reason
        assert got.state.running == ACTION_HEAT
        assert got.state.last_run == ACTION_HEAT

    def test_with_keep_climate_on_it_returns_to_holding_its_last_direction_cool(self) -> None:
        got = _go(
            23.0, _dry_after(ACTION_COOL), unit_running=True, keep_on=True, humidity=HUMIDITY_NORMAL
        )
        assert (got.action, got.setpoint, got.send) == (ACTION_COOL, COOL_SP, True)

    def test_with_keep_climate_on_and_no_known_direction_it_is_left_alone(self) -> None:
        got = _go(
            23.0, _dry_after(None), unit_running=True, keep_on=True, humidity=HUMIDITY_NORMAL
        )
        assert (got.action, got.send) == (ACTION_OFF, False)

    def test_a_shadow_room_does_not_start_holding_a_direction(self) -> None:
        got = _go(
            23.0,
            _dry_after(ACTION_HEAT),
            unit_running=True,
            keep_on=True,
            humidity=HUMIDITY_NORMAL,
            commands_unit=False,
        )
        assert got.action == ACTION_OFF

    def test_a_refused_change_turns_dry_off_even_with_keep_climate_on(self) -> None:
        state = DecisionState(
            running=ACTION_DRY,
            last_run=ACTION_COOL,
            last_direction_change=NOW - timedelta(minutes=30),
        )
        got = _go(19.0, state, unit_running=True, keep_on=True, humidity=HUMIDITY_NORMAL)
        assert got.refusal is not None
        assert (got.action, got.send) == (ACTION_OFF, True)

    def test_fan_is_left_the_same_way(self) -> None:
        state = DecisionState(
            running=ACTION_FAN, last_run=ACTION_COOL, last_direction_change=NOW - timedelta(hours=9)
        )
        got = _go(23.4, state, unit_running=True, keep_on=True, outside_temp=15.0)
        assert (got.action, got.send) == (ACTION_COOL, True)
        assert got.setpoint == COOL_SP


class TestEmptyRoomReason:
    """A room under the band that is not heated yet must say why."""

    def test_below_band_but_under_the_widened_start_edge(self) -> None:
        got = _go(20.6, motion_idle=True)
        assert got.action == ACTION_OFF
        assert "below the comfort band" in got.reason
        assert "heating only starts under 20.0" in got.reason

    def test_over_the_trigger_but_under_the_widened_start_edge(self) -> None:
        got = _go(24.5, motion_idle=True)
        assert got.action == ACTION_OFF
        assert "over the cooling trigger" in got.reason
        assert "cooling only starts over 24.8" in got.reason

    def test_the_same_room_occupied_heats(self) -> None:
        assert _go(20.6, motion_idle=False).action == ACTION_HEAT
