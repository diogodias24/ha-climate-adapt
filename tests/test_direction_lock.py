# SPDX-License-Identifier: Apache-2.0
"""The outside sample ring and the direction bookkeeping around it.

Three layers, bottom up:

- the sample ring (``OutsideTrend``): the screen every outside reading passes
  (stale, reset, spike, out of order) before it reaches the daily means,
  its bound, and its persistence;
- the coordinator's direction bookkeeping: the off/dry/fan/unknown branch
  obeying the dwell and the confirming ticks, what is remembered about the
  last direction run, and how the ring is fed;
- restart survival of the ring and of the last direction.

Which change of direction the ladder allows is covered in ``test_decision``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.const import (
    HVAC_MODE_COOL,
    HVAC_MODE_HEAT,
    HVAC_MODE_HEAT_COOL,
    MIN_COOLING_PERCEIVED_TEMP,
    MODE_CONFIRM_TICKS,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.decision import DIRECTION_DWELL_MINUTES
from custom_components.climate_adapt.direction_lock import (
    REJECT_OLD,
    REJECT_RESET,
    REJECT_SPIKE,
    REJECT_STALE,
    OutsideTrend,
)

from .comfort_helpers import FULL_UNIT, decide, prepare_live, register_climate

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

T0 = datetime(2031, 10, 27, 12, 0, tzinfo=UTC)
CLIMATE_ENTITY = "climate.office_ac"
OUTSIDE_TEMP = "sensor.outside_temperature"
OUTSIDE_HUMIDITY = "sensor.outside_humidity"


def _minutes(n: float) -> timedelta:
    return timedelta(minutes=n)


def _filled(value: float, *, hours: float = 6.0, every: int = 30) -> OutsideTrend:
    """A ring holding `value` reported every `every` min for `hours`, ending T0."""
    trend = OutsideTrend()
    steps = int(hours * 60 // every)
    for i in range(steps, -1, -1):
        stamp = T0 - _minutes(i * every)
        assert trend.add(stamp, value, 50.0, stamp) is None
    return trend


# --------------------------------------------------------------------------
# Sensor sanity
# --------------------------------------------------------------------------

class TestSensorSanity:
    def test_a_good_reading_is_kept(self) -> None:
        trend = OutsideTrend()
        assert trend.add(T0, 17.0, 60.0, T0) is None
        assert trend.samples == [(T0, 17.0)]

    def test_a_stale_report_is_not_a_sample(self) -> None:
        trend = OutsideTrend()
        assert trend.add(T0 - _minutes(91), 17.0, 60.0, T0) == REJECT_STALE
        assert trend.samples == []
        assert trend.last_rejection == REJECT_STALE

    def test_a_report_just_inside_ninety_minutes_is_a_sample(self) -> None:
        trend = OutsideTrend()
        assert trend.add(T0 - _minutes(90), 17.0, 60.0, T0) is None

    def test_zero_with_zero_humidity_is_a_reset_not_a_reading(self) -> None:
        trend = OutsideTrend()
        assert trend.add(T0, 0.0, 0.0, T0) == REJECT_RESET
        assert trend.samples == []

    def test_zero_with_real_humidity_is_a_genuine_zero(self) -> None:
        trend = OutsideTrend()
        assert trend.add(T0, 0.0, 80.0, T0) is None

    def test_zero_with_unknown_humidity_is_not_called_a_reset(self) -> None:
        trend = OutsideTrend()
        assert trend.add(T0, 0.0, None, T0) is None

    def test_a_jump_of_more_than_six_inside_fifteen_minutes_is_a_spike(self) -> None:
        trend = OutsideTrend()
        trend.add(T0, 15.0, 60.0, T0)
        at = T0 + _minutes(5)
        assert trend.add(at, 28.3, 60.0, at) == REJECT_SPIKE
        assert trend.samples == [(T0, 15.0)]
        assert trend.last_rejection == REJECT_SPIKE

    def test_exactly_six_degrees_is_not_a_spike(self) -> None:
        trend = OutsideTrend()
        trend.add(T0, 15.0, 60.0, T0)
        at = T0 + _minutes(5)
        assert trend.add(at, 21.0, 60.0, at) is None

    def test_the_same_jump_after_fifteen_minutes_is_weather(self) -> None:
        trend = OutsideTrend()
        trend.add(T0, 15.0, 60.0, T0)
        later = T0 + _minutes(15)
        assert trend.add(later, 28.3, 60.0, later) is None

    def test_the_same_report_offered_twice_is_one_sample(self) -> None:
        trend = OutsideTrend()
        trend.add(T0, 15.0, 60.0, T0)
        assert trend.add(T0, 15.0, 60.0, T0) == REJECT_OLD
        assert len(trend.samples) == 1

    def test_the_ring_is_bounded(self) -> None:
        trend = OutsideTrend()
        for i in range(2000):
            stamp = T0 + timedelta(seconds=20 * i)
            trend.add(stamp, 15.0, 50.0, stamp)
        assert len(trend.samples) <= 400

    def test_samples_older_than_the_window_are_pruned_and_the_newest_is_kept(self) -> None:
        trend = _filled(14.0, hours=12.0)
        # 6 h window plus the 90 min a sample is held.
        assert trend.samples[0][0] >= T0 - _minutes(7.5 * 60)
        assert trend.samples[-1][0] == T0
        assert len(trend.samples) < 25

    def test_a_ring_never_prunes_itself_empty(self) -> None:
        trend = OutsideTrend()
        trend.add(T0, 14.0, 50.0, T0)
        trend._prune(T0 + timedelta(days=3))
        assert trend.samples == [(T0, 14.0)]


class TestTrendPersistence:
    def test_the_ring_round_trips(self) -> None:
        trend = _filled(14.0)
        restored = OutsideTrend()
        restored.load(trend.to_store())

        assert restored.samples == trend.samples
        assert restored.to_store() == trend.to_store()

    def test_the_stored_form_is_plain_json(self) -> None:
        json.dumps(_filled(14.0).to_store())

    @pytest.mark.parametrize(
        "stored",
        [
            {},
            {"outside_samples": None},
            {
                "outside_samples": [
                    ["not a date", 1],
                    [None, 2],
                    ["2031-10-27T12:00:00"],
                ]
            },
            {"outside_samples": [["2031-10-27T12:00:00+00:00", "warm"]]},
        ],
    )
    def test_a_damaged_store_is_dropped_not_raised(self, stored: dict) -> None:
        trend = OutsideTrend()
        trend.load(stored)
        assert trend.samples == []

    def test_unsorted_stored_samples_are_sorted(self) -> None:
        trend = OutsideTrend()
        trend.load(
            {
                "outside_samples": [
                    ["2031-10-27T12:00:00+00:00", 15.0],
                    ["2031-10-27T11:00:00+00:00", 14.0],
                ]
            }
        )
        assert [v for _, v in trend.samples] == [14.0, 15.0]

    def test_a_naive_stored_time_is_read_as_utc(self) -> None:
        trend = OutsideTrend()
        trend.load({"outside_samples": [["2031-10-27T12:00:00", 15.0]]})
        assert trend.samples == [(T0, 15.0)]


# --------------------------------------------------------------------------
# The coordinator
# --------------------------------------------------------------------------



@pytest.fixture
def coordinator(hass: HomeAssistant, mock_config_entry_with_outside):
    mock_config_entry_with_outside.add_to_hass(hass)
    coord = ClimateAdaptCoordinator(hass, mock_config_entry_with_outside)
    coord.mode = "auto"
    coord.hvac_mode = HVAC_MODE_HEAT_COOL
    coord.keep_climate_on = True
    coord.dry_mode_enabled = False
    coord.humidity = 50.0
    coord.target_temp = 22.5
    coord.last_direction_change = None
    return coord


def _just_now(stamp: datetime | None) -> bool:
    return stamp is not None and abs(dt_util.utcnow() - stamp) < timedelta(seconds=5)


def _register(hass: HomeAssistant) -> list:
    calls: list = []
    register_climate(hass, calls)
    return calls


def _room(coord: ClimateAdaptCoordinator, perceived: float, *, keep_on: bool = True) -> None:
    """Hybrid pins Tc to 24.0: band [22.0, 25.5], cool sends 24.5, heat sends 23.5."""
    prepare_live(coord, target=24.0, room=perceived, keep_on=keep_on)


def _outside(coord: ClimateAdaptCoordinator, mean: float) -> None:
    """Give the room a ring of 13 samples of `mean`, one every 30 minutes up to now."""
    trend = OutsideTrend()
    now = dt_util.utcnow()
    for i in range(12, -1, -1):
        stamp = now - _minutes(i * 30)
        trend.add(stamp, mean, 50.0, stamp)
    coord._outside_trend = trend


async def _decide(hass: HomeAssistant, coord: ClimateAdaptCoordinator, unit: str) -> None:
    coord.last_adaptation = None
    await decide(hass, coord, unit)


def _cool_calls(calls: list) -> list:
    return [c for c in calls if c.data.get("hvac_mode") == HVAC_MODE_COOL]


# The ladder's guard decides a change of direction (see
# test_decision.TestDirectionGuard and
# test_comfort_wiring.TestDirectionGuard). What is checked below is the
# bookkeeping around it: the dwell, the confirming ticks and what is remembered.


class TestTheGuardAppliesWhateverTheUnitIsDoing:
    """A change from the last direction run is judged against that direction."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("unit", ["off", "dry", "fan_only", "unknown"])
    async def test_it_refuses_inside_the_dwell_whatever_state_the_unit_is_in(
        self, hass: HomeAssistant, coordinator, unit: str
    ) -> None:
        calls = _register(hass)
        coordinator.last_run_direction = HVAC_MODE_HEAT
        coordinator.last_direction_change = dt_util.utcnow() - _minutes(5)
        # Tc 24.0: cooling starts over 24.8 and the dwell bypass is over 26.3, so
        # 25.5 is hot enough to ask for cool but not far enough beyond to skip the
        # dwell. (27.0 is past the bypass.)
        _room(coordinator, 25.5)

        await _decide(hass, coordinator, unit)

        assert _cool_calls(calls) == []
        refusal = coordinator.last_direction_refusal
        assert refusal["from"] == HVAC_MODE_HEAT
        assert refusal["wanted"] == HVAC_MODE_COOL
        assert "only 5 of 120 minutes have passed (115 to go)" in refusal["reason"]
        assert coordinator.last_run_direction == HVAC_MODE_HEAT

    @pytest.mark.asyncio
    async def test_it_goes_through_once_the_dwell_has_run_out(
        self, hass: HomeAssistant, coordinator, freezer
    ) -> None:
        calls = _register(hass)
        coordinator.last_run_direction = HVAC_MODE_HEAT
        start = dt_util.utcnow()
        coordinator.last_direction_change = start
        # Over the cooling start (24.8), under the dwell bypass (26.3).
        _room(coordinator, 25.5)

        await _decide(hass, coordinator, "off")
        assert _cool_calls(calls) == []

        freezer.move_to(start + _minutes(DIRECTION_DWELL_MINUTES + 1))
        _room(coordinator, 25.5)
        await _decide(hass, coordinator, "off")

        assert len(_cool_calls(calls)) == 1
        assert coordinator.last_run_direction == HVAC_MODE_COOL
        assert _just_now(coordinator.last_direction_change)

    @pytest.mark.asyncio
    async def test_a_running_heat_unit_is_moved_to_the_fan_not_reversed_inside_the_dwell(
        self, hass: HomeAssistant, coordinator
    ) -> None:
        """A refused cool falls back to the fan, never on running heat."""
        calls = _register(hass)
        coordinator.last_run_direction = HVAC_MODE_HEAT
        coordinator.last_direction_change = dt_util.utcnow() - _minutes(10)
        # At the ceiling (25.5) so cool is wanted, under the dwell bypass (26.3).
        _room(coordinator, 25.5, keep_on=False)

        await _decide(hass, coordinator, HVAC_MODE_HEAT)

        assert _cool_calls(calls) == []
        assert [c.service for c in calls] == ["set_hvac_mode", "set_fan_mode"]
        assert calls[0].data["hvac_mode"] == "fan_only"
        assert "refused" in coordinator.last_flip_refusal
        assert "only 10 of 120 minutes have passed" in coordinator.last_flip_refusal
        assert coordinator.last_direction_refusal["path"] == "ladder"
        assert coordinator.last_run_direction == HVAC_MODE_HEAT

    @pytest.mark.asyncio
    async def test_a_running_heat_unit_is_turned_off_not_reversed_when_there_is_no_fan(
        self, hass: HomeAssistant, coordinator
    ) -> None:
        """With no fan_only mode on the unit a refused cool still ends in off."""
        calls = _register(hass)
        coordinator.last_run_direction = HVAC_MODE_HEAT
        coordinator.last_direction_change = dt_util.utcnow() - _minutes(10)
        _room(coordinator, 25.5, keep_on=False)
        no_fan = {**FULL_UNIT, "hvac_modes": ["off", "heat", "cool", "dry", "heat_cool"]}

        coordinator.last_adaptation = None
        await decide(hass, coordinator, HVAC_MODE_HEAT, no_fan)

        assert [c.service for c in calls] == ["turn_off"]
        assert "refused" in coordinator.last_flip_refusal
        assert coordinator.last_direction_refusal["path"] == "ladder"

    @pytest.mark.asyncio
    async def test_a_running_heat_unit_reverses_once_dwell_and_confirmation_are_met(
        self, hass: HomeAssistant, coordinator
    ) -> None:
        calls = _register(hass)
        coordinator.last_run_direction = HVAC_MODE_HEAT
        coordinator.last_direction_change = dt_util.utcnow() - _minutes(600)
        coordinator._ladder_state = replace(
            coordinator._ladder_state, pending=HVAC_MODE_COOL, pending_ticks=MODE_CONFIRM_TICKS - 1
        )
        _room(coordinator, 27.0)

        await _decide(hass, coordinator, HVAC_MODE_HEAT)

        assert [c.data["hvac_mode"] for c in calls] == [HVAC_MODE_COOL]
        assert coordinator.last_run_direction == HVAC_MODE_COOL
        assert coordinator.last_direction_refusal is None
        assert coordinator.last_flip_refusal is None

    @pytest.mark.asyncio
    async def test_an_unknown_clock_is_started_when_a_change_is_asked_for(
        self, hass: HomeAssistant, coordinator
    ) -> None:
        calls = _register(hass)
        coordinator.last_run_direction = HVAC_MODE_HEAT
        coordinator.last_direction_change = None
        _room(coordinator, 25.5)  # over the cooling start (24.8), under the bypass (26.3)

        await _decide(hass, coordinator, "off")

        assert _just_now(coordinator.last_direction_change)
        assert _cool_calls(calls) == []
        assert "minutes have passed" in coordinator.last_direction_refusal["reason"]

    @pytest.mark.asyncio
    async def test_a_unit_asking_for_no_change_is_not_held(
        self, hass: HomeAssistant, coordinator
    ) -> None:
        calls = _register(hass)
        coordinator.last_run_direction = HVAC_MODE_COOL
        coordinator.last_direction_change = None
        _room(coordinator, 27.0)

        await _decide(hass, coordinator, "off")

        assert len(_cool_calls(calls)) == 1
        assert coordinator.last_direction_refusal is None

    @pytest.mark.asyncio
    async def test_an_extreme_cold_bypasses_the_dwell_for_a_stopped_cooling_unit(
        self, hass: HomeAssistant, coordinator
    ) -> None:
        calls = _register(hass)
        coordinator.last_run_direction = HVAC_MODE_COOL
        coordinator.last_direction_change = dt_util.utcnow() - _minutes(10)
        _room(coordinator, 18.5)  # 3.5 K under the bottom of the band

        await _decide(hass, coordinator, "off")
        assert calls == []
        assert "minutes have passed" not in coordinator.last_flip_refusal
        assert "1 of 2 confirming ticks" in coordinator.last_flip_refusal
        await _decide(hass, coordinator, "off")

        assert [c.data["hvac_mode"] for c in calls] == [HVAC_MODE_HEAT]
        assert coordinator.last_run_direction == HVAC_MODE_HEAT
        assert coordinator.last_flip_refusal is None

    @pytest.mark.asyncio
    async def test_a_unit_that_never_ran_anything_is_not_held(
        self, hass: HomeAssistant, coordinator
    ) -> None:
        """First start ever: nothing to change from, so nothing to refuse."""
        calls = _register(hass)
        coordinator.last_run_direction = None
        coordinator._last_run_seed = None
        _room(coordinator, 27.0)

        await _decide(hass, coordinator, "off")

        assert len(_cool_calls(calls)) == 1
        assert coordinator.last_direction_refusal is None

    @pytest.mark.asyncio
    async def test_a_refusal_sends_nothing_and_does_not_oscillate(
        self, hass: HomeAssistant, coordinator, freezer
    ) -> None:
        """A refusal never starts cool or heat; it holds the fan and no more."""
        calls = _register(hass)
        coordinator.last_run_direction = HVAC_MODE_HEAT
        coordinator.last_direction_change = dt_util.utcnow() - _minutes(10)
        _room(coordinator, 25.5)  # at the ceiling (25.5), under the bypass (26.3)

        for _ in range(4):
            await _decide(hass, coordinator, "off")
            freezer.tick(_minutes(15))

        assert calls
        assert _cool_calls(calls) == []
        assert {c.data.get("hvac_mode") for c in calls if c.service == "set_hvac_mode"} == {
            "fan_only"
        }
        assert not [c for c in calls if c.service in ("set_temperature", "turn_off")]
        assert coordinator.last_run_direction == HVAC_MODE_HEAT

    @pytest.mark.asyncio
    async def test_a_hold_stamps_the_adaptation_so_a_push_does_not_re_decide(
        self, hass: HomeAssistant, coordinator
    ) -> None:
        _register(hass)
        coordinator.last_run_direction = HVAC_MODE_HEAT
        coordinator.last_direction_change = dt_util.utcnow() - _minutes(10)
        _room(coordinator, 25.5)  # over the cooling start (24.8), under the bypass (26.3)

        await _decide(hass, coordinator, "off")

        assert coordinator.last_direction_refusal is not None
        assert _just_now(coordinator.last_adaptation)

    @pytest.mark.asyncio
    async def test_the_floor_still_withholds_cooling_after_the_dwell(
        self, hass: HomeAssistant, coordinator
    ) -> None:
        calls = _register(hass)
        coordinator.last_run_direction = HVAC_MODE_HEAT
        coordinator.last_direction_change = dt_util.utcnow() - _minutes(600)
        prepare_live(
            coordinator, target=20.0, room=MIN_COOLING_PERCEIVED_TEMP - 0.5, keep_on=True
        )

        await _decide(hass, coordinator, "off")

        assert coordinator.cooling_withheld is True
        assert _cool_calls(calls) == []
        assert coordinator.last_direction_refusal is None


