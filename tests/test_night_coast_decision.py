# SPDX-License-Identifier: Apache-2.0
"""Night coast as a rung of the comfort ladder.

Tc is 23.0, so the numbers are round: band 21.0 - 24.5, cooling starts over 23.8,
comfort ceiling 24.5, heat starts under 21.0. The forecast minimum is 9.0 C, well
under the band; the room ceiling is the default 25.0.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climate_adapt.comfort import (
    HUMIDITY_HUMID,
    HUMIDITY_NORMAL,
    comfort_band,
)
from custom_components.climate_adapt.decision import (
    ACTION_COOL,
    ACTION_DRY,
    ACTION_FAN,
    ACTION_HEAT,
    ACTION_OFF,
    Decision,
    DecisionInputs,
    DecisionState,
    decide,
)
from custom_components.climate_adapt.night_coast import (
    SOURCE_FORECAST,
    SOURCE_NONE,
    SOURCE_OUTSIDE,
    CoastInputs,
    DuskSettings,
    forecast_stand_in,
    read_dusk,
)

NOW = datetime(2031, 10, 22, 22, 0, tzinfo=UTC)
TC = 23.0
LOW = 21.0
COAST = CoastInputs(
    night=True,
    forecast_min=9.0,
    forecast_hours=8,
    ceiling=25.0,
    margin=0.0,
)
COASTING_TEXT = "Coasting: night, forecast min 9.0 C < band 21.0, room {room} C < 25.0"
NO_FAN = (
    ". Coast fan step skipped, this unit offers no fan_only mode, "
    "there is no fan to run: off until the coast ends"
)


def _state(running: str | None, *, latched: bool = False) -> DecisionState:
    direction = running if running in (ACTION_HEAT, ACTION_COOL) else None
    return DecisionState(
        running=running,
        fan_since=NOW - timedelta(minutes=5) if running == ACTION_FAN else None,
        fan_start_temp=24.0 if running == ACTION_FAN else None,
        last_run=direction,
        last_direction_change=NOW - timedelta(hours=10) if direction else None,
        coast_overridden=latched,
    )


def _go(
    temp: float,
    running: str | None = None,
    *,
    coast: CoastInputs | None = COAST,
    latched: bool = False,
    state: DecisionState | None = None,
    **kw: object,
) -> Decision:
    kw.setdefault("unit_running", running is not None)
    kw.setdefault("fan_offered", False)
    kw.setdefault("humidity", HUMIDITY_NORMAL)
    inputs = DecisionInputs(
        now=NOW,
        room_temp=temp,
        band=comfort_band(TC, False),
        coast=coast,
        **kw,  # type: ignore[arg-type]
    )
    return decide(inputs, state or _state(running, latched=latched))


class TestCoasting:
    def test_a_warm_room_on_a_cold_night_is_not_cooled(self) -> None:
        plain = _go(24.0, coast=None)
        assert plain.action == ACTION_COOL
        coasting = _go(24.0)
        assert coasting.action == ACTION_OFF
        assert coasting.send is False
        assert coasting.reason == COASTING_TEXT.format(room="24.0")
        assert coasting.coast is not None
        assert coasting.coast.active is True

    def test_a_non_round_tc_without_a_fan_coasts_off(self) -> None:
        # A Tc that is not a round number, and a unit with no fan to run.
        band = comfort_band(21.7, False)
        inputs = DecisionInputs(
            now=NOW,
            room_temp=24.3,
            band=band,
            humidity=HUMIDITY_NORMAL,
            fan_offered=False,
            cooling_floor=None,
            coast=COAST,
        )
        decision = decide(inputs, _state(None))
        assert decision.action == ACTION_OFF
        assert decision.reason.startswith(
            f"Coasting: night, forecast min 9.0 C < band {band.low:.1f}"
        )

    def test_a_margin_is_named_in_the_reason(self) -> None:
        decision = _go(24.0, coast=replace(COAST, margin=1.0))
        assert decision.reason == (
            "Coasting: night, forecast min 9.0 C < band 21.0 - 1.0, room 24.0 C < 25.0"
        )

    def test_a_cooling_unit_is_stopped(self) -> None:
        decision = _go(24.0, ACTION_COOL)
        assert decision.action == ACTION_OFF
        assert decision.send is True
        assert decision.reason == COASTING_TEXT.format(room="24.0") + NO_FAN

    def test_a_cooling_unit_is_stopped_with_no_dwell_or_confirming_ticks_in_the_way(
        self,
    ) -> None:
        # Off is not a direction: the guard that holds a heat <-> cool flip is not asked.
        state = replace(
            _state(ACTION_COOL), last_direction_change=NOW - timedelta(minutes=1)
        )
        decision = _go(24.0, state=state)
        assert decision.action == ACTION_OFF
        assert decision.refusal is None

    def test_keep_climate_on_does_not_hold_a_cooling_unit_while_coasting(self) -> None:
        decision = _go(24.0, ACTION_COOL, keep_on=True)
        assert decision.action == ACTION_OFF
        assert decision.send is True
        assert decision.reason == (
            f"{COASTING_TEXT.format(room='24.0')}{NO_FAN}; "
            "Keep Climate On does not hold cooling while coasting: turning the unit off."
        )

    def test_keep_climate_on_still_turns_off_a_cooling_unit_below_tc(self) -> None:
        decision = _go(22.0, ACTION_COOL, keep_on=True)
        assert decision.action == ACTION_OFF
        assert decision.send is True
        assert "Keep Climate On would hold cool" in decision.reason

    def test_keep_climate_on_cannot_restart_cooling_while_coasting(self) -> None:
        decision = _go(
            24.0,
            ACTION_DRY,
            keep_on=True,
            state=replace(_state(ACTION_DRY), last_run=ACTION_COOL),
        )
        assert decision.action == ACTION_OFF

    @pytest.mark.parametrize("running", [ACTION_FAN, ACTION_DRY])
    def test_leaving_fan_or_dry_and_holding_heat_keeps_the_sentences_apart(
        self, running: str
    ) -> None:
        decision = _go(
            23.0,
            running,
            keep_on=True,
            state=replace(_state(running), last_run=ACTION_HEAT),
        )
        what = "the fan" if running == ACTION_FAN else "dry"
        assert decision.action == ACTION_HEAT
        assert decision.reason.startswith(COASTING_TEXT.format(room="23.0"))
        assert decision.reason.endswith(
            f"; leaving {what} and holding heat at {decision.setpoint:.1f} C."
        )

    def test_an_off_unit_stays_off_and_sends_nothing(self) -> None:
        decision = _go(24.0, None)
        assert (decision.action, decision.send) == (ACTION_OFF, False)

    def test_a_room_in_band_is_not_cooled_either(self) -> None:
        decision = _go(22.4)
        assert decision.action == ACTION_OFF
        assert decision.coast is not None
        assert decision.coast.active is True


class TestCeilings:
    @pytest.mark.parametrize("temp", [25.0, 25.1, 27.0])
    def test_the_room_at_or_over_its_ceiling_is_cooled_as_today(
        self, temp: float
    ) -> None:
        decision = _go(temp)
        assert decision.action == ACTION_COOL
        assert decision.coast is not None
        assert (decision.coast.active, decision.coast.overridden) == (False, True)
        assert (
            f"Coast skipped: ceiling reached {temp:.1f} C, cooling until the normal exit"
            in (decision.reason)
        )

    def test_just_under_the_room_ceiling_coasts(self) -> None:
        assert _go(24.9).action == ACTION_OFF

    def test_there_is_no_feels_like_ceiling(self) -> None:
        assert not hasattr(COAST, "feels_ceiling")
        assert not hasattr(_go(24.9).coast, "feels_ceiling")

    def test_the_ceiling_is_the_setting(self) -> None:
        assert _go(24.0, coast=replace(COAST, ceiling=23.5)).action == ACTION_COOL
        assert _go(24.0, coast=replace(COAST, ceiling=24.5)).action == ACTION_OFF


class TestWhenItDoesNotApply:
    def test_by_day(self) -> None:
        assert _go(24.0, coast=replace(COAST, night=False)).action == ACTION_COOL

    def test_a_forecast_at_the_band_edge_is_not_colder(self) -> None:
        assert _go(24.0, coast=replace(COAST, forecast_min=LOW)).action == ACTION_COOL
        assert (
            _go(24.0, coast=replace(COAST, forecast_min=LOW - 0.1)).action == ACTION_OFF
        )

    def test_the_margin_moves_the_edge(self) -> None:
        wide = replace(COAST, forecast_min=18.9, margin=2.0)
        assert _go(24.0, coast=wide).action == ACTION_OFF
        assert _go(24.0, coast=replace(wide, forecast_min=19.0)).action == ACTION_COOL

    def test_no_forecast(self) -> None:
        assert _go(24.0, coast=replace(COAST, forecast_min=None)).action == ACTION_COOL

    def test_switched_off_is_the_ladder_without_it(self) -> None:
        assert _go(24.0, coast=None).coast is None

    @pytest.mark.parametrize("humidity", [HUMIDITY_NORMAL, HUMIDITY_HUMID])
    @pytest.mark.parametrize(
        "running", [None, ACTION_COOL, ACTION_FAN, ACTION_DRY, ACTION_HEAT]
    )
    @pytest.mark.parametrize("temp", [19.0, 20.9, 22.0, 23.4, 23.9, 24.6, 26.0])
    @pytest.mark.parametrize("fan_offered", [True, False])
    @pytest.mark.parametrize("keep_on", [True, False])
    def test_a_coast_that_cannot_apply_changes_nothing(
        self,
        humidity: str,
        running: str | None,
        temp: float,
        fan_offered: bool,
        keep_on: bool,
    ) -> None:
        kw = {"humidity": humidity, "fan_offered": fan_offered, "keep_on": keep_on}
        today = _go(temp, running, coast=None, **kw)
        for off in (
            replace(COAST, night=False),
            replace(COAST, forecast_min=None),
            replace(COAST, forecast_min=LOW),
        ):
            other = _go(temp, running, coast=off, **kw)
            assert (
                other.action,
                other.setpoint,
                other.send,
                other.reason,
                other.state,
            ) == (
                today.action,
                today.setpoint,
                today.send,
                today.reason,
                replace(today.state, coast_overridden=False),
            )


class TestLadderNeighbours:
    def test_fan_first_is_still_allowed(self) -> None:
        decision = _go(24.0, fan_offered=True)
        assert decision.action == ACTION_FAN
        assert decision.fan_mode == "low"
        assert decision.reason == (
            COASTING_TEXT.format(room="24.0")
            + ". Coast fan step, 0 of 20 min: fan only, low speed, then off"
        )

    def test_a_failed_fan_does_not_escalate_to_cool_while_coasting(self) -> None:
        state = replace(_state(ACTION_FAN), fan_since=NOW - timedelta(minutes=60))
        today = _go(24.0, coast=None, fan_offered=True, state=state)
        assert today.action == ACTION_COOL
        decision = _go(24.0, fan_offered=True, state=state)
        assert decision.action == ACTION_FAN
        assert decision.state.fan_failed_at is None

    def test_a_room_at_the_ceiling_with_a_failed_fan_cools(self) -> None:
        state = replace(_state(ACTION_FAN), fan_since=NOW - timedelta(minutes=60))
        assert _go(25.2, fan_offered=True, state=state).action == ACTION_COOL

    def test_heat_is_untouched(self) -> None:
        for temp in (19.0, 20.9):
            decision = _go(temp)
            today = _go(temp, coast=None)
            assert decision.action == today.action == ACTION_HEAT
            assert decision.reason == today.reason
            assert decision.coast is not None
            assert decision.coast.active is False

    def test_a_latched_heat_run_is_untouched(self) -> None:
        decision = _go(21.5, ACTION_HEAT)
        assert decision.action == ACTION_HEAT

    def test_dry_mode_is_blocked_while_coasting(self) -> None:
        today = _go(23.4, coast=None, humidity=HUMIDITY_HUMID)
        assert today.action == ACTION_DRY
        decision = _go(23.4, humidity=HUMIDITY_HUMID)
        assert decision.action == ACTION_OFF
        assert decision.reason == COASTING_TEXT.format(room="23.4")

    def test_a_drying_unit_is_stopped(self) -> None:
        decision = _go(23.4, ACTION_DRY, humidity=HUMIDITY_HUMID)
        assert decision.action == ACTION_OFF
        assert decision.send is True


class TestLatch:
    def test_reaching_the_ceiling_sets_it(self) -> None:
        decision = _go(25.1)
        assert decision.state.coast_overridden is True

    def test_it_keeps_cooling_when_the_room_drops_back_under_the_ceiling(self) -> None:
        first = _go(25.1)
        assert first.action == ACTION_COOL
        second = _go(24.9, ACTION_COOL, state=first.state)
        assert second.action == ACTION_COOL
        assert second.coast is not None
        assert second.coast.overridden is True
        assert second.state.coast_overridden is True
        assert "Coast skipped" in second.reason

    def test_without_the_latch_the_same_tick_would_coast(self) -> None:
        assert _go(24.9, ACTION_COOL).action == ACTION_OFF

    def test_it_clears_at_the_normal_cool_exit(self) -> None:
        # Room 22.0 is under the cool setpoint: the ladder leaves cool by itself.
        decision = _go(22.0, ACTION_COOL, latched=True)
        assert decision.action == ACTION_OFF
        assert decision.state.coast_overridden is False

    def test_it_clears_when_heat_is_decided(self) -> None:
        decision = _go(19.0, None, latched=True)
        assert decision.action == ACTION_HEAT
        assert decision.state.coast_overridden is False

    def test_it_clears_when_day_arrives(self) -> None:
        decision = _go(
            24.9, ACTION_COOL, latched=True, coast=replace(COAST, night=False)
        )
        assert decision.action == ACTION_COOL
        assert decision.state.coast_overridden is False
        # The next night starts from a clean latch: a room under both ceilings coasts.
        again = _go(24.0, ACTION_COOL, state=decision.state)
        assert again.action == ACTION_OFF

    def test_it_clears_when_the_forecast_stops_being_colder(self) -> None:
        decision = _go(
            24.9, ACTION_COOL, latched=True, coast=replace(COAST, forecast_min=LOW)
        )
        assert decision.state.coast_overridden is False

    def test_it_clears_when_night_coast_is_switched_off(self) -> None:
        decision = _go(24.9, ACTION_COOL, latched=True, coast=None)
        assert decision.state.coast_overridden is False

    def test_it_is_not_set_when_the_ceiling_was_reached_but_nothing_cools(self) -> None:
        # Over the ceiling but the cooling floor holds the compressor off.
        decision = _go(25.1, cooling_floor=26.0)
        assert decision.action != ACTION_COOL
        assert decision.state.coast_overridden is False

    def test_a_room_hovering_at_the_ceiling_does_not_short_cycle(self) -> None:
        state = _state(None)
        seen = []
        for temp in (25.1, 24.9, 25.1, 24.9, 24.9):
            running = state.running
            decision = _go(temp, running, state=state)
            seen.append(decision.action)
            state = decision.state
        assert seen == [ACTION_COOL] * 5


DUSK_NOTE = "sun 7.0 deg, PV 0.6 kW, outside 3.0 K cooler"
DUSK = replace(COAST, night=False, dusk=True, dusk_note=DUSK_NOTE)
DUSK_TEXT = (
    f"Coasting: dusk ({DUSK_NOTE}), forecast min 9.0 C < band 21.0, room {{room}} C < 25.0"
)
OUTSIDE = replace(COAST, forecast_min=17.8, forecast_source=SOURCE_OUTSIDE)


class TestDusk:
    def test_a_warm_room_at_dusk_is_not_cooled_and_the_reason_says_why(self) -> None:
        decision = _go(23.4, coast=DUSK)
        assert decision.action == ACTION_OFF
        assert decision.send is False
        assert decision.reason == DUSK_TEXT.format(room="23.4")
        assert decision.coast is not None
        assert (decision.coast.active, decision.coast.dusk) == (True, True)
        assert decision.coast.night is False

    def test_the_same_room_by_day_without_dusk_is_cooled(self) -> None:
        assert _go(24.0, coast=replace(DUSK, dusk=False)).action == ACTION_COOL

    def test_the_reason_without_a_note_still_says_dusk(self) -> None:
        decision = _go(24.0, coast=replace(DUSK, dusk_note=None))
        assert decision.reason == (
            "Coasting: dusk, forecast min 9.0 C < band 21.0, room 24.0 C < 25.0"
        )

    def test_dusk_needs_a_forecast_colder_than_the_band(self) -> None:
        assert _go(24.0, coast=replace(DUSK, forecast_min=LOW)).action == ACTION_COOL
        assert _go(24.0, coast=replace(DUSK, forecast_min=None)).action == ACTION_COOL

    def test_the_night_text_is_unchanged(self) -> None:
        assert _go(24.0).reason == COASTING_TEXT.format(room="24.0")

    @pytest.mark.parametrize("temp", [25.0, 25.1])
    def test_the_ceiling_hands_over_to_the_normal_ladder_at_dusk(
        self, temp: float
    ) -> None:
        decision = _go(temp, coast=DUSK)
        assert decision.action == ACTION_COOL
        assert decision.state.coast_overridden is True
        assert "Coast skipped" in decision.reason

    def test_the_latch_holds_the_cooling_until_the_unit_leaves_cool(self) -> None:
        first = _go(25.1, coast=DUSK)
        second = _go(24.0, ACTION_COOL, coast=DUSK, state=first.state)
        assert second.action == ACTION_COOL
        assert second.state.coast_overridden is True

    def test_heat_is_untouched_at_dusk(self) -> None:
        decision = _go(19.0, coast=DUSK)
        assert decision.action == ACTION_HEAT
        assert decision.reason == _go(19.0, coast=None).reason

    def test_the_fan_may_run_at_dusk(self) -> None:
        decision = _go(24.0, coast=DUSK, fan_offered=True)
        assert decision.action == ACTION_FAN
        assert decision.fan_mode == "low"
        assert decision.reason == (
            DUSK_TEXT.format(room="24.0")
            + ". Coast fan step, 0 of 20 min: fan only, low speed, then off"
        )

    @pytest.mark.parametrize("humidity", [HUMIDITY_NORMAL, HUMIDITY_HUMID])
    @pytest.mark.parametrize(
        "running", [None, ACTION_COOL, ACTION_FAN, ACTION_DRY, ACTION_HEAT]
    )
    @pytest.mark.parametrize("temp", [19.0, 20.9, 22.0, 23.4, 23.9, 24.6, 26.0])
    @pytest.mark.parametrize("fan_offered", [True, False])
    @pytest.mark.parametrize("keep_on", [True, False])
    def test_without_dusk_the_by_day_ladder_is_exactly_today(
        self,
        humidity: str,
        running: str | None,
        temp: float,
        fan_offered: bool,
        keep_on: bool,
    ) -> None:
        kw = {"humidity": humidity, "fan_offered": fan_offered, "keep_on": keep_on}
        today = _go(temp, running, coast=None, **kw)
        by_day = _go(temp, running, coast=replace(DUSK, dusk=False, dusk_note=None), **kw)
        assert (
            by_day.action,
            by_day.setpoint,
            by_day.send,
            by_day.reason,
            by_day.state,
        ) == (today.action, today.setpoint, today.send, today.reason, today.state)


class TestForecastFallback:
    def test_the_outside_temperature_stands_in_and_the_reason_says_so(self) -> None:
        decision = _go(24.0, coast=OUTSIDE)
        assert decision.action == ACTION_OFF
        assert decision.reason == (
            "Coasting: night, outside 17.8 C (no forecast) < band 21.0, room 24.0 C < 25.0"
        )
        assert decision.coast is not None
        assert decision.coast.forecast_source == SOURCE_OUTSIDE

    def test_at_dusk_the_reason_names_both(self) -> None:
        decision = _go(23.4, coast=replace(OUTSIDE, night=False, dusk=True, dusk_note=DUSK_NOTE))
        assert decision.reason == (
            f"Coasting: dusk ({DUSK_NOTE}), outside 17.8 C (no forecast) < band 21.0, "
            "room 23.4 C < 25.0"
        )

    def test_an_outside_reading_not_under_the_band_cools(self) -> None:
        assert _go(24.0, coast=replace(OUTSIDE, forecast_min=21.0)).action == ACTION_COOL

    def test_the_source_defaults_to_the_forecast(self) -> None:
        decision = _go(24.0)
        assert decision.coast is not None
        assert decision.coast.forecast_source == SOURCE_FORECAST

    @pytest.mark.parametrize(
        ("forecast", "outside", "room", "gap", "want"),
        [
            (9.0, 30.0, 24.0, 2.0, (9.0, SOURCE_FORECAST)),
            (None, 17.8, 24.0, 2.0, (17.8, SOURCE_OUTSIDE)),
            (None, 22.0, 24.0, 2.0, (22.0, SOURCE_OUTSIDE)),
            (None, 22.1, 24.0, 2.0, (None, SOURCE_NONE)),
            (None, 24.0, 24.0, 0.0, (24.0, SOURCE_OUTSIDE)),
            (None, 24.1, 24.0, 0.0, (None, SOURCE_NONE)),
            (None, None, 24.0, 2.0, (None, SOURCE_NONE)),
        ],
    )
    def test_the_stand_in(self, forecast, outside, room, gap, want) -> None:
        assert forecast_stand_in(forecast, outside, room, gap) == want


class TestKeepClimateOnAndCoasting:
    @pytest.mark.parametrize("running", [ACTION_COOL, ACTION_DRY, ACTION_FAN])
    @pytest.mark.parametrize("temp", [23.4, 24.0, 24.9])
    @pytest.mark.parametrize("coast", [COAST, DUSK, OUTSIDE], ids=["night", "dusk", "fallback"])
    def test_a_unit_in_cool_dry_or_fan_is_turned_off_and_it_is_sent(
        self, running: str, temp: float, coast: CoastInputs
    ) -> None:
        decision = _go(temp, running, coast=coast, keep_on=True)
        assert decision.action == ACTION_OFF
        assert decision.send is True
        assert decision.reason.endswith(
            "; Keep Climate On does not hold cooling while coasting: turning the unit off."
        )
        assert decision.refusal is None

    def test_it_is_sent_again_on_every_tick_while_the_unit_runs(self) -> None:
        state = _state(ACTION_COOL)
        for _ in range(3):
            decision = _go(24.0, ACTION_COOL, keep_on=True, state=state)
            assert (decision.action, decision.send) == (ACTION_OFF, True)
            state = replace(decision.state, running=ACTION_COOL)

    def test_an_off_unit_is_sent_nothing(self) -> None:
        decision = _go(24.0, None, keep_on=True)
        assert (decision.action, decision.send) == (ACTION_OFF, False)
        assert "Keep Climate On" not in decision.reason

    def test_a_shadow_ladder_sends_nothing(self) -> None:
        decision = _go(24.0, ACTION_COOL, keep_on=True, commands_unit=False)
        assert decision.send is False
        assert "Keep Climate On does not hold" not in decision.reason

    def test_without_keep_climate_on_the_stop_is_the_old_one(self) -> None:
        decision = _go(24.0, ACTION_COOL, keep_on=False)
        assert decision.send is True
        assert decision.reason == COASTING_TEXT.format(room="24.0") + NO_FAN

    def test_heat_is_never_waived(self) -> None:
        for temp in (19.0, 20.9):
            decision = _go(temp, ACTION_HEAT, keep_on=True)
            assert decision.action == ACTION_HEAT
            assert "does not hold cooling" not in decision.reason

    def test_a_heat_run_in_a_room_under_tc_is_still_held(self) -> None:
        decision = _go(22.0, ACTION_HEAT, keep_on=True)
        assert decision.action == ACTION_HEAT
        assert "does not hold cooling" not in decision.reason

    def test_a_unit_last_cooling_in_dry_is_turned_off_not_held_in_cool(self) -> None:
        decision = _go(
            24.0,
            ACTION_DRY,
            keep_on=True,
            state=replace(_state(ACTION_DRY), last_run=ACTION_COOL),
        )
        assert (decision.action, decision.send) == (ACTION_OFF, True)

    def test_the_fan_rung_asking_for_fan_wins_over_the_stop(self) -> None:
        decision = _go(24.0, ACTION_FAN, keep_on=True, fan_offered=True)
        assert decision.action == ACTION_FAN
        assert "does not hold cooling" not in decision.reason

    def test_the_ceiling_hands_over_and_the_unit_keeps_cooling(self) -> None:
        decision = _go(25.1, ACTION_COOL, keep_on=True)
        assert decision.action == ACTION_COOL
        assert "does not hold cooling" not in decision.reason

    def test_with_no_coast_the_normal_ladder_keeps_cooling(self) -> None:
        decision = _go(24.0, ACTION_COOL, keep_on=True, coast=replace(COAST, night=False))
        assert decision.action == ACTION_COOL
        assert "does not hold cooling" not in decision.reason


def _sky(**changes):
    values = {
        "sun_state": "above_horizon",
        "rising": False,
        "elevation": 8.0,
        "pv_kw": 0.4,
        "room": 24.0,
        "outside": 19.0,
        "was_active": False,
    }
    values.update(changes)
    return values


SETTINGS = DuskSettings(enabled=True, elevation=10.0, pv_kw=1.0, gap=2.0)


class TestReadDusk:
    def test_a_low_sun_with_weak_pv_and_a_cooler_outside_holds(self) -> None:
        reading = read_dusk(SETTINGS, **_sky())
        assert reading.holds is True
        assert reading.note == "sun 8.0 deg, PV 0.4 kW, outside 5.0 K cooler"

    def test_an_evening_with_a_low_sun_and_weak_pv_holds(self) -> None:
        reading = read_dusk(
            SETTINGS, **_sky(elevation=7.5, pv_kw=0.7, room=22.8, outside=17.4)
        )
        assert reading.holds is True
        assert reading.note == "sun 7.5 deg, PV 0.7 kW, outside 5.4 K cooler"

    @pytest.mark.parametrize(
        "changes",
        [
            {"sun_state": "below_horizon"},
            {"sun_state": "unavailable"},
            {"sun_state": None},
            {"rising": True},
            {"rising": None},
            {"rising": "false"},
            {"elevation": 12.0, "pv_kw": 1.2},
            {"elevation": 10.0, "pv_kw": 1.0},
            {"elevation": None, "pv_kw": None},
            {"elevation": None, "pv_kw": 1.5},
            {"elevation": 12.0, "pv_kw": None},
            {"outside": 22.1},
            {"outside": None},
        ],
    )
    def test_each_condition_is_needed(self, changes: dict) -> None:
        assert read_dusk(SETTINGS, **_sky(**changes)).holds is False

    def test_the_switch_off_is_never_dusk(self) -> None:
        off = DuskSettings(enabled=False, elevation=10.0, pv_kw=1.0, gap=2.0)
        assert read_dusk(off, **_sky()).holds is False
        assert read_dusk(off, **_sky(was_active=True)).holds is False

    @pytest.mark.parametrize(
        "changes",
        [{"elevation": None}, {"pv_kw": None}],
        ids=["no elevation", "no PV"],
    )
    def test_one_unreadable_half_counts_as_false_and_the_other_decides(
        self, changes: dict
    ) -> None:
        assert read_dusk(SETTINGS, **_sky(**changes)).holds is True

    def test_the_gap_is_at_most_the_room_less_z(self) -> None:
        assert read_dusk(SETTINGS, **_sky(outside=22.0)).holds is True
        assert read_dusk(SETTINGS, **_sky(outside=22.1)).holds is False

    def test_a_midday_cloud_with_a_high_sun_enters(self) -> None:
        cloud = _sky(elevation=45.0, pv_kw=0.8, outside=18.5)
        assert read_dusk(SETTINGS, **cloud).holds is True

    def test_the_cloud_passing_exits_once_pv_is_over_y_plus_half_a_kilowatt(self) -> None:
        sunny = _sky(elevation=45.0, outside=18.5, was_active=True)
        assert read_dusk(SETTINGS, **{**sunny, "pv_kw": 1.4}).holds is True
        assert read_dusk(SETTINGS, **{**sunny, "pv_kw": 1.5}).holds is False
        assert read_dusk(SETTINGS, **{**sunny, "pv_kw": 1.6}).holds is False

    def test_pv_oscillating_around_y_does_not_toggle(self) -> None:
        active = False
        seen = []
        for pv in (0.9, 1.1, 0.95, 1.1, 0.9):
            active = read_dusk(
                SETTINGS, **_sky(elevation=45.0, pv_kw=pv, was_active=active)
            ).holds
            seen.append(active)
        assert seen == [True] * 5

    def test_without_the_memory_the_same_pv_does_not_enter(self) -> None:
        assert read_dusk(SETTINGS, **_sky(elevation=45.0, pv_kw=1.1)).holds is False

    def test_a_clear_memory_starts_at_the_entry_thresholds(self) -> None:
        assert read_dusk(SETTINGS, **_sky(elevation=12.0, pv_kw=None)).holds is False

    def test_the_sun_staying_a_little_above_x_keeps_it(self) -> None:
        sky = _sky(pv_kw=2.0, was_active=True)
        assert read_dusk(SETTINGS, **{**sky, "elevation": 12.9}).holds is True
        assert read_dusk(SETTINGS, **{**sky, "elevation": 13.0}).holds is False

    def test_the_gap_may_shrink_by_a_kelvin_while_it_holds(self) -> None:
        sky = _sky(was_active=True)
        assert read_dusk(SETTINGS, **{**sky, "outside": 23.0}).holds is True
        assert read_dusk(SETTINGS, **{**sky, "outside": 23.1}).holds is False

    def test_a_zero_gap_never_needs_less_than_zero(self) -> None:
        zero = DuskSettings(enabled=True, elevation=10.0, pv_kw=1.0, gap=0.5)
        sky = _sky(was_active=True, outside=24.0)
        assert read_dusk(zero, **sky).holds is True
        assert read_dusk(zero, **{**sky, "outside": 24.1}).holds is False

    @pytest.mark.parametrize(
        "changes",
        [{"rising": True}, {"rising": None}, {"sun_state": "below_horizon"}],
    )
    def test_the_sun_no_longer_setting_leaves_at_once(self, changes: dict) -> None:
        assert read_dusk(SETTINGS, **_sky(was_active=True, **changes)).holds is False
