# SPDX-License-Identifier: Apache-2.0
"""The arrival tick, its rate limit and the empty-room hold.

Everything is driven the way production drives it: the motion sensor changes
state on a real Home Assistant, the coordinator's own subscription fires, and
the assertions are on the climate service calls and the command ledger.

Tc is pinned to 23.2 through Hybrid mode, so the staging is: fan above 24.0 on the
room temperature, cool at the 24.7 ceiling, and a start edge that is 25.0 once the
room has been empty for the hold.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.core import State
from homeassistant.util import dt as dt_util

from custom_components.climate_adapt.const import (
    DEFAULT_MOTION_HOLD_MINUTES,
    MOTION_HOLD_MAX_MINUTES,
    MOTION_HOLD_MIN_MINUTES,
)
from custom_components.climate_adapt.coordinator import ClimateAdaptCoordinator
from custom_components.climate_adapt.occupancy import (
    ARRIVAL_MIN_INTERVAL_MINUTES,
    SOURCE_OCCUPANCY,
    ArrivalGate,
    read_occupancy,
)
from custom_components.climate_adapt.sensor import ClimateAdaptComfortSensor

from .comfort_helpers import (
    FULL_UNIT,
    MOTION_SENSOR,
    decide,
    prepare_live,
    register_climate,
    set_motion,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

TEMP = "sensor.office_temperature"
HUMIDITY = "sensor.office_humidity"
CLIMATE = "climate.office_ac"
SUN = "sun.sun"
PV = "sensor.pv_power"
BLIND = "cover.office_blind"

# Room 24.3: over the 24.0 fan trigger, under the 24.7 ceiling.
ROOM = 24.3
HUMID = 60.0
TC = 23.2
TRIGGER = TC + 0.8  # the cooling trigger
IDLE_TRIGGER = TC + 1.8  # the start edge once the room has been empty for the hold
FIXTURE = Path(__file__).parent / "fixtures" / "occupancy" / "room_occupancy_24h.json"


def _minutes(value: float) -> timedelta:
    return timedelta(minutes=value)


def _readings(hass: HomeAssistant, room: float = ROOM, humidity: float = HUMID) -> None:
    hass.states.async_set(TEMP, str(room))
    hass.states.async_set(HUMIDITY, str(humidity))


def _unit(hass: HomeAssistant, state: str = "off") -> None:
    hass.states.async_set(CLIMATE, state, FULL_UNIT)


@pytest.fixture
def calls(hass: HomeAssistant) -> list:
    recorded: list = []
    register_climate(hass, recorded)
    return recorded


@pytest.fixture
def office(hass, mock_config_entry_with_motion, calls, freezer):
    """A room with motion on, Hybrid Tc 23.2, room 24.3, unit off."""
    entry = mock_config_entry_with_motion
    entry.add_to_hass(hass)
    coordinator = ClimateAdaptCoordinator(hass, entry)
    prepare_live(coordinator, target=TC, room=ROOM)
    _readings(hass)
    _unit(hass)
    hass.states.async_set(MOTION_SENSOR, "off")
    coordinator.async_track_motion_sensor()
    yield coordinator
    coordinator.async_stop_periodic()


async def _flip(hass: HomeAssistant, state: str, **attributes: Any) -> None:
    hass.states.async_set(MOTION_SENSOR, state, attributes)
    await hass.async_block_till_done()


async def _arrive(hass: HomeAssistant) -> None:
    """The sensor goes off, then on: one off -> on event."""
    if hass.states.get(MOTION_SENSOR).state != "off":
        await _flip(hass, "off")
    await _flip(hass, "on")


async def _regular(hass: HomeAssistant, office: ClimateAdaptCoordinator) -> None:
    """An interval tick on the sensor readings as they are now."""
    _unit(hass, hass.states.get(CLIMATE).state)
    office.refresh_readings()
    await decide(hass, office, hass.states.get(CLIMATE).state)


def _sent(calls: list) -> list[str]:
    return [
        call.data.get("hvac_mode") or call.service
        for call in calls
        if call.service in ("set_hvac_mode", "set_temperature", "turn_off")
    ]


def _set_sun_load(hass: HomeAssistant) -> None:
    hass.states.async_set(SUN, "above_horizon")
    hass.states.async_set(PV, "3.7", {"unit_of_measurement": "kW"})
    hass.states.async_set(BLIND, "open")


class TestTheArrivalTick:
    @pytest.mark.asyncio
    async def test_an_arrival_starts_the_fan_at_once(self, hass, office, calls) -> None:
        await _arrive(hass)

        assert _sent(calls) == ["fan_only"]
        assert office.comfort["action"] == "fan"
        assert office.last_sent_command["source"] == SOURCE_OCCUPANCY
        assert office.last_sent_command["mode"] == "fan_only"
        assert office.occupancy_view()["arrival_ticks"] == 1

    @pytest.mark.asyncio
    async def test_it_does_not_move_the_fifteen_minute_schedule(self, hass, office) -> None:
        await _arrive(hass)
        assert office.last_adaptation is None

        office.last_adaptation = dt_util.utcnow() - _minutes(3)
        stamp = office.last_adaptation
        await _flip(hass, "off")
        await _flip(hass, "on")
        # The interval gate does not hold an arrival, and it does not restamp it.
        assert office.last_adaptation == stamp

    @pytest.mark.asyncio
    async def test_the_sun_load_goes_straight_to_cool(self, hass, office, calls) -> None:
        _set_sun_load(hass)
        await _arrive(hass)

        assert _sent(calls) == ["cool"]
        assert office.last_sent_command["source"] == SOURCE_OCCUPANCY
        assert office.comfort["heat_load"] is True

    @pytest.mark.asyncio
    async def test_a_barred_fan_goes_straight_to_cool(self, hass, office, calls) -> None:
        office._ladder_state = replace(
            office._ladder_state, fan_failed_at=dt_util.utcnow() - _minutes(30)
        )
        await _arrive(hass)

        assert _sent(calls) == ["cool"]
        assert office.last_sent_command["source"] == SOURCE_OCCUPANCY

    @pytest.mark.asyncio
    async def test_it_is_not_a_confirming_tick(self, hass, office, calls, freezer) -> None:
        """Heat after cool still needs two interval ticks: the arrival is neither of them."""
        _readings(hass, room=19.0)
        office.last_run_direction = "cool"
        office.last_direction_change = dt_util.utcnow() - _minutes(300)

        await _regular(hass, office)  # interval tick 1 of 2
        assert calls == []
        assert office._ladder_state.pending_ticks == 1

        freezer.tick(_minutes(1))
        _readings(hass, room=19.0)
        await _arrive(hass)
        assert calls == []
        assert office._ladder_state.pending_ticks == 1  # not 2, and not reset
        assert "1 of 2 confirming ticks" in office.comfort["reason"]

        freezer.tick(_minutes(15))
        _readings(hass, room=19.0)
        await _regular(hass, office)  # interval tick 2 of 2
        assert _sent(calls) == ["heat"]

    @pytest.mark.asyncio
    async def test_heat_after_heat_starts_on_the_first_tick(self, hass, office, calls) -> None:
        """Nothing is accelerated or slowed: the last direction decides, as for any tick."""
        _readings(hass, room=19.0)
        office.last_run_direction = "heat"
        await _arrive(hass)

        assert _sent(calls) == ["heat"]
        assert office.last_sent_command["source"] == SOURCE_OCCUPANCY

    @pytest.mark.asyncio
    async def test_the_dwell_still_holds_a_change_of_direction(self, hass, office, calls) -> None:
        _readings(hass, room=19.0)
        office.last_run_direction = "cool"
        office.last_direction_change = dt_util.utcnow() - _minutes(10)
        await _arrive(hass)

        assert calls == []
        assert "to go" in office.comfort["reason"]

    @pytest.mark.asyncio
    async def test_it_does_not_wait_for_the_fifteen_minutes(self, hass, office, calls) -> None:
        """Three minutes after an interval tick, an arrival still sends."""
        office.last_adaptation = dt_util.utcnow() - _minutes(3)
        stamp = office.last_adaptation

        await _arrive(hass)

        assert _sent(calls) == ["fan_only"]
        assert office.last_sent_command["source"] == SOURCE_OCCUPANCY
        assert office.last_adaptation == stamp

    @pytest.mark.asyncio
    async def test_it_does_not_erase_a_confirming_tick(self, hass, office, calls, freezer) -> None:
        """An arrival that wants no change must leave the interval ticks' count as it was."""
        _readings(hass, room=19.0)
        office.last_run_direction = "cool"
        office.last_direction_change = dt_util.utcnow() - _minutes(300)
        await _regular(hass, office)  # interval tick 1 of 2
        assert office._ladder_state.pending == "heat"
        assert office._ladder_state.pending_ticks == 1

        freezer.tick(_minutes(1))
        _readings(hass, room=TC)  # back at comfort: the ladder asks for no direction
        await _arrive(hass)

        assert calls == []
        assert office._ladder_state.pending == "heat"
        assert office._ladder_state.pending_ticks == 1

    @pytest.mark.asyncio
    async def test_it_does_not_count_towards_a_humid_reading(self, hass, office, freezer) -> None:
        """RH over 65 needs two interval ticks to count as humid; an arrival is not one."""
        _readings(hass, room=20.0, humidity=70.0)
        await _regular(hass, office)
        assert office._humidity_state.high_ticks == 1

        freezer.tick(_minutes(1))
        _readings(hass, room=20.0, humidity=70.0)
        await _arrive(hass)

        assert office._humidity_state.high_ticks == 1
        assert office._humidity_state.status != "humid"