class TestWhatTheCoordinatorRemembers:
    @pytest.mark.asyncio
    async def test_a_command_records_the_direction_and_starts_the_dwell(
        self, hass: HomeAssistant, coordinator
    ) -> None:
        _register(hass)
        coordinator.last_run_direction = HVAC_MODE_HEAT
        coordinator.last_direction_change = dt_util.utcnow() - _minutes(600)
        _room(coordinator, 26.5)
        assert not _just_now(coordinator.last_direction_change)

        # A unit that is off needs no confirming tick for cool, so the
        # first decision past the dwell sends it and records the new direction.
        await _decide(hass, coordinator, "off")

        assert coordinator.last_run_direction == HVAC_MODE_COOL
        assert _just_now(coordinator.last_direction_change)

    @pytest.mark.asyncio
    async def test_a_unit_seen_running_is_remembered(
        self, hass: HomeAssistant, coordinator
    ) -> None:
        _register(hass)
        assert coordinator.last_run_direction is None
        _room(coordinator, 24.0)

        await _decide(hass, coordinator, HVAC_MODE_COOL)

        assert coordinator.last_run_direction == HVAC_MODE_COOL

    @pytest.mark.asyncio
    async def test_off_dry_and_fan_do_not_overwrite_the_last_direction(
        self, hass: HomeAssistant, coordinator
    ) -> None:
        _register(hass)
        coordinator.last_run_direction = HVAC_MODE_HEAT
        coordinator.last_direction_change = dt_util.utcnow() - _minutes(600)
        _room(coordinator, 24.0)

        for unit in ("off", "dry", "fan_only", "unknown"):
            await _decide(hass, coordinator, unit)
            assert coordinator.last_run_direction == HVAC_MODE_HEAT

    @pytest.mark.asyncio
    async def test_the_ring_is_fed_from_the_outside_sensor_state(
        self, hass: HomeAssistant, coordinator, freezer
    ) -> None:
        hass.states.async_set(OUTSIDE_HUMIDITY, "60")
        hass.states.async_set(OUTSIDE_TEMP, "15.0")
        coordinator._observe_outside(dt_util.utcnow())
        assert [v for _, v in coordinator._outside_trend.samples] == [15.0]

        # Unavailable / unknown / non-numeric: nothing is added, nothing raised.
        for bad in ("unavailable", "unknown", "", "warm", "nan"):
            freezer.tick(_minutes(1))
            hass.states.async_set(OUTSIDE_TEMP, bad)
            coordinator._observe_outside(dt_util.utcnow())
        assert [v for _, v in coordinator._outside_trend.samples] == [15.0]

        # 0.0 with 0 % humidity is the sensor's reset frame.
        freezer.tick(_minutes(1))
        hass.states.async_set(OUTSIDE_HUMIDITY, "0")
        hass.states.async_set(OUTSIDE_TEMP, "0.0")
        coordinator._observe_outside(dt_util.utcnow())
        assert coordinator._outside_trend.last_rejection == REJECT_RESET
        assert [v for _, v in coordinator._outside_trend.samples] == [15.0]

        # A 13 C step well inside 15 minutes of the last accepted sample.
        freezer.tick(_minutes(1))
        hass.states.async_set(OUTSIDE_HUMIDITY, "60")
        hass.states.async_set(OUTSIDE_TEMP, "28.3")
        coordinator._observe_outside(dt_util.utcnow())
        assert coordinator._outside_trend.last_rejection == REJECT_SPIKE
        assert [v for _, v in coordinator._outside_trend.samples] == [15.0]

    @pytest.mark.asyncio
    async def test_the_same_report_seen_by_two_calls_is_one_sample(
        self, hass: HomeAssistant, coordinator
    ) -> None:
        hass.states.async_set(OUTSIDE_TEMP, "15.0")
        coordinator._observe_outside(dt_util.utcnow())
        coordinator._observe_outside(dt_util.utcnow())
        assert len(coordinator._outside_trend.samples) == 1

    @pytest.mark.asyncio
    async def test_a_coordinator_without_an_outside_sensor_still_works(
        self, hass: HomeAssistant, mock_config_entry
    ) -> None:
        mock_config_entry.add_to_hass(hass)
        coord = ClimateAdaptCoordinator(hass, mock_config_entry)
        coord._observe_outside(dt_util.utcnow())
        assert coord._outside_trend.samples == []