class TestWhenItDoesNotRun:
    @pytest.mark.asyncio
    async def test_motion_off_in_the_options_means_nothing_on_the_event(
        self, hass, mock_config_entry_with_motion, calls
    ) -> None:
        entry = mock_config_entry_with_motion
        entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(entry, data={**entry.data, "enable_motion": False})
        coordinator = ClimateAdaptCoordinator(hass, entry)
        prepare_live(coordinator, target=TC, room=ROOM)
        _readings(hass)
        _unit(hass)
        hass.states.async_set(MOTION_SENSOR, "off")
        coordinator.async_track_motion_sensor()

        await _arrive(hass)
        # And the handler itself refuses, should something call it anyway.
        event = SimpleNamespace(
            data={
                "old_state": State(MOTION_SENSOR, "off"),
                "new_state": State(MOTION_SENSOR, "on"),
            }
        )
        await coordinator.async_on_motion_event(event)

        assert calls == []
        assert coordinator._motion_unsub is None
        assert coordinator._arrivals.counters() == {
            "arrival_ticks": 0,
            "arrival_dropped_rate_limit": 0,
            "arrival_skipped_unit_running": 0,
            "arrival_skipped_not_off_event": 0,
        }
        assert coordinator.occupancy_view() is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("before", ["unavailable", "unknown"])
    async def test_an_event_from_an_unreadable_state_is_not_an_arrival(
        self, hass, office, calls, before
    ) -> None:
        await _flip(hass, before)
        await _flip(hass, "on")

        assert calls == []
        view = office.occupancy_view()
        assert view["arrival_ticks"] == 0
        assert view["arrival_skipped_not_off_event"] == 2

    @pytest.mark.asyncio
    async def test_on_to_on_is_not_an_arrival(self, hass, office, calls) -> None:
        await _flip(hass, "on")
        calls.clear()
        office._arrivals = ArrivalGate()
        await _flip(hass, "on", battery=80)

        assert calls == []
        assert office.occupancy_view()["arrival_skipped_not_off_event"] == 1
        assert office.occupancy_view()["arrival_ticks"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("running", ["cool", "fan_only", "heat"])
    async def test_a_running_unit_is_left_alone(self, hass, office, calls, running) -> None:
        _unit(hass, running)
        await _arrive(hass)

        assert calls == []
        view = office.occupancy_view()
        assert view["arrival_skipped_unit_running"] == 1
        assert view["arrival_ticks"] == 0
        assert view["last_arrival_tick_at"] is None

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("office")
    async def test_a_running_unit_does_not_spend_the_rate_window(
        self, hass, calls, freezer
    ) -> None:
        _unit(hass, "cool")
        await _arrive(hass)
        freezer.tick(_minutes(1))
        _unit(hass)
        _readings(hass)
        await _arrive(hass)

        assert _sent(calls) == ["fan_only"]


class TestTheRateLimit:
    @pytest.mark.asyncio
    async def test_two_arrivals_three_minutes_apart_run_one_tick(
        self, hass, office, calls, freezer
    ) -> None:
        await _arrive(hass)
        freezer.tick(_minutes(3))
        _readings(hass)
        await _arrive(hass)

        view = office.occupancy_view()
        assert view["arrival_ticks"] == 1
        assert view["arrival_dropped_rate_limit"] == 1
        assert _sent(calls) == ["fan_only"]

    @pytest.mark.asyncio
    async def test_two_arrivals_five_minutes_apart_run_two(self, hass, office, freezer) -> None:
        await _arrive(hass)
        freezer.tick(_minutes(ARRIVAL_MIN_INTERVAL_MINUTES))
        _readings(hass)
        await _arrive(hass)

        view = office.occupancy_view()
        assert view["arrival_ticks"] == 2
        assert view["arrival_dropped_rate_limit"] == 0

    @pytest.mark.asyncio
    async def test_a_dropped_event_leaves_no_trailing_evaluation(
        self, hass, office, calls, freezer
    ) -> None:
        await _arrive(hass)
        calls.clear()
        freezer.tick(_minutes(2))
        await _arrive(hass)
        freezer.tick(_minutes(10))
        await hass.async_block_till_done()

        assert calls == []
        assert office.occupancy_view()["arrival_ticks"] == 1

    @pytest.mark.asyncio
    async def test_a_regular_tick_does_not_reset_the_window(self, hass, office, freezer) -> None:
        await _arrive(hass)
        freezer.tick(_minutes(2))
        _readings(hass)
        await _regular(hass, office)  # an interval tick in the window
        freezer.tick(_minutes(2))
        _readings(hass)
        await _arrive(hass)
        # 4 min after the arrival tick: dropped, although a regular tick ran 2 min ago.
        assert office.occupancy_view()["arrival_dropped_rate_limit"] == 1

        freezer.tick(_minutes(1.5))
        _readings(hass)
        await _arrive(hass)
        # 5.5 min after the arrival tick: runs, although the regular tick was 3.5 min ago.
        assert office.occupancy_view()["arrival_ticks"] == 2


class TestTheGatesAreTheNormalOnes:
    @pytest.mark.asyncio
    async def test_a_disarmed_room_shadows_and_sends_nothing(self, hass, office, calls) -> None:
        office.enabled = False
        await _arrive(hass)

        assert calls == []
        assert office.comfort["shadow"] is True
        assert office.comfort["action"] == "fan"
        assert office.occupancy_view()["arrival_ticks"] == 1
        assert office.command_history == []

    @pytest.mark.asyncio
    async def test_the_manual_cooldown_holds_it(self, hass, office, calls) -> None:
        office.manual_override_until = dt_util.now() + _minutes(60)
        await _arrive(hass)

        assert calls == []
        assert office.command_history == []

    @pytest.mark.asyncio
    async def test_the_startup_gate_holds_it(self, hass, office, calls) -> None:
        office._startup_gate_armed = True
        office._startup_gate_released = False
        office._startup_complete = False
        await _arrive(hass)

        assert calls == []
        assert office.command_history[-1]["kind"] == "skip_startup_seed"
        assert office.command_history[-1]["source"] == SOURCE_OCCUPANCY

    @pytest.mark.asyncio
    async def test_keep_climate_on_does_not_make_it_start_a_stopped_unit_off(
        self, hass, office, calls
    ) -> None:
        office.keep_climate_on = True
        await _arrive(hass)

        assert _sent(calls) == ["fan_only"]

    @pytest.mark.asyncio
    async def test_the_selector_pin_still_applies(self, hass, office, calls) -> None:
        office.hvac_mode = "heat"
        await _arrive(hass)

        assert "fan_only" not in _sent(calls)
        assert "cool" not in _sent(calls)

    @pytest.mark.asyncio
    async def test_a_held_arrival_does_not_coalesce_into_a_regular_tick_row(
        self, hass, office
    ) -> None:
        office._startup_gate_armed = True
        office._startup_gate_released = False
        office._startup_complete = False
        await _regular(hass, office)
        await _arrive(hass)

        kinds = [(row["kind"], row["source"]) for row in office.command_history]
        assert kinds == [
            ("skip_startup_seed", "auto"),
            ("skip_startup_seed", SOURCE_OCCUPANCY),
        ]

    @pytest.mark.asyncio
    async def test_the_arrival_does_not_restart_the_poll_timer(self, hass, office) -> None:
        # async_set_updated_data reschedules the coordinator's 15-minute timer.
        with (
            patch.object(office, "async_set_updated_data") as reschedule,
            patch.object(office, "async_update_listeners") as notify,
        ):
            await _arrive(hass)

        reschedule.assert_not_called()
        notify.assert_called()
        assert office.data["comfort"]["action"] == "fan"


class TestTheHold:
    """`off` for the whole hold is empty; everything else is occupied."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("quiet", [0.0, 5.0, 19.9])
    async def test_inside_the_hold_the_normal_band_acts(
        self, hass, office, calls, freezer, quiet
    ) -> None:
        set_motion(hass, freezer, "off", quiet)
        _readings(hass, room=ROOM, humidity=50.0)
        await _regular(hass, office)

        assert office.comfort["cool_starts_above"] == pytest.approx(TRIGGER)
        assert _sent(calls) == ["fan_only"]

    @pytest.mark.asyncio
    async def test_after_the_hold_the_start_edge_is_wide_and_the_unit_stays_off(
        self, hass, office, calls, freezer
    ) -> None:
        set_motion(hass, freezer, "off", 20.0)
        _readings(hass, room=ROOM, humidity=50.0)
        await _regular(hass, office)

        assert office.comfort["cool_starts_above"] == pytest.approx(IDLE_TRIGGER)
        assert calls == []
        view = office.occupancy_view()
        assert view["occupied"] is False
        assert view["empty_for_minutes"] == 20.0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("quiet", "wide"), [(4.9, False), (5.0, True)])
    async def test_the_option_is_respected(
        self, hass, office, calls, freezer, *, quiet, wide
    ) -> None:
        office.motion_hold_minutes = 5
        set_motion(hass, freezer, "off", quiet)
        _readings(hass, room=ROOM, humidity=50.0)
        await _regular(hass, office)

        assert (office.comfort["cool_starts_above"] == pytest.approx(IDLE_TRIGGER)) is wide
        assert (calls == []) is wide

    @pytest.mark.asyncio
    async def test_while_the_sensor_is_on_the_band_is_normal(self, hass, office, freezer) -> None:
        set_motion(hass, freezer, "on", 600.0)
        _readings(hass, room=ROOM, humidity=50.0)
        await _regular(hass, office)

        assert office.comfort["cool_starts_above"] == pytest.approx(TRIGGER)
        view = office.occupancy_view()
        assert view["occupied"] is True
        assert view["occupied_since"] is not None
        assert view["empty_for_minutes"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["unavailable", "unknown"])
    async def test_an_unreadable_sensor_counts_as_occupied(
        self, hass, office, freezer, state
    ) -> None:
        set_motion(hass, freezer, state, 600.0)
        _readings(hass, room=ROOM, humidity=50.0)
        await _regular(hass, office)

        assert office.comfort["cool_starts_above"] == pytest.approx(TRIGGER)
        assert office.occupancy_view()["occupied"] is True
        assert office.occupancy_view()["state"] == state

    @pytest.mark.asyncio
    async def test_a_missing_sensor_counts_as_occupied(self, hass, office) -> None:
        hass.states.async_remove(MOTION_SENSOR)
        _readings(hass, room=ROOM, humidity=50.0)
        await _regular(hass, office)

        assert office.comfort["cool_starts_above"] == pytest.approx(TRIGGER)
        view = office.occupancy_view()
        assert view["occupied"] is True
        assert view["state"] is None

    @pytest.mark.asyncio
    async def test_a_state_with_no_usable_timestamp_counts_as_occupied(self, office) -> None:
        office.hass = SimpleNamespace(
            states=SimpleNamespace(get=lambda _entity: SimpleNamespace(state="off"))
        )
        assert office._occupancy(dt_util.utcnow()).occupied is True

    def test_the_bounds_and_the_default(self) -> None:
        assert (MOTION_HOLD_MIN_MINUTES, DEFAULT_MOTION_HOLD_MINUTES) == (1, 20)
        assert MOTION_HOLD_MAX_MINUTES == 240

    @pytest.mark.asyncio
    async def test_a_restart_means_one_hold_of_occupied(self, hass, office, freezer) -> None:
        """After a restart `last_changed` is the restart: the room reads occupied for 20 min."""
        set_motion(hass, freezer, "off", 0.0)
        _readings(hass, room=ROOM, humidity=50.0)
        assert office._occupancy(dt_util.utcnow()).occupied is True
        freezer.tick(_minutes(20))
        assert office._occupancy(dt_util.utcnow()).occupied is False


class TestReadOccupancy:
    NOW = datetime(2031, 10, 15, 12, 0, tzinfo=dt_util.UTC)

    def _read(self, state, quiet=None, *, enabled=True, sensor="binary_sensor.x"):
        return read_occupancy(
            sensor=sensor,
            enabled=enabled,
            state=state,
            last_changed=None if quiet is None else self.NOW - _minutes(quiet),
            now=self.NOW,
            hold_minutes=20,
        )

    def test_an_untracked_room_has_no_reading(self) -> None:
        for kwargs in ({"enabled": False}, {"sensor": None}, {"sensor": ""}):
            got = self._read("off", 60, **kwargs)
            assert got.tracked is False
            assert got.occupied is None
            assert got.idle is False

    def test_off_with_no_timestamp_is_occupied(self) -> None:
        assert self._read("off", None).occupied is True

    def test_a_clock_behind_the_state_is_occupied(self) -> None:
        got = self._read("off", -5)
        assert got.occupied is True
        assert got.empty_for_minutes == 0.0

    def test_only_a_long_off_is_idle(self) -> None:
        assert self._read("off", 20).idle is True
        assert self._read("off", 19.99).idle is False
        assert self._read("on", 600).idle is False


@pytest.mark.asyncio
async def test_the_sensor_attribute_is_live(hass, office, calls, freezer) -> None:
    """`occupancy` on the comfort sensor reads the sensor now, not at the last tick."""
    sensor = ClimateAdaptComfortSensor(office, office.entry)
    set_motion(hass, freezer, "off", 7.0)
    attrs = sensor.extra_state_attributes["occupancy"]
    assert attrs["sensor"] == MOTION_SENSOR
    assert attrs["state"] == "off"
    assert attrs["occupied"] is True
    assert attrs["empty_for_minutes"] == 7.0
    assert attrs["hold_minutes"] == 20
    assert attrs["last_arrival_tick_at"] is None
    for key in (
        "arrival_ticks",
        "arrival_dropped_rate_limit",
        "arrival_skipped_unit_running",
    ):
        assert attrs[key] == 0
    # set_motion removes and re-adds the sensor, and Home Assistant may deliver
    # those two events to the listener at once; the attribute must show the
    # gate's own count, whatever that is.
    assert attrs["arrival_skipped_not_off_event"] == office._arrivals.skipped_not_off_event

    diagnostics = office.build_diagnostics()["inputs"]["motion"]
    assert diagnostics["hold_minutes"] == 20
    assert diagnostics["occupancy"]["empty_for_minutes"] == 7.0


@pytest.mark.skipif(not FIXTURE.exists(), reason="maintainer-only recorded data not distributed")
@pytest.mark.asyncio
async def test_a_synthetic_flapping_day_through_the_limiter_and_the_unit_off_gate(
    hass: HomeAssistant,
) -> None:
    """24 h of a flapping motion sensor through the rate limiter and the unit-off gate."""
    fixture = json.loads(FIXTURE.read_text())
    unit = [(datetime.fromisoformat(t), s) for t, s in fixture["unit"]]

    def unit_at(moment: datetime) -> str:
        state = unit[0][1]
        for changed, value in unit:
            if changed <= moment:
                state = value
        return state

    gate = ArrivalGate()
    previous: str | None = None
    ran: list[datetime] = []
    arrivals = 0
    for stamp, state in fixture["occupancy"]:
        moment = datetime.fromisoformat(stamp)
        if previous == "off" and state == "on":
            arrivals += 1
        if gate.admit(previous, state, unit_state=unit_at(moment), now=moment):
            ran.append(moment)
        previous = state

    per_hour: dict[str, int] = {}
    for moment in ran:
        key = moment.strftime("%Y-%m-%dT%H")
        per_hour[key] = per_hour.get(key, 0) + 1

    counters = gate.counters()
    assert arrivals > 0
    assert (
        counters["arrival_ticks"]
        + counters["arrival_dropped_rate_limit"]
        + counters["arrival_skipped_unit_running"]
        == arrivals
    )
    assert counters["arrival_ticks"] == len(ran)
    assert max(per_hour.values()) <= 12
    assert all(
        later - earlier >= _minutes(ARRIVAL_MIN_INTERVAL_MINUTES)
        for earlier, later in pairwise(ran)
    )
    # The limiter alone, with the unit-off gate taken away: the worst case.
    bare, last = ArrivalGate(), None
    worst: dict[str, int] = {}
    for stamp, state in fixture["occupancy"]:
        moment = datetime.fromisoformat(stamp)
        if bare.admit(last, state, unit_state="off", now=moment):
            key = moment.strftime("%Y-%m-%dT%H")
            worst[key] = worst.get(key, 0) + 1
        last = state
    assert max(worst.values()) <= 12
    assert bare.counters()["arrival_ticks"] >= counters["arrival_ticks"]


class TestOffers:
    """The offer to turn the unit on reads the same band, so it follows the occupancy."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("state", "quiet", "offered"),
        [("on", 0, True), ("off", 5, True), ("off", 20, False)],
    )
    async def test_the_offer_is_earlier_when_occupied(
        self, hass, office, freezer, *, state, quiet, offered
    ) -> None:
        office.enabled = False
        set_motion(hass, freezer, state, quiet)
        _readings(hass, room=ROOM, humidity=50.0)
        climate = office.refresh_readings()
        evaluation = office._evaluate_comfort(
            dt_util.utcnow(), climate, shadow=True, allow_stale=False
        )

        assessment = office.offers.assess(climate, evaluation)

        assert (assessment.wanted == "fan") is offered
        assert (assessment.wanted is None) is not offered

    @pytest.mark.asyncio
    async def test_an_arrival_in_a_disarmed_room_ticks_the_offers_once(self, hass, office) -> None:
        office.enabled = False
        with patch.object(office.offers, "async_tick") as tick:
            await _arrive(hass)

        tick.assert_awaited_once()
        evaluation = tick.await_args.args[1]
        assert evaluation.decision.action == "fan"
        assert evaluation.inputs.now is not None