class TestRestart:
    @pytest.mark.asyncio
    async def test_ring_and_last_direction_survive(
        self, hass: HomeAssistant, coordinator, freezer
    ) -> None:
        _outside(coordinator, 14.0)
        coordinator.last_run_direction = HVAC_MODE_COOL
        await coordinator.async_save()

        # Two hours later, outside sensor dead: the stored samples are kept.
        freezer.tick(timedelta(hours=2))
        restarted = ClimateAdaptCoordinator(hass, coordinator.entry)
        await restarted.async_setup()
        restarted._observe_outside(dt_util.utcnow())

        assert restarted._outside_trend.samples == coordinator._outside_trend.samples
        assert restarted.last_run_direction == HVAC_MODE_COOL
        assert restarted.effective_last_run == HVAC_MODE_COOL

    @pytest.mark.asyncio
    async def test_a_restart_with_the_sensor_back_adds_to_the_stored_ring(
        self, hass: HomeAssistant, coordinator, freezer
    ) -> None:
        _outside(coordinator, 14.0)
        await coordinator.async_save()

        freezer.tick(_minutes(10))
        hass.states.async_set(OUTSIDE_HUMIDITY, "60")
        hass.states.async_set(OUTSIDE_TEMP, "14.0")
        restarted = ClimateAdaptCoordinator(hass, coordinator.entry)
        await restarted.async_setup()
        restarted._observe_outside(dt_util.utcnow())

        samples = restarted._outside_trend.samples
        assert len(samples) == len(coordinator._outside_trend.samples) + 1
        assert samples[:-1] == coordinator._outside_trend.samples
        assert samples[-1][1] == 14.0

    @pytest.mark.asyncio
    async def test_a_store_from_before_the_lock_seeds_from_the_resolved_direction(
        self, hass: HomeAssistant, coordinator
    ) -> None:
        """No `last_run_direction` key: a cooling room is not first-flip free."""
        await coordinator.async_save()
        stored = await coordinator.store.async_load()
        stored["resolved_mode"] = HVAC_MODE_COOL
        for key in ("last_run_direction", "outside_samples"):
            stored.pop(key, None)
        await coordinator.store.async_save(stored)

        restarted = ClimateAdaptCoordinator(hass, coordinator.entry)
        await restarted.async_setup()

        assert restarted.last_run_direction is None
        assert restarted.effective_last_run == HVAC_MODE_COOL
        assert restarted._outside_trend.samples == []
        after = await restarted.store.async_load()
        assert after.get("last_run_direction") is None

    @pytest.mark.asyncio
    async def test_seeded_state_is_replaced_by_the_first_real_observation(
        self, coordinator
    ) -> None:
        coordinator._last_run_seed = HVAC_MODE_COOL
        coordinator._note_unit_direction(HVAC_MODE_HEAT)
        assert coordinator.last_run_direction == HVAC_MODE_HEAT
        assert coordinator._last_run_seed is None

    @pytest.mark.asyncio
    async def test_a_direction_change_is_saved_at_once_a_sample_is_throttled(
        self, coordinator
    ) -> None:
        saves: list[int] = []

        async def counting_save() -> None:
            saves.append(1)
            coordinator._lock_dirty = False
            coordinator._lock_force_save = False
            coordinator._lock_last_saved = dt_util.utcnow()

        with patch.object(coordinator, "async_save", counting_save):
            now = dt_util.utcnow()
            # First sample ever: nothing saved yet, so it is due.
            coordinator._lock_dirty = True
            await coordinator._async_persist_lock_state(now)
            assert len(saves) == 1

            # Another sample soon after: throttled.
            coordinator._lock_dirty = True
            await coordinator._async_persist_lock_state(now + _minutes(1))
            assert len(saves) == 1

            # A direction change: saved at once.
            coordinator._note_unit_direction(HVAC_MODE_COOL)
            await coordinator._async_persist_lock_state(now + _minutes(2))
            assert len(saves) == 2

            # Nothing dirty: nothing saved, however long it has been.
            await coordinator._async_persist_lock_state(now + _minutes(60))
            assert len(saves) == 2
